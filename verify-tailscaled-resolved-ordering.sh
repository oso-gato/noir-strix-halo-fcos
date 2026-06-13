#!/usr/bin/env bash
# verify-tailscaled-resolved-ordering.sh   (noir v1.0 / FCOS 44)
#
# Decides whether noir.bu's tailscaled.service drop-in
# `/etc/systemd/system/tailscaled.service.d/10-resolved.conf` is now
# REDUNDANT given what the Fedora-packaged tailscale RPM ships in
# /usr/lib/systemd/system/tailscaled.service.
#
# Background: noir's drop-in adds `After=systemd-resolved.service` to
# work around tailscale/tailscale#4934 (MagicDNS race). Tailscale's
# UPSTREAM unit has shipped that After= line since commit ac07ff4
# (2021-06-15). The question on FCOS 44 is whether the Fedora packagers
# kept it (drop-in redundant → safe to remove) or stripped it
# (drop-in still required → keep).
#
# Usage:  sudo bash verify-tailscaled-resolved-ordering.sh
# Exits: 0 = drop-in redundant (safe to remove)
#        1 = drop-in required (keep)
#        2 = tailscale not layered or unit file missing
set -u

UNIT=tailscaled.service
RPM_UNIT=/usr/lib/systemd/system/${UNIT}
DROPIN=/etc/systemd/system/${UNIT}.d/10-resolved.conf
NEED=systemd-resolved.service

C_HDR=$'\033[1;34m'; C_OK=$'\033[0;32m'; C_FAIL=$'\033[0;31m'; C_END=$'\033[0m'
[ -t 1 ] || { C_HDR=""; C_OK=""; C_FAIL=""; C_END=""; }

printf "%s== noir tailscaled After=systemd-resolved ordering probe ==%s\n" "$C_HDR" "$C_END"
echo

# ── 1. Fedora-packaged unit: what does the RPM ship? ──
printf "%s[1] Fedora RPM unit (%s)%s\n" "$C_HDR" "$RPM_UNIT" "$C_END"
if [ ! -f "$RPM_UNIT" ]; then
  printf "%sFAIL%s — %s is missing. Is the tailscale package layered?\n" \
    "$C_FAIL" "$C_END" "$RPM_UNIT"
  exit 2
fi
echo "    After= lines in the bare unit:"
grep -nE '^\s*After=' "$RPM_UNIT" | sed 's/^/      /' \
  || echo "      (no After= directive in bare unit)"
echo

# ── 2. Effective merged unit (main + every drop-in) ──
printf "%s[2] systemctl cat %s (merged: main + drop-ins)%s\n" "$C_HDR" "$UNIT" "$C_END"
systemctl cat "$UNIT" 2>/dev/null \
  | grep -nE '^(# /|After=)' \
  | sed 's/^/    /' \
  || echo "    (systemctl cat returned nothing)"
echo

# ── 3. noir's drop-in presence ──
printf "%s[3] noir drop-in %s%s\n" "$C_HDR" "$DROPIN" "$C_END"
if [ -f "$DROPIN" ]; then
  echo "    PRESENT. Contents:"
  sed 's/^/      /' "$DROPIN"
else
  echo "    ABSENT (already removed, or noir.bu didn't ship it on this build)."
fi
echo

# ── 4. Fully-resolved After= property (post-merge) ──
printf "%s[4] systemctl show — fully-resolved After= property%s\n" "$C_HDR" "$C_END"
RESOLVED=$(systemctl show "$UNIT" --property=After --value 2>/dev/null)
echo "    Resolved After= entries (sorted, one per line):"
echo "$RESOLVED" | tr ' ' '\n' | sort -u | sed 's/^/      /'
echo

# ── 5. Verdict — does the BARE Fedora unit already order after resolved? ──
printf "%s[5] Verdict%s\n" "$C_HDR" "$C_END"
if grep -E '^\s*After=.*\b'"$NEED"'\b' "$RPM_UNIT" >/dev/null 2>&1; then
  printf "    %sPASS%s — Fedora RPM unit already contains After=%s.\n" \
    "$C_OK" "$C_END" "$NEED"
  echo "             Drop-in 10-resolved.conf is REDUNDANT."
  echo "             → REMOVE the dropin from noir.bu's tailscaled.service."
  exit 0
else
  printf "    %sKEEP%s — Fedora RPM unit does NOT order after %s.\n" \
    "$C_FAIL" "$C_END" "$NEED"
  echo "             Drop-in 10-resolved.conf is REQUIRED."
  echo "             → KEEP the dropin in noir.bu."
  exit 1
fi
