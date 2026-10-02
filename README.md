# Astra Panel

An open-source touchscreen hub for your home: UniFi Protect alarms, network and power equipment, Sonos audio, and Philips Hue lighting in a glass-style interface.

Astra runs locally on a Raspberry Pi. Its landscape interface includes a breathing aurora lock screen, swipe-to-unlock, a PIN keypad, and compact controls designed for touch.

**Status:** working prototype, version **0.2.55**. Hardware and UniFi compatibility need to be checked for each installation. This is not a certified alarm system or a monitored emergency service.

## What it does

- **Alarm:** Arm Stay, Arm Away and PIN-protected Disarm, entry/exit countdowns, sensor checks, bypass reminders, and optional Protect siren output.
- **Systems:** discover supported UniFi Network equipment, rename devices, restart supported equipment, inspect PoE ports and outlets, view Wi-Fi credentials after a fresh PIN check, and run a gateway internet speed test.
- **Audio:** discover Sonos rooms, show music metadata and available album artwork, control playback and volume, and link or unlink rooms.
- **Lighting:** connect a Philips Hue Bridge locally, switch individual lights or rooms, adjust brightness, and activate saved Hue scenes. Lutron is the only other planned lighting provider; it is not implemented yet.
- **Setup:** configure the panel on its touchscreen or through a temporarily paired computer browser on the same network.
- **Appliance:** a Raspberry Pi kiosk installer, startup/reconnection screen, persistent private configuration, and signed application packages.

The separate **experimental fleet portal** provides health reporting and a dashboard preview. Remote troubleshooting sessions, cloud update delivery, and remote alarm control are not implemented. Camera discovery exists in the panel; camera streaming remains future work.

## Repository layout

| Directory | Contents |
| --- | --- |
| [`unifi-keypad/`](unifi-keypad/) | Panel backend, touchscreen interface, tests and Raspberry Pi installer |
| [`astra-portal/`](astra-portal/) | Experimental fleet health dashboard, reporting agent and cloud deployment scaffold |

The `unifi-keypad` directory and `loxicom` service names are retained for compatibility with existing installations.

## Try it locally

The panel requires **Python 3.9+**, with no third-party Python packages. Node.js and OpenSSL are used for development tests and release tooling.

```sh
git clone https://github.com/pete-lgtm/astra-panel.git
cd astra-panel/unifi-keypad
python3 server.py
```

Open **http://127.0.0.1:8765/** on that computer. A clean checkout opens onboarding and has no saved credentials, sensors or PIN. For a standalone animation preview, see the [Raspberry Pi guide](unifi-keypad/deploy/README.md#preview-the-startup-screen).

To connect real equipment, first configure certificate trust and complete the [panel setup guide](unifi-keypad/README.md). Run only one alarm controller per site. The main panel server deliberately listens on loopback; use the [paired HTTPS setup flow](unifi-keypad/deploy/WEB-SETUP.md) for a separate computer.

## Install on a Raspberry Pi

The current installer targets a **Raspberry Pi 5 with Touch Display 2**, running Raspberry Pi OS. It configures Chromium, Cage and greetd for a landscape kiosk. It is an installer, not a flashable SD-card image.

Follow the [installation and signed-update guide](unifi-keypad/deploy/README.md). Build your own release using your own signing key. Software updates do not include site data, credentials or PINs. Automatic update activation is restricted to unconfigured bench devices.

## Develop and contribute

See [CONTRIBUTING.md](CONTRIBUTING.md) for test commands and contribution guidance. The tests use simulated devices; running them does not control your home.

Please keep installation data out of issues, commits and screenshots: API keys, UniFi cookies, PIN databases, Wi-Fi passwords, enrollment credentials and signing keys are private. See [SECURITY.md](SECURITY.md) to report a vulnerability privately.

## License

[MIT](LICENSE) © 2026 Pete Jones. Third-party notices are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Astra is an independent project and is not affiliated with or endorsed by Ubiquiti, Sonos, Signify/Philips Hue, Lutron or Raspberry Pi.
