#!/usr/bin/env python3
"""
transpile.py — hand-transpile noir.bu → noir-{preserve,wipe}.ign  (v1.0)

Butane fcos 1.6.0 source → Ignition 3.5.0 JSON. Two output variants share every
byte of config except four booleans on the 4 TB data drive:
  - storage.disks[0].wipeTable
  - storage.filesystems[*].wipeFilesystem  (3 filesystems)

The .mount units that Butane auto-generates from `with_mount_unit: true` are
emitted by hand here so this transpiler is self-contained (no external Butane
binary dependency). The shape matches what Butane produces.

v1.0.0: FCOS 44 stable base (kernel 6.19; ISO pinned in build-iso.sh).
Twelve layered packages — the Wi-Fi 5-pack (mt7xxx-firmware, wireless-regdb,
NetworkManager-wifi, wpa_supplicant) layered on boot 1 so the radio comes up on
boot 2; the Cockpit 7-pack; and tailscale. No credentials are baked — the core
password and Wi-Fi are set at first boot by noir-setup. Subnet router for
10.0.50.0/24, underlay pinned to bond0; no exit node. See noir.bu for the full
design rationale; this file is its byte-synced transpiler (keep them in lockstep
via sync_check.py).

Usage
-----
    python3 transpile.py                 # noir-preserve.ign (default)
    python3 transpile.py --wipe          # noir-wipe.ign
"""
import argparse
import base64
import json
import os


# ─── CLI ─────────────────────────────────────────────────────────────────────
ap = argparse.ArgumentParser()
ap.add_argument(
    "--wipe",
    action="store_true",
    help="Emit the WIPE variant (wipeTable=true, wipeFilesystem=true).",
)
args = ap.parse_args()

WIPE = args.wipe
VARIANT = "wipe" if WIPE else "preserve"

HERE = os.path.dirname(os.path.abspath(__file__))


# ─── Helpers ─────────────────────────────────────────────────────────────────
def b64url(s: str) -> str:
    """Encode string content as Ignition-style base64 data URL."""
    b = base64.b64encode(s.encode("utf-8")).decode("ascii")
    return f"data:;base64,{b}"


def systemd_escape_path(p: str) -> str:
    """
    Equivalent of `systemd-escape --path` for our paths.
    Strips leading '/', escapes existing '-' as '\\x2d', then replaces '/'
    with '-'. Adequate for /var/home, /var/lib/containers, /var/log.
    """
    p = p.lstrip("/")
    p = p.replace("-", r"\x2d")
    p = p.replace("/", "-")
    return p


# ─── 1. Files (storage.files) ─────────────────────────────────────────────────
files = []


def add_file(path: str, mode: int, body: str, overwrite: bool = True) -> None:
    files.append(
        {
            "path": path,
            "mode": mode,
            "overwrite": overwrite,
            "contents": {"source": b64url(body)},
        }
    )


# /etc/hostname  (Butane `inline: noir` → "noir\n" — YAML adds the newline)
add_file("/etc/hostname", 0o644, "noir\n")

# /etc/yum.repos.d/tailscale.repo
# Verbatim from pkgs.tailscale.com/stable/fedora/tailscale.repo (verified
# 2026-04-25). gpgcheck=1 because Tailscale signs Fedora packages now.
add_file(
    "/etc/yum.repos.d/tailscale.repo",
    0o644,
    """[tailscale-stable]
name=Tailscale stable
baseurl=https://pkgs.tailscale.com/stable/fedora/$basearch
enabled=1
type=rpm
repo_gpgcheck=1
gpgcheck=1
gpgkey=https://pkgs.tailscale.com/stable/fedora/repo.gpg
""",
)

# /etc/motd.d/00-noir-cockpit-tailscale  (renamed in v0.7 to reflect scope)
add_file(
    "/etc/motd.d/00-noir-cockpit-tailscale",
    0o644,
    """
────────────────────────────────────────────────────────────────────
  noir — FCOS · bond0 LACP · Wi-Fi · Tailscale · Cockpit
────────────────────────────────────────────────────────────────────

  Activate Tailscale:
  $ sudo tailscale up --hostname=noir --advertise-routes=10.0.50.0/24
    → approve advertisements at https://login.tailscale.com/admin/machines

  Wi-Fi toggle (bond0 stays primary at metric 100):
  $ sudo noir-wifi on        # raise Wi-Fi slot (metric 50, beats bond0)
  $ sudo noir-wifi off       # fall back to bond0
  $ sudo noir-wifi status    # show state

  SSH        :  OpenSSH on :22
  Podman sock: /run/podman/podman.sock
  OS updates : zincati (auto, stable stream)
  Web admin  : https://noir:9090
────────────────────────────────────────────────────────────────────
""",
)

# bond0 master
# route-metric=100 on the bond; the Wi-Fi slot keyfiles (written at first boot
# by noir-setup) declare route-metric=50, so any associated Wi-Fi owns the
# internet default route while bond0 stays the steady wired fallback. Tailscale's
# underlay is pinned to bond0 via fwmark policy routing (see below), so the
# tailnet stays on the wired link regardless of the default route.
add_file(
    "/etc/NetworkManager/system-connections/bond0.nmconnection",
    0o600,
    """[connection]
id=bond0
type=bond
interface-name=bond0
autoconnect=true
autoconnect-priority=200

[bond]
mode=802.3ad
miimon=100
lacp_rate=slow
xmit_hash_policy=layer3+4

[ipv4]
method=auto
route-metric=100
route1=0.0.0.0/0,10.0.50.1,100
route1_options=table=100
routing-rule1=priority 5200 fwmark 0x80000/0xff0000 table 100

[ipv6]
method=auto
addr-gen-mode=eui64
route-metric=100
routing-rule1=priority 5200 fwmark 0x80000/0xff0000 table 100
""",
)

# bond0 slaves — multi-connect=3 (MULTIPLE) auto-attaches to both NICs at boot
add_file(
    "/etc/NetworkManager/system-connections/bond0-slave.nmconnection",
    0o600,
    """[connection]
id=bond0-slave
type=ethernet
master=bond0
slave-type=bond
autoconnect=true
autoconnect-priority=200
multi-connect=3

[match]
interface-name=enp97s0;enp98s0

[ethernet]
cloned-mac-address=preserve
""",
)

# First-boot stamp under /var/lib/<name>/ — FCOS-canonical state-marker location.
# Removed by noir-firstboot-install.service after layering tailscale.
add_file(
    "/var/lib/noir/firstboot.stamp",
    0o644,
    "# noir first-boot stamp. Removed after rpm-ostree install tailscale.\n",
)

# Tailscale IP forwarding sysctls — required for advertise-routes to
# actually forward subnet traffic. Filename per Tailscale
# KB 1019 / 1103 verbatim.
add_file(
    "/etc/sysctl.d/99-tailscale.conf",
    0o644,
    """net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1
""",
)

