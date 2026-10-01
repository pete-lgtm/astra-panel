# Raspberry Pi installation and signed releases

The installer targets **Raspberry Pi 5 + Touch Display 2** with Raspberry Pi OS
and an arm64 or armhf userland on the Pi's 64-bit kernel. Use a spare bench unit
for first installation. The application remains a prototype requiring physical
touch, sensor, alert, power-loss and restart checks at each site.

## Preview the startup screen

From `unifi-keypad/`:

```sh
KEYPAD_RECOVERY_PORT=18764 python3 deploy/kiosk/recovery.py
```

Open **http://127.0.0.1:18764/preview**. This labeled animation preview does not
contact an alarm controller. The deployed recovery screen normally runs on
loopback port 8764 and hands off to the panel on port 8765. Its breathing aurora
intro lasts at least 15 seconds on an ordinary start; an active alarm skips the
decorative wait. Reconnection does not repeat that delay.

## Build a signed application package

The release builder copies an explicit list of runtime files. It excludes
credentials, PIN databases, site configuration, logs and signing keys.

Create your own signing key **outside the checkout**. Never reuse another
installation's private key or publish your key with a release. For example:

```sh
umask 077
mkdir -p "$HOME/.local/share/astra-signing"
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 \
  -out "$HOME/.local/share/astra-signing/release-private.pem"
openssl pkey -in "$HOME/.local/share/astra-signing/release-private.pem" \
  -pubout -out "$HOME/.local/share/astra-signing/release-public.pem"
```

Use a new directory if you already have keys; the commands above are for initial
key creation, not rotation. Keep a protected backup. Then, from `unifi-keypad/`:

```sh
python3 deploy/release.py build --output dist/0.2.53-signed \
  --signing-key "$HOME/.local/share/astra-signing/release-private.pem"
python3 deploy/release.py verify --package dist/0.2.53-signed \
  --public-key "$HOME/.local/share/astra-signing/release-public.pem"
```

The output is `app.zip`, `manifest.json` and `manifest.sig`. Omitting
`--signing-key` creates an unsigned development bundle that the installer refuses.
The verifier rejects altered, incompatible and unexpected files. Provision the
trusted public key through an independently trusted channel.

Use a new numeric `VERSION` for each release. This application-level verifier
does not implement whole-OS rollback or a complete fleet key-revocation system.

## Install a new Pi

1. Install Raspberry Pi OS, finish its initial account/network setup and enable
   SSH. Connect the display and provide suitable power, storage and cooling.
2. Copy this `deploy/` directory, your signed package and your trusted **public**
   key to the Pi. The private signing key stays on your signing computer.
3. On the Pi, review and run:

   ```sh
   sudo bash deploy/pi/install.sh /path/to/signed-package /path/to/release-public.pem
   ```

   An existing Raspberry Pi Desktop/LightDM installation requires
   `--replace-desktop` before those two paths. Other desktop managers are not
   supported by this installer. It retains the current desktop until reboot.
4. Configure a trusted CA with `UNIFI_CA_FILE`, or an independently verified
   console certificate fingerprint with `UNIFI_CERT_SHA256`, in
   `/etc/loxicom/keypad.env`. Restart `loxicom-keypad.service` to load changes.
5. Reboot, complete onboarding, create the administrator PIN, review the Protect
   rule changes and physically check each selected sensor before enabling
   protection. Use a keyboard for fields without an on-screen text keyboard,
   or configure [computer-assisted setup](WEB-SETUP.md).

The installer configures Chromium under Cage/greetd, a hidden cursor and a
landscape display. Application files live in `/opt/loxicom/releases/<version>`;
`/opt/loxicom/current` selects the active version. Private state lives separately
in `/var/lib/loxicom/keypad`. These legacy paths are intentionally retained.

For a Desktop installation, restore the previous display manager over SSH with:

```sh
sudo /usr/local/lib/loxicom/restore-desktop.sh
```

## Display and touch alignment

The intended output is **1280 × 720** landscape. Chromium uses **175% page zoom**,
giving approximately **731 × 411 CSS pixels**. The default Wayland output is
`DSI-1` with a 90-degree transform; the included Goodix udev rule applies the
matching libinput calibration matrix `0 -1 1 1 0 0`.

`KEYPAD_DISPLAY_OUTPUT`, `KEYPAD_DISPLAY_TRANSFORM` and `KEYPAD_DISPLAY_SCALE`
can be set in the **kiosk session environment**, which is separate from the
alarm service environment. A different rotation requires a matching touch
matrix. Verify physical taps in all corners and an upward unlock swipe after
changing display hardware or orientation.

## Stage an update

Staging verifies and copies a release without switching the running service:

```sh
sudo python3 /usr/local/lib/loxicom/release.py stage \
  --package /path/to/signed-package --public-key /etc/loxicom/release-public.pem
```

On an **unconfigured bench device only**, activation is available:

```sh
sudo python3 /usr/local/lib/loxicom/release.py activate-bench --version 0.2.53
```

Automatic activation refuses configured sites. Do not erase state or bypass
that guard. An installed alarm needs an attended, disarmed maintenance process
that verifies fresh Protect state, preserves private data, checks recovery and
allows rollback. A generic live-site updater is not provided in this snapshot.
Never run a second live controller as part of an update.

The [experimental fleet portal](../../astra-portal/README.md) reports health;
it does not distribute or activate panel updates.
