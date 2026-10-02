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

## Philips Hue lighting

Unlock the panel and tap the lightbulb in the bottom toolbar. An administrator
PIN can connect a bridge: choose **Connect Hue**, select the bridge discovered
on your LAN (or enter its private IPv4 address), press the physical button on
the Hue Bridge, and tap **Connect** within its pairing window.

Use a Hue Bridge with the v2 API (the square BSB002 bridge or a compatible newer
bridge); the older round BSB001 bridge is not supported. Add and organize lights,
rooms, zones and scenes in the Hue app, then refresh Lighting in Astra. Any valid
panel PIN can switch lights/rooms, adjust supported brightness, and recall saved
scenes. Color selection and scene editing remain in the Hue app.

All control is local HTTPS. The first bridge certificate is trusted when you
select it and pinned for pairing and subsequent requests. Pair on a trusted LAN.
A changed certificate or rejected key requires reconnecting. The application key
and certificate pin live in owner-only `.lighting-hue.json` in the state directory;
they survive panel restarts and are never returned to the browser or packaged
with releases. Disconnect removes Astra's local connection; it does not revoke
the bridge's application authorization. That can be removed in Hue separately.

Failed reads disable stale controls. Writes are never automatically retried;
Astra reads the bridge again after an adjustment settles. Lighting does not need UniFi
sign-in and bridge network operations do not hold the alarm controller lock.

Only Philips Hue is currently implemented. **Lutron is the only other planned
lighting integration**; its connection method will depend on the installed
processor. There is no generic third-party lighting connector.

## Volume and brightness sliders

Sliders show your chosen level immediately and start adjusting the device while
you drag. Movements are combined over 150 milliseconds, with one request at a
time per provider and only the latest queued level for each room or light.
Releasing sends the final level next, without replaying intermediate positions.

On the touchscreen, swipe vertically over a slider to scroll its list. Drag
horizontally to adjust the level, or tap the track to set it when you lift your
finger. Touching a slider does not change a level before the gesture is known.

The chosen value stays steady while fresh device readings catch up. Acceptance
alone is not treated as confirmation: after six seconds of mismatching readings,
Astra shows the device's reported level with a notice. Failed writes discard
queued changes; locking or leaving the screen also cancels unsent changes.

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

The local panel plays short beeps during the displayed exit and entry countdowns,
including while a PIN is being entered. Beeps occur once per second and twice
per second for the final ten seconds. They stop when the countdown ends or
Protect confirms disarm; an accepted request alone does not silence the timer.
Settings → Panel → Test sound plays a short local cue without arming the alarm.
These cues do not use Sonos or replace the configured alarm siren.

A connected audio speaker is required. Raspberry Pi 5 has no built-in speaker;
a USB audio speaker is a simple option. Use a speaker that carries audio over
USB, not a speaker that only takes power from USB and needs a separate audio
jack. The kiosk installer allows autoplay only on its two local panel origins
and delegates that permission to the panel frame, so entry beeps can start after
a reboot before anyone touches the screen. Existing installations also need
the updated kiosk wrapper and Chromium policy; an application-only update is
not sufficient. Verify the chosen audio output and audible level on the device.

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

### Music artwork and lock-screen controls

The Audio page shows large artwork with a shaded title/artist strip. Long text
scrolls only when it overflows; reduced-motion preferences keep it still.
Supported Sonos sources also expose Next alongside playback and room volume.

When music is playing, the local lock screen shows its artwork beside the clock
and Astra aurora. Volume, play/pause and supported track skip are available
without entering a PIN. Swiping up still opens PIN entry. Pausing retains the
card for up to five minutes so music can be resumed; stopped/offline playback
returns to the clock. Exit/entry countdowns and breaches take priority.

These controls use short-lived grants bound to the displayed room and group,
separate from PIN sessions. They cannot arm/disarm, change settings, link rooms,
select media or control lighting. They are served by the existing loopback-only
app with its Host, Origin and request-token checks, and are excluded from the
remote setup allowlist. Sonos writes retain device identity, group and live
capability checks and are never automatically retried.