# v0.6: Cockpit web admin config. Three [WebService] directives:
#   LoginTo = false        — disables multihost / remote-login feature
#                            (CVE-2026-4631 mitigation; verify advisory at
#                            access.redhat.com/security/cve/cve-2026-4631).
#                            Doesn't affect local browser → local cockpit-ws.
#   AllowUnencrypted=false — refuses plain HTTP (default; explicit is good).
#   MaxStartups = 10       — caps concurrent unauthenticated sessions
#                            (brute-force dampener).
# NOT setting RequireHost — would break LAN+tailnet flexibility (browse via
# different hostnames). Default cockpit.socket binds dual-stack [::]:9090
# = all interfaces, which is what we want for LAN+tailnet reachability.
add_file(
    "/etc/cockpit/cockpit.conf",
    0o644,
    """[WebService]
LoginTo = false
AllowUnencrypted = false
MaxStartups = 10
""",
)


# /usr/local/bin/noir-wifi — Wi-Fi uplink control across three slots.
# Mode 0755 explicit (Butane defaults to 0644). Replaces noir-wifi: all
# three slot keyfiles (wifi-primary/secondary/tertiary) declare route-metric=50,
# so any associated Wi-Fi beats bond0 (metric=100); only one associates at a
# time on wlp99s0. NM does NOT auto-roam between SSIDs, so slot selection is
# operator-triggered. set-primary swaps the [wifi]/[wifi-security] network
# payload between the chosen slot and primary (keeping id=wifi-primary /
# priority=100 anchored to the primary file), rewrites the keyfiles 0600, and
# reloads NM. Guards on the wlp99s0 wifi device so it errors cleanly before
# first-boot layering brings the Wi-Fi stack up.
add_file(
    "/usr/local/bin/noir-wifi",
    0o755,
    r"""#!/bin/bash
# noir-wifi — Wi-Fi uplink control across three slots (primary/secondary/tertiary).
# All slot keyfiles set route-metric=50, so any associated Wi-Fi beats bond0
# (metric=100). NM does NOT auto-roam between SSIDs, so slot selection is
# operator-triggered. Only one Wi-Fi associates at a time on wlp99s0.
# Usage: sudo noir-wifi {on|off|switch <slot>|status|list|set-primary <slot|SSID>}
#   slot = primary | secondary | tertiary

set -euo pipefail
export LC_ALL=C   # locale-safe nmcli/awk output parsing (canonical NM/RH idiom)

DEV="wlp99s0"
SC="/etc/NetworkManager/system-connections"
SLOTS="primary secondary tertiary"

# Guard: Wi-Fi stack only exists after boot-1 layering + reboot. If the NM wifi
# plugin or the wlp99s0 device is absent, fail cleanly rather than spewing nmcli
# errors.
require_wifi() {
  nmcli -t -f DEVICE,TYPE device status 2>/dev/null \
    | awk -F: -v d="$DEV" '$1==d && $2=="wifi"{f=1} END{exit f?0:1}' \
    || { echo "[err] Wi-Fi not ready — run after first-boot completes" >&2; exit 2; }
}

slot_file() { echo "$SC/wifi-$1.nmconnection"; }

# The [connection] id NM knows this slot by. Anchored to wifi-<slot> by the
# keyfile; set-primary never moves it (it moves the network payload instead).
slot_id() {
  local f; f=$(slot_file "$1")
  [ -r "$f" ] || { echo "wifi-$1"; return; }
  awk -F= '/^\[/{s=$0} s=="[connection]" && $1=="id"{print $2; exit}' "$f"
}

# SSID configured in this slot's keyfile [wifi] section, or empty if unset/missing.
slot_ssid() {
  local f; f=$(slot_file "$1")
  [ -r "$f" ] || { echo ""; return; }
  awk -F= '/^\[/{s=$0} s=="[wifi]" && $1=="ssid"{print $2; exit}' "$f"
}

# Echo the highest-priority slot (primary>secondary>tertiary order) that has an
# ssid set; empty if none configured.
first_configured() {
  local s
  for s in $SLOTS; do
    [ -n "$(slot_ssid "$s")" ] && { echo "$s"; return; }
  done
  echo ""
}

# Echo the id of the active wifi connection on wlp99s0 (empty if none).
active_conn() {
  nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status 2>/dev/null \
    | awk -F: -v d="$DEV" '$1==d && $2=="wifi" && $3=="connected"{print $4}'
}

# Resolve a primary|secondary|tertiary token, OR an SSID, to a slot name.
resolve_slot() {
  local arg="$1" s
  for s in $SLOTS; do
    [ "$arg" = "$s" ] && { echo "$s"; return; }
  done
  for s in $SLOTS; do
    [ "$arg" = "$(slot_ssid "$s")" ] && { echo "$s"; return; }
  done
  echo ""
}

bring_up() {
  local slot="$1" id; id=$(slot_id "$slot")
  if [ -z "$(slot_ssid "$slot")" ]; then
    echo "[err] $slot slot not configured (no ssid set)" >&2; exit 1
  fi
  if err=$(nmcli --wait 30 connection up "$id" 2>&1); then
    echo "[ok] $slot ($id) associated; metric=50 owns the default route"
    logger -t noir-wifi -p notice "$slot ($id) associated"
  else
    echo "[warn] $slot ($id) association failed: $err" >&2
    logger -t noir-wifi -p warning "$slot ($id) association failed: $err"
    exit 1
  fi
}

require_wifi

case "${1-}" in
  on)
    slot=$(first_configured)
    [ -n "$slot" ] || { echo "[err] no Wi-Fi slot configured — run: noir-wifi list" >&2; exit 1; }
    bring_up "$slot"
    ;;
  switch)
    slot=$(resolve_slot "${2-}")
    [ -n "$slot" ] || { echo "Usage: $0 switch {primary|secondary|tertiary}" >&2; exit 1; }
    bring_up "$slot"
    ;;
  off)
    conn=$(active_conn)
    if [ -n "$conn" ]; then
      nmcli --wait 5 connection down "$conn"
      echo "[ok] $conn down; NM falls back to bond0 (metric=100)"
      logger -t noir-wifi -p notice "$conn down, fell back to bond0"
    else
      echo "[noop] no active Wi-Fi on $DEV"
    fi
    ;;
  status)
    echo "── Default route ──"; ip -4 route show default; echo
    echo "── Active connection on $DEV ──"
    conn=$(active_conn)
    echo "${conn:-(none)}"
    ;;
  list)
    echo "── Wi-Fi slots ──"
    for s in $SLOTS; do
      ssid=$(slot_ssid "$s")
      printf '  %-9s  %s\n' "$s" "${ssid:-(not configured)}"
    done
    ;;
  set-primary)
    target=$(resolve_slot "${2-}")
    [ -n "$target" ] || { echo "Usage: $0 set-primary {primary|secondary|tertiary|SSID}" >&2; exit 1; }
    [ -n "$(slot_ssid "$target")" ] || { echo "[err] ${2-} not configured (no ssid set)" >&2; exit 1; }
    if [ "$target" = "primary" ]; then
      echo "[noop] primary slot already holds $(slot_ssid primary)"; exit 0
    fi
    # Promote: the primary keyfile keeps id=wifi-primary / priority=100 (the
    # metric-50 winner), so we swap the NETWORK payload — the [wifi] and
    # [wifi-security] sections — between the primary file and the target file.
    # The chosen network thus lands in the priority-100 primary slot; the old
    # primary network demotes into the target slot. Files rewritten 0600, then
    # nmcli reload re-reads them.
    pf=$(slot_file primary); tf=$(slot_file "$target")
    ptmp=$(mktemp); ttmp=$(mktemp)
    # Emit one file using its own [connection] section + the other file's
    # [wifi]/[wifi-security] sections.
    swap() {
      local conn_src="$1" net_src="$2"
      awk -F= '
        /^\[/{s=$0; if(s=="[connection]"){c=1} else {c=0}}
        c{print}
      ' "$conn_src"
      awk -F= '
        /^\[/{s=$0; if(s=="[wifi]"||s=="[wifi-security]"){w=1} else {w=0}}
        w{print}
      ' "$net_src"
      awk -F= '
        /^\[/{s=$0; if(s=="[connection]"||s=="[wifi]"||s=="[wifi-security]"){o=0} else {o=1}}
        o && NF{print}
      ' "$conn_src"
    }
    swap "$pf" "$tf" >"$ptmp"
    swap "$tf" "$pf" >"$ttmp"
    install -m 0600 "$ptmp" "$pf"
    install -m 0600 "$ttmp" "$tf"
    rm -f "$ptmp" "$ttmp"
    nmcli connection reload
    echo "[ok] ${2-} promoted; primary slot now holds $(slot_ssid primary)"
    logger -t noir-wifi -p notice "set-primary: ${2-} promoted into primary slot"
    ;;
  *) echo "Usage: $0 {on|off|switch <slot>|status|list|set-primary <slot|SSID>}" >&2; exit 1 ;;
esac
""",
)


