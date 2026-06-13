#!/bin/bash
# =============================================================================
# noir diagnostic v1.0 — Fedora CoreOS 44 deployment health probe
# =============================================================================
# Target host : Minisforum MS-S1 MAX (AMD Ryzen AI Max+ 395 / Strix Halo)
# Base ISO    : fedora-coreos-44.20260523.3.1-live-iso.x86_64.iso
# Kernel band : 6.19.x (FCOS 44 base; F44 did NOT move to 7.0)
# FCOS major  : 44
#
# Validates ten layers of the noir stack (FCOS health, storage, bond LACP,
# Wi-Fi/MT7925, Tailscale, SSH, Cockpit, Podman, noir orchestration, watch-
# items). All checks are local — no tailnet, no DNS, no external network. Run
# as root for full coverage; non-root works but loses journalctl -b 0 access
# on hardened journald defaults.
#
# Exit code: 0 iff zero FAILs (WARN/INFO are advisory). Runtime ≤ 30s.
# Tools used: rpm-ostree, systemctl, journalctl, ip, ethtool, ss, nmcli,
# rpm, podman, tailscale, curl, ausearch, sysctl, df, findmnt, awk, grep,
# sed — all in FCOS base or pulled in by the 12 layered packages.
# =============================================================================

set -u
LC_ALL=C
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin

VERBOSE=0
[ "${1:-}" = "--verbose" ] && VERBOSE=1

# ──────────────────────────────────────────────────────────────────────────────
# Counters and section bookkeeping
# ──────────────────────────────────────────────────────────────────────────────
PASS_TOTAL=0; FAIL_TOTAL=0; WARN_TOTAL=0; INFO_TOTAL=0
PASS_SEC=0;   FAIL_SEC=0;   WARN_SEC=0;   INFO_SEC=0
declare -a SECTION_NAMES SECTION_PASS SECTION_FAIL SECTION_WARN SECTION_INFO

# ANSI: only emit colour when stdout is a terminal.
if [ -t 1 ]; then
  C_PASS=$'\e[32m'; C_FAIL=$'\e[31m'; C_WARN=$'\e[33m'; C_INFO=$'\e[36m'; C_HDR=$'\e[1m'; C_END=$'\e[0m'
else
  C_PASS=; C_FAIL=; C_WARN=; C_INFO=; C_HDR=; C_END=
fi

emit() {
  # emit <PASS|FAIL|WARN|INFO> <message>
  local lvl="$1"; shift
  local msg="$*"
  case "$lvl" in
    PASS) printf "  [%sPASS%s] %s\n" "$C_PASS" "$C_END" "$msg"; PASS_SEC=$((PASS_SEC+1));;
    FAIL) printf "  [%sFAIL%s] %s\n" "$C_FAIL" "$C_END" "$msg"; FAIL_SEC=$((FAIL_SEC+1));;
    WARN) printf "  [%sWARN%s] %s\n" "$C_WARN" "$C_END" "$msg"; WARN_SEC=$((WARN_SEC+1));;
    INFO) printf "  [%sINFO%s] %s\n" "$C_INFO" "$C_END" "$msg"; INFO_SEC=$((INFO_SEC+1));;
  esac
}

verbose() { [ "$VERBOSE" -eq 1 ] && printf "        %s\n" "$1" || true; }

section() {
  # Roll prior section's counters into totals and the per-section table.
  if [ -n "${CURRENT_SECTION:-}" ]; then
    SECTION_NAMES+=("$CURRENT_SECTION")
    SECTION_PASS+=("$PASS_SEC"); SECTION_FAIL+=("$FAIL_SEC")
    SECTION_WARN+=("$WARN_SEC"); SECTION_INFO+=("$INFO_SEC")
    PASS_TOTAL=$((PASS_TOTAL+PASS_SEC)); FAIL_TOTAL=$((FAIL_TOTAL+FAIL_SEC))
    WARN_TOTAL=$((WARN_TOTAL+WARN_SEC)); INFO_TOTAL=$((INFO_TOTAL+INFO_SEC))
  fi
  PASS_SEC=0; FAIL_SEC=0; WARN_SEC=0; INFO_SEC=0
  CURRENT_SECTION="$1"
  printf "\n%s%s%s\n" "$C_HDR" "── $1 ──" "$C_END"
}

# ──────────────────────────────────────────────────────────────────────────────
# Header
# ──────────────────────────────────────────────────────────────────────────────
printf "%snoir diagnostic v1.0  ·  FCOS 44 build  ·  $(date -Iseconds)%s\n" "$C_HDR" "$C_END"
printf "host: $(hostname)   kernel: $(uname -r)   uptime: $(uptime -p)\n"
[ "$(id -u)" -ne 0 ] && printf "%s(running non-root — some journal/dmesg checks may be limited)%s\n" "$C_WARN" "$C_END"

# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — FCOS base + deployment health
# ─────────────────────────────────────────────────────────────────────────────
section "Layer 1: FCOS base + deployment health"

# rpm-ostree booted version (expected 44.x).
booted_ver="$(rpm-ostree status --json 2>/dev/null | awk -F'"' '/"version":/ {print $4; exit}')"
if [ -z "$booted_ver" ]; then
  emit FAIL "rpm-ostree status returned no booted version"
elif [[ "$booted_ver" == 44.* ]]; then
  emit PASS "rpm-ostree booted version: $booted_ver (44.x as expected)"
else
  emit FAIL "rpm-ostree booted version: $booted_ver (expected 44.x for v1.0)"
fi
verbose "$(rpm-ostree status 2>/dev/null | sed -n '1,8p')"

# Kernel version (expected 6.19.x — F44 did not move to 7.0).
kernel="$(uname -r)"
if [[ "$kernel" == 6.19.* ]]; then
  emit PASS "kernel: $kernel (6.19.x band as expected)"
elif [[ "$kernel" == 6.20.* ]] || [[ "$kernel" == 7.* ]]; then
  emit WARN "kernel: $kernel (newer than expected; F44 baseline is 6.19.x)"
else
  emit FAIL "kernel: $kernel (expected 6.19.x)"
fi

# systemctl --failed must be empty.
failed_units="$(systemctl --failed --no-legend --plain 2>/dev/null | awk '{print $1}' | grep -v '^$' || true)"
if [ -z "$failed_units" ]; then
  emit PASS "systemctl --failed: empty"
else
  emit FAIL "systemctl --failed: $(echo "$failed_units" | wc -l) failed unit(s) — $(echo "$failed_units" | tr '\n' ',' | sed 's/,$//')"
  verbose "$failed_units"
fi

# Error-priority journal count, current boot. Cosmetic baseline ≈ small
# double-digit count from ACPI + mt7925 + dbus-broker. Hard fail above 100.
errs="$(journalctl -p err -b 0 --no-pager 2>/dev/null | wc -l)"
if [ "$errs" -lt 50 ]; then
  emit INFO "journalctl -p err -b 0: $errs lines (baseline cosmetic noise)"
elif [ "$errs" -lt 100 ]; then
  emit WARN "journalctl -p err -b 0: $errs lines (above expected cosmetic baseline)"
else
  emit FAIL "journalctl -p err -b 0: $errs lines (well above cosmetic baseline; investigate)"
fi

# Zincati polling for upgrades.
if systemctl is-active --quiet zincati.service; then
  emit PASS "zincati.service: active (polling for FCOS updates)"
else
  emit FAIL "zincati.service: not active ($(systemctl is-active zincati.service 2>/dev/null))"
fi

# Disk pressure across noir's writable filesystems.
# Note: / on FCOS 43+ is mounted as composefs — a read-only verity-protected
# manifest of the booted ostree deployment, sized at the manifest itself
# (~5–6 MB) and therefore 100% utilised by design. Checking df / is a
# false-positive trap; we check /sysroot (the real OS partition holding the
# ostree repo + every staged commit) instead. Defensive: also skip any
# overlay/composefs fstype if a future FCOS version surfaces one elsewhere.
for mp in /sysroot /var/home /var/lib/containers /var/log; do
  if ! mountpoint -q "$mp" 2>/dev/null; then
    emit WARN "disk usage [$mp]: not a mountpoint (covered by Layer 2)"
    continue
  fi
  fstype="$(findmnt -no FSTYPE "$mp" 2>/dev/null)"
  case "$fstype" in
    composefs|overlay)
      emit INFO "disk usage [$mp]: ${fstype} (read-only manifest, 100% by design — skipped)"
      continue
      ;;
  esac
  pct="$(df -P "$mp" 2>/dev/null | awk 'NR==2 {gsub("%",""); print $5}')"
  if [ -z "$pct" ]; then
    emit WARN "disk usage [$mp]: df returned no data"
  elif [ "$pct" -lt 80 ]; then
    emit PASS "disk usage [$mp]: ${pct}%"
  elif [ "$pct" -lt 90 ]; then
    emit WARN "disk usage [$mp]: ${pct}% (over 80%, monitor)"
  else
    emit FAIL "disk usage [$mp]: ${pct}% (critical)"
  fi
done

# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — Storage layout (Ignition-declared XFS partitions)
# ─────────────────────────────────────────────────────────────────────────────
section "Layer 2: Storage layout (Ignition-declared)"

