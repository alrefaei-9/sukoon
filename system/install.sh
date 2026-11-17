#!/usr/bin/env bash
# Sukoon installer for a fresh Raspberry Pi OS (64-bit) on a Raspberry Pi 4B.
#
#   cp system/config.example system/config.env    # then edit it (hotspot password)
#   sudo ./system/install.sh
#
# What it does (safe to run again):
#   1. installs the system packages (PortAudio, ALSA tools, GCC, mDNS)
#   2. creates the Python environment in .venv from requirements.txt
#   3. downloads and builds RNNoise from its pinned, SHA-256-checked source (src/librnnoise.so)
#   4. sets the Wi-Fi country, the hostname and the Wi-Fi hotspot (from system/config.env)
#   5. installs and enables the services, so Sukoon starts by itself at every power-up
#
# Options:
#   --skip-hotspot      leave the network settings alone (steps 4: hotspot and hostname)
#   --rebuild-rnnoise   build RNNoise again even when src/librnnoise.so already exists
#   --no-start          enable the services but do not start them now
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$REPO/system/config.env"
SKIP_HOTSPOT=0; REBUILD=0; START=1
for arg in "$@"; do
  case "$arg" in
    --skip-hotspot) SKIP_HOTSPOT=1 ;;
    --rebuild-rnnoise) REBUILD=1 ;;
    --no-start) START=0 ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
fail() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail "run with sudo: sudo ./system/install.sh"
SERVICE_USER="${SUDO_USER:-}"
[[ -n "$SERVICE_USER" && "$SERVICE_USER" != root ]] || fail "run it with sudo from your normal user account, not as root"
as_user() { sudo -u "$SERVICE_USER" -H "$@"; }

if [[ ! -f "$CONFIG" ]]; then
  cp "$REPO/system/config.example" "$CONFIG"
  chown "$SERVICE_USER:" "$CONFIG"; chmod 600 "$CONFIG"
  fail "created system/config.env from config.example. Edit it (at least HOTSPOT_PASSWORD), then run this again."
fi
# shellcheck source=config.example
source "$CONFIG"
HOTSPOT_ENABLE="${HOTSPOT_ENABLE:-yes}"; HOTSPOT_SSID="${HOTSPOT_SSID:-Sukoon}"; HOTSPOT_PASSWORD="${HOTSPOT_PASSWORD:-}"
WIFI_COUNTRY="${WIFI_COUNTRY:-}"; HOSTNAME_NEW="${PI_HOSTNAME:-}"; HTTP_PORT="${HTTP_PORT:-5000}"
[[ "$HTTP_PORT" =~ ^[0-9]+$ ]] || fail "HTTP_PORT must be a number"
if [[ $SKIP_HOTSPOT -eq 0 && "$HOTSPOT_ENABLE" == yes ]]; then
  (( ${#HOTSPOT_PASSWORD} >= 8 && ${#HOTSPOT_PASSWORD} <= 63 )) || fail "HOTSPOT_PASSWORD in system/config.env must be 8 to 63 characters"
fi
[[ "$(uname -m)" == aarch64 ]] || echo "Warning: Sukoon was built and measured on 64-bit Raspberry Pi OS (aarch64); this is $(uname -m)."

step "1/5 System packages"
apt-get update
apt-get install -y python3 python3-venv python3-dev gcc libportaudio2 alsa-utils avahi-daemon network-manager

step "2/5 Python environment (.venv)"
as_user python3 -m venv "$REPO/.venv"
as_user "$REPO/.venv/bin/pip" install --upgrade pip
as_user "$REPO/.venv/bin/pip" install -r "$REPO/requirements.txt"

step "3/5 RNNoise (pinned source, checked with SHA-256, compiled for this CPU)"
if [[ $REBUILD -eq 1 || ! -f "$REPO/src/librnnoise.so" || ! -f "$REPO/src/librnnoise.json" ]]; then
  as_user "$REPO/.venv/bin/python" "$REPO/src/build_rnnoise.py"
else
  echo "src/librnnoise.so already built (use --rebuild-rnnoise to build it again)"
fi

step "4/5 Network: Wi-Fi country, hostname, hotspot"
if [[ $SKIP_HOTSPOT -eq 1 ]]; then
  echo "skipped (--skip-hotspot)"
else
  if [[ -n "$WIFI_COUNTRY" ]]; then
    if command -v raspi-config >/dev/null; then raspi-config nonint do_wifi_country "$WIFI_COUNTRY" || true
    else iw reg set "$WIFI_COUNTRY" 2>/dev/null || true; fi
    rfkill unblock wifi 2>/dev/null || true
  fi
  if [[ -n "$HOSTNAME_NEW" && "$(hostname)" != "$HOSTNAME_NEW" ]]; then
    OLD="$(hostname)"
    hostnamectl set-hostname "$HOSTNAME_NEW"
    if grep -q '^127\.0\.1\.1' /etc/hosts; then sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$HOSTNAME_NEW/" /etc/hosts
    else printf '127.0.1.1\t%s\n' "$HOSTNAME_NEW" >> /etc/hosts; fi
    echo "hostname: $OLD -> $HOSTNAME_NEW"
  fi
  systemctl enable --now avahi-daemon
  if [[ "$HOTSPOT_ENABLE" == yes ]]; then
    CON=sukoon-hotspot
    nmcli -t -f NAME con show | grep -qx "$CON" || nmcli con add type wifi ifname wlan0 con-name "$CON" ssid "$HOTSPOT_SSID"
    nmcli con modify "$CON" 802-11-wireless.ssid "$HOTSPOT_SSID" 802-11-wireless.mode ap 802-11-wireless.band bg \
      ipv4.method shared ipv6.method ignore wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$HOTSPOT_PASSWORD" \
      connection.autoconnect yes connection.autoconnect-priority 100
    nmcli con up "$CON" || echo "Warning: the hotspot did not come up now; it will try again at the next boot."
  else
    echo "hotspot disabled in config.env"
  fi
fi

step "5/5 Services (start at power-up)"
usermod -aG audio "$SERVICE_USER"
loginctl enable-linger "$SERVICE_USER"          # lets the battery test run as a user service without a login
sed -e "s#@USER@#$SERVICE_USER#g" -e "s#@REPO@#$REPO#g" -e "s#@PORT@#$HTTP_PORT#g" \
  "$REPO/system/sukoon.service" > /etc/systemd/system/sukoon.service
install -m 644 "$REPO/system/sukoon-governor.service" /etc/systemd/system/sukoon-governor.service
systemctl daemon-reload
systemctl enable sukoon-governor.service sukoon.service
if [[ $START -eq 1 ]]; then
  systemctl restart sukoon-governor.service sukoon.service
fi

NAME="${HOSTNAME_NEW:-$(hostname)}"
cat <<EOF

Sukoon is installed.
  Dashboard:  http://$NAME.local:$HTTP_PORT   (on the hotspot "$HOTSPOT_SSID", also http://10.42.0.1:$HTTP_PORT)
  Status:     systemctl status sukoon
  Logs:       journalctl -u sukoon -b
Plug the USB sound card in only while the Pi is off. Sukoon starts by itself at every power-up.
EOF
