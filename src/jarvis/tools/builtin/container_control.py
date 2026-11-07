"""Start, stop, or query a small allowlist of local Docker applications through a constrained proxy."""

import os
import re
from typing import Any, Dict, Optional

import requests

from ..base import Tool, ToolContext
from ..types import ToolExecutionResult
from ...debug import debug_log


_ALLOWED_TARGETS = {"immich", "crafty", "jellyfin"}

_STATUS_RE = re.compile(
    r"\b(?:status|estado|aberto|aberta|abriu|ligado|ligada|rodando|"
    r"executando|funcionando|online|dispon[ií]vel|ativo|ativa|"
    r"parado|parada|desligado|desligada|"
    r"running|open|available|active|started|stopped|up|down|offline)\b",
    re.IGNORECASE,
)
_START_RE = re.compile(
    r"\b(?:abra|abre|abrir|inicie|inicia|iniciar|liga|ligue|ligar|"
    r"open|start|launch|turn on)\b",
    re.IGNORECASE,
)
_STOP_RE = re.compile(
    r"\b(?:pare|parar|desliga|desligue|desligar|fecha|feche|fechar|"
    r"stop|close|shut down|turn off)\b",
    re.IGNORECASE,
)
_NEGATION_RE = re.compile(r"\b(?:n[aã]o|not|never|don't|do not|no)\b", re.IGNORECASE)
_META_RE = re.compile(
    r"\b(?:como|how|why|por que|o que|what|explain|explique|"
    r"queria saber|quero saber|gostaria de saber|should|can i|i can|we can|"
    r"posso|devo|deveria|que tal|vale a pena|maybe|perhaps|talvez|"
    r"what if|e se|mais tarde|depois|tomorrow|later|i think|we think|"
    r"eu acho|acho que|do you want)\b",
    re.IGNORECASE,
)
_PAST_REPORT_RE = re.compile(
    r"\b(?:mandei|pedi|perguntei|disse|falei|tentei|asked|told|said|tried|requested)\b",
    re.IGNORECASE,
)
_CONDITIONAL_RE = re.compile(r"\b(?:if|caso|se)\b", re.IGNORECASE)


def infer_container_control_request(text: str) -> Optional[Dict[str, str]]:
    """Recognise one unambiguous direct request for a configured app.

    Read-only status questions are safe to route directly. Start and stop are
    routed only when their verb precedes the named target; negated,
    hypothetical, retrospective and
    explanatory mentions remain on the normal conversation path.
    """
    normalized = " ".join(str(text or "").casefold().split())
    target_matches = [
        (match, target)
        for target in _ALLOWED_TARGETS
        if (match := re.search(rf"(?<!\w){re.escape(target)}(?!\w)", normalized))
    ]
    if len(target_matches) != 1:
        return None
    target_match, target = target_matches[0]

    start_match = _START_RE.search(normalized)
    stop_match = _STOP_RE.search(normalized)
    blocked_action = bool(
        _NEGATION_RE.search(normalized)
        or _META_RE.search(normalized)
        or _PAST_REPORT_RE.search(normalized)
        or _CONDITIONAL_RE.search(normalized)
    )

    def is_direct_action(match: Optional[re.Match[str]]) -> bool:
        if match is None or blocked_action:
            return False
        return match.start() < target_match.start()

    start_requested = is_direct_action(start_match)
    stop_requested = is_direct_action(stop_match)
    if start_requested and stop_requested:
        return None
    if start_requested:
        return {"action": "start", "target": target}
    if stop_requested:
        return {"action": "stop", "target": target}
    if _STATUS_RE.search(normalized):
        return {"action": "status", "target": target}
    return None


