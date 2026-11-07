"""
Jarvis Web Server & API for CasaOS / Headless Ubuntu Server.
Provides modern web chat UI, voice input/output API, dynamic model selection,
Home Assistant integration settings, and Telegram Bot management.
"""

import os
import sys
import io
import time
import json
import base64
import ipaddress
import logging
from logging.handlers import RotatingFileHandler
import secrets
import tempfile
import threading
from pathlib import Path
from typing import Optional, Any, Dict, List
from urllib.parse import urlsplit

import requests
from flask import Flask, request, jsonify, render_template_string

# Add parent directories to sys.path so 'jarvis' can be imported
current_dir = Path(__file__).resolve().parent
src_dir = current_dir.parent
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from jarvis.config import load_settings, Settings
from jarvis.memory.db import Database
from jarvis.memory.conversation import DialogueMemory
from jarvis.reply.engine import run_reply_engine
from jarvis.tools.registry import initialize_mcp_tools
from jarvis.tools.builtin.home_assistant import HA_CONFIG_PATH, get_ha_config
from jarvis.tools.builtin.camera_monitor import FRIGATE_CONFIG_PATH, get_frigate_config
from server_app.telegram_bot import (
    start_telegram_bot, stop_telegram_bot, get_telegram_status,
    save_telegram_config, load_telegram_config, test_telegram_token
)

default_log_dir = Path.home() / ".local" / "share" / "jarvis" / "logs"
log_dir = Path(os.environ.get("JARVIS_LOG_DIR", str(default_log_dir)))
log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(log_dir / "server.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"),
    ],
)
logger = logging.getLogger("jarvis_web")

app = Flask(__name__)

# Global instances
cfg: Optional[Settings] = None
db: Optional[Database] = None
dm: Optional[DialogueMemory] = None
whisper_model = None
whisper_lock = threading.Lock()
chat_lock = threading.Lock()


def init_jarvis():
    global cfg, db, dm, whisper_model
    logger.info("Initializing Jarvis Server Engine...")
    cfg = load_settings()

    # Apply environment variable overrides if provided
    overrides = {
        "tool_selection_strategy": os.environ.get("TOOL_SELECTION_STRATEGY", "llm"),
        "tts_enabled": False,
        "planner_timeout_sec": 25.0,
        "intent_judge_timeout_sec": 25.0,
        "llm_tools_timeout_sec": 30.0
    }

    # Check for persisted model selection
    active_model_file = Path.home() / ".config" / "jarvis" / "active_model.txt"
    if active_model_file.exists():
        try:
            persisted_model = active_model_file.read_text(encoding="utf-8").strip()
            if persisted_model:
                overrides["llm_chat_model"] = persisted_model
                overrides["ollama_chat_model"] = persisted_model
                overrides["fast_model"] = persisted_model
        except Exception:
            pass

    if os.environ.get("OLLAMA_BASE_URL"):
        overrides["llm_base_url"] = os.environ["OLLAMA_BASE_URL"]
        overrides["ollama_base_url"] = os.environ["OLLAMA_BASE_URL"]
    if os.environ.get("LLM_CHAT_MODEL") and "llm_chat_model" not in overrides:
        overrides["llm_chat_model"] = os.environ["LLM_CHAT_MODEL"]
        overrides["ollama_chat_model"] = os.environ["LLM_CHAT_MODEL"]
    if os.environ.get("FAST_MODEL") and "fast_model" not in overrides:
        overrides["fast_model"] = os.environ["FAST_MODEL"]
    if os.environ.get("LLM_PROVIDER"):
        overrides["llm_provider"] = os.environ["LLM_PROVIDER"]

    if overrides:
        d = cfg.__dict__.copy()
        d.update(overrides)
        cfg = Settings(**d)

    logger.info(f"LLM Provider: {cfg.llm_provider} | Base URL: {cfg.llm_base_url} | Model: {cfg.llm_chat_model}")

    # Database & Memory
    db = Database(cfg.db_path, cfg.sqlite_vss_path)
    dm = DialogueMemory(inactivity_timeout=cfg.dialogue_memory_timeout)

    # Initialize MCP tools
    try:
        initialize_mcp_tools(cfg)
    except Exception as e:
        logger.warning(f"MCP tools initialization warning: {e}")

    # Whisper
    try:
        from faster_whisper import WhisperModel
        whisper_device = os.environ.get("WHISPER_DEVICE", "cpu")
        compute_type = "int8" if whisper_device == "cpu" else "float16"
        model_size = os.environ.get("WHISPER_MODEL", "base")
        logger.info(f"Loading Whisper model '{model_size}' on {whisper_device} ({compute_type})...")
        whisper_model = WhisperModel(model_size, device=whisper_device, compute_type=compute_type)
        logger.info("Whisper model loaded successfully.")
    except Exception as e:
        logger.warning(f"Whisper not loaded: {e}")

    # Start Telegram Bot if configured and enabled
    try:
        start_telegram_bot(db, cfg, dm, whisper_model, whisper_lock)
    except Exception as e:
        logger.warning(f"Telegram Bot start warning: {e}")

    # Start Background Diary & Knowledge Graph memory updater
    def _diary_background_loop():
        logger.info("Background diary memory flusher started.")
        while True:
            time.sleep(60)
            try:
                if dm and db and cfg and dm.has_pending_chunks():
                    from jarvis.memory.conversation import update_diary_from_dialogue_memory
                    logger.info("Persisting dialogue memory to diary and knowledge graph...")
                    update_diary_from_dialogue_memory(db, dm, cfg, force=False)
            except Exception as e:
                logger.debug(f"Diary update pass: {e}")

    diary_thread = threading.Thread(target=_diary_background_loop, daemon=True)
    diary_thread.start()

    logger.info("Jarvis Server Engine ready!")