# UUIDs are baked into noir.bu storage.filesystems. If a partition is missing
# its UUID won't match — same failure mode noir-postinstall-verify catches.
declare -A FS_UUID=(
  [/var/home]=a5f7e3c8-9b2d-4e6f-8a1c-3f5b9d7e2a41
  [/var/lib/containers]=b8c2d1f4-6a3e-4b9c-a5d7-1e8f2c4b6d83
  [/var/log]=d1a8b5e3-7f2c-4d8e-9b4a-6e3c1f5d8b2a
)

for mp in /var/home /var/lib/containers /var/log; do
  expected="${FS_UUID[$mp]}"
  actual="$(findmnt -no UUID "$mp" 2>/dev/null)"
  fstype="$(findmnt -no FSTYPE "$mp" 2>/dev/null)"
  if [ -z "$actual" ]; then
    emit FAIL "$mp: not mounted"
    continue
  fi
  if [ "$fstype" != "xfs" ]; then
    emit FAIL "$mp: filesystem is $fstype (expected xfs)"
    continue
  fi
  if [ "$actual" = "$expected" ]; then
    emit PASS "$mp: xfs UUID=$actual"
  else
    emit FAIL "$mp: UUID=$actual (expected $expected)"
  fi
done

# All three mount units active. systemd escapes /var/home to var-home.mount.
for mp in /var/home /var/lib/containers /var/log; do
  unit="$(systemd-escape --suffix=mount --path "$mp")"
  if systemctl is-active --quiet "$unit"; then
    emit PASS "$unit: active"
  else
    emit FAIL "$unit: $(systemctl is-active "$unit" 2>/dev/null)"
  fi
done

# ─────────────────────────────────────────────────────────────────────────────
# Layer 3 — Network: bond0 LACP (UniFi UDM Pro SE upstream, 802.3ad slow)
# ─────────────────────────────────────────────────────────────────────────────
section "Layer 3: Network — bond0 LACP"

if [ ! -e /proc/net/bonding/bond0 ]; then
  emit FAIL "bond0: not present (no /proc/net/bonding/bond0)"
else
  bonding="$(cat /proc/net/bonding/bond0)"

  mode="$(echo "$bonding" | awk -F': ' '/^Bonding Mode/ {print $2; exit}')"
  if echo "$mode" | grep -q "IEEE 802.3ad"; then
    emit PASS "bond0 mode: $mode"
  else
    emit FAIL "bond0 mode: $mode (expected IEEE 802.3ad / LACP)"
  fi

  rate="$(echo "$bonding" | awk -F': ' '/^LACP active|^LACP rate/ {print $2}' | head -1)"
  if [ "$rate" = "slow" ]; then
    emit PASS "bond0 LACP rate: slow"
  elif [ -n "$rate" ]; then
    emit WARN "bond0 LACP rate: $rate (noir.bu sets 'slow'; UniFi default is fast)"
  fi

  # Both NICs enslaved. enp97s0 + enp98s0 are RTL8127A 10 GbE ports per
  # noir.bu storage docs.
  for nic in enp97s0 enp98s0; do
    if ip link show "$nic" master bond0 >/dev/null 2>&1; then
      emit PASS "bond0 slave: $nic enslaved"
    else
      emit FAIL "bond0 slave: $nic NOT enslaved (check cable / UniFi LAG config)"
    fi
  done

  # Aggregator IDs must match across slaves for LACP to be up.
  agg_ids="$(echo "$bonding" | awk '/^Aggregator ID:/ {print $3}' | sort -u)"
  if [ "$(echo "$agg_ids" | wc -l)" -eq 1 ] && [ -n "$agg_ids" ]; then
    emit PASS "bond0 aggregator: matching ID $agg_ids on both slaves"
  else
    emit FAIL "bond0 aggregator: mismatched IDs ($(echo "$agg_ids" | tr '\n' ' '))"
  fi

  # Partner Churn State must be `none`. `churned` ⇒ peer not negotiating.
  # Source: Documentation/networking/bonding.rst (Linux kernel docs).
  churn="$(echo "$bonding" | awk -F': ' '/Partner Churn State/ {print $2}' | sort -u | tr '\n' ',' | sed 's/,$//')"
  if [ "$churn" = "none" ]; then
    emit PASS "bond0 partner churn: none (LACP healthy)"
  else
    emit FAIL "bond0 partner churn: $churn (expected 'none' — LACP not negotiating)"
  fi

  # ethtool bond0 — Speed=20000 means both 10G slaves are LACP-up; 10000
  # means single-link / degraded. Source: drivers/net/bonding/bond_main.c
  # bond_ethtool_get_link_ksettings() sums slave speeds when 802.3ad is up.
  speed="$(ethtool bond0 2>/dev/null | awk -F': ' '/Speed/ {print $2}')"
  case "$speed" in
    20000Mb/s)  emit PASS "bond0 ethtool Speed: $speed (both 10G NICs aggregating)" ;;
    10000Mb/s)  emit FAIL "bond0 ethtool Speed: $speed (degraded — one slave dropped)" ;;
    *)          emit FAIL "bond0 ethtool Speed: ${speed:-<empty>} (expected 20000Mb/s)" ;;
  esac

  # IPv4 from UDM Pro SE on 10.0.50.0/24.
  bond_ip="$(ip -4 -o addr show dev bond0 2>/dev/null | awk '{print $4}' | head -1)"
  if [ -z "$bond_ip" ]; then
    emit FAIL "bond0: no IPv4 address (DHCP from UDM Pro SE failed)"
  elif [[ "$bond_ip" == 10.0.50.* ]]; then
    emit PASS "bond0 IPv4: $bond_ip (UDM Pro SE LAN)"
  else
    emit WARN "bond0 IPv4: $bond_ip (outside 10.0.50.0/24 — verify UniFi network)"
  fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# Layer 4 — Network: Wi-Fi (MT7925 + wireless-regdb)