add_file(
    "/usr/local/bin/noir-setup",
    0o755,
    r"""#!/bin/bash
# noir-setup — interactive first-boot setter (runs on boot 2, after Wi-Fi layers).
# Sets the Core password, generates the three Wi-Fi slots' NM keyfiles, runs
# Tailscale onboarding, then persists everything (password hash, keyfiles,
# tailscaled.state) to the preserved data drive so a preserve re-flash restores
# it. flock + a completion sentinel make it first-one-wins across tty1 and SSH.
# Usage: sudo noir-setup
set -euo pipefail
export LC_ALL=C

SECRETS=/var/home/.noir-secrets
SENTINEL="$SECRETS/.setup-done"
NMDIR=/etc/NetworkManager/system-connections
LOCK=/run/lock/noir-setup.lock

if [ "$(id -u)" -ne 0 ]; then
  echo "noir-setup must run as root: sudo noir-setup" >&2
  exit 1
fi

# ── Single-instance lock (tty1 vs SSH can't race) ─────────────────────────────
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "noir-setup is already running in another session." >&2
  exit 1
fi

# ── Already configured? ───────────────────────────────────────────────────────
if [ -e "$SENTINEL" ]; then
  echo "already configured"
  exit 0
fi

# ── (0) Readiness guard — boot 1 still layering the Wi-Fi stack ────────────────
# If the NetworkManager wifi plugin or wlp99s0 is absent, we're on boot 1 (the
# operator SSH'd in before the auto-reboot). Set NOTHING — no partial state.
if ! nmcli -t -f NAME general 2>/dev/null | grep -q . \
   || ! nmcli -t -f DEVICE,TYPE device status 2>/dev/null | grep -q '^wlp99s0:wifi$'; then
  echo "first boot still completing — the host will reboot once; re-run 'sudo noir-setup' after it returns"
  exit 0
fi

echo "── noir first-boot setup ──"
echo

# ── (1) Core password ─────────────────────────────────────────────────────────
while :; do
  printf 'Set the core password: '
  read -rs PW1; echo
  printf 'Confirm the core password: '
  read -rs PW2; echo
  if [ -z "$PW1" ]; then
    echo "[warn] empty password rejected; try again." >&2
    continue
  fi
  if [ "$PW1" != "$PW2" ]; then
    echo "[warn] passwords did not match; try again." >&2
    continue
  fi
  break
done
printf 'core:%s\n' "$PW1" | chpasswd
unset PW2
echo "[ok] core password set."
echo

# ── (2) Wi-Fi slots ───────────────────────────────────────────────────────────
write_slot() {
  slot="$1"; prio="$2"; ssid="$3"; psk="$4"
  f="$NMDIR/wifi-$slot.nmconnection"
  umask 077
  {
    printf '[connection]\n'
    printf 'id=wifi-%s\n' "$slot"
    printf 'type=wifi\n'
    printf 'autoconnect=false\n'
    printf 'autoconnect-priority=%s\n' "$prio"
    printf '\n'
    printf '[wifi]\n'
    printf 'mode=infrastructure\n'
    printf 'ssid=%s\n' "$ssid"
    printf 'cloned-mac-address=preserve\n'
    printf '\n'
    printf '[wifi-security]\n'
    printf 'key-mgmt=sae\n'
    printf 'pmf=3\n'
    printf 'psk=%s\n' "$psk"
    printf '\n'
    printf '[ipv4]\n'
    printf 'method=auto\n'
    printf 'route-metric=50\n'
    printf '\n'
    printf '[ipv6]\n'
    printf 'method=auto\n'
    printf 'addr-gen-mode=default\n'
    printf 'route-metric=50\n'
  } >"$f"
  chmod 0600 "$f"
  echo "[ok] wrote $f"
}

WROTE_WIFI=0
for pair in "primary 100" "secondary 90" "tertiary 80"; do
  set -- $pair
  slot="$1"; prio="$2"
  printf 'Wi-Fi %s SSID (blank to skip): ' "$slot"
  read -r SSID
  if [ -z "$SSID" ]; then
    echo "[skip] $slot"
    continue
  fi
  printf 'Wi-Fi %s PSK: ' "$slot"
  read -rs PSK; echo
  write_slot "$slot" "$prio" "$SSID" "$PSK"
  unset PSK
  WROTE_WIFI=1
done
if [ "$WROTE_WIFI" -eq 1 ]; then
  nmcli connection reload
  echo "[ok] reloaded NetworkManager connections."
fi
echo

# ── (3) Tailscale onboarding ──────────────────────────────────────────────────
echo "── Tailscale onboarding ──"
echo "Running: tailscale up --hostname=noir --advertise-routes=10.0.50.0/24"
echo "Approve the printed auth URL in your browser, then approve route"
echo "advertisements at https://login.tailscale.com/admin/machines"
tailscale up --hostname=noir --advertise-routes=10.0.50.0/24 || \
  echo "[warn] tailscale up exited non-zero; re-run 'sudo tailscale up --hostname=noir --advertise-routes=10.0.50.0/24' if needed." >&2
echo

# ── (4) Persist to the preserved data drive ───────────────────────────────────
mkdir -p "$SECRETS"
chown root:root "$SECRETS"
chmod 0700 "$SECRETS"

# core password hash (field 2 of getent shadow)
getent shadow core | cut -d: -f2 >"$SECRETS/core.hash"
chmod 0600 "$SECRETS/core.hash"

# generated wifi keyfiles
for slot in primary secondary tertiary; do
  src="$NMDIR/wifi-$slot.nmconnection"
  if [ -f "$src" ]; then
    install -m 0600 -o root -g root "$src" "$SECRETS/wifi-$slot.nmconnection"
  fi
done

# tailscale node identity
if [ -f /var/lib/tailscale/tailscaled.state ]; then
  install -m 0600 -o root -g root /var/lib/tailscale/tailscaled.state "$SECRETS/tailscaled.state"
fi

# completion sentinel — last, so a crash mid-setup leaves it re-runnable
: >"$SENTINEL"
chmod 0600 "$SENTINEL"
unset PW1

echo "[ok] persisted credentials to $SECRETS"
echo "── noir setup complete ──"
""",
)

