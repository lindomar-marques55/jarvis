"""
Telegram Bot Service for Jarvis.
Enables interacting with Jarvis via Telegram (text and voice messages)
with background long-polling and no extra heavy dependencies.
"""

import os
import time
import json
import logging
import tempfile
import threading
from pathlib import Path
from typing import Optional, Dict, Any
import requests

from jarvis.reply.engine import run_reply_engine

logger = logging.getLogger("jarvis.telegram")

TELEGRAM_CONFIG_PATH = Path.home() / ".config" / "jarvis" / "telegram.json"

_bot_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_bot_info: Dict[str, Any] = {"running": False, "username": None, "first_name": None}


def load_telegram_config() -> Dict[str, Any]:
    if TELEGRAM_CONFIG_PATH.exists():
        try:
            with open(TELEGRAM_CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error reading telegram config: {e}")
    return {"token": "", "enabled": False, "allowed_chat_id": ""}


def save_telegram_config(config: Dict[str, Any]) -> None:
    TELEGRAM_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(TELEGRAM_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def test_telegram_token(token: str) -> Optional[Dict[str, Any]]:
    """Test if a Telegram bot token is valid."""
    try:
        url = f"https://api.telegram.org/bot{token}/getMe"
        res = requests.get(url, timeout=10.0)
        if res.status_code == 200 and res.json().get("ok"):
            return res.json().get("result")
    except Exception as e:
        logger.error(f"Error testing token: {e}")
    return None


def _telegram_worker(token: str, allowed_chat_id: str, db, cfg, dm, whisper_model, whisper_lock):
    global _bot_info
    logger.info("Telegram Bot worker started.")
    offset = 0

    base_url = f"https://api.telegram.org/bot{token}"

    while not _stop_event.is_set():
        try:
            updates_url = f"{base_url}/getUpdates?offset={offset}&timeout=20"
            res = requests.get(updates_url, timeout=25.0)
            if res.status_code != 200:
                time.sleep(3.0)
                continue

            data = res.json()
            if not data.get("ok"):
                time.sleep(3.0)
                continue

            updates = data.get("result", [])
            for update in updates:
                update_id = update.get("update_id", 0)
                offset = max(offset, update_id + 1)

                msg = update.get("message")
                if not msg:
                    continue

                chat = msg.get("chat", {})
                chat_id = str(chat.get("id", ""))
                from_user = msg.get("from", {}).get("first_name", "Usuário")

                # Filter by allowed_chat_id if configured
                if allowed_chat_id and chat_id != str(allowed_chat_id):
                    logger.warning(f"Telegram unauthorized access attempt from chat_id: {chat_id}")
                    continue

                # Indicate typing to user
                requests.post(f"{base_url}/sendChatAction", json={"chat_id": chat_id, "action": "typing"}, timeout=5.0)

                user_text = ""

                # Handle Text Message
                if "text" in msg:
                    user_text = msg["text"].strip()
                    if user_text == "/start":
                        requests.post(
                            f"{base_url}/sendMessage",
                            json={
                                "chat_id": chat_id,
                                "text": f"Olá {from_user}! Sou o Jarvis, seu assistente local inteligente. Como posso ajudar?"
                            },
                            timeout=10.0
                        )
                        continue

                # Handle Voice / Audio Message
                elif "voice" in msg or "audio" in msg:
                    voice_obj = msg.get("voice") or msg.get("audio")
                    file_id = voice_obj.get("file_id")
                    if file_id and whisper_model is not None:
                        try:
                            # 1. Get file path from Telegram
                            file_info_res = requests.get(f"{base_url}/getFile?file_id={file_id}", timeout=10.0).json()
                            if file_info_res.get("ok"):
                                file_path = file_info_res["result"]["file_path"]
                                download_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
                                audio_bytes = requests.get(download_url, timeout=20.0).content

                                with tempfile.NamedTemporaryFile(suffix=".oga", delete=False) as tmp:
                                    tmp.write(audio_bytes)
                                    tmp_path = tmp.name

                                try:
                                    with whisper_lock:
                                        segments, _ = whisper_model.transcribe(tmp_path, language="pt", beam_size=5)
                                        user_text = " ".join([s.text for s in segments]).strip()
                                finally:
                                    if os.path.exists(tmp_path):
                                        os.remove(tmp_path)
                        except Exception as e:
                            logger.error(f"Error transcribing Telegram voice note: {e}")
                            requests.post(f"{base_url}/sendMessage", json={"chat_id": chat_id, "text": "Desculpe, não consegui transcrever o seu áudio."}, timeout=10.0)
                            continue

                if not user_text:
                    continue

                logger.info("Telegram message received")

                # Send query to Jarvis reply engine
                try:
                    reply = run_reply_engine(
                        db=db,
                        cfg=cfg,
                        tts=None,
                        text=user_text,
                        dialogue_memory=dm,
                        language="pt",
                        quiet=True
                    )
                    reply_text = reply or "..."
                    requests.post(
                        f"{base_url}/sendMessage",
                        json={"chat_id": chat_id, "text": reply_text},
                        timeout=15.0
                    )
                except Exception as e:
                    logger.error(f"Error in Jarvis reply engine for Telegram: {e}")
                    requests.post(
                        f"{base_url}/sendMessage",
                        json={"chat_id": chat_id, "text": f"Ocorreu um erro ao processar sua mensagem: {e}"},
                        timeout=10.0
                    )

        except Exception as e:
            if not _stop_event.is_set():
                logger.error(f"Telegram bot polling exception: {e}")
                time.sleep(5.0)

    logger.info("Telegram Bot worker stopped.")
    _bot_info["running"] = False


def start_telegram_bot(db, cfg, dm, whisper_model, whisper_lock) -> bool:
    global _bot_thread, _stop_event, _bot_info
    config = load_telegram_config()
    token = config.get("token", "").strip()
    enabled = config.get("enabled", False)
    allowed_chat_id = config.get("allowed_chat_id", "").strip()

    if not token or not enabled:
        return False

    bot_user = test_telegram_token(token)
    if not bot_user:
        logger.error("Failed to start Telegram Bot: Token invalid or unreachable.")
        return False

    stop_telegram_bot()

    _stop_event.clear()
    _bot_info = {
        "running": True,
        "username": bot_user.get("username"),
        "first_name": bot_user.get("first_name")
    }

    _bot_thread = threading.Thread(
        target=_telegram_worker,
        args=(token, allowed_chat_id, db, cfg, dm, whisper_model, whisper_lock),
        daemon=True
    )
    _bot_thread.start()
    logger.info(f"Telegram bot started as @{_bot_info['username']}")
    return True


def stop_telegram_bot():
    global _bot_thread, _stop_event, _bot_info
    if _bot_thread and _bot_thread.is_alive():
        _stop_event.set()
        _bot_thread.join(timeout=2.0)
    _bot_info["running"] = False


def get_telegram_status() -> Dict[str, Any]:
    config = load_telegram_config()
    return {
        "enabled": config.get("enabled", False),
        "has_token": bool(config.get("token")),
        "allowed_chat_id": config.get("allowed_chat_id", ""),
        "running": _bot_info.get("running", False),
        "username": _bot_info.get("username")
    }