# ─────────────────────────────────────────────────────────────────────────────
section "Layer 4: Network — Wi-Fi (MT7925)"

# Five layered packages required for wifi to come up. Note: NetworkManager
# itself is in FCOS base; only the wifi plugin is layered.
for p in mt7xxx-firmware wireless-regdb NetworkManager-wifi wpa_supplicant tailscale; do
  if rpm -q "$p" >/dev/null 2>&1; then
    emit PASS "package: $p ($(rpm -q --qf '%{VERSION}-%{RELEASE}' "$p"))"
  else
    emit FAIL "package: $p NOT installed"
  fi
done

# regulatory.db is the smoking-gun missing on FCOS without wireless-regdb.
# Source: noir_fcos_wireless_regdb memory + linux-wireless mailing list.
if [ -f /lib/firmware/regulatory.db ]; then
  emit PASS "/lib/firmware/regulatory.db present (radio can lock to a regdomain)"
else
  emit FAIL "/lib/firmware/regulatory.db MISSING (radio will be 'unavailable')"
fi

# wlp99s0 device state — FCOS strips wifi support such that without
# wireless-regdb the device shows up as 'unavailable'.
wlan_state="$(nmcli -g GENERAL.STATE device show wlp99s0 2>/dev/null | head -1)"
if [ -z "$wlan_state" ]; then
  emit FAIL "wlp99s0: device not present per nmcli (driver load failure?)"
elif echo "$wlan_state" | grep -qiE 'unavailable|unmanaged'; then
  emit FAIL "wlp99s0: state='$wlan_state' (regdb / firmware / NM-wifi issue)"
else
  emit PASS "wlp99s0: state='$wlan_state'"
fi

# Wi-Fi firmware build-time stamp. mt7925 logs "WM Firmware Version: __,
# Build Time: <timestamp>" at probe.
wifi_bt="$(dmesg 2>/dev/null | grep -m1 -oE 'mt7925e.*Build Time: [0-9]+' | awk '{print $NF}')"
if [ -z "$wifi_bt" ]; then
  emit WARN "MT7925 wifi firmware: build-time stamp not found in dmesg"
elif [ "$wifi_bt" -ge 20260106153120 ] 2>/dev/null; then
  emit PASS "MT7925 wifi firmware build-time: $wifi_bt (≥ 20260106153120)"
else
  emit WARN "MT7925 wifi firmware build-time: $wifi_bt (older than 20260106153120 baseline)"
fi
verbose "$(dmesg 2>/dev/null | grep -i 'mt7925.*firmware' | head -5)"

# BT firmware + setup-completion line.
bt_bt="$(dmesg 2>/dev/null | grep -m1 -oE 'Bluetooth.*hci0.*Build [Tt]ime[: ]+[0-9]+' | grep -oE '[0-9]{14}$')"
if [ -z "$bt_bt" ]; then
  emit INFO "MT7925 BT firmware: build-time stamp not found (BT may be disabled in BIOS)"
elif [ "$bt_bt" -ge 20260106153314 ] 2>/dev/null; then
  emit PASS "MT7925 BT firmware build-time: $bt_bt (≥ 20260106153314)"
else
  emit WARN "MT7925 BT firmware build-time: $bt_bt (older than baseline)"
fi
if dmesg 2>/dev/null | grep -q 'Bluetooth: hci0: Device setup in'; then
  emit PASS "BT setup completed (hci0 Device setup line present)"
else
  emit INFO "BT setup not completed in dmesg (BT may be disabled in BIOS)"
fi