# /usr/local/bin/noir-firstboot-setup — boot-2+ orchestrator. RESTORE persisted
# creds (preserve re-flash) silently, else publish the MOTD prompt + let the
# tty1 unit (noir-setup-tty1.service) offer interactive noir-setup. The flock +
# /var/home/.noir-secrets/.setup-done sentinel that noir-setup writes make the
# tty1 and SSH paths mutually exclusive (first-one-wins); once the sentinel
# exists this orchestrator removes the MOTD banner. Mode 0755 explicit (Butane
# defaults to 0644). r"""...""" so backslashes/`$` in the body stay literal.
add_file(
    "/usr/local/bin/noir-firstboot-setup",
    0o755,
    r"""#!/bin/bash
# noir-firstboot-setup — boot-2+ orchestrator. Either RESTORE persisted creds
# (preserve re-flash) silently, or make noir-setup available on BOTH channels
# (MOTD for SSH + an interactive prompt on tty1), first-one-wins.
# Driven by noir-firstboot-setup.service; not meant to be run by hand.
set -euo pipefail
export LC_ALL=C

SECRETS=/var/home/.noir-secrets
SETUP_DONE="$SECRETS/.setup-done"
APPLIED="$SECRETS/.setup-done-applied"
NM_DIR=/etc/NetworkManager/system-connections
TS_STATE=/var/lib/tailscale/tailscaled.state
MOTD=/etc/motd.d/10-noir-setup

# THIS-boot reset: $APPLIED lives on the persistent data drive, so make it mean
# "restored on THIS boot" by clearing any stale copy the first time the service
# runs after a fresh boot. The witness is a tmpfs token under /run (wiped every
# boot); if it's absent we just booted → drop the stale $APPLIED, then plant the
# token so a re-run within the same boot is a no-op and won't churn tailscaled.
if [ ! -e /run/noir/firstboot-setup.booted ]; then
  rm -f "$APPLIED"
  install -d -m 0755 /run/noir
  : > /run/noir/firstboot-setup.booted
fi

# ── If setup already finished (sentinel present), the fresh path is over:
# tear the MOTD banner down so the operator stops seeing the prompt. ────────────
if [ -f "$SETUP_DONE" ]; then
  rm -f "$MOTD"
fi

# ── PRESERVE re-flash: persisted creds exist and we have NOT restored them on
# THIS boot yet → restore silently, no prompt. ──────────────────────────────────
if [ -d "$SECRETS" ] && [ -f "$SETUP_DONE" ] && [ ! -f "$APPLIED" ]; then
  # core password hash (restore exactly what was applied at first setup).
  if [ -f "$SECRETS/core.hash" ]; then
    hash=$(cat "$SECRETS/core.hash")
    usermod -p "$hash" core
    logger -t noir-firstboot-setup -p notice "restored core password hash"
  fi

  # Wi-Fi keyfiles → /etc/NetworkManager/system-connections, 0600, then reload.
  reloaded=0
  for kf in "$SECRETS"/wifi-*.nmconnection; do
    [ -e "$kf" ] || continue
    install -m 0600 -o root -g root "$kf" "$NM_DIR/$(basename "$kf")"
    reloaded=1
  done
  if [ "$reloaded" = 1 ]; then
    nmcli connection reload
    logger -t noir-firstboot-setup -p notice "restored Wi-Fi keyfiles and reloaded NetworkManager"
  fi

  # Tailscale node identity → rejoin the tailnet with no re-auth.
  if [ -f "$SECRETS/tailscaled.state" ]; then
    install -d -m 0700 "$(dirname "$TS_STATE")"
    install -m 0600 -o root -g root "$SECRETS/tailscaled.state" "$TS_STATE"
    systemctl restart tailscaled
    logger -t noir-firstboot-setup -p notice "restored tailscaled.state; restarted tailscaled (rejoining tailnet)"
  fi

  touch "$APPLIED"
  logger -t noir-firstboot-setup -p notice "preserve restore complete; no prompt shown"
  exit 0
fi

# ── FRESH / wipe (no persisted creds, or setup not yet finished): publish the
# MOTD banner so an SSH operator knows to run noir-setup. The tty1 interactive
# path is a separate unit (noir-setup-tty1.service); the flock + sentinel that
# noir-setup writes make the two mutually exclusive (first-one-wins). ───────────
if [ ! -f "$SETUP_DONE" ]; then
  install -d -m 0755 "$(dirname "$MOTD")"
  printf '%s\n' \
    '────────────────────────────────────────────────────────────────────' \
    '  noir — first-boot setup not done yet' \
    '────────────────────────────────────────────────────────────────────' \
    '' \
    '  Set the core password, Wi-Fi, and Tailscale:' \
    '  $ ssh core@noir' \
    '  $ sudo noir-setup' \
    '' \
    '  (a prompt is also waiting on the local console / tty1)' \
    '────────────────────────────────────────────────────────────────────' \
    > "$MOTD"
  chmod 0644 "$MOTD"
fi
exit 0
""",
)


# ─── (in the `units = [ ... ]` list, appended after noir-postinstall-verify) ──
    # ── Boot-2+ first-boot setup orchestrator (restore-or-publish) ───────────
    # Runs ONLY on the post-layering boot (Wi-Fi + tailscaled live): same gate
    # as noir-firstboot-enable — ConditionPathExists=!firstboot.stamp +
    # After=noir-firstboot-enable.service. NEVER ConditionFirstBoot (boot 1 is
    # too early; wlp99s0 doesn't exist yet). Either restores persisted creds
    # silently (preserve) or publishes the MOTD prompt for the SSH path.

