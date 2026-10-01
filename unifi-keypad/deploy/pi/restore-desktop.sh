#!/bin/bash
# Recovery for an install made with --replace-desktop; site data are retained.
set -euo pipefail
[[ $EUID == 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
backup=/etc/loxicom/desktop-backup
[[ -f $backup/service && $(cat "$backup/service") == lightdm.service ]] || {
  echo 'No saved Raspberry Pi Desktop installation was found.' >&2; exit 1;
}
systemctl disable --now greetd.service
systemctl unmask getty@tty1.service
case $(cat "$backup/getty-state") in
  enabled) systemctl enable getty@tty1.service ;;
  masked) systemctl mask getty@tty1.service ;;
esac
if [[ -f $backup/default-display-manager ]]; then
  cp -p "$backup/default-display-manager" /etc/X11/default-display-manager
fi
systemctl enable --force lightdm.service
systemctl daemon-reload
systemctl start lightdm.service
echo 'Previous desktop restored. Loxicom services and saved site data were retained.'