# Wi-Fi connection profiles laid down by Ignition (generic slots, no SSID baked).
for prof in wifi-primary wifi-secondary wifi-tertiary; do
  if nmcli -g NAME connection show 2>/dev/null | grep -qx "$prof"; then
    emit PASS "NM connection profile: $prof loaded"
  else
    emit FAIL "NM connection profile: $prof NOT loaded"
  fi
done

# noir-wifi helper script integrity.
if [ ! -e /usr/local/bin/noir-wifi ]; then
  emit FAIL "/usr/local/bin/noir-wifi: missing"
elif [ ! -x /usr/local/bin/noir-wifi ]; then
  emit FAIL "/usr/local/bin/noir-wifi: present but not executable"
elif bash -n /usr/local/bin/noir-wifi 2>/dev/null; then
  emit PASS "/usr/local/bin/noir-wifi: present, executable, syntax valid"
else
  emit FAIL "/usr/local/bin/noir-wifi: syntax errors (bash -n failed)"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Layer 5 — Network: Tailscale
# ─────────────────────────────────────────────────────────────────────────────
section "Layer 5: Network — Tailscale"

if command -v tailscale >/dev/null 2>&1; then
  emit PASS "tailscale binary: $(tailscale version 2>/dev/null | head -1)"
else
  emit FAIL "tailscale binary: not in PATH"
fi

if systemctl is-active --quiet tailscaled.service; then
  emit PASS "tailscaled.service: active"
else
  emit FAIL "tailscaled.service: $(systemctl is-active tailscaled.service 2>/dev/null)"
fi

# Forwarding sysctls — required for subnet routing (10.0.50.0/24 advertised).
# noir is a subnet router only (no exit node), so ip_forward is still needed;
# no rp_filter change is made (that was an exit-node-only requirement).
# Source: Tailscale KB articles 1019 and 1103.
v4f="$(sysctl -n net.ipv4.ip_forward 2>/dev/null)"
v6f="$(sysctl -n net.ipv6.conf.all.forwarding 2>/dev/null)"
[ "$v4f" = "1" ] && emit PASS "net.ipv4.ip_forward = 1" \
                  || emit FAIL "net.ipv4.ip_forward = ${v4f:-<unset>} (expected 1)"
[ "$v6f" = "1" ] && emit PASS "net.ipv6.conf.all.forwarding = 1" \
                  || emit FAIL "net.ipv6.conf.all.forwarding = ${v6f:-<unset>} (expected 1)"

# Underlay pin — Tailscale's own marked traffic (fwmark 0x80000) is policy-
# routed to bond0 via dedicated table 100 (default via 10.0.50.1). This keeps
# the tailnet underlay on the wired bond even when a metric-50 Wi-Fi slot owns
# the main default route. Laid down by the bond0 NM keyfile ([ipv4]/[ipv6]
# routing-rule priority 5200 + route default via 10.0.50.1 table 100).
if ip route show table 100 2>/dev/null | grep -qE '^default via 10\.0\.50\.1'; then
  emit PASS "underlay pin: table 100 default via 10.0.50.1 present"
else
  emit FAIL "underlay pin: table 100 has no 'default via 10.0.50.1' route"
fi
if ip rule show 2>/dev/null | grep -qiE 'fwmark 0x80000(/0xff0000)?\b.*lookup (100|table 100)'; then
  emit PASS "underlay pin: fwmark 0x80000 routing-rule → table 100 present"
else
  emit FAIL "underlay pin: no fwmark 0x80000 routing-rule pointing at table 100"
fi

# tailscale-udp-gro.service is a one-shot RemainAfterExit unit; it should
# read 'inactive (dead)' or 'active (exited)' depending on systemd flavour,
# and its journal record should show ExecStart succeeded. KB 1320.
gro_state="$(systemctl show -p ActiveState,SubState --value tailscale-udp-gro.service 2>/dev/null | tr '\n' ' ')"
if echo "$gro_state" | grep -qE 'active exited|inactive dead'; then
  if journalctl -u tailscale-udp-gro.service -b 0 --no-pager 2>/dev/null | grep -q 'tailscale-udp-gro.service: Deactivated successfully\|Started'; then
    emit PASS "tailscale-udp-gro.service: ran (state=$gro_state)"
  else
    emit WARN "tailscale-udp-gro.service: state=$gro_state but no journal record this boot"
  fi
else
  emit FAIL "tailscale-udp-gro.service: state=$gro_state"
fi

# tailnet status — script must work pre-`tailscale up`.
ts_status="$(tailscale status --json 2>/dev/null)"
if [ -z "$ts_status" ]; then
  ts_text="$(tailscale status 2>&1 | head -3)"
  if echo "$ts_text" | grep -qiE 'not logged in|stopped|NeedsLogin'; then
    emit INFO "tailnet: not logged in (expected pre-\`tailscale up\`)"
  else
    emit WARN "tailnet: status query failed — $(echo "$ts_text" | tr '\n' ' ')"
  fi