# /usr/local/bin/noir-table100 — dynamic safety-net + v6 populator for routing
# table 100's default route(s). Mode 0755 explicit (Butane defaults to 0644).
# bond0's keyfile statically sets the v4 table-100 default via 10.0.50.1 and the
# v4+v6 fwmark routing-rules; this re-points v4 if the gateway moves and populates
# v6 from the RA-learned gateway the keyfile can't know statically. Idempotent
# (`ip route replace`); driven by noir-table100.timer.
add_file(
    "/usr/local/bin/noir-table100",
    0o755,
    r"""#!/bin/bash
# noir-table100 — keep routing table 100's default route(s) tracking bond0's
# CURRENT gateway, so the Tailscale-underlay fwmark pin survives DHCP/RA churn.
# bond0's keyfile statically sets the v4 table-100 default via 10.0.50.1 plus
# the v4+v6 fwmark routing-rules; this is the dynamic safety-net (re-point v4 if
# the gateway moves) and the v6 populator (the keyfile can't statically know the
# RA-learned v6 gateway). Idempotent via `ip route replace`. Driven by the timer.

set -euo pipefail
export LC_ALL=C   # locale-safe `ip route` output parsing (canonical idiom)

# v4: re-point table 100's default at bond0's current main-table gateway.
gw4=$(ip -4 route show default dev bond0 | awk '/default/{print $3; exit}')
if [ -n "${gw4:-}" ]; then
  ip route replace default via "$gw4" dev bond0 table 100
  logger -t noir-table100 -p info "table 100 v4 default → $gw4 dev bond0"
fi

# v6: populate/refresh table 100's default from bond0's RA-learned gateway.
gw6=$(ip -6 route show default dev bond0 | awk '/default/{print $3; exit}')
if [ -n "${gw6:-}" ]; then
  ip -6 route replace default via "$gw6" dev bond0 table 100
  logger -t noir-table100 -p info "table 100 v6 default → $gw6 dev bond0"
fi
""",
)


# --- and append these two dicts to the `units = [ ... ]` list (after the
#     noir-postinstall-verify.service dict, before the closing `]`): ---

    # ── Table-100 default-route keeper (dynamic safety-net + v6 populator) ────
    # bond0's keyfile statically pins the v4 table-100 default via 10.0.50.1 and
    # declares the v4+v6 fwmark routing-rules. But a static keyfile can't track a
    # gateway that moves under DHCP/RA, and can't know the RA-learned v6 gateway
    # at write time. This oneshot reads bond0's CURRENT default gateway for each
    # family and `ip route replace`s table 100's default to match — keeping the
    # Tailscale-underlay fwmark pin correct. Idempotent (replace). The timer
    # re-runs it every 5 min; the service is order-only (no [Install] — the
    # timer owns activation).

# ─── 2. Storage: disks + filesystems ──────────────────────────────────────────
DATA_DEVICE = "/dev/disk/by-id/nvme-WD_BLACK_SN850X_4000GB_25278B803296"

disks = [
    {
        "device": DATA_DEVICE,
        "wipeTable": WIPE,
        "partitions": [
            {
                "label": "noir-home",
                "number": 1,
                "sizeMiB": 2560000,             # 2500 GiB
                "resize": False,
                "wipePartitionEntry": False,
            },
            {
                "label": "noir-containers",
                "number": 2,
                "sizeMiB": 1024000,             # 1000 GiB
                "resize": False,
                "wipePartitionEntry": False,
            },
            {
                "label": "noir-log",
                "number": 3,
                "sizeMiB": 0,                   # remainder (~225 GiB)
                "resize": False,
                "wipePartitionEntry": False,
            },
        ],
    }
]

# Three filesystems — XFS, deterministic UUIDs, nofail mount, with `path:` so
# Ignition mounts them at /sysroot/<path> during its own run (so e.g.
# useradd --create-home writes to the data partition, not the root fs).
filesystems = [
    {
        "device": "/dev/disk/by-partlabel/noir-home",
        "format": "xfs",
        "path": "/var/home",
        "label": "noir-home",
        "uuid": "a5f7e3c8-9b2d-4e6f-8a1c-3f5b9d7e2a41",
        "wipeFilesystem": WIPE,
        "mountOptions": ["defaults", "noatime", "nofail"],
    },
    {
        "device": "/dev/disk/by-partlabel/noir-containers",
        "format": "xfs",
        "path": "/var/lib/containers",
        "label": "noir-ctr",
        "uuid": "b8c2d1f4-6a3e-4b9c-a5d7-1e8f2c4b6d83",
        "wipeFilesystem": WIPE,
        "mountOptions": ["defaults", "noatime", "nofail"],
    },
    {
        "device": "/dev/disk/by-partlabel/noir-log",
        "format": "xfs",
        "path": "/var/log",
        "label": "noir-log",
        "uuid": "d1a8b5e3-7f2c-4d8e-9b4a-6e3c1f5d8b2a",
        "wipeFilesystem": WIPE,
        "mountOptions": ["defaults", "noatime", "nofail"],
    },
]


# ─── 3. systemd units ─────────────────────────────────────────────────────────
def mount_unit(fs_device: str, fs_format: str, mount_path: str,
               mount_options: list) -> dict:
    """
    Hand-emit a systemd .mount unit equivalent to what Butane produces from
    `with_mount_unit: true`. WantedBy=local-fs.target (not RequiredBy=) so
    `nofail` semantics are honoured: a failed mount doesn't block the target.
    """
    esc_path = systemd_escape_path(mount_path)
    opts_str = ",".join(mount_options)
    contents = (
        f"# Generated equivalent to Butane `with_mount_unit: true` for {mount_path}\n"
        "[Unit]\n"
        f"Description=Mount {mount_path}\n"
        "\n"
        "[Mount]\n"
        f"Where={mount_path}\n"
        f"What={fs_device}\n"
        f"Type={fs_format}\n"
        f"Options={opts_str}\n"
        "\n"
        "[Install]\n"
        "WantedBy=local-fs.target\n"
    )
    return {"name": f"{esc_path}.mount", "enabled": True, "contents": contents}


