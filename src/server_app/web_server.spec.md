# Headless Web Server Specification

## Network exposure

- Direct execution listens on loopback by default.
- Docker may listen on all container interfaces so the published host port can reach it. The host port binds to loopback by default and may be restricted to a specific interface such as Tailscale.
- Publishing the host port to a non-loopback address requires both `JARVIS_WEB_USERNAME` and `JARVIS_WEB_PASSWORD`; startup fails closed when either is missing.
- `JARVIS_WEB_HOST` controls the Flask listener. `JARVIS_WEB_BIND_ADDRESSES` lists every host-side published address so startup can enforce the credential requirement for all interfaces.
- HTTP Basic authentication protects every page and API route when credentials are configured. Credentials are compared without timing-sensitive string equality.
- State-changing browser requests with an `Origin` or `Referer` from another host are rejected.
- The service does not provide TLS. Non-loopback HTTP is only suitable for a trusted private network; use an HTTPS reverse proxy for untrusted networks.
- Structured application logs are written to `JARVIS_LOG_DIR/server.log`, rotated at 10 MB with five backups. Docker's own captured output must also have a bounded size and retention.
- User chat text and Telegram message/transcription text must not be written to logs.

## Secret handling

- The Home Assistant alarm PIN has no built-in default.
- Read APIs may report whether a secret exists, but never return its value.
- The settings page accepts a new PIN, preserves the current PIN when the field is blank, and offers an explicit clear action.
- Credentials and PINs must not be written to logs or returned in API response bodies.