class ContainerControlTool(Tool):
    """Start, stop, or query one preconfigured local application."""

    @property
    def name(self) -> str:
        return "containerControl"

    @property
    def description(self) -> str:
        return (
            "Start, stop, or check the current status of one configured local Docker application. For questions "
            "about whether an application is running, open, online, or available, use action=status and report "
            "the returned state; never guess from memory or a previous answer. A clear request to open, launch, turn on, "
            "or bring an application online means action=start (including Portuguese 'abrir', 'ligar' or 'iniciar'); "
            "a clear request to stop, close or shut it down means action=stop (including 'parar' or 'desligar'). "
            "Use only for a direct request about Immich, Crafty, or Jellyfin; mentioning an application, asking "
            "whether you can control it, or discussing a plan is not a request to change it. Starting Immich starts "
            "Postgres, Redis, machine-learning, then the server. Stopping Immich stops the server, machine-learning, "
            "Redis, then Postgres. After starting Immich, provide its returned access links; this server-side tool "
            "cannot open a browser on the user's device. This tool cannot restart, remove, or control any other container."
        )

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["start", "stop", "status"],
                    "description": "status for questions about current state; start for explicit requests to start/open/launch; stop for explicit requests to stop/close.",
                },
                "target": {
                    "type": "string",
                    "enum": ["immich", "crafty", "jellyfin"],
                    "description": "The configured application the user asked about.",
                }
            },
            "required": ["action", "target"],
            "additionalProperties": False,
        }

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        args = args or {}
        action = args.get("action")
        target = args.get("target")
        if action not in ("start", "stop", "status") or not isinstance(target, str) or target not in _ALLOWED_TARGETS:
            return ToolExecutionResult(
                success=False,
                reply_text="No container was changed. Choose status, start, or stop and one configured target: Immich, Crafty, or Jellyfin.",
            )

        base_url = os.environ.get("JARVIS_DOCKER_CONTROL_URL", "").strip().rstrip("/")
        token = os.environ.get("JARVIS_DOCKER_CONTROL_TOKEN", "")
        if not base_url or not token:
            return ToolExecutionResult(
                success=False,
                reply_text="Container control is not configured; no container was changed.",
            )

        debug_log(f"Requested {action} for allowlisted container target '{target}'", "docker")
        action_label = {"start": "inicialização", "stop": "parada", "status": "consulta de estado"}[action]
        context.user_print(f"🐳 Solicitando {action_label} de {target}...")
        try:
            response = requests.post(
                base_url,
                headers={"Authorization": f"Bearer {token}"},
                json={"action": action, "target": target},
                timeout=(3, 30),
            )
        except requests.RequestException as exc:
            debug_log(f"Container control request failed: {type(exc).__name__}", "docker")
            return ToolExecutionResult(
                success=False,
                reply_text="The local container-control service could not be reached; no result was confirmed.",
            )

        try:
            result = response.json()
        except ValueError:
            result = {}
        if action == "status":
            containers = result.get("containers", [])
            valid_containers = (
                isinstance(containers, list)
                and bool(containers)
                and all(
                    isinstance(item, dict)
                    and isinstance(item.get("name"), str)
                    and isinstance(item.get("status"), str)
                    for item in containers
                )
            )
            if response.status_code != 200 or not valid_containers:
                debug_log(f"Container status request returned HTTP {response.status_code} for {target}", "docker")
                return ToolExecutionResult(
                    success=False,
                    reply_text=f"Could not confirm the current status of {target}; no live state was returned.",
                )
            detail = f"target={target}; status=" + ", ".join(
                f"{item['name']}:{item['status']}" for item in containers
            )
            if target == "immich":
                lan_url = os.environ.get("JARVIS_IMMICH_LAN_URL", "").strip()
                tailscale_url = os.environ.get("JARVIS_IMMICH_TAILSCALE_URL", "").strip()
                if lan_url:
                    detail += f"; lan_url={lan_url}"
                if tailscale_url:
                    detail += f"; tailscale_url={tailscale_url}"
            return ToolExecutionResult(success=True, reply_text=detail)

        changed_key = "started" if action == "start" else "stopped"
        unchanged_key = "already_running" if action == "start" else "already_stopped"
        changed = result.get(changed_key, [])
        unchanged = result.get(unchanged_key, [])
        if not isinstance(changed, list) or not isinstance(unchanged, list):
            changed, unchanged = [], []

        if response.status_code != 200:
            if changed or unchanged:
                detail = f"Partial {action} result for {target}: {changed_key}={', '.join(str(name) for name in changed) or 'none'}; {unchanged_key}={', '.join(str(name) for name in unchanged) or 'none'}"
            else:
                detail = f"Container {action} request failed with HTTP {response.status_code}; no success was confirmed."
            debug_log(f"Container control returned HTTP {response.status_code} for {action} {target}", "docker")
            return ToolExecutionResult(success=False, reply_text=detail)

        detail = (
            f"target={target}; {changed_key}={', '.join(str(name) for name in changed) or 'none'}; "
            f"{unchanged_key}={', '.join(str(name) for name in unchanged) or 'none'}"
        )
        if target == "immich" and action == "start":
            lan_url = os.environ.get("JARVIS_IMMICH_LAN_URL", "").strip()
            tailscale_url = os.environ.get("JARVIS_IMMICH_TAILSCALE_URL", "").strip()
            if lan_url:
                detail += f"; lan_url={lan_url}"
            if tailscale_url:
                detail += f"; tailscale_url={tailscale_url}"
        return ToolExecutionResult(success=True, reply_text=detail)