units = [
    # Auto-generated mount units (one per filesystem with with_mount_unit:true).
    mount_unit("/dev/disk/by-partlabel/noir-home", "xfs", "/var/home",
               ["defaults", "noatime", "nofail"]),
    mount_unit("/dev/disk/by-partlabel/noir-containers", "xfs", "/var/lib/containers",
               ["defaults", "noatime", "nofail"]),
    mount_unit("/dev/disk/by-partlabel/noir-log", "xfs", "/var/log",
               ["defaults", "noatime", "nofail"]),

    # ── Wait-for-mount drop-ins ───────────────────────────────────────────────
    {
        "name": "sshd.service",
        "dropins": [
            {
                "name": "10-waitmount.conf",
                "contents": "[Unit]\nRequiresMountsFor=/var/home\n",
            },
        ],
    },
    {
        "name": "podman.service",
        "dropins": [
            {
                "name": "10-waitmount.conf",
                "contents": "[Unit]\nRequiresMountsFor=/var/lib/containers\n",
            },
        ],
    },
    # tailscaled order-fix removed in v1.0 — Fedora's tailscale RPM unit
    # ships `After=systemd-resolved.service` natively (verified on-box
    # 2026-04-30). The drop-in was redundant; closing Tailscale issue #4934
    # is now upstream-handled.

    # ── First-boot install: layer tailscale, then reboot ─────────────────────
    # Defensive hardening:
    #   Before=zincati.service — canonical FCOS first-boot idiom. Mostly
    #   redundant in success path (zincati's After=boot-complete.target
    #   already orders it ~2 min post-multi-user, by which time our reboot
    #   has fired). Kept because it's the documented pattern and adds defense
    #   in failure-then-restart paths.
    #   Restart=on-failure + StartLimitBurst=3 + StartLimitIntervalSec=600 —
    #   retry up to 3 times in 10 minutes on transient rpm-ostree failures
    #   (network blip, repo unreachable).
    {
        "name": "noir-firstboot-install.service",
        "enabled": True,
        "contents": """[Unit]
Description=noir first-boot package layering (FCOS-stripped wireless support + tailscale)
ConditionPathExists=/var/lib/noir/firstboot.stamp
Wants=network-online.target
After=network-online.target
Before=zincati.service
StartLimitIntervalSec=600
StartLimitBurst=3

[Service]
Type=oneshot
RemainAfterExit=yes
Restart=on-failure
RestartSec=60s
# --idempotent     : succeed if any package is already installed (re-run safe).
# --allow-inactive : defensive (in case a package ever lands in the base).
#
# Layered packages by category:
#
# tailscale       : kernel-coupled VPN daemon. Layered per FCOS pattern
#   (TUN device, host network namespace, systemd-resolved integration).
#
# FCOS-stripped wireless support data — kernel-coupled data sub-packages
# that FCOS removes from the base to keep the image lean (FCOS tracker
# #1575). The category, not the per-package name, is the durable rule:
#   mt7xxx-firmware : MediaTek MT7925 Wi-Fi firmware blob. Without it
#     mt7925e loads but hardware init fails (verified 2026-04-25).
#   wireless-regdb  : 802.11 regulatory domain database. FCOS strips
#     wireless-regdb from the base image alongside per-vendor firmware
#     sub-packages (FCOS tracker #1575). Without /lib/firmware/
#     regulatory.db, mac80211 has no regulatory domain and the radio
#     stays administratively DOWN — NM reports the device as
#     `unavailable`. Pulls `iw` in transitively (wireless-regdb
#     Requires: iw). Verified on noir 2026-04-26 (this is the v0.4 bug).
#
# NetworkManager-wifi: NM's wifi plugin (libnm-device-plugin-wifi.so). FCOS
#   strips this sub-package from the base image alongside firmware data —
#   without it, NM creates wifi devices as generic (managed=no) and refuses
#   to manage them. Symptom: nmcli reports `wlp99s0 wifi unmanaged`, NM
#   journal says "'wifi' plugin not available; creating generic device".
#   This failure is distinct from a missing regdb (which leaves the radio
#   "unavailable"): with regdb present but this plugin absent, the radio
#   is up yet NM still refuses to manage it. The Wi-Fi pack and this
#   plugin are both required.
#
# wpa_supplicant : WPA/WPA2/WPA3-SAE supplicant. NM's wifi plugin uses it
#   as the association backend; no supplicant means no protected-AP
#   association even with the wifi plugin loaded. FCOS strips it from
#   the base alongside the other wifi-stack pieces. Upstream FCOS
#   layering-examples (coreos/layering-examples wifi/Containerfile) lists
#   it next to NetworkManager-wifi for this exact reason. Required for
#   the Wi-Fi slot profiles (written at first boot) to actually connect.
#
# ethtool          : in FCOS base (manifest-lock confirmed 2026-04-27 F43; F44 testing-devel re-verified per v1.0).
#   The tailscale-udp-gro.service ExecStart uses /usr/sbin/ethtool from
#   base — no layering needed. Earlier revs layered defensively; rev 5
#   audit confirmed redundancy and removed it for less-is-more.
#
# Cockpit web admin (v0.6, 7 explicit subpackages — see header for the
# full reasoning chain). Order: cockpit-bridge first (plumbing), then
# the body-of-work subpackages alphabetically. Explicit subpackages
# rather than the `cockpit` meta-package — the meta uses rich-Recommends
# `(cockpit-packagekit if dnf)` which evaluates false on FCOS (no dnf
# binary), but rpm-ostree's resolver has historical quirks with rich
# `if`-deps. Explicit list is bullet-proof.
#   cockpit-bridge        : Python plumbing; the per-session DBus relay.
#   cockpit-files         : web file-browser (upload / download / perms).
#                           Inherits Cockpit's session privilege model
#                           (operates as logged-in user; sudo escalates).
#   cockpit-networkmanager: bond/wifi/tailnet visualisation.
#   cockpit-ostree        : rpm-ostree deployment view + rollback UI.
#   cockpit-podman        : Podman container management UI — noir's
#                           headline use case beyond `podman ps`.
#   cockpit-system        : services / journal / hardware overview.
#   cockpit-ws            : the Cockpit web service itself; the listener
#                           on :9090. Pulls cockpit-ws-selinux on FCOS.
# Deliberately NOT layered (deferred to v0.7 if real usage exposes gaps):
#   cockpit-storaged   — drags udisks2 (~18-22 RPMs); noir's storage
#                        layout is static, declared in Ignition.
#   cockpit-selinux    — drags setroubleshoot-server which has FCOS
#                        issue #1720 (data dir not auto-created);
#                        `ausearch -m AVC -ts recent` over SSH covers
#                        the same data without the layering cost.
#   cockpit-sosreport  — not relevant on a single-host homelab.
ExecStart=/usr/bin/rpm-ostree install --idempotent --allow-inactive tailscale mt7xxx-firmware wireless-regdb NetworkManager-wifi wpa_supplicant cockpit-bridge cockpit-files cockpit-networkmanager cockpit-ostree cockpit-podman cockpit-system cockpit-ws
ExecStart=/usr/bin/rm -f /var/lib/noir/firstboot.stamp
ExecStart=/usr/bin/systemctl --no-block reboot

[Install]
WantedBy=multi-user.target
""",
    },

    # ── Tailscale UDP-GRO forwarding optimisation (every boot) ───────────────
    # Tailscale KB 1320 recommends this ethtool tweak on kernels ≥6.2 acting
    # as a subnet router. Idempotent; runs every
    # boot to survive any nm/bond rebuild.
    {
        "name": "tailscale-udp-gro.service",
        "enabled": True,
        "contents": """[Unit]
Description=Tailscale UDP-GRO forwarding tweak for bond0
Wants=sys-subsystem-net-devices-bond0.device
After=sys-subsystem-net-devices-bond0.device NetworkManager.service
Before=tailscaled.service

[Service]
Type=oneshot
RemainAfterExit=yes
# Leading '-' tolerates failure: some kernel versions don't expose
# rx-gro-list on bond devices, and we'd rather log the partial-set
# error than leave the unit in failed state on every boot.
ExecStart=-/usr/sbin/ethtool -K bond0 rx-udp-gro-forwarding on rx-gro-list off

[Install]
WantedBy=multi-user.target
""",
    },

    # ── Post-layering boot: enable tailscaled ────────────────────────────────
    {
        "name": "noir-firstboot-enable.service",
        "enabled": True,
        "contents": """[Unit]
Description=noir first-boot service enablement (post-layering)
ConditionPathExists=!/var/lib/noir/firstboot.stamp
ConditionPathExists=!/var/lib/noir/firstboot-enabled.stamp
Wants=network-online.target
After=network-online.target noir-firstboot-install.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/systemctl enable --now tailscaled.service
ExecStart=/usr/bin/touch /var/lib/noir/firstboot-enabled.stamp

[Install]
WantedBy=multi-user.target
""",
    },

    # ── v0.6: post-layering enable cockpit.socket ────────────────────────────
    # Cockpit listens via socket activation, not a long-running daemon — we
    # enable cockpit.socket (the listener), and systemd spawns cockpit-ws on
    # demand when a connection arrives. Default ListenStream=9090 binds
    # dual-stack [::]:9090 (all interfaces) — reachable from LAN+tailnet.
    # Stamp pattern mirrors noir-firstboot-enable.service for idempotency.
    {
        "name": "noir-firstboot-cockpit-enable.service",
        "enabled": True,
        "contents": """[Unit]
Description=noir first-boot Cockpit enablement (post-layering)
ConditionPathExists=!/var/lib/noir/firstboot.stamp
ConditionPathExists=!/var/lib/noir/cockpit-enabled.stamp
Wants=network-online.target
After=network-online.target noir-firstboot-enable.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/systemctl enable --now cockpit.socket
ExecStart=/usr/bin/touch /var/lib/noir/cockpit-enabled.stamp

[Install]
WantedBy=multi-user.target
""",
    },

    # ── Post-install verification (runs once on the second boot) ─────────────
    # Three prior bugs (v0.1 home-mount lost-on-reboot, v0.2 multi-connect=2
    # leaving one NIC unbonded, v0.4 missing wireless-regdb) all share DNA:
    # Butane was verified against documentation, not against a running system.
    # This unit closes the gap. Each ExecStart asserts one invariant; failure
    # logs to journal under tag `noir-verify` and exits non-zero (visible in
    # `systemctl --failed`). Stamp condition makes it idempotent.
    {
        "name": "noir-postinstall-verify.service",
        "enabled": True,
        "contents": """[Unit]
Description=noir post-install verification of provisioning invariants
ConditionPathExists=!/var/lib/noir/postinstall-verified.stamp
Wants=network-online.target
After=network-online.target noir-firstboot-enable.service NetworkManager.service

[Service]
Type=oneshot
RemainAfterExit=yes
# Each ExecStart asserts one invariant. Failures log to journal with tag
# noir-verify and exit non-zero so the unit shows in `systemctl --failed`.
ExecStart=/bin/sh -c 'test -f /lib/firmware/regulatory.db || { logger -t noir-verify -p err "regulatory.db missing — wifi will be unavailable"; exit 1; }'
# findmnt -no UUID is checked against the deterministic UUIDs baked
# into storage.filesystems above. UUID comparison is robust across
# device-name changes (nvme0n1p2 vs sda2) and catches v0.1's failure
# mode — data landing on the root fs because the named fs never got
# mounted at /var/home — deterministically. mountpoint -q would have
# also missed: a tmpfs at /var/home would satisfy "is a mount" while
# the real partition sat unmounted.
ExecStart=/bin/sh -c 'test "$(findmnt -no UUID /var/home)" = "a5f7e3c8-9b2d-4e6f-8a1c-3f5b9d7e2a41" || { logger -t noir-verify -p err "/var/home not on noir-home partition (expected UUID a5f7e3c8-…)"; exit 1; }'
ExecStart=/bin/sh -c 'test "$(findmnt -no UUID /var/lib/containers)" = "b8c2d1f4-6a3e-4b9c-a5d7-1e8f2c4b6d83" || { logger -t noir-verify -p err "/var/lib/containers not on noir-containers partition (expected UUID b8c2d1f4-…)"; exit 1; }'
ExecStart=/bin/sh -c 'test "$(findmnt -no UUID /var/log)" = "d1a8b5e3-7f2c-4d8e-9b4a-6e3c1f5d8b2a" || { logger -t noir-verify -p err "/var/log not on noir-log partition (expected UUID d1a8b5e3-…)"; exit 1; }'
ExecStart=/bin/sh -c 'ip link show bond0 >/dev/null 2>&1 && ip link show enp97s0 master bond0 >/dev/null 2>&1 && ip link show enp98s0 master bond0 >/dev/null 2>&1 || { logger -t noir-verify -p err "bond0 missing or not enslaving both NICs"; exit 1; }'
ExecStart=/bin/sh -c 'state=$(nmcli -g GENERAL.STATE device show wlp99s0 2>/dev/null | head -1); case "$state" in ""|*unavailable*|*unmanaged*) logger -t noir-verify -p err "wlp99s0 in error state: ${state:-not present}"; exit 1 ;; *) ;; esac'
ExecStart=/bin/sh -c 'command -v tailscale >/dev/null || { logger -t noir-verify -p err "tailscale binary missing"; exit 1; }'
ExecStart=/bin/sh -c 'test -x /usr/local/bin/noir-wifi || { logger -t noir-verify -p err "noir-wifi script missing or non-executable"; exit 1; }'
ExecStart=/bin/sh -c 'bash -n /usr/local/bin/noir-wifi || { logger -t noir-verify -p err "noir-wifi script has syntax errors"; exit 1; }'
ExecStart=/bin/sh -c '[ "$(sysctl -n net.ipv4.ip_forward)" = "1" ] || { logger -t noir-verify -p err "ip_forward not enabled"; exit 1; }'
# ── v0.6 Cockpit assertions ─────────────────────────────────────────
# cockpit.socket active (socket-activated, not a daemon).
ExecStart=/bin/sh -c 'systemctl is-active --quiet cockpit.socket || { logger -t noir-verify -p err "cockpit.socket not active"; exit 1; }'
# Listener on any-interface :9090 (accepts wildcard *, 0.0.0.0, or [::]
# forms — rejects loopback-only or single-interface bindings, which
# would break LAN+tailnet reachability).
ExecStart=/bin/sh -c 'ss -tlnH "sport = :9090" | grep -Eq "^LISTEN.*[[:space:]](\\\\*|0\\\\.0\\\\.0\\\\.0|\\\\[::\\\\]):9090[[:space:]]" || { logger -t noir-verify -p err "cockpit not listening on any-interface :9090"; exit 1; }'
# All 7 cockpit subpackages present in the deployment.
ExecStart=/bin/sh -c 'rpm -q cockpit-bridge cockpit-system cockpit-ws cockpit-podman cockpit-ostree cockpit-networkmanager cockpit-files >/dev/null || { logger -t noir-verify -p err "cockpit subpackage(s) missing"; exit 1; }'
# cockpit.conf present with LoginTo = false (CVE-2026-4631 mitigation).
ExecStart=/bin/sh -c 'test -f /etc/cockpit/cockpit.conf && grep -Eq "^[[:space:]]*LoginTo[[:space:]]*=[[:space:]]*false[[:space:]]*$" /etc/cockpit/cockpit.conf || { logger -t noir-verify -p err "cockpit.conf missing or LoginTo!=false"; exit 1; }'
ExecStart=/usr/bin/touch /var/lib/noir/postinstall-verified.stamp
ExecStart=/bin/sh -c 'logger -t noir-verify -p notice "all invariants verified"'

[Install]
WantedBy=multi-user.target
""",
    },
    {
        "name": "noir-firstboot-setup.service",
        "enabled": True,
        "contents": """[Unit]
Description=noir first-boot setup orchestrator (restore-or-publish, boot 2+)
ConditionPathExists=!/var/lib/noir/firstboot.stamp
Wants=network-online.target
After=noir-firstboot-enable.service NetworkManager-wait-online.service network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/bin/noir-firstboot-setup

[Install]
WantedBy=multi-user.target
""",
    },

    # ── Boot-2+ interactive setup on tty1 (first-one-wins vs the SSH path) ────
    # Getty-ordered oneshot that takes over /dev/tty1 (Conflicts=getty@tty1)
    # with StandardInput=tty so noir-setup can prompt at the local console.
    # Gated on the same boot-2 condition PLUS the .setup-done sentinel absence
    # and /dev/tty1 presence — so it only fires when a console exists and setup
    # isn't already done. noir-setup's flock + sentinel make this and the SSH
    # path mutually exclusive; the loser sees the sentinel and no-ops.
    {
        "name": "noir-setup-tty1.service",
        "enabled": True,
        "contents": """[Unit]
Description=noir first-boot interactive setup on tty1 (boot 2+, first-one-wins)
ConditionPathExists=!/var/lib/noir/firstboot.stamp
ConditionPathExists=!/var/home/.noir-secrets/.setup-done
ConditionPathExists=/dev/tty1
After=noir-firstboot-setup.service getty.target
Conflicts=getty@tty1.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/noir-setup
StandardInput=tty
StandardOutput=tty
StandardError=tty
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes

[Install]
WantedBy=multi-user.target
""",
    },
    {
        "name": "noir-table100.service",
        "enabled": True,
        "contents": """[Unit]
Description=noir routing-table-100 default-route keeper (bond0 underlay pin)
After=NetworkManager-wait-online.service tailscaled.service
Wants=NetworkManager-wait-online.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/noir-table100
""",
    },

    # The timer fires 30s after boot (let NM settle the bond + gateway), then
    # every 5 min thereafter. Persistent=true catches up a missed run after the
    # machine was asleep/off. Install into timers.target so `systemctl enable`
    # arms it on every boot.
    {
        "name": "noir-table100.timer",
        "enabled": True,
        "contents": """[Unit]
Description=noir routing-table-100 default-route keeper schedule

[Timer]
OnBootSec=30s
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
""",
    },
]


