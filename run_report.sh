#!/bin/bash
# Weekly iPhone Screen Time report.
#
#   ./run_report.sh 2026-08-26 2026-09-06 [app ...]
#
# Starts WebDriverAgent on the phone, scrapes Settings > Tiempo en pantalla,
# and prints/records a per-app report. Safe to re-run; leaves WDA stopped.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# --- config -----------------------------------------------------------
# Put your own values in config.local.sh (gitignored); see config.example.sh.
[ -f "$HERE/config.local.sh" ] && . "$HERE/config.local.sh"

PHONE_HOST="${PHONE_HOST:-}"          # e.g. my-iphone.local
DEVICE_UDID="${DEVICE_UDID:-}"        # xcrun devicectl list devices
DEV_TEAM="${DEV_TEAM:-}"              # Apple Developer team id
WDA_DIR="${WDA_DIR:-$HOME/WebDriverAgent}"
RUN_AS="${RUN_AS:-$(stat -f '%Su' /dev/console)}"

for v in PHONE_HOST DEVICE_UDID DEV_TEAM; do
  if [ -z "${!v}" ]; then
    echo "!! $v is not set. Copy config.example.sh to config.local.sh and fill it in." >&2
    exit 1
  fi
done

FROM="${1:?usage: run_report.sh FROM_YYYY-MM-DD TO_YYYY-MM-DD [apps...]}"
TO="${2:?usage: run_report.sh FROM_YYYY-MM-DD TO_YYYY-MM-DD [apps...]}"
shift 2
APPS=("$@")
if [ ${#APPS[@]} -eq 0 ]; then
  APPS=(claude github "google docs" anydesk tuflota)
fi

mkdir -p out shots

# Run everything as the logged-in user: devicectl/xcodebuild talk to
# user-scoped XPC services and misbehave from a root shell.
as_user() {
  if [ "$(id -u)" -eq 0 ]; then sudo -u "$RUN_AS" -H "$@"; else "$@"; fi
}

# --- 1. find the phone on the LAN ------------------------------------
echo "· locating the iPhone…"
IP="$(ping -c 1 -W 2000 "$PHONE_HOST" 2>/dev/null \
      | sed -n 's/.*(\([0-9.]*\)).*/\1/p' | head -1)"
if [ -z "$IP" ]; then
  echo "!! could not resolve $PHONE_HOST — is the phone on the same network?" >&2
  exit 1
fi
echo "  iPhone at $IP"
BASE="http://$IP:8100"

# --- 2. start WebDriverAgent if it isn't already up -------------------
WDA_PID=""
if curl -s -m 4 "$BASE/status" >/dev/null 2>&1; then
  echo "· WebDriverAgent already running"
else
  echo "· starting WebDriverAgent on the phone…"
  ( cd "$WDA_DIR" && as_user xcodebuild \
      -project WebDriverAgent.xcodeproj \
      -scheme WebDriverAgentRunner \
      -destination "id=$DEVICE_UDID" \
      -allowProvisioningUpdates \
      DEVELOPMENT_TEAM="$DEV_TEAM" \
      USE_PORT=8100 \
      test-without-building ) > "$HERE/out/wda_run.log" 2>&1 &
  WDA_PID=$!
  for _ in $(seq 1 60); do
    curl -s -m 4 "$BASE/status" >/dev/null 2>&1 && break
    sleep 3
  done
  curl -s -m 4 "$BASE/status" >/dev/null 2>&1 || {
    echo "!! WebDriverAgent never came up; see out/wda_run.log" >&2
    tail -20 "$HERE/out/wda_run.log" >&2
    exit 1
  }
fi

cleanup() {
  if [ -n "$WDA_PID" ]; then
    echo "· stopping WebDriverAgent"
    kill "$WDA_PID" 2>/dev/null || true
    pkill -f "WebDriverAgentRunner" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# --- 3. scrape --------------------------------------------------------
STAMP="$(date +%Y-%m-%d)"
REPORT="$HERE/out/report_${FROM}_to_${TO}.txt"
as_user python3 -u screentime_report.py \
  --base "$BASE" \
  --from "$FROM" --to "$TO" \
  --out "$HERE/out/screentime_${FROM}_to_${TO}.json" \
  --apps "${APPS[@]}" | tee "$REPORT"

echo
echo "report  -> $REPORT"
echo "raw     -> out/screentime_${FROM}_to_${TO}.json"
