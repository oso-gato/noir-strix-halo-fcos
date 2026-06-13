#!/bin/bash
# build-iso.sh — build customised noir install ISO(s).  (v1.0)
#
# Default invocation: builds BOTH ISOs.
#
#   ./build-iso.sh             → noir-preserve.iso AND noir-wipe.iso
#   ./build-iso.sh --preserve  → preserve only
#   ./build-iso.sh --wipe      → wipe only
#
# Inputs (must sit next to this script):
#   - fedora-coreos-44.20260523.3.1-live-iso.x86_64.iso  (base FCOS 44 stable live ISO)
#   - noir.bu                                            (Butane source — source of truth)
#   - transpile.py        (auto-regenerates .ign files from noir.bu)
#   - sync_check.py       (verifies noir.bu ↔ noir-preserve.ign drift)
#   - noir-preserve.ign + noir-wipe.ign                  (auto-regenerated)
#   - guard.sh                                           (pre-install sanity check)
#
# Outputs:
#   - noir-preserve.iso  and/or  noir-wipe.iso
#
# What gets baked in:
#   --dest-device    : pins install target to the 2 TB WD_BLACK SN850X by-id
#                      symlink (deterministic across reboots; guard.sh re-verifies).
#   --dest-ignition  : noir-preserve.ign or noir-wipe.ign — variants differ
#                      only in 4 booleans on the 4 TB data drive.
#   --pre-install    : guard.sh runs in the live env before any disk write,
#                      aborting the install if the target doesn't match.
#
# Wipe-vs-preserve choice:
#   FCOS does not natively support an interactive wipe/preserve picker on a
#   single ISO. The design-compliant pattern is two ISOs from one source —
#   the operator picks the right USB stick at install time.
#
# Runtime:
#   Uses the upstream coreos-installer container (no local install required).
#   Works on macOS (podman machine or Docker Desktop) or Linux (native podman/
#   docker). On macOS: `podman machine start` once per session.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

# ─── Parse flags ─────────────────────────────────────────────────────────────
BUILD_PRESERVE=1
BUILD_WIPE=1
if [ $# -gt 0 ]; then
  case "$1" in
    --wipe)
      BUILD_PRESERVE=0
      BUILD_WIPE=1
      ;;
    --preserve)
      BUILD_PRESERVE=1
      BUILD_WIPE=0
      ;;
    -h|--help)
      sed -n '2,37p' "$0"
      exit 0
      ;;
    *)
      echo "build-iso: unknown flag '$1' (see --help)" >&2
      exit 1
      ;;
  esac
  shift
  if [ $# -gt 0 ]; then
    echo "build-iso: unexpected extra arguments: $*" >&2
    echo "  (takes at most one flag: --preserve, --wipe, or nothing for both)" >&2
    exit 1
  fi
fi

VARIANTS=()
[ "$BUILD_PRESERVE" = "1" ] && VARIANTS+=("preserve")
[ "$BUILD_WIPE"     = "1" ] && VARIANTS+=("wipe")
echo "build-iso: will build → ${VARIANTS[*]}"

BASE_ISO="fedora-coreos-44.20260523.3.1-live-iso.x86_64.iso"
GUARD="guard.sh"

# Install target — 2 TB WD_BLACK SN850X (serial 25281F806642). guard.sh
# re-verifies this at pre-install time.
DEST_DEVICE="/dev/disk/by-id/nvme-WD_BLACK_SN850X_2000GB_25281F806642"

# ─── Auto-regenerate Ignition files from noir.bu ─────────────────────────────
if [ -f "$HERE/transpile.py" ] && [ -f "$HERE/noir.bu" ]; then
  echo "build-iso: regenerating .ign files from noir.bu via transpile.py"
  for v in "${VARIANTS[@]}"; do
    if [ "$v" = "wipe" ]; then
      python3 "$HERE/transpile.py" --wipe
    else
      python3 "$HERE/transpile.py"
    fi
  done

  # Sync-check gate — confirm regenerated noir-preserve.ign matches noir.bu.
  # Exit codes: 0=clean, 1=drift (block), 2=pyyaml missing (advisory, skip).
  if [ -f "$HERE/sync_check.py" ]; then
    echo "build-iso: running sync_check"
    set +e
    python3 "$HERE/sync_check.py" > /dev/null 2> /tmp/sync_check.err
    sync_rc=$?
    set -e
    case $sync_rc in
      0)
        echo "build-iso: [OK] sync_check clean"
        ;;
      2)
        echo "build-iso: [WARN] sync_check skipped (pyyaml not available)" >&2
        sed 's/^/    /' /tmp/sync_check.err >&2
        echo "  build continues — drift check is advisory." >&2
        ;;
      *)
        echo "build-iso: FAIL — sync_check reports noir.bu ↔ noir-preserve.ign drift" >&2
        echo "  run:  python3 sync_check.py" >&2
        echo "  to see specific fields that differ; fix noir.bu or transpile.py." >&2
        exit 1
        ;;
    esac
    rm -f /tmp/sync_check.err
  fi