# ─── 4. Users ────────────────────────────────────────────────────────────────
# v0.6: passwordHash on `core` — required for Cockpit web auth via PAM.
# Substitute with a real bcrypt hash before building. Mint locally on macOS
# (htpasswd ships in Apache, on the system PATH — no brew install needed):
#     read -rs PW && printf '%s' "$PW" | htpasswd -niB -C 12 core | cut -d: -f2; unset PW
# sync_check.py fail-fasts on the placeholder string and on any non-bcrypt
# prefix ($2a$ / $2b$ / $2y$). Same hash MUST appear in noir.bu line ~80.
users = [
    {
        "name": "core",
        "groups": ["wheel", "sudo", "adm", "systemd-journal"],
        "sshAuthorizedKeys": [
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICUwWXf++jqM/BFKhM2vpvggqmluPgVQFMmmApOsAk/h bear-alchemist_GitHub",
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPrrgPxGleG3LBIf0STSnu1c1910KMBIaxubjx8oVUe6 bear-alchemist_1Password",
        ],
    }
]


# ─── 5. Kernel arguments ─────────────────────────────────────────────────────
# v0.7: empty. Earlier versions declared `amd_iommu=on iommu=pt` as
# defensive placeholders for libvirt VFIO passthrough (libvirt removed in
# v0.3) and for Strix Halo NPU/iGPU SVA paths. The kernel auto-enables
# AMD IOMMU when BIOS exposes it; AMD XDNA NPU SVA's IOMMU dependency is
# satisfied by BIOS=Enabled (Strix Halo default), not by kernel cmdline.
# Less-is-more: omit redundant flags. Re-add via Ignition if a future
# workload needs an explicit override.
kernel_arguments = {"shouldExist": []}