def _web_auth_settings() -> tuple[str, tuple[str, ...], str, str]:
    """Return listener and published-address settings for the web interface."""
    host = os.environ.get("JARVIS_WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"
    raw_addresses = os.environ.get(
        "JARVIS_WEB_BIND_ADDRESSES",
        os.environ.get("JARVIS_WEB_BIND_ADDRESS", "127.0.0.1"),
    )
    published_addresses = tuple(address.strip() for address in raw_addresses.split(",") if address.strip())
    if not published_addresses:
        published_addresses = ("127.0.0.1",)
    username = os.environ.get("JARVIS_WEB_USERNAME", "")
    password = os.environ.get("JARVIS_WEB_PASSWORD", "")
    return host, published_addresses, username, password


def _is_loopback_address(value: str) -> bool:
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


@app.before_request
def protect_web_interface():
    """Require Basic Auth whenever credentials are configured.

    When Compose publishes the service beyond loopback, startup separately
    requires credentials. This keeps direct local development friction-free.
    """
    _, _, expected_username, expected_password = _web_auth_settings()
    auth_enabled = bool(expected_username and expected_password)
    if not auth_enabled:
        return None

    auth = request.authorization
    supplied_username = ((auth.username or "") if auth else "").encode("utf-8")
    supplied_password = ((auth.password or "") if auth else "").encode("utf-8")
    username_matches = secrets.compare_digest(supplied_username, expected_username.encode("utf-8"))
    password_matches = secrets.compare_digest(supplied_password, expected_password.encode("utf-8"))
    if not (username_matches & password_matches):
        logger.warning("Rejected unauthenticated web request from %s", request.remote_addr)
        response = jsonify({"error": "Authentication required"})
        response.status_code = 401
        response.headers["WWW-Authenticate"] = 'Basic realm="Jarvis", charset="UTF-8"'
        return response

    # A browser may attach cached Basic Auth credentials to a cross-origin
    # request. Reject foreign origins on state-changing routes to prevent CSRF.
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        source = request.headers.get("Origin") or request.headers.get("Referer")
        if source:
            try:
                source_host = urlsplit(source).netloc.lower()
            except ValueError:
                source_host = ""
            if source_host != request.host.lower():
                return jsonify({"error": "Cross-origin request rejected"}), 403
    return None


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Jarvis AI - CasaOS</title>
  <link rel="icon" href="https://raw.githubusercontent.com/isair/jarvis/main/docs/img/face.png">
  <style>
    :root {
      --bg: #0d0f12;
      --card-bg: #15181e;
      --accent: #f59e0b;
      --accent-glow: rgba(245, 158, 11, 0.25);
      --text: #f3f4f6;
      --text-muted: #9ca3af;
      --border: #262b35;
      --user-bubble: #1e2430;
      --jarvis-bubble: #1b202a;
      --success: #22c55e;
      --danger: #ef4444;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      display: flex;
      flex-direction: column;
      height: 100vh;
      overflow: hidden;
    }

    header {
      background: var(--card-bg);
      border-bottom: 1px solid var(--border);
      padding: 10px 20px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      box-shadow: 0 4px 12px rgba(0,0,0,0.4);
      z-index: 10;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
    }

    .brand img {
      width: 36px;
      height: 36px;
      border-radius: 50%;
      box-shadow: 0 0 15px var(--accent-glow);
    }

    .brand h1 {
      font-size: 1.25rem;
      font-weight: 700;
      color: var(--accent);
      letter-spacing: 0.5px;
    }

    .nav-actions {
      display: flex;
      align-items: center;
      gap: 12px;
    }

    .model-selector-wrap {
      display: flex;
      align-items: center;
      background: #111419;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 4px 8px;
      gap: 6px;
    }

    .model-selector-wrap label {
      font-size: 0.75rem;
      color: var(--text-muted);
      font-weight: 600;
    }

    select {
      background: transparent;
      color: var(--accent);
      border: none;
      outline: none;
      font-size: 0.85rem;
      font-weight: 700;
      cursor: pointer;
    }

    select option {
      background: #15181e;
      color: #fff;
    }

    .badge {
      background: rgba(34, 197, 94, 0.1);
      border: 1px solid rgba(34, 197, 94, 0.3);
      color: var(--success);
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 0.75rem;
      font-weight: 600;
    }

    .btn-icon {
      background: #1e2430;
      border: 1px solid var(--border);
      color: var(--text);
      padding: 8px 12px;
      border-radius: 8px;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 0.85rem;
      font-weight: 600;
      transition: all 0.2s;
    }

    .btn-icon:hover {
      border-color: var(--accent);
      color: var(--accent);
      box-shadow: 0 0 8px var(--accent-glow);
    }

    .container {
      display: flex;
      flex: 1;
      height: calc(100vh - 65px);
      max-width: 1300px;
      margin: 0 auto;
      width: 100%;
    }

    main {
      flex: 1;
      display: flex;
      flex-direction: column;
      height: 100%;
    }

    #chat-box {
      flex: 1;
      overflow-y: auto;
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 16px;
    }

    .msg {
      display: flex;
      flex-direction: column;
      max-width: 80%;
      padding: 12px 16px;
      border-radius: 12px;
      line-height: 1.5;
      font-size: 0.95rem;
      word-break: break-word;
      animation: fadeIn 0.2s ease-in-out;
    }

    @keyframes fadeIn {
      from { opacity: 0; transform: translateY(6px); }
      to { opacity: 1; transform: translateY(0); }
    }

    .msg.user {
      align-self: flex-end;
      background: var(--user-bubble);
      border: 1px solid #333d4e;
      border-bottom-right-radius: 2px;
    }

    .msg.jarvis {
      align-self: flex-start;
      background: var(--jarvis-bubble);
      border: 1px solid var(--border);
      border-left: 3px solid var(--accent);
      border-bottom-left-radius: 2px;
    }

    .msg .sender {
      font-size: 0.75rem;
      font-weight: 700;
      color: var(--accent);
      margin-bottom: 4px;
    }

    .input-bar {
      background: var(--card-bg);
      border-top: 1px solid var(--border);
      padding: 14px 20px;
      display: flex;
      gap: 10px;
      align-items: center;
    }

    input[type="text"], input[type="password"] {
      flex: 1;
      background: #0f1216;
      border: 1px solid var(--border);
      color: var(--text);
      padding: 12px 16px;
      border-radius: 8px;
      font-size: 0.95rem;
      outline: none;
      transition: border-color 0.2s;
    }

    input:focus {
      border-color: var(--accent);
      box-shadow: 0 0 8px var(--accent-glow);
    }

    button.primary-btn {
      background: var(--accent);
      color: #000;
      border: none;
      font-weight: 700;
      padding: 12px 20px;
      border-radius: 8px;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 8px;
      transition: all 0.2s;
    }

    button.primary-btn:hover {
      filter: brightness(1.1);
      box-shadow: 0 0 12px var(--accent-glow);
    }

    button.mic-btn {
      background: #222834;
      color: var(--text);
      border: 1px solid var(--border);
      padding: 12px;
      border-radius: 8px;
      cursor: pointer;
    }

    button.mic-btn.recording {
      background: var(--danger);
      color: #fff;
      border-color: #f87171;
      box-shadow: 0 0 15px rgba(239, 68, 68, 0.5);
      animation: pulse 1s infinite alternate;
    }

    @keyframes pulse {
      from { transform: scale(1); }
      to { transform: scale(1.05); }
    }

    .sidebar {
      width: 320px;
      border-left: 1px solid var(--border);
      background: var(--card-bg);
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 16px;
      overflow-y: auto;
    }

    @media (max-width: 820px) {
      .sidebar { display: none; }
    }

    .sidebar h2 {
      font-size: 0.9rem;
      color: var(--accent);
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }

    .info-card {
      background: rgba(255, 255, 255, 0.03);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
      font-size: 0.85rem;
      line-height: 1.5;
    }

    .orb-container {
      display: flex;
      justify-content: center;
      align-items: center;
      padding: 16px 0;
    }

    .orb {
      width: 90px;
      height: 90px;
      border-radius: 50%;
      background: radial-gradient(circle at 35% 35%, #fbbf24, #d97706, #78350f);
      box-shadow: 0 0 30px rgba(245, 158, 11, 0.5), inset 0 0 15px rgba(255, 255, 255, 0.3);
      transition: all 0.3s;
    }

    .orb.talking {
      animation: pulse-orb 0.8s infinite alternate;
    }

    @keyframes pulse-orb {
      0% { transform: scale(1); box-shadow: 0 0 25px rgba(245, 158, 11, 0.4); }
      100% { transform: scale(1.15); box-shadow: 0 0 50px rgba(245, 158, 11, 0.8); }
    }

    /* Modal Styles */
    .modal-overlay {
      display: none;
      position: fixed;
      top: 0; left: 0; right: 0; bottom: 0;
      background: rgba(0,0,0,0.7);
      backdrop-filter: blur(4px);
      z-index: 100;
      justify-content: center;
      align-items: center;
    }

    .modal-overlay.active {
      display: flex;
    }

    .modal {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      width: 90%;
      max-width: 580px;
      box-shadow: 0 20px 40px rgba(0,0,0,0.6);
      overflow: hidden;
      animation: fadeIn 0.2s ease-in-out;
    }

    .modal-header {
      padding: 16px 20px;
      border-bottom: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      align-items: center;
    }

    .modal-header h3 {
      font-size: 1.1rem;
      color: var(--accent);
    }

    .modal-close {
      background: transparent;
      border: none;
      color: var(--text-muted);
      font-size: 1.5rem;
      cursor: pointer;
    }

    .modal-tabs {
      display: flex;
      border-bottom: 1px solid var(--border);
      background: #111419;
    }

    .modal-tab {
      flex: 1;
      padding: 12px;
      text-align: center;
      font-size: 0.9rem;
      font-weight: 600;
      cursor: pointer;
      color: var(--text-muted);
      border-bottom: 2px solid transparent;
      transition: all 0.2s;
    }

    .modal-tab.active {
      color: var(--accent);
      border-bottom-color: var(--accent);
      background: var(--card-bg);
    }

    .modal-body {
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 16px;
      max-height: 70vh;
      overflow-y: auto;
    }

    .form-group {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }

    .form-group label {
      font-size: 0.85rem;
      font-weight: 600;
      color: var(--text-muted);
    }

    .switch-wrap {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-top: 6px;
    }

    .toast {
      position: fixed;
      bottom: 20px;
      right: 20px;
      background: #1e2430;
      border: 1px solid var(--accent);
      color: #fff;
      padding: 10px 16px;
      border-radius: 8px;
      font-size: 0.85rem;
      display: none;
      box-shadow: 0 4px 16px rgba(0,0,0,0.5);
      z-index: 200;
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <img src="https://raw.githubusercontent.com/isair/jarvis/main/docs/img/face.png" alt="Jarvis">
      <h1>JARVIS</h1>
    </div>
    <div class="nav-actions">
      <div class="model-selector-wrap">
        <label for="model-select">MODELO:</label>
        <select id="model-select" onchange="switchModel(this.value)">
          <option value="{{ model }}">{{ model }}</option>
        </select>
      </div>
      <button class="btn-icon" onclick="openSettings()">
        ⚙️ Configurações
      </button>
      <span class="badge" id="status-badge">● Online</span>
    </div>
  </header>

  <div class="container">
    <main>
      <div id="chat-box">
        <div class="msg jarvis">
          <div class="sender">Jarvis</div>
          <div>Olá! Estou pronto e rodando no seu Ubuntu Server via CasaOS. Em que posso ser útil hoje?</div>
        </div>
      </div>

      <div class="input-bar">
        <button id="mic-btn" class="mic-btn" title="Falar com o Jarvis">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"></path>
            <path d="M19 10v2a7 7 0 0 1-14 0v-2"></path>
            <line x1="12" y1="19" x2="12" y2="23"></line>
            <line x1="8" y1="23" x2="16" y2="23"></line>
          </svg>
        </button>
        <input type="text" id="user-input" placeholder="Converse com Jarvis ou controle o Home Assistant..." autocomplete="off">
        <button id="send-btn" class="primary-btn">
          <span>Enviar</span>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="22" y1="2" x2="11" y2="13"></line>
            <polygon points="22 2 15 22 11 13 2 9 22 2"></polygon>
          </svg>
        </button>
      </div>
    </main>

    <div class="sidebar">
      <div class="orb-container">
        <div id="jarvis-orb" class="orb"></div>
      </div>
      <h2>Status do Sistema</h2>
      <div class="info-card">
        <strong>Ollama:</strong> {{ ollama_url }}<br>
        <strong>Modelo Atual:</strong> <span id="sidebar-model">{{ model }}</span><br>
        <strong>Home Assistant:</strong> <span id="ha-badge">Carregando...</span><br>
        <strong>Frigate NVR:</strong> <span id="frigate-badge">Carregando...</span><br>
        <strong>Telegram Bot:</strong> <span id="tg-badge">Carregando...</span>
      </div>

      <h2>Ações Rápidas</h2>
      <button style="width:100%; background:#222834; color:var(--text); border:1px solid var(--border); padding:10px; border-radius:8px; cursor:pointer;" onclick="clearChat()">Limpar Sessão</button>
    </div>
  </div>

  <!-- Settings Modal -->
  <div id="settings-modal" class="modal-overlay">
    <div class="modal">
      <div class="modal-header">
        <h3>Configurações do Jarvis</h3>
        <button class="modal-close" onclick="closeSettings()">&times;</button>
      </div>
      <div class="modal-tabs">
        <div class="modal-tab active" onclick="setTab('ha')">🏠 Home Assistant</div>
        <div class="modal-tab" onclick="setTab('frigate')">📹 Frigate NVR</div>
        <div class="modal-tab" onclick="setTab('tg')">✈️ Telegram Bot</div>
      </div>

      <!-- Home Assistant Tab Content -->
      <div id="tab-ha" class="modal-body">
        <p style="font-size:0.85rem; color:var(--text-muted);">
          Conecte o Jarvis ao seu Home Assistant para controlar luzes, interruptores e consultar sensores da sua casa por voz ou texto.
        </p>
        <div class="form-group">
          <label>URL do Home Assistant:</label>
          <input type="text" id="ha-url" placeholder="http://homeassistant:8123">
        </div>
        <div class="form-group">
          <label>Token de Longa Duração (Long-Lived Access Token):</label>
          <input type="password" id="ha-token" placeholder="Cole o token gerado no seu perfil do Home Assistant">
          <small style="color:var(--text-muted); font-size:0.75rem;">
            (Gere em: Seu Perfil no Home Assistant -> Tokens de Acesso de Longa Duração)
          </small>
        </div>
        <div class="form-group">
          <label>Código PIN do Alarme (para desarmar):</label>
          <input type="password" id="ha-alarm-code" placeholder="Digite para definir ou alterar">
          <small style="color:var(--text-muted); font-size:0.75rem;">
            (PIN salvo nunca é exibido novamente; deixe em branco para mantê-lo. Marque abaixo para removê-lo.)
          </small>
        </div>
        <div class="switch-wrap">
          <input type="checkbox" id="ha-clear-alarm-code">
          <label for="ha-clear-alarm-code" style="font-size:0.9rem;">Remover PIN salvo</label>
        </div>
        <div style="display:flex; gap:10px; margin-top:8px;">
          <button class="primary-btn" onclick="saveHAConfig()">Salvar Configuração</button>
          <button class="btn-icon" onclick="testHA()">Testar Conexão</button>
        </div>
        <div id="ha-test-result" style="font-size:0.85rem; margin-top:4px;"></div>
      </div>

      <!-- Frigate NVR Tab Content -->
      <div id="tab-frigate" class="modal-body" style="display:none;">
        <p style="font-size:0.85rem; color:var(--text-muted);">
          Integração com Frigate NVR para detecção inteligente de pessoas, veículos e vigilância em tempo real.
        </p>
        <div class="form-group">
          <label>URL do Frigate NVR:</label>
          <input type="text" id="frigate-url" placeholder="http://frigate:5000">
        </div>
        <div class="switch-wrap">
          <input type="checkbox" id="frigate-enabled" checked>
          <label for="frigate-enabled" style="font-size:0.9rem; font-weight:600;">Ativar Integração com Frigate</label>
        </div>
        <div style="display:flex; gap:10px; margin-top:8px;">
          <button class="primary-btn" onclick="saveFrigateConfig()">Salvar Configuração</button>
          <button class="btn-icon" onclick="testFrigate()">Testar Conexão Frigate</button>
        </div>
        <div id="frigate-test-result" style="font-size:0.85rem; margin-top:4px;"></div>
      </div>

      <!-- Telegram Tab Content -->
      <div id="tab-tg" class="modal-body" style="display:none;">
        <p style="font-size:0.85rem; color:var(--text-muted);">
          Interaja com o Jarvis enviando mensagens de texto e áudio pelo aplicativo do Telegram no seu smartphone.
        </p>
        <div class="form-group">
          <label>Token do Bot do Telegram:</label>
          <input type="password" id="tg-token" placeholder="Token fornecido pelo @BotFather">
        </div>
        <div class="form-group">
          <label>Chat ID Permitido (Opcional):</label>
          <input type="text" id="tg-chat-id" placeholder="Ex: 123456789 (deixe vazio para aceitar qualquer chat)">
        </div>
        <div class="switch-wrap">
          <input type="checkbox" id="tg-enabled">
          <label for="tg-enabled" style="font-size:0.9rem; font-weight:600;">Ativar Bot do Telegram</label>
        </div>
        <div style="display:flex; gap:10px; margin-top:8px;">
          <button class="primary-btn" onclick="saveTelegram()">Salvar e Atualizar Bot</button>
        </div>
        <div id="tg-status-result" style="font-size:0.85rem; margin-top:4px;"></div>
      </div>
    </div>
  </div>

  <div id="toast" class="toast"></div>

  <script>
    const chatBox = document.getElementById('chat-box');
    const userInput = document.getElementById('user-input');
    const sendBtn = document.getElementById('send-btn');
    const micBtn = document.getElementById('mic-btn');
    const orb = document.getElementById('jarvis-orb');

    function showToast(msg) {
      const t = document.getElementById('toast');
      t.textContent = msg;
      t.style.display = 'block';
      setTimeout(() => { t.style.display = 'none'; }, 3500);
    }

    function appendMessage(sender, text, isUser) {
      const msg = document.createElement('div');
      msg.className = 'msg ' + (isUser ? 'user' : 'jarvis');
      const senderDiv = document.createElement('div');
      senderDiv.className = 'sender';
      senderDiv.textContent = sender;
      const contentDiv = document.createElement('div');
      contentDiv.textContent = text;
      msg.appendChild(senderDiv);
      msg.appendChild(contentDiv);
      chatBox.appendChild(msg);
      chatBox.scrollTop = chatBox.scrollHeight;
    }

    async function sendMessage() {
      const text = userInput.value.trim();
      if (!text) return;

      appendMessage('Você', text, true);
      userInput.value = '';
      orb.classList.add('talking');

      try {
        const res = await fetch('/api/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text })
        });
        const data = await res.json();
        if (data.reply) {
          appendMessage('Jarvis', data.reply, false);
        } else {
          appendMessage('Jarvis', 'Desculpe, ocorreu um erro ao gerar a resposta.', false);
        }
      } catch (err) {
        appendMessage('Jarvis', 'Erro de conexão com o servidor Jarvis.', false);
      } finally {
        orb.classList.remove('talking');
      }
    }

    sendBtn.addEventListener('click', sendMessage);
    userInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') sendMessage();
    });

    // Voice recording via browser microphone
    let mediaRecorder = null;
    let audioChunks = [];

    micBtn.addEventListener('click', async () => {
      if (mediaRecorder && mediaRecorder.state === 'recording') {
        mediaRecorder.stop();
        micBtn.classList.remove('recording');
        return;
      }

      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        mediaRecorder = new MediaRecorder(stream);
        audioChunks = [];

        mediaRecorder.ondataavailable = (e) => {
          if (e.data.size > 0) audioChunks.push(e.data);
        };

        mediaRecorder.onstop = async () => {
          const audioBlob = new Blob(audioChunks, { type: 'audio/webm' });
          const formData = new FormData();
          formData.append('audio', audioBlob, 'voice.webm');

          orb.classList.add('talking');
          appendMessage('Você', '🎙️ [Áudio enviado...]', true);

          try {
            const res = await fetch('/api/voice', {
              method: 'POST',
              body: formData
            });
            const data = await res.json();
            if (data.user_text) {
              appendMessage('Você', data.user_text, true);
            }
            if (data.reply) {
              appendMessage('Jarvis', data.reply, false);
            }
          } catch (err) {
            appendMessage('Jarvis', 'Erro ao transcrever ou processar áudio.', false);
          } finally {
            orb.classList.remove('talking');
          }
        };

        mediaRecorder.start();
        micBtn.classList.add('recording');
      } catch (err) {
        alert('Permissão de microfone negada ou indisponível: ' + err.message);
      }
    });

    function clearChat() {
      fetch('/api/reset', { method: 'POST' }).then(() => {
        chatBox.innerHTML = '<div class="msg jarvis"><div class="sender">Jarvis</div><div>Sessão reiniciada. Em que posso ajudar?</div></div>';
      });
    }

    // Model management
    async function loadModels() {
      try {
        const res = await fetch('/api/models');
        const data = await res.json();
        const select = document.getElementById('model-select');
        select.innerHTML = '';
        data.models.forEach(m => {
          const opt = document.createElement('option');
          opt.value = m;
          opt.textContent = m;
          if (m === data.current) opt.selected = true;
          select.appendChild(opt);
        });
        document.getElementById('sidebar-model').textContent = data.current;
      } catch (e) {
        console.error('Failed to load models:', e);
      }
    }

    async function switchModel(newModel) {
      try {
        const res = await fetch('/api/models/select', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model: newModel })
        });
        const data = await res.json();
        if (data.status === 'ok') {
          document.getElementById('sidebar-model').textContent = newModel;
          showToast('Modelo alterado para ' + newModel);
        }
      } catch (e) {
        alert('Falha ao alternar modelo: ' + e);
      }
    }

    // Modal tabs
    function openSettings() {
      document.getElementById('settings-modal').classList.add('active');
      loadHAStatus();
      loadFrigateStatus();
      loadTelegramStatus();
    }

    function closeSettings() {
      document.getElementById('settings-modal').classList.remove('active');
    }

    function setTab(tab) {
      document.querySelectorAll('.modal-tab').forEach(t => t.classList.remove('active'));
      document.getElementById('tab-ha').style.display = tab === 'ha' ? 'flex' : 'none';
      document.getElementById('tab-frigate').style.display = tab === 'frigate' ? 'flex' : 'none';
      document.getElementById('tab-tg').style.display = tab === 'tg' ? 'flex' : 'none';
      event.target.classList.add('active');
    }

    // Home Assistant Config
    async function loadHAStatus() {
      try {
        const res = await fetch('/api/ha');
        const data = await res.json();
        if (data.url) document.getElementById('ha-url').value = data.url;
        document.getElementById('ha-alarm-code').value = '';
        document.getElementById('ha-clear-alarm-code').checked = false;
        const badge = document.getElementById('ha-badge');
        badge.textContent = data.has_token ? '🟢 Configurado' : '⚪ Não autenticado';
        badge.style.color = data.has_token ? 'var(--success)' : 'var(--text-muted)';
      } catch (e) {}
    }

    async function saveHAConfig() {
      const url = document.getElementById('ha-url').value.trim();
      const token = document.getElementById('ha-token').value.trim();
      const alarm_code = document.getElementById('ha-alarm-code').value.trim();
      const clear_alarm_code = document.getElementById('ha-clear-alarm-code').checked;
      try {
        const res = await fetch('/api/ha', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url, token, alarm_code, clear_alarm_code })
        });
        const data = await res.json();
        if (data.status === 'ok') {
          showToast('Configurações do Home Assistant salvas!');
          loadHAStatus();
        }
      } catch (e) {
        alert('Erro ao salvar: ' + e);
      }
    }

    async function testHA() {
      const resDiv = document.getElementById('ha-test-result');
      resDiv.textContent = 'Testando conexão...';
      resDiv.style.color = 'var(--accent)';
      try {
        const res = await fetch('/api/ha/test', { method: 'POST' });
        const data = await res.json();
        if (data.status === 'ok') {
          resDiv.textContent = '✅ ' + data.message;
          resDiv.style.color = 'var(--success)';
        } else {
          resDiv.textContent = '❌ ' + data.message;
          resDiv.style.color = 'var(--danger)';
        }
      } catch (e) {
        resDiv.textContent = '❌ Erro de requisição: ' + e;
        resDiv.style.color = 'var(--danger)';
      }
    }

    // Frigate NVR Config
    async function loadFrigateStatus() {
      try {
        const res = await fetch('/api/frigate');
        const data = await res.json();
        if (data.url) document.getElementById('frigate-url').value = data.url;
        document.getElementById('frigate-enabled').checked = data.enabled !== false;
        const badge = document.getElementById('frigate-badge');
        badge.textContent = data.enabled ? '🟢 Habilitado' : '⚪ Desativado';
        badge.style.color = data.enabled ? 'var(--success)' : 'var(--text-muted)';
      } catch (e) {}
    }

    async function saveFrigateConfig() {
      const url = document.getElementById('frigate-url').value.trim();
      const enabled = document.getElementById('frigate-enabled').checked;
      try {
        const res = await fetch('/api/frigate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url, enabled })
        });
        const data = await res.json();
        if (data.status === 'ok') {
          showToast('Configuração do Frigate salva!');
          loadFrigateStatus();
        }
      } catch (e) {
        alert('Erro ao salvar Frigate: ' + e);
      }
    }

    async function testFrigate() {
      const resDiv = document.getElementById('frigate-test-result');
      resDiv.textContent = 'Testando conexão com Frigate...';
      resDiv.style.color = 'var(--accent)';
      try {
        const res = await fetch('/api/frigate/test', { method: 'POST' });
        const data = await res.json();
        if (data.status === 'ok') {
          resDiv.textContent = '✅ ' + data.message;
          resDiv.style.color = 'var(--success)';
        } else {
          resDiv.textContent = '❌ ' + data.message;
          resDiv.style.color = 'var(--danger)';
        }
      } catch (e) {
        resDiv.textContent = '❌ Erro ao conectar: ' + e;
        resDiv.style.color = 'var(--danger)';
      }
    }

    // Telegram Bot Config
    async function loadTelegramStatus() {
      try {
        const res = await fetch('/api/telegram');
        const data = await res.json();
        document.getElementById('tg-enabled').checked = data.enabled;
        if (data.allowed_chat_id) document.getElementById('tg-chat-id').value = data.allowed_chat_id;
        
        const badge = document.getElementById('tg-badge');
        const resDiv = document.getElementById('tg-status-result');
        if (data.running) {
          badge.textContent = '🟢 Online (@' + data.username + ')';
          badge.style.color = 'var(--success)';
          resDiv.textContent = 'Bot ativo e respondendo como @' + data.username;
          resDiv.style.color = 'var(--success)';
        } else if (data.has_token) {
          badge.textContent = '🟡 Parado';
          badge.style.color = 'var(--accent)';
          resDiv.textContent = 'Token salvo, mas o bot está desativado.';
          resDiv.style.color = 'var(--text-muted)';
        } else {
          badge.textContent = '⚪ Desativado';
          badge.style.color = 'var(--text-muted)';
          resDiv.textContent = 'Nenhum token configurado.';
          resDiv.style.color = 'var(--text-muted)';
        }
      } catch (e) {}
    }

    async function saveTelegram() {
      const token = document.getElementById('tg-token').value.trim();
      const allowed_chat_id = document.getElementById('tg-chat-id').value.trim();
      const enabled = document.getElementById('tg-enabled').checked;

      try {
        const res = await fetch('/api/telegram', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ token, allowed_chat_id, enabled })
        });
        const data = await res.json();
        if (data.status === 'ok') {
          showToast(data.message);
          loadTelegramStatus();
        } else {
          alert('Erro: ' + data.message);
        }
      } catch (e) {
        alert('Erro ao salvar Telegram: ' + e);
      }
    }

    // Initial Load
    window.onload = () => {
      loadModels();
      loadHAStatus();
      loadFrigateStatus();
      loadTelegramStatus();
    };
  </script>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def index():
    return render_template_string(
        HTML_TEMPLATE,
        model=cfg.llm_chat_model if cfg else "Ollama",
        provider=cfg.llm_provider if cfg else "ollama",
        ollama_url=cfg.llm_base_url if cfg else "http://localhost:11434"
    )


