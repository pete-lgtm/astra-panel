# Astra fleet workspace

An experimental, self-hosted health dashboard and reporting agent for Astra panels.
Use your own cloud server, hostname and identity provider; no hosted service is included.

This is a local health portal and cloud deployment scaffold. The preview uses
clearly labeled sample sites. Remote access and update delivery remain planned.

## Built in this preparation

- Searchable fleet inventory: online, offline, awaiting first check-in, revoked.
- Panel details: software version, service status, temperature, memory, root
  filesystem usage, uptime and load. Offline readings are labeled as historical.
- An actual SQLite-backed HTTPS ingestion endpoint, with a different identity
  and key per panel, HMAC authentication, timestamp/nonce replay rejection,
  revocation, bounded payloads and an explicit list of accepted measurements.
- Server-local enrollment and revocation. Keys are written to private files,
  not printed or returned to the browser. Unknown devices cannot self-enroll.
- A separate low-priority Linux health agent and systemd unit, not installed on
  the live Pi. The agent never reads the panel's private state or consumes commands.
- A production scaffold using Caddy HTTPS, OAuth2 Proxy, an explicit operator
  allowlist, Gunicorn, private container networking and persistent data volumes.
- Software inventory and a **design preview** of support and update workflows.

## Not built or enabled yet

Remote LAN sessions, customer support consent/revocation UI, session expiry and
auditing, automatic update download/activation/rollback, fleet alert delivery,
and real cloud identity-provider integration remain implementation milestones.
There are **no remote arm, disarm, reboot, shell, proxy or update APIs** in this
service. Health means OS/service measurements, not proof that a home's alarm is
armed or correctly commissioned.

The `fleet_heartbeat.py` inside `../unifi-keypad` is an older, disabled prototype
protocol. Do not turn it on against this server. This preparation uses the
separate `agent/health_agent.py` reporter with a durable per-panel enrollment,
and omits alarm state entirely. Do not enable the older protocol for this portal.

## Open the preview

```sh
cd astra-portal
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python preview.py
```

Open **http://127.0.0.1:18790/** on the computer running the preview. Try **Needs attention**, search for a
site, or choose **View** for its health details. Software and Remote support
explain their current availability. Fixtures live only in a temporary database;
this loopback preview cannot accept a real panel's heartbeat. Production does
not seed demo sites and will show an empty inventory until a panel is enrolled.

Python 3.10+ is required by the pinned production server dependencies; the
original prototype was tested with Python 3.12.14. The health agent itself uses only the
standard library and works independently of Flask/Gunicorn.

## Cloud and support preparation

- [New cloud server runbook](deploy/README.md): sizing, DNS, operator sign-in,
  backups, installation and pilot enrollment.
- [Remote support design](REMOTE-SUPPORT.md): access to approved LAN devices,
  site separation, local support workspace and alarm permission boundary.
- [Validation record](VALIDATION.md): what was tested and what still requires
  the cloud account, identity provider and a bench panel.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
node --check static/portal.js
```

No test reaches the live Pi or UniFi, arms/disarms, runs a siren, or reboots equipment.

## Health protocol

`POST /api/v1/heartbeat`, Content-Type `application/json`, maximum 4096 bytes.
Required headers: `X-Astra-Panel`, `X-Astra-Time` (Unix seconds), `X-Astra-Nonce`
(16 random bytes as lowercase hex), `X-Astra-Signature` (lowercase SHA-256 hex).
Signature input is exactly:

```text
astra-health-v1\n{panel_id}\n{timestamp}\n{nonce}\n{raw_request_body}
```

HMAC key is the 32 bytes decoded from the enrollment secret's hex value. The
receiver accepts at most 120 seconds of clock skew, remembers accepted nonces
for ten minutes, rejects older device timestamps and limits accepted writes to
one per 15 seconds per device. The normal reporting interval is 60–70 seconds,
with bounded retries up to roughly five minutes. After 180 seconds without an
accepted check-in, the inventory marks the panel offline. Last-seen time comes
from the server, not the device. Both machines need working time synchronization.

The response is HTTP 204 with no commands. The agent discards any response body,
refuses redirects and plaintext HTTP, and validates HTTPS with the OS trust store.
Current storage retains only each panel's most recent health snapshot. Enrollment,
first contact and revocation are audited; the management CLI can prune audit
history older than 90 days. This is not a tamper-proof support session audit log.

Per-device secrets are stored in the owner-only fleet database and a credential
file on that panel. Server administrators therefore have access to these keys.
Use encrypted off-server backups and a provider-encrypted volume. Cloud storage
must never contain the release signing key or customer UniFi/PIN state.

## Project layout

`app.py`, `store.py`, `protocol.py`: health backend. `manage.py`: server-local
device lifecycle. `static/`: dashboard. `preview.py`: isolated sample mode.
`agent/`: optional panel reporter. `deploy/`: dedicated cloud server scaffold.
`tests/`: authentication, privacy, persistence and protocol coverage.