else
  ts_self_ip="$(echo "$ts_status" | grep -oE '"TailscaleIPs"[^]]*]' | grep -oE '"[0-9.]+"' | head -1 | tr -d '"')"
  ts_peers="$(echo "$ts_status" | grep -oE '"Peer"\s*:\s*\{[^}]*\}' | wc -l)"
  if [ -n "$ts_self_ip" ]; then
    emit PASS "tailnet: this host = $ts_self_ip; visible peers = $ts_peers"
  else
    emit WARN "tailnet: status JSON returned but no TailscaleIPs found"
  fi
fi

# Tailscaled ordered After=systemd-resolved (either Fedora-shipped or noir
# drop-in). Without this, /etc/resolv.conf is not yet stable when tailscaled
# starts and DNS suffix resolution misfires.
if systemctl cat tailscaled.service 2>/dev/null | grep -qE '^After=.*systemd-resolved\.service'; then
  emit PASS "tailscaled.service ordered After=systemd-resolved.service"
else
  emit FAIL "tailscaled.service NOT ordered After=systemd-resolved.service (DNS race)"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Layer 6 — SSH access
# ─────────────────────────────────────────────────────────────────────────────
section "Layer 6: SSH access"

if systemctl is-active --quiet sshd.service; then
  emit PASS "sshd.service: active"
else
  emit FAIL "sshd.service: $(systemctl is-active sshd.service 2>/dev/null)"
fi

# Listening on 22 (any-interface).
if ss -tlnH 2>/dev/null | awk '{print $4}' | grep -qE ':22$'; then
  emit PASS "sshd: listening on :22"
else
  emit FAIL "sshd: NOT listening on port 22"
fi

# core authorized_keys.d/ignition must hold >=1 SSH key. Keys are injected at
# BUILD time from the account's GitHub-published set (github.com/oso-gato.keys),
# each tagged by a SHA256 fingerprint (oSo/Alchemist/Fatima/…). The exact set is
# whatever was current at build; the invariant that matters is "not zero" —
# passwordless core + key-only SSH means zero keys = an unreachable host.
ak="/var/home/core/.ssh/authorized_keys.d/ignition"
if [ -f "$ak" ]; then
  nkeys=$(grep -cE '^(ssh-|ecdsa-|sk-)' "$ak" 2>/dev/null || true)
  if [ "${nkeys:-0}" -ge 1 ]; then
    tags=$(awk '{print $NF}' "$ak" 2>/dev/null | paste -sd, - 2>/dev/null)
    emit PASS "core authorized_keys.d/ignition: $nkeys key(s) present (${tags:-untagged})"
  else
    emit FAIL "core authorized_keys.d/ignition: present but holds 0 SSH keys (host unreachable)"
  fi
else
  emit FAIL "core authorized_keys.d/ignition: not found at $ak"
fi

# Confirm sshd accepts publickey (host key reachable on loopback).
if ssh-keyscan -T 3 -t ed25519 127.0.0.1 2>/dev/null | grep -q 'ssh-ed25519'; then
  emit PASS "ssh-keyscan localhost: host key reachable (sshd answering)"
else
  emit WARN "ssh-keyscan localhost: did not return ed25519 host key in 3s"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Layer 7 — Cockpit
# ─────────────────────────────────────────────────────────────────────────────
section "Layer 7: Cockpit web admin"

for p in cockpit-bridge cockpit-system cockpit-ws cockpit-podman cockpit-ostree cockpit-networkmanager cockpit-files; do
  if rpm -q "$p" >/dev/null 2>&1; then
    emit PASS "package: $p ($(rpm -q --qf '%{VERSION}-%{RELEASE}' "$p"))"
  else
    emit FAIL "package: $p NOT installed"
  fi
done

# cockpit-ws-selinux is pulled in via Recommends from cockpit-ws. Without it
# you hit the cockpit#22481 cockpit-tls AVC.
if rpm -q cockpit-ws-selinux >/dev/null 2>&1; then
  emit PASS "package: cockpit-ws-selinux (transitive Recommends from cockpit-ws)"
else
  emit WARN "package: cockpit-ws-selinux missing (cockpit#22481 SELinux denial likely)"
fi

if systemctl is-active --quiet cockpit.socket; then
  emit PASS "cockpit.socket: active"
else
  emit FAIL "cockpit.socket: $(systemctl is-active cockpit.socket 2>/dev/null)"
fi

