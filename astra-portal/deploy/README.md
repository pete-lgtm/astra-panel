# Prepare a new Astra cloud server

Use your own hostname in place of **astra.example.com** throughout these examples.
It must resolve to your cloud server. A panel’s private computer-setup hostname
is separate from the fleet portal.

## Server specification

Start the health-only pilot with a dedicated Linux VM: **2 vCPU, 2 GB RAM and
40 GB SSD**, Ubuntu 24.04 LTS, static public IP, provider firewall and encrypted
off-server backups. This is an initial sizing estimate; measure before promising
fleet capacity. The stack is portable across VM providers and includes no paid
provider-specific service. No provider account, server or spending commitment
was created. Separate video/remote desktop relay capacity from this VM if needed.

Production uses Gunicorn for Flask, following the
[Flask deployment guidance](https://flask.palletsprojects.com/en/stable/deploying/gunicorn/).
The Compose stack has only Caddy's HTTP/HTTPS ports published. The database and
application backend are private. The backend trusts the identity headers set by
OAuth2 Proxy, so **never publish port 8000 or proxy browser requests directly to
it**. Caddy strips incoming identity headers; OAuth2 Proxy authenticates and
sets the operator identity. An explicit email allowlist is checked again by the
backend. No wildcard operator domain is accepted.

## Inputs still needed

1. A cloud provider account with a budget and a server provisioned to this spec.
2. An identity provider supporting OIDC and MFA for the support operators. Register
   callback `https://astra.example.com/oauth2/callback`, with authorization-code
   flow and PKCE. Obtain issuer URL, client ID and secret.
3. Specific operator email addresses and a certificate contact email.
4. A static server IP for the Squarespace `astra` DNS record.

MFA must be enforced by the identity provider. Merely deploying OAuth2 Proxy does
not enable MFA. The proxy's configuration and supported options are documented
by [OAuth2 Proxy](https://oauth2-proxy.github.io/oauth2-proxy/configuration/overview/).

## Install on the new VM

Use SSH keys for server administration and restrict SSH at the provider firewall
to the administrator's source addresses. Allow inbound TCP 80 and 443 for Caddy.
Do not expose app/database/auth container ports. Install Docker Engine and the
Compose plugin using the provider-supported
[official Docker instructions](https://docs.docker.com/engine/install/ubuntu/).
Apply OS security updates and configure time synchronization.

Copy only this `astra-portal` project to `/opt/astra-portal`. Exclude `.venv`, `var`,
`private`, `artifacts`, `__pycache__`, `.env` and any secrets. The Docker build
context itself is allowlisted in `.dockerignore`; it does not include the panel
repository, private state or release signing key.

From `/opt/astra-portal`:

```sh
cp deploy/.env.example deploy/.env
cp deploy/operators.example.txt deploy/operators.txt
```

Edit `deploy/.env` with the real hostname, contact email, OIDC issuer and client
ID. Add exact operator emails to `deploy/operators.txt`. Keep that file readable
by containers, with no secrets in it. Then generate the private cookie key and
enter the OIDC secret through a hidden prompt:

```sh
sudo python3 deploy/prepare_secrets.py
sudo python3 deploy/preflight.py
docker compose --project-directory deploy --env-file deploy/.env -f deploy/compose.yml config --quiet
```

`prepare_secrets.py` creates a root-only parent directory, assigns secret files
to the non-root auth container's UID 65532, and gives them mode 0400. It will not
overwrite existing secrets. The cookie-secret file is 32 raw random bytes, as
required by the selected proxy version. Do not paste secrets into chat, arguments
or a source-controlled `.env`. Back them up encrypted along with the database.

Create a DNS **A** record `astra` with your DNS provider pointing to the VM's public IPv4.
Only add an AAAA record if the VM has verified working IPv6. Do not point this
public service at a private LAN address. Once DNS and the firewall are ready:

```sh
docker compose --project-directory deploy --env-file deploy/.env -f deploy/compose.yml up -d --build
docker compose --project-directory deploy --env-file deploy/.env -f deploy/compose.yml ps
```

Caddy is configured for ordinary public HTTPS issuance and renewal. Persist its
`caddy-data` volume. This does not need Squarespace DNS automation because the
public VM can answer certificate challenges. See
[Caddy automatic HTTPS](https://caddyserver.com/docs/automatic-https).

Image tags are pinned for Caddy 2.11.4 and OAuth2 Proxy 7.15.4; Python dependencies
are frozen in `requirements.lock`. The Python base is the 3.12 Bookworm slim
series. Before production rollout, pull, scan and record the tested container
digests and replace tags with those immutable digests. Keep a tested patch-update
process. This workstation has no Docker daemon; a full container deployment and
real OIDC/TLS flow are not yet verified.

## Commission before enrolling the first panel

- The hostname has a trusted certificate. Anonymous browser/API reads are
  redirected to sign-in or rejected. Forged identity headers cannot bypass it.
- An allowed operator with MFA can see an empty inventory; an unlisted account
  cannot. No frontend control can arm/disarm or submit a software/remote command.
- Port 8000 and the auth container port are unreachable from the public Internet.
- A signed synthetic heartbeat reaches the app only for its enrolled identity;
  a modified/replayed/revoked request fails. Remove the synthetic identity.
- Reboot the cloud VM and confirm data and sign-in survive. Restore an encrypted
  backup to an isolated environment and verify device identities and inventory.
- Add external uptime/certificate checks; bound logs and audit retention. Failed
  check-ins are visible in the portal, but email/SMS/push alert delivery is not
  implemented in this phase.

## Enroll a panel without disclosing its credential

Enrollment is an administrator action on the server, not a public signup API.
Using the same Compose file/environment options as above, run:

```sh
docker compose --project-directory deploy --env-file deploy/.env -f deploy/compose.yml exec portal \
  python manage.py enroll --site 'Pilot site' --name 'Bench panel' \
  --origin https://astra.example.com --output /var/lib/astra/enrollments/pilot.json
```

The CLI reports a non-secret panel ID and writes the key to a private enrollment
file. Transfer that file directly over authenticated SSH/SFTP through a private
staging directory; do not print it with `cat`, commit it, email it, or put it in
the browser. Install it on the bench Pi as root-owned mode 0600 at
`/etc/astra/enrollment.json`. Keep other customers' enrollment files separate.

Copy `protocol.py` and `agent/health_agent.py` to `/opt/astra-health/` preserving
that layout, root-owned and not writable by the service user. Install
`agent/astra-health.service` as `/etc/systemd/system/astra-health.service`.
The systemd `LoadCredential` mechanism supplies a private runtime copy to a
separate dynamic user. That user has no access to keypad state or UniFi credentials.

After reviewing the installed files on the bench Pi:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now astra-health.service
sudo systemctl status astra-health.service
```

This starts only the health reporter. It must not restart, depend on or stop
`loxicom-keypad.service`. Disconnect the bench's WAN and verify local alarm UI
responsiveness while cloud check-ins fail; verify backoff, memory use and service
restart behavior. Do not enable it on the live home panel until these checks pass.

Revoke a device from the server by its ID:

```sh
docker compose --project-directory deploy --env-file deploy/.env -f deploy/compose.yml exec portal \
  python manage.py revoke astra-REPLACE_WITH_PANEL_ID
```

That revokes health ingestion only. Later support/VPN credentials will require
their own linked revocation path; this command must not claim to close a tunnel
that is not managed by this service.

## Backups and maintenance

Use a consistent SQLite backup, not a blind copy during a transaction. Encrypt
the backup before it leaves the VM; fleet credentials and operator records are
private. Back up identity configuration and Caddy data separately. Keep the
application image/config version with the backup. Test restoration and restore
file ownership/modes before starting the app.

Run `python manage.py prune` within the portal container during scheduled server
maintenance to remove audit rows older than 90 days. Container logs rotate at
10 MB × 3 files. The initial health snapshot is overwritten on each check-in;
this version is not a historical metrics database. Fleet credential rotation
uses revoke/re-enroll until a dedicated rotation workflow is implemented.

## Move from health to remote access

Follow [REMOTE-SUPPORT.md](../REMOTE-SUPPORT.md). Do not expose a generic reverse
proxy or shell through this server as a shortcut. The first private route needs
its own account, per-site policy, customer approval/expiry mechanism, limited
equipment credentials and live denial/revocation tests before Connect is enabled.
