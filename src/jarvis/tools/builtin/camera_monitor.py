"""
Camera Monitoring and Frigate NVR Tool for Jarvis.
Allows Jarvis to check camera events (people, cars, movement) and run
background surveillance with alerts. Configurable via web UI.
"""

import os
import time
import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional, List
import requests

from ..base import Tool, ToolContext
from ..types import ToolExecutionResult
from ...debug import debug_log

logger = logging.getLogger("jarvis.tools.camera_monitor")

FRIGATE_CONFIG_PATH = Path.home() / ".config" / "jarvis" / "frigate.json"


def get_frigate_config() -> Dict[str, Any]:
    """Retrieve Frigate URL and configuration from file or environment."""
    config = {
        "url": os.environ.get("FRIGATE_URL", "").rstrip("/"),
        "enabled": True
    }

    if FRIGATE_CONFIG_PATH.exists():
        try:
            with open(FRIGATE_CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
                if saved.get("url"):
                    config["url"] = saved["url"].rstrip("/")
                if "enabled" in saved:
                    config["enabled"] = bool(saved["enabled"])
        except Exception as e:
            debug_log(f"Error reading Frigate config: {e}", "camera_monitor")

    return config


_monitor_thread: Optional[threading.Thread] = None
_stop_monitor_event = threading.Event()
_is_monitoring = False
_last_processed_event_time = 0.0


def _camera_monitor_loop(frigate_url: str, telegram_token: str, telegram_chat_id: str):
    global _is_monitoring, _last_processed_event_time
    logger.info("Camera monitor background loop started.")
    _is_monitoring = True

    while not _stop_monitor_event.is_set():
        try:
            res = requests.get(f"{frigate_url}/api/events?limit=5", timeout=6.0)
            if res.status_code == 200:
                events = res.json()
                for ev in events:
                    start_time = float(ev.get("start_time", 0))
                    if start_time > _last_processed_event_time and _last_processed_event_time > 0:
                        camera = ev.get("camera", "câmera")
                        label = ev.get("label", "objeto")
                        sub_label = ev.get("sub_label")

                        subject = sub_label if sub_label else label
                        alert_msg = f"🚨 Jarvis Alerta: Detecção de {subject} na câmera '{camera}'!"
                        logger.info(alert_msg)

                        # Notify via Telegram if configured
                        if telegram_token and telegram_chat_id:
                            try:
                                tg_url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
                                requests.post(tg_url, json={"chat_id": telegram_chat_id, "text": alert_msg}, timeout=10.0)
                            except Exception as tg_err:
                                logger.error(f"Error sending camera alert to Telegram: {tg_err}")

                    _last_processed_event_time = max(_last_processed_event_time, start_time)

            time.sleep(10.0)
        except Exception as e:
            if not _stop_monitor_event.is_set():
                logger.error(f"Error in camera monitor loop: {e}")
                time.sleep(15.0)

    _is_monitoring = False
    logger.info("Camera monitor background loop stopped.")


class CameraMonitorTool(Tool):
    """Tool for checking cameras and managing surveillance via Frigate NVR."""

    @property
    def name(self) -> str:
        return "cameraMonitor"

    @property
    def description(self) -> str:
        return (
            "Monitor security cameras and check detection events from Frigate NVR. "
            "Use this tool when the user asks to: "
            "1. Monitor cameras ('monitore as câmeras', 'fique de olho nas câmeras'), "
            "2. Stop monitoring ('pare de monitorar as câmeras', 'cancelar monitoramento'), "
            "3. Check recent activity or detections ('quem passou lá fora?', 'o que as câmeras viram?'), "
            "4. Check camera status."
        )

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["start_monitoring", "stop_monitoring", "get_recent_events", "get_status"],
                    "description": (
                        "Action to perform: 'start_monitoring' to begin surveillance, "
                        "'stop_monitoring' to cease surveillance, "
                        "'get_recent_events' to see recent camera detections, "
                        "'get_status' to check if monitoring is currently active."
                    )
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of recent events to retrieve (default: 5)."
                }
            },
            "required": ["action"]
        }

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        global _monitor_thread, _stop_monitor_event, _is_monitoring, _last_processed_event_time
        if not args:
            return ToolExecutionResult(success=False, reply_text="Nenhum argumento fornecido para o monitor de câmeras.")

        frigate_cfg = get_frigate_config()
        frigate_url = frigate_cfg["url"]
        enabled = frigate_cfg.get("enabled", True)

        if not enabled:
            return ToolExecutionResult(
                success=False,
                reply_text="A integração com o Frigate NVR está desativada nas configurações da interface web."
            )

        action = args.get("action", "")

        if action == "start_monitoring":
            if _is_monitoring and _monitor_thread and _monitor_thread.is_alive():
                return ToolExecutionResult(
                    success=True,
                    reply_text="O monitoramento de câmeras já está ativo e em execução no momento."
                )

            # Get telegram config for alerts
            tg_token = ""
            tg_chat_id = ""
            try:
                from server_app.telegram_bot import load_telegram_config
                tg_cfg = load_telegram_config()
                if tg_cfg.get("enabled"):
                    tg_token = tg_cfg.get("token", "")
                    tg_chat_id = tg_cfg.get("allowed_chat_id", "")
            except Exception:
                pass

            _stop_monitor_event.clear()
            _last_processed_event_time = time.time()
            _monitor_thread = threading.Thread(
                target=_camera_monitor_loop,
                args=(frigate_url, tg_token, tg_chat_id),
                daemon=True
            )
            _monitor_thread.start()
            _is_monitoring = True

            context.user_print("📹 Monitoramento de câmeras ativado com sucesso via Frigate NVR.")
            return ToolExecutionResult(
                success=True,
                reply_text=(
                    "Iniciei o monitoramento contínuo das câmeras via Frigate NVR. "
                    "Qualquer detecção de pessoas ou veículos será vigiada e alertada. "
                    "Basta me dizer 'Jarvis, pare' ou 'pare de monitorar' quando quiser que eu interrompa."
                )
            )

        elif action == "stop_monitoring":
            if not _is_monitoring:
                return ToolExecutionResult(
                    success=True,
                    reply_text="O monitoramento de câmeras não estava ativo."
                )

            _stop_monitor_event.set()
            if _monitor_thread and _monitor_thread.is_alive():
                _monitor_thread.join(timeout=2.0)
            _is_monitoring = False

            context.user_print("🛑 Monitoramento de câmeras desativado.")
            return ToolExecutionResult(
                success=True,
                reply_text="O monitoramento de câmeras foi interrompido com sucesso."
            )

        elif action == "get_recent_events":
            limit = int(args.get("limit", 5))
            try:
                res = requests.get(f"{frigate_url}/api/events?limit={limit}", timeout=6.0)
                if res.status_code == 200:
                    events = res.json()
                    if not events:
                        return ToolExecutionResult(success=True, reply_text="Nenhum evento recente detectado nas câmeras.")

                    lines = []
                    for ev in events:
                        camera = ev.get("camera", "câmera")
                        label = ev.get("label", "objeto")
                        sub_label = ev.get("sub_label")
                        st = ev.get("start_time", 0)
                        horario = time.strftime("%H:%M:%S", time.localtime(st))
                        desc = f"{sub_label} ({label})" if sub_label else label
                        lines.append(f"- Às {horario} na câmera '{camera}': {desc}")

                    summary = "\n".join(lines)
                    return ToolExecutionResult(
                        success=True,
                        reply_text=f"Últimas detecções registradas pelas câmeras:\n{summary}"
                    )
                else:
                    return ToolExecutionResult(
                        success=False,
                        reply_text=f"Falha ao consultar Frigate NVR (HTTP {res.status_code})."
                    )
            except Exception as e:
                return ToolExecutionResult(success=False, reply_text=f"Erro ao acessar Frigate NVR: {e}")

        elif action == "get_status":
            status_text = "ativo" if _is_monitoring else "inativo"
            return ToolExecutionResult(
                success=True,
                reply_text=f"O monitoramento de câmeras está atualmente {status_text} (Frigate URL: {frigate_url})."
            )

        else:
            return ToolExecutionResult(success=False, reply_text=f"Ação desconhecida: {action}")
