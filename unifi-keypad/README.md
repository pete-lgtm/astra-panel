# Astra touchscreen panel

This directory contains the panel's Python backend, web interface, tests and
Raspberry Pi deployment tooling. The application uses Python 3.9+ and the
standard library; there is no npm build step.

## Start on a development computer

From this directory:

```sh
python3 server.py
```

Open **http://127.0.0.1:8765/**. A clean checkout shows initial setup. No account,
API key or home configuration is included. State is stored in this directory by
default, in files excluded by the repository's `.gitignore`.

For a separate development state directory, set `KEYPAD_STATE_DIR` to an
absolute path you own. `KEYPAD_PORT` changes the loopback port. Environment
variables must be exported or supplied by a service; `.env` files are not
automatically loaded.

## Connect UniFi

1. Configure trust for the console's HTTPS certificate **before** entering
   credentials. Set `UNIFI_CA_FILE` to a trusted CA file, or
   `UNIFI_CERT_SHA256` to the exact certificate fingerprint verified through
   an independent trusted channel. Restart the server after setting these.
   Certificate validation must not be disabled; a changed fingerprint needs
   investigation and an explicitly verified replacement.
2. Use onboarding to enter the console's private IPv4 address and Protect
   integration API key, then sign in to the local UniFi console. Complete MFA
   if requested. These two connections serve different API capabilities.
3. Choose already-adopted door sensors, an alarm profile, phone-alert
   recipients and optional supported sirens. Create a personal keypad PIN.
4. Review the proposed Protect changes before saving. Complete physical
   sensor checks and the protection-enablement workflow while disarmed.

Use a PC or Mac to obtain the API key. A separate computer can complete setup
through [temporarily paired HTTPS](deploy/WEB-SETUP.md) once the panel's setup
certificate and hostname are configured. The main server remains on loopback.

Only one controller should manage a site. Use a separate bench console when
developing changes while an installed panel is running.

## Stored state and access

The API key and console-issued session are stored separately in owner-only
files. Session renewal is attempted automatically; logout or explicit renewal
rejection requires sign-in again. Passwords and MFA codes are not saved.
PIN verifiers, users and activity are stored in a private SQLite database.
This is file-permission protection, not at-rest encryption.

Installer setup saves stable sensor/profile identities in private `site.json`.
The Pi installer puts all state in `/var/lib/loxicom/keypad`, outside application
releases. Do not copy state between homes or include it in a public release.

Ordinary unlock opens controls without changing alarm state. Administrator PIN
grants protect configuration and equipment controls; Wi-Fi password viewing
requires another fresh PIN check. Automatic locking can be configured in settings.

## Alarm behavior and limits

The current implementation supports one-minute entry and exit delays. Protect
supplies the native exit countdown. The local controller delays the configured
phone alert and optional siren after an entry event; Protect may record a native
breach immediately. Confirmed disarm cancels the pending local alert.

The controller journals entry deadlines and delivery intent. Restart recovery
re-checks Protect, saved sensor checks and managed rules before resuming. Unknown
or ambiguous delivery is not automatically repeated. The panel cannot count
down or deliver an alert while it has no power; verify outage/restart behavior
on your own installation. There is no monitoring or dispatch service.

Some UniFi integrations use compatibility endpoints and may change between
console versions. Sonos discovery requires reachable players on the local
network; playback metadata, artwork and controls depend on the source/device.

## Hardware, releases and tests

- [Raspberry Pi installation and updates](deploy/README.md)
- [Computer-assisted setup](deploy/WEB-SETUP.md)
- [Test and contribution instructions](../CONTRIBUTING.md)
- [Experimental fleet portal](../astra-portal/README.md)

`setup_lab.py` and `rule_setup.py` are older compatibility experiments retained
as source; normal commissioning uses `server.py` onboarding. `/lab` is a separate
simulated entry-delay page and does not represent live alarm state.