else
  echo "build-iso: transpile.py not present → using existing .ign files as-is"
fi

# ─── Container runtime: prefer podman, fall back to docker ───────────────────
if command -v podman >/dev/null 2>&1; then
  RUNTIME=podman
elif command -v docker >/dev/null 2>&1; then
  RUNTIME=docker
else
  echo "build-iso: neither podman nor docker on PATH. Install one and retry." >&2
  exit 1
fi
echo "build-iso: using $RUNTIME"

# ─── Pre-flight: every required input present ────────────────────────────────
if [ ! -f "$HERE/$BASE_ISO" ]; then
  echo "build-iso: FAIL — missing $BASE_ISO in $HERE" >&2
  exit 1
fi
if [ ! -f "$HERE/$GUARD" ]; then
  echo "build-iso: FAIL — missing $GUARD in $HERE" >&2
  exit 1
fi
for v in "${VARIANTS[@]}"; do
  if [ ! -f "$HERE/noir-${v}.ign" ]; then
    echo "build-iso: FAIL — missing noir-${v}.ign in $HERE" >&2
    echo "  (run: python3 transpile.py $([ "$v" = "wipe" ] && echo --wipe))" >&2
    exit 1
  fi
done
echo "build-iso: inputs verified in $HERE"

# guard.sh must be executable inside the live env
chmod +x "$HERE/$GUARD"

# ─── Build each variant ──────────────────────────────────────────────────────
for VARIANT in "${VARIANTS[@]}"; do
  OUT_ISO="noir-${VARIANT}.iso"
  IGN="noir-${VARIANT}.ign"

  echo ""
  echo "==================================================================="
  echo "  Building $OUT_ISO (variant = $VARIANT)"
  echo "==================================================================="

  rm -f "$HERE/$OUT_ISO"

  "$RUNTIME" run --rm --pull=always \
    --security-opt label=disable \
    -v "$HERE":/data \
    -w /data \
    quay.io/coreos/coreos-installer:release \
      iso customize \
        --dest-device   "$DEST_DEVICE" \
        --dest-ignition "$IGN" \
        --pre-install   "$GUARD" \
        -o "$OUT_ISO" \
        "$BASE_ISO"

  echo "build-iso: [OK] $OUT_ISO"
done

# ─── Final summary ───────────────────────────────────────────────────────────
echo ""
echo "==================================================================="
echo "  DONE"
echo "==================================================================="
for VARIANT in "${VARIANTS[@]}"; do
  printf "  %-24s  %s\n" "noir-${VARIANT}.iso" "$HERE/noir-${VARIANT}.iso"
done
echo ""
if [ "$BUILD_WIPE" = "1" ]; then
  echo "[WARN]  noir-wipe.iso WIPES the 4 TB data drive at install time."
  echo "        Label the USB stick loudly before it gets mixed up with preserve."
fi
if [ "$BUILD_PRESERVE" = "1" ]; then
  echo "[OK]    noir-preserve.iso PRESERVES the 4 TB data drive (safe default)."
fi
echo ""
echo "Next:"
echo "  1. Flash each ISO to its own USB stick:"
echo "       sudo dd if=<ISO> of=/dev/diskN bs=4m status=progress conv=sync"
echo "     (macOS: /dev/rdiskN ; Linux: /dev/sdX — verify the target)"
echo "  2. Boot noir from the chosen USB. guard.sh verifies the 2 TB drive,"
echo "     coreos-installer writes FCOS, Ignition provisions the 4 TB drive,"
echo "     noir-firstboot-install layers tailscale and reboots,"
echo "     noir-firstboot-enable starts tailscaled."
