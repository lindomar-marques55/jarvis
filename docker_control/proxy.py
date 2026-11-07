"""Private allowlisted status/start/stop API in front of the Docker Engine socket."""

import hmac
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import docker
from docker.errors import DockerException, NotFound


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("jarvis.docker_control")

TOKEN = os.environ.get("JARVIS_DOCKER_CONTROL_TOKEN", "")
if not TOKEN:
    raise SystemExit("JARVIS_DOCKER_CONTROL_TOKEN is required")

STOP_ORDER = {
    "immich": ("immich-server", "immich-machine-learning", "immich-redis", "immich-postgres"),
    "crafty": ("crafty-container",),
    "jellyfin": ("jellyfin",),
}
START_ORDER = {
    "immich": ("immich-postgres", "immich-redis", "immich-machine-learning", "immich-server"),
    "crafty": ("crafty-container",),
    "jellyfin": ("jellyfin",),
}

client = docker.from_env(timeout=25)


class Handler(BaseHTTPRequestHandler):
    server_version = "JarvisDockerControl/1"
    sys_version = ""

    def _respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path != "/health":
            self._respond(404, {"error": "not found"})
            return
        try:
            available = bool(client.ping())
            self._respond(200 if available else 503, {"status": "ok" if available else "unavailable"})
        except DockerException:
            self._respond(503, {"status": "unavailable"})

    def do_POST(self) -> None:
        if self.path != "/api/control":
            self._respond(404, {"error": "not found"})
            return

        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {TOKEN}"
        if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
            self._respond(401, {"error": "unauthorized"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._respond(400, {"error": "invalid content length"})
            return
        if length < 1 or length > 1024:
            self._respond(413, {"error": "request body must be between 1 and 1024 bytes"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._respond(400, {"error": "invalid JSON"})
            return

        if not isinstance(payload, dict) or set(payload) != {"action", "target"}:
            self._respond(400, {"error": "request must contain only an action and allowlisted target"})
            return
        action = payload.get("action")
        target = payload.get("target")
        if action not in ("start", "stop", "status"):
            self._respond(400, {"error": "action must be status, start, or stop"})
            return
        if not isinstance(target, str):
            self._respond(400, {"error": "target is not allowlisted"})
            return
        names = START_ORDER.get(target) if action == "start" else STOP_ORDER.get(target)
        if not names:
            self._respond(400, {"error": "target is not allowlisted"})
            return

        try:
            containers = [client.containers.get(name) for name in names]
        except NotFound:
            logger.warning("Target %s is missing a configured container; no container was changed", target)
            self._respond(404, {"error": "target is incomplete; no containers were changed"})
            return
        except DockerException as exc:
            logger.error("Docker lookup failed for %s %s (%s)", action, target, type(exc).__name__)
            self._respond(502, {"error": "Docker lookup failed"})
            return

        changed = []
        unchanged = []
        if action == "status":
            statuses = []
            try:
                for container in containers:
                    container.reload()
                    statuses.append({"name": container.name, "status": container.status})
            except DockerException as exc:
                logger.error("Docker status lookup failed for %s (%s)", target, type(exc).__name__)
                self._respond(502, {"error": "Docker status lookup failed"})
                return
            logger.info("Completed action=status target=%s containers=%s", target, statuses)
            self._respond(200, {"action": "status", "target": target, "containers": statuses})
            return

        changed_key = "started" if action == "start" else "stopped"
        unchanged_key = "already_running" if action == "start" else "already_stopped"
        try:
            for container in containers:
                container.reload()
                active = container.status in ("running", "paused", "restarting")
                if action == "start" and not active:
                    container.start()
                    changed.append(container.name)
                elif action == "stop" and active:
                    container.stop(timeout=15)
                    changed.append(container.name)
                else:
                    unchanged.append(container.name)
        except DockerException as exc:
            logger.error("Docker %s failed for %s (%s)", action, target, type(exc).__name__)
            result = {"error": f"Docker {action} failed", "target": target, changed_key: changed, unchanged_key: unchanged}
            self._respond(
                502,
                result,
            )
            return

        logger.info("Completed action=%s target=%s %s=%s %s=%s", action, target, changed_key, changed, unchanged_key, unchanged)
        self._respond(200, {"action": action, "target": target, changed_key: changed, unchanged_key: unchanged})

    def log_message(self, format_string: str, *args) -> None:
        logger.info("%s %s", self.client_address[0], format_string % args)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8765), Handler)
    server.serve_forever()