# Listening on 9090 on any-interface (NOT 127.0.0.1-only).
if ss -tlnH 2>/dev/null | awk '{print $4}' | grep -qE '^(\*|0\.0\.0\.0|\[?::\]?):9090$'; then
  emit PASS "cockpit: listening on *:9090 (any-interface)"
elif ss -tlnH 2>/dev/null | awk '{print $4}' | grep -qE '^127\.0\.0\.1:9090$'; then
  emit FAIL "cockpit: listening only on 127.0.0.1:9090 (LAN access broken)"
else
  emit FAIL "cockpit: not listening on :9090 anywhere"
fi

# /etc/cockpit/cockpit.conf with LoginTo = false (CVE-2026-4631 mitigation).
if [ -f /etc/cockpit/cockpit.conf ] && \
   grep -Eq '^[[:space:]]*LoginTo[[:space:]]*=[[:space:]]*false[[:space:]]*$' /etc/cockpit/cockpit.conf; then
  emit PASS "/etc/cockpit/cockpit.conf: LoginTo = false"
else
  emit FAIL "/etc/cockpit/cockpit.conf: missing or LoginTo != false"
fi

# Reachable on loopback. -k accepts self-signed; -sSI fetches headers only.
http_code="$(curl -k -sS -o /dev/null -w '%{http_code}' --max-time 5 -I https://127.0.0.1:9090 2>/dev/null || echo 000)"
if [ "$http_code" = "200" ] || [ "$http_code" = "302" ]; then
  emit PASS "cockpit https://127.0.0.1:9090: HTTP $http_code"
else
  emit FAIL "cockpit https://127.0.0.1:9090: HTTP $http_code (expected 200/302)"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Layer 8 — Podman
# ─────────────────────────────────────────────────────────────────────────────
section "Layer 8: Podman"

if pv="$(podman --version 2>/dev/null)"; then
  emit PASS "$pv"
else
  emit FAIL "podman --version: failed"
fi

# podman.socket is socket-activated; usually 'listening (waiting)'.
if systemctl is-active --quiet podman.socket; then
  emit PASS "podman.socket: active"
else
  emit WARN "podman.socket: $(systemctl is-active podman.socket 2>/dev/null) (FCOS sometimes lazy-activates)"
fi

if [ -S /run/podman/podman.sock ]; then
  emit PASS "/run/podman/podman.sock: socket present"
else
  emit WARN "/run/podman/podman.sock: not present (will spawn on first use)"
fi

# Containers root must point into noir-containers partition.
gr="$(podman info 2>/dev/null | awk '/graphRoot:/ {print $2; exit}')"
if [ -z "$gr" ]; then
  emit FAIL "podman info: no graphRoot reported"
elif [[ "$gr" == /var/lib/containers* ]]; then
  emit PASS "podman graphRoot: $gr"
else
  emit WARN "podman graphRoot: $gr (expected /var/lib/containers/...)"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Layer 9 — noir orchestration units
# ─────────────────────────────────────────────────────────────────────────────
section "Layer 9: noir-specific orchestration"

# The four one-shot units: each exits 0 once the work is done; ConditionPath
# stamps cause subsequent boots to no-op.
for u in noir-firstboot-install.service \
         noir-firstboot-enable.service \
         noir-firstboot-cockpit-enable.service \
         noir-postinstall-verify.service; do
  result="$(systemctl show -p Result --value "$u" 2>/dev/null)"
  exit_code="$(systemctl show -p ExecMainStatus --value "$u" 2>/dev/null)"
  active="$(systemctl is-active "$u" 2>/dev/null)"
  if [ "$result" = "success" ] && [ "$exit_code" = "0" ]; then
    emit PASS "$u: Result=success ExecMainStatus=0 (active=$active)"
  elif [ -z "$result" ] || [ "$result" = "unknown" ]; then
    emit FAIL "$u: not present or never ran"
  else
    emit FAIL "$u: Result=$result ExecMainStatus=${exit_code:-?}"
  fi
done

# Stamps: post-Stage 1 the install stamp goes away; the three completion
# stamps stay forever to mark idempotency.
for stamp in firstboot-enabled.stamp cockpit-enabled.stamp postinstall-verified.stamp; do
  if [ -f "/var/lib/noir/$stamp" ]; then
    emit PASS "stamp: /var/lib/noir/$stamp present"
  else
    emit FAIL "stamp: /var/lib/noir/$stamp MISSING"
  fi
done
# firstboot.stamp must be GONE post-install.
if [ ! -e /var/lib/noir/firstboot.stamp ]; then
  emit PASS "stamp: /var/lib/noir/firstboot.stamp removed (Stage 1 complete)"
else
  emit FAIL "stamp: /var/lib/noir/firstboot.stamp still present (Stage 1 incomplete)"
fi

