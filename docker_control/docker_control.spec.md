# Docker Control Proxy Specification

- The proxy is the only Jarvis-related container that mounts `/var/run/docker.sock`.
- It is reachable only on a Compose `internal` network shared with Jarvis and publishes no host port.
- It requires a shared bearer token and enforces a fixed target-to-container allowlist independently of Jarvis tool schemas.
- Its only application route is `POST /api/control`, accepting only `status`, `start`, or `stop`; `status` is read-only, and it has no restart, remove, exec, or arbitrary Docker API route.
- All containers for a requested target are resolved before changing any member.
- Immich starts Postgres, Redis, machine-learning, then the server; it stops the server, machine-learning, Redis, then Postgres.
- Requests have a bounded JSON body; responses and logs never contain the bearer token or raw user conversation.
