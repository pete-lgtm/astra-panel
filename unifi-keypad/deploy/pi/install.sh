#!/bin/bash
# First installation only. Python/web assets work with arm64 and armhf userlands.
set -euo pipefail
[[ $(uname -s) == Linux && $(uname -m) == aarch64 && $EUID == 0 ]] || {
  echo 'Run as root on Raspberry Pi OS on a Pi 5, not on your development computer.' >&2; exit 1;
}
[[ -r /proc/device-tree/model ]] && [[ $(tr -d '\0' </proc/device-tree/model) == *'Raspberry Pi 5'* ]] || {
  echo 'This first installer targets Raspberry Pi 5 only.' >&2; exit 1;
}
case $(dpkg --print-architecture) in
  arm64|armhf) ;;
  *) echo 'Raspberry Pi OS with arm64 or armhf packages is required.' >&2; exit 1 ;;
esac
replace_desktop=0
if [[ ${1:-} == --replace-desktop ]]; then
  replace_desktop=1
  shift
fi
[[ $# == 2 ]] || { echo 'Usage: sudo ./install.sh [--replace-desktop] SIGNED_PACKAGE_DIRECTORY TRUSTED_PUBLIC_KEY' >&2; exit 1; }
[[ ! -e /opt/loxicom/current ]] || { echo 'Already installed. Use release staging instead.' >&2; exit 1; }
desktop_service=''
if [[ -e /etc/systemd/system/display-manager.service ]]; then
  desktop_service=$(basename -- "$(readlink -f /etc/systemd/system/display-manager.service)")
  [[ $replace_desktop == 1 && $desktop_service == lightdm.service ]] || {
    echo 'An existing display manager was found. For Raspberry Pi Desktop with LightDM, pass --replace-desktop.' >&2; exit 1;
  }
fi
deploy_dir=$(cd -- "$(dirname -- "$0")/.." && pwd)
package_dir=$(realpath -- "$1")
public_key=$(realpath -- "$2")
python3 "$deploy_dir/release.py" verify --package "$package_dir" --public-key "$public_key"

# Preserve the Desktop login before package installation can change DM defaults.
if [[ -n $desktop_service ]]; then
  install -d -m 0700 /etc/loxicom/desktop-backup
  printf '%s\n' "$desktop_service" >/etc/loxicom/desktop-backup/service
  systemctl is-enabled getty@tty1.service >/etc/loxicom/desktop-backup/getty-state || true
  if [[ -f /etc/X11/default-display-manager ]]; then
    cp -p /etc/X11/default-display-manager /etc/loxicom/desktop-backup/default-display-manager
  fi
fi

# Prevent a newly installed greeter starting before its kiosk configuration exists.
systemctl mask --runtime greetd.service
trap 'systemctl unmask --runtime greetd.service >/dev/null 2>&1 || true' EXIT
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  python3 openssl cage greetd chromium dbus-user-session fonts-dejavu-core wlr-randr

id loxicom >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin loxicom
id loxicom-kiosk >/dev/null 2>&1 || useradd --create-home --shell /usr/sbin/nologin loxicom-kiosk
usermod -a -G video,render,input loxicom-kiosk
install -d -m 0755 /opt/loxicom /opt/loxicom/releases /usr/local/lib/loxicom
install -d -m 0700 -o loxicom -g loxicom /var/lib/loxicom/keypad
install -d -m 0750 -o root -g loxicom /etc/loxicom
install -m 0644 "$public_key" /etc/loxicom/release-public.pem
install -m 0755 "$deploy_dir/release.py" /usr/local/lib/loxicom/release.py
release_dir=$(python3 - "$deploy_dir" "$package_dir" "$public_key" <<'PY'
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from release import stage
print(stage(Path(sys.argv[2]), Path(sys.argv[3]), Path('/opt/loxicom')))
PY
)
ln -s "$release_dir" /opt/loxicom/current
install -d -m 0755 /opt/loxicom/recovery
for name in recovery.py index.html recovery.js recovery.css aurora.js; do
  install -m 0644 "$deploy_dir/kiosk/$name" "/opt/loxicom/recovery/$name"
done
install -m 0755 "$deploy_dir/pi/kiosk-browser.sh" /usr/local/lib/loxicom/kiosk-browser.sh
install -m 0755 "$deploy_dir/pi/configure-kiosk-display.sh" /usr/local/lib/loxicom/configure-kiosk-display.sh
install -m 0644 "$deploy_dir/pi/95-loxicom-touchscreen.rules" /etc/udev/rules.d/95-loxicom-touchscreen.rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=input --action=change
install -m 0755 "$deploy_dir/pi/configure-kiosk-zoom.py" /usr/local/lib/loxicom/configure-kiosk-zoom.py
install -m 0755 "$deploy_dir/pi/install-touch-cursor.py" /usr/local/lib/loxicom/install-touch-cursor.py
python3 /usr/local/lib/loxicom/install-touch-cursor.py
install -m 0755 "$deploy_dir/pi/restore-desktop.sh" /usr/local/lib/loxicom/restore-desktop.sh
install -m 0644 "$deploy_dir/pi/keypad.service" /etc/systemd/system/loxicom-keypad.service
install -m 0644 "$deploy_dir/pi/recovery.service" /etc/systemd/system/loxicom-recovery.service
install -d -m 0755 /etc/greetd
[[ ! -f /etc/greetd/config.toml ]] || cp -p /etc/greetd/config.toml /etc/greetd/config.toml.pre-loxicom
install -m 0644 "$deploy_dir/pi/greetd.toml" /etc/greetd/config.toml
install -d -m 0755 /etc/systemd/system/greetd.service.d
install -m 0644 "$deploy_dir/pi/greetd-override.conf" /etc/systemd/system/greetd.service.d/loxicom.conf
install -d -m 0755 /etc/chromium/policies/managed
install -m 0644 "$deploy_dir/pi/chromium-policy.json" /etc/chromium/policies/managed/loxicom.json
if [[ ! -f /etc/loxicom/device-id ]]; then
  python3 -c 'import uuid; print(uuid.uuid4())' >/etc/loxicom/device-id
  chmod 0644 /etc/loxicom/device-id
fi
if [[ ! -f /etc/loxicom/keypad.env ]]; then
  install -m 0640 -o root -g loxicom /dev/null /etc/loxicom/keypad.env
  echo '# Installer: configure verified UNIFI_CERT_SHA256 or UNIFI_CA_FILE here.' >/etc/loxicom/keypad.env
fi
systemctl daemon-reload
systemctl enable loxicom-keypad.service loxicom-recovery.service
systemctl start loxicom-keypad.service loxicom-recovery.service

# Do not change the next boot's display manager until both local services answer.
python3 - <<'PY'
import json
import time
from urllib.request import urlopen
for attempt in range(30):
    try:
        with urlopen('http://127.0.0.1:8765/health', timeout=2) as response:
            health = json.load(response)
        assert health.get('service') == 'loxicom-keypad' and health.get('processHealthy') is True
        with urlopen('http://127.0.0.1:8764/', timeout=2) as response:
            assert response.status == 200
        break
    except (OSError, ValueError, AssertionError):
        if attempt == 29:
            raise SystemExit('Local service check failed; the existing desktop remains selected.')
        time.sleep(1)
PY

systemctl unmask --runtime greetd.service
trap - EXIT
if [[ -n $desktop_service ]]; then
  # Keep the running desktop until the planned reboot, retaining SSH access.
  systemctl disable "$desktop_service"
fi
systemctl disable getty@tty1.service
systemctl mask getty@tty1.service
systemctl enable --force greetd.service
install -d -m 0755 /etc/X11
printf '/usr/sbin/greetd\n' >/etc/X11/default-display-manager
systemctl set-default graphical.target
echo 'Installed. Configure this site before commissioning. Reboot to test the touchscreen startup.'
echo 'No remote updater has been enabled. No UniFi credentials or home settings were copied.'
if [[ -n $desktop_service ]]; then
  echo 'Restore the previous desktop over SSH with: sudo /usr/local/lib/loxicom/restore-desktop.sh'
fi