# ─── 6. Assemble the Ignition document ───────────────────────────────────────
# v0.7: kernelArguments key intentionally omitted from the assembly when
# shouldExist is empty. Keeps the output Ignition JSON minimal — Ignition
# parses an absent kernelArguments block as "no changes," matching the
# noir.bu source where the kernel_arguments block has no entries.
ignition = {
    "ignition": {"version": "3.5.0"},
    "passwd": {"users": users},
    "storage": {
        "disks": disks,
        "filesystems": filesystems,
        "files": files,
    },
    "systemd": {"units": units},
}
if kernel_arguments["shouldExist"]:
    ignition["kernelArguments"] = kernel_arguments

out = json.dumps(ignition, indent=2, ensure_ascii=False)

OUT_NAME = f"noir-{VARIANT}.ign"
OUT_PATH = os.path.join(HERE, OUT_NAME)

with open(OUT_PATH, "w") as f:
    f.write(out + "\n")

# ─── Summary ─────────────────────────────────────────────────────────────────
print(f"wrote {OUT_NAME}: {len(out)} bytes")
print(f"  variant       : {VARIANT}")
print(f"  wipeTable     : {WIPE}")
print(f"  wipeFilesystem: {WIPE} (on all {len(filesystems)} fs)")
print(f"  files         : {len(files)}")
_mounts      = sum(1 for u in units if u["name"].endswith(".mount"))
_bodied      = sum(1 for u in units if not u["name"].endswith(".mount") and u.get("contents"))
_dropin_only = sum(1 for u in units if u.get("dropins") and not u.get("contents"))
_enable_only = len(units) - _mounts - _bodied - _dropin_only
print(f"  units         : {len(units)} ({_mounts} mount + {_bodied} bodied + "
      f"{_dropin_only} dropin-only + {_enable_only} enable-only)")
print(f"  path          : {OUT_PATH}")