# noir-verify success line in journal.
if journalctl -t noir-verify -b 0 --no-pager 2>/dev/null | grep -q 'all invariants verified'; then
  emit PASS "journal: noir-verify 'all invariants verified' present"
else
  emit FAIL "journal: noir-verify success line missing"
fi
verbose "$(journalctl -t noir-verify -b 0 --no-pager 2>/dev/null | tail -10)"

# ─────────────────────────────────────────────────────────────────────────────
# Layer 10 — Watch-items (cosmetic, not failure-blocking)
# ─────────────────────────────────────────────────────────────────────────────
section "Layer 10: Watch-items (cosmetic)"

# AE_NOT_FOUND on GPP9.DEV0 — a Strix Halo platform firmware quirk; expected
# once per boot. Reference: noir prior boot data (this fires during ACPI
# device-init when the PCIe slot is empty / disabled).
acpi_n="$(dmesg 2>/dev/null | grep -c 'AE_NOT_FOUND.*GPP9' || true)"
if [ "$acpi_n" -le 5 ]; then
  emit INFO "ACPI AE_NOT_FOUND on GPP9.DEV0: $acpi_n occurrence(s) (expected ~1/boot)"
else
  emit WARN "ACPI AE_NOT_FOUND on GPP9.DEV0: $acpi_n occurrences (higher than expected)"
fi

# mt7925 logs "WM Firmware Version: ____000000" cosmetically at probe time
# when the version field in the firmware header is unset. Not a defect.
mt_n="$(dmesg 2>/dev/null | grep -c 'WM Firmware Version: ____000000' || true)"
if [ "$mt_n" -ge 1 ]; then
  emit INFO "mt7925 'WM Firmware Version: ____000000': $mt_n line(s) (cosmetic)"
else
  emit INFO "mt7925 'WM Firmware Version: ____000000': not seen (unusual but not failing)"
fi

# BT enhanced-setup-sync-conn advertised-but-not-supported notice — one
# line per probe, same on every kernel since 6.4.
bt_n="$(dmesg 2>/dev/null | grep -c 'HCI Enhanced Setup Synchronous Connection command is advertised, but not supported' || true)"
emit INFO "BT 'Enhanced Setup Sync Connection' notice: $bt_n line(s) (firmware quirk)"

# cockpit-tls SELinux denial — cockpit#22481.
if command -v ausearch >/dev/null 2>&1; then
  avc_n="$(ausearch -m AVC -c cockpit-tls 2>/dev/null | grep -c '^type=AVC' || true)"
  if [ "$avc_n" -eq 0 ]; then
    emit PASS "ausearch AVC cockpit-tls: 0 denials"
  else
    emit WARN "ausearch AVC cockpit-tls: $avc_n denial(s) (cockpit#22481)"
  fi
else
  emit INFO "ausearch not available — skipping AVC check"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Final summary table
# ─────────────────────────────────────────────────────────────────────────────
# Roll the last section in.
section "—"
unset 'SECTION_NAMES[-1]' 'SECTION_PASS[-1]' 'SECTION_FAIL[-1]' 'SECTION_WARN[-1]' 'SECTION_INFO[-1]'

printf "\n%s════════════ summary ════════════%s\n" "$C_HDR" "$C_END"
printf "%-50s  %5s %5s %5s %5s\n" "Section" "PASS" "FAIL" "WARN" "INFO"
printf -- "---------------------------------------------------------------------------\n"
for i in "${!SECTION_NAMES[@]}"; do
  printf "%-50s  %5d %5d %5d %5d\n" \
    "${SECTION_NAMES[$i]}" \
    "${SECTION_PASS[$i]}" "${SECTION_FAIL[$i]}" \
    "${SECTION_WARN[$i]}" "${SECTION_INFO[$i]}"
done
printf -- "---------------------------------------------------------------------------\n"
printf "%-50s  %s%5d%s %s%5d%s %s%5d%s %s%5d%s\n" "TOTAL" \
  "$C_PASS" "$PASS_TOTAL" "$C_END" \
  "$C_FAIL" "$FAIL_TOTAL" "$C_END" \
  "$C_WARN" "$WARN_TOTAL" "$C_END" \
  "$C_INFO" "$INFO_TOTAL" "$C_END"

if [ "$FAIL_TOTAL" -eq 0 ]; then
  printf "\n%sresult: HEALTHY%s — 0 FAILs, %d WARNs (advisory)\n" "$C_PASS" "$C_END" "$WARN_TOTAL"
  exit 0
else
  printf "\n%sresult: UNHEALTHY%s — %d FAIL(s); investigate per-section output above\n" "$C_FAIL" "$C_END" "$FAIL_TOTAL"
  exit 1
fi
