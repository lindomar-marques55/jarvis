# Container Control Tool Specification

## Scope

- The tool accepts exactly one target: `immich`, `crafty`, or `jellyfin`.
- It performs read-only status queries and start or stop requests only. It must never restart, remove, pause, or inspect arbitrary containers.
- A direct request to open, launch, turn on or bring an application online (including Portuguese `abrir`, `ligar` or `iniciar`) maps to `start`; a direct request to stop, close or shut it down maps to `stop`.
- A question about whether an application is running, open, online or available maps to `status`; report only the returned live state, never infer it.
- A target changes state only when the user clearly asks to start or stop that named application. Mentioning it, asking whether Jarvis can control it, asking its status, or discussing a plan is not authorization to act.
- `immich` means the complete stack: start `immich-postgres`, `immich-redis`, `immich-machine-learning`, then `immich-server`; stop `immich-server` first, then `immich-machine-learning`, `immich-redis`, and `immich-postgres`.
- `crafty` maps only to `crafty-container`; `jellyfin` maps only to `jellyfin`.
- The Jarvis container never receives the Docker socket. It calls a private control service with a shared token.
- Both the tool schema and the private control service enforce the action and target allowlists. Unknown actions or targets fail without changing any container.

The reply engine recognises one unambiguous direct request for a configured
application before planning. It executes a live status query, or a clear direct
start/stop command, through the same allowlist. Negated, conditional,
retrospective, explanatory and multi-target mentions do not execute an action.

## Results

- Report verified container states for status queries, and which containers started or stopped or were already in the requested state.
- A missing member of a target group fails before any member is stopped.
- A Docker API error is reported as a failure; do not claim the target stopped unless the proxy confirms it.
- After an Immich start request, include the configured LAN and Tailscale access links in the reply. The server-side tool cannot open a browser on the user's device.
- Logs may record the target and result but must not record the shared token or raw user conversation.
