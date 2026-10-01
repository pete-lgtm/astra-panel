# Validation — September 30, 2026

## Passed locally

- **25 automated tests**, Python 3.12.14, Flask 3.1.3: signed telemetry,
  persistence across app recreation, replay/timestamp/order rejection,
  per-device revocation, sender rate limiting, payload size/type/privacy limits,
  missing measurements, correct pending/offline distinction, private file modes,
  explicit operator authorization, no command/alarm APIs, private enrollment,
  HTTPS-only agent configuration, redirect refusal and response-body isolation.
- Python compilation and JavaScript syntax checks.
- **Real-process smoke check** using Gunicorn 26.2.0 with two workers and Caddy
  2.11.4 on temporary loopback ports. Verified public browser requests go to the
  auth path, client-supplied identity headers are removed, valid device telemetry
  reaches the private backend, replay is rejected and inventory excludes secrets.
  Authentication used a deliberately denying stub, not a real OIDC provider.
- Docker Compose **5.5.1** parsed the deployment scaffold with synthetic inputs.
  Verified only Caddy publishes ports, the backend network is internal and the
  app is hardcoded to production mode. Caddy's own `validate` command accepted
  the configuration. Validation did not issue a certificate or change DNS.
- Production preflight refuses to proceed while operator/identity/secrets are
  unconfigured, which is the expected current state.
- Browser checks: fleet load, search, attention filter, panel health drawer,
  version counts, remote-support availability and chronological activity.
  No warning/error console entries were observed. Tested the normal desktop
  viewport and 390 × 844 responsive viewport; fixed page-wide horizontal overflow
  and a narrow-screen drawer offset. Only the inventory table scrolls horizontally
  on small screens. The temporary viewport override was reset.

Caddy and Compose validation binaries were downloaded from their official GitHub
releases and checked against the release API's published SHA-256 digests before
execution. They remain temporary tools outside the distributable project.

## Not yet validated

- Building/running the complete Docker stack on Linux (this Mac has no Docker
  daemon), real OIDC sign-in/MFA and operator denial through that provider.
- Public DNS, HTTPS issuance/automatic renewal and external firewall behavior.
- Encrypted backup/restore, container restart/reboot recovery and sustained load.
- Installing or benchmarking the health agent/systemd sandbox on a bench Pi.
- Real customer enrollment, private LAN routing, support consent/expiry,
  cross-site denial, remote desktop, Sonos discovery or camera viewing.
- Unattended signed update activation, rollback and interrupted-update recovery.

No tests touched the live panel, customer Protect state, credentials, alarm
controls, siren, router configuration or DNS. No production fleet monitoring or
remote access has been enabled.