@app.route("/api/models", methods=["GET"])
def get_models():
    models_list = [cfg.llm_chat_model]
    try:
        base_url = cfg.llm_base_url.rstrip("/")
        res = requests.get(f"{base_url}/api/tags", timeout=5.0)
        if res.status_code == 200:
            data = res.json()
            models_list = [m["name"] for m in data.get("models", [])]
    except Exception as e:
        logger.warning(f"Failed to query Ollama models: {e}")

    return jsonify({
        "current": cfg.llm_chat_model,
        "models": models_list
    })


@app.route("/api/models/select", methods=["POST"])
def select_model():
    global cfg
    data = request.get_json(force=True, silent=True) or {}
    new_model = data.get("model", "").strip()
    if not new_model:
        return jsonify({"status": "error", "message": "Model not provided"}), 400

    logger.info(f"Switching active model to: {new_model}")
    d = cfg.__dict__.copy()
    d["llm_chat_model"] = new_model
    d["ollama_chat_model"] = new_model
    d["fast_model"] = new_model
    cfg = Settings(**d)

    try:
        active_model_file = Path.home() / ".config" / "jarvis" / "active_model.txt"
        active_model_file.parent.mkdir(parents=True, exist_ok=True)
        active_model_file.write_text(new_model, encoding="utf-8")
    except Exception as e:
        logger.warning(f"Could not persist active model: {e}")

    return jsonify({"status": "ok", "current": new_model})


@app.route("/api/ha", methods=["GET", "POST"])
def ha_config_endpoint():
    if request.method == "GET":
        ha_cfg = get_ha_config()
        return jsonify({
            "url": ha_cfg["url"],
            "has_token": bool(ha_cfg["token"])
        })
    else:
        data = request.get_json(force=True, silent=True) or {}
        url = data.get("url", "").strip()
        token = data.get("token", "").strip()
        alarm_code = data.get("alarm_code", "").strip()
        clear_alarm_code = bool(data.get("clear_alarm_code", False))

        curr_cfg = get_ha_config()
        if url:
            curr_cfg["url"] = url
        if token:
            curr_cfg["token"] = token
        if clear_alarm_code:
            curr_cfg["alarm_code"] = ""
        elif alarm_code:
            curr_cfg["alarm_code"] = alarm_code

        HA_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(HA_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(curr_cfg, f, indent=2)

        return jsonify({"status": "ok", "message": "Home Assistant configurado com sucesso!"})


@app.route("/api/ha/test", methods=["POST"])
def ha_test_endpoint():
    ha_cfg = get_ha_config()
    url = ha_cfg["url"]
    token = ha_cfg["token"]
    if not token:
        return jsonify({"status": "error", "message": "Nenhum token configurado."}), 400

    try:
        res = requests.get(f"{url}/api/", headers={"Authorization": f"Bearer {token}"}, timeout=6.0)
        if res.status_code == 200:
            msg = res.json().get("message", "API acessível!")
            return jsonify({"status": "ok", "message": f"Conectado com sucesso! ({msg})"})
        else:
            return jsonify({"status": "error", "message": f"HTTP {res.status_code}: {res.text}"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route("/api/frigate", methods=["GET", "POST"])
def frigate_config_endpoint():
    if request.method == "GET":
        return jsonify(get_frigate_config())
    else:
        data = request.get_json(force=True, silent=True) or {}
        url = data.get("url", "").strip()
        enabled = bool(data.get("enabled", True))

        curr_cfg = get_frigate_config()
        if url:
            curr_cfg["url"] = url
        curr_cfg["enabled"] = enabled

        FRIGATE_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(FRIGATE_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(curr_cfg, f, indent=2)

        return jsonify({"status": "ok", "message": "Configuração do Frigate NVR salva com sucesso!"})


@app.route("/api/frigate/test", methods=["POST"])
def frigate_test_endpoint():
    f_cfg = get_frigate_config()
    url = f_cfg["url"]
    try:
        res = requests.get(f"{url}/api/version", timeout=5.0)
        if res.status_code == 200:
            version = res.text.strip().strip('"')
            cameras = []
            try:
                cfg_res = requests.get(f"{url}/api/config", timeout=5.0)
                if cfg_res.status_code == 200:
                    c_data = cfg_res.json()
                    cameras = list(c_data.get("cameras", {}).keys())
            except Exception:
                pass
            cam_str = ", ".join(cameras) if cameras else "nenhuma câmera listada"
            return jsonify({
                "status": "ok",
                "message": f"Conectado ao Frigate v{version}! Câmeras: {cam_str}"
            })
        else:
            return jsonify({"status": "error", "message": f"HTTP {res.status_code}: {res.text}"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route("/api/telegram", methods=["GET", "POST"])
def telegram_config_endpoint():
    if request.method == "GET":
        return jsonify(get_telegram_status())
    else:
        data = request.get_json(force=True, silent=True) or {}
        token = data.get("token", "").strip()
        allowed_chat_id = data.get("allowed_chat_id", "").strip()
        enabled = bool(data.get("enabled", False))

        curr_config = load_telegram_config()
        if token:
            curr_config["token"] = token
        curr_config["allowed_chat_id"] = allowed_chat_id
        curr_config["enabled"] = enabled

        save_telegram_config(curr_config)

        if enabled:
            success = start_telegram_bot(db, cfg, dm, whisper_model, whisper_lock)
            if success:
                return jsonify({"status": "ok", "message": "Bot do Telegram iniciado com sucesso!"})
            else:
                return jsonify({"status": "error", "message": "Falha ao iniciar o Bot. Verifique o Token."}), 400
        else:
            stop_telegram_bot()
            return jsonify({"status": "ok", "message": "Bot do Telegram desativado."})


@app.route("/api/chat", methods=["POST"])
def chat_endpoint():
    data = request.get_json(force=True, silent=True) or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "Empty text"}), 400

    with chat_lock:
        try:
            logger.info("Processing web chat request")
            reply = run_reply_engine(
                db=db,
                cfg=cfg,
                tts=None,
                text=text,
                dialogue_memory=dm,
                language="pt",
                quiet=True
            )
            return jsonify({"status": "ok", "reply": reply or "..."})
        except Exception as e:
            logger.error(f"Error in reply engine: {e}", exc_info=True)
            return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/voice", methods=["POST"])
def voice_endpoint():
    if "audio" not in request.files:
        return jsonify({"error": "No audio file provided"}), 400

    audio_file = request.files["audio"]
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
        audio_file.save(tmp.name)
        tmp_path = tmp.name

    user_text = ""
    try:
        if whisper_model is not None:
            with whisper_lock:
                segments, info = whisper_model.transcribe(tmp_path, language="pt", beam_size=5)
                user_text = " ".join([s.text for s in segments]).strip()
        else:
            user_text = "[Transcrição de voz não configurada no servidor]"

        if not user_text:
            return jsonify({"error": "No speech detected"}), 400

        with chat_lock:
            reply = run_reply_engine(
                db=db,
                cfg=cfg,
                tts=None,
                text=user_text,
                dialogue_memory=dm,
                language="pt",
                quiet=True
            )

        return jsonify({
            "status": "ok",
            "user_text": user_text,
            "reply": reply or "..."
        })
    except Exception as e:
        logger.error(f"Error processing voice: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


@app.route("/api/status", methods=["GET"])
def status_endpoint():
    return jsonify({
        "status": "online",
        "provider": cfg.llm_provider if cfg else "unknown",
        "model": cfg.llm_chat_model if cfg else "unknown",
        "base_url": cfg.llm_base_url if cfg else "unknown"
    })


@app.route("/api/reset", methods=["POST"])
def reset_endpoint():
    if dm is not None:
        dm.clear()
    return jsonify({"status": "ok", "message": "Session reset"})


if __name__ == "__main__":
    host, published_addresses, username, password = _web_auth_settings()
    if any(not _is_loopback_address(address) for address in published_addresses) and not (username and password):
        raise RuntimeError(
            "Publishing the Jarvis web interface beyond localhost requires "
            "JARVIS_WEB_USERNAME and JARVIS_WEB_PASSWORD."
        )
    if bool(username) != bool(password):
        raise RuntimeError("Set both JARVIS_WEB_USERNAME and JARVIS_WEB_PASSWORD, or leave both empty.")
    port = int(os.environ.get("PORT", 5050))
    init_jarvis()
    logger.info(f"Starting Jarvis Web Server on port {port}...")
    app.run(host=host, port=port, debug=False)
