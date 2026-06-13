#!/bin/bash
# guard.sh — coreos-installer --pre-install sanity check.  (v1.0.0)
#
# Runs inside the FCOS live environment BEFORE coreos-installer writes
# anything to disk. Exits non-zero (aborts install) unless every check
# below passes — there are no warnings, only PASS or FAIL.
#
# Hardware profile (must match physical noir):
#   System drive : WD_BLACK SN850X 2000GB, serial 25281F806642
#                  → installed-onto, fully overwritten by coreos-installer.
#   Data drive   : WD_BLACK SN850X 4000GB, serial 25278B803296
#                  → not touched by coreos-installer; provisioned by Ignition
#                    via storage.disks (preserve or wipe per the chosen ISO).
#
# Why this script exists:
#   coreos-installer is given --dest-device pinned to the system drive's
#   by-id symlink. by-id symlinks are stable across reboots, but the
#   physical device underneath could differ from expectation if:
#     - the wrong USB stick was flashed onto a different machine
#     - a drive has been replaced and a serial in noir.bu went stale
#     - the data drive ended up in the system-drive symlink slot
#   Any of those silently destroying the wrong drive is unacceptable.
#   This script catches those cases.

set -euo pipefail

# ─── Expected hardware (matches noir.bu) ─────────────────────────────────────
TARGET="/dev/disk/by-id/nvme-WD_BLACK_SN850X_2000GB_25281F806642"
TARGET_EXPECTED_SIZE_GB=1862        # 2000 GB (decimal) ≈ 1862 GiB
TARGET_EXPECTED_MODEL="SN850X 2000GB"

DATA="/dev/disk/by-id/nvme-WD_BLACK_SN850X_4000GB_25278B803296"
DATA_EXPECTED_SIZE_GB=3725          # 4000 GB (decimal) ≈ 3725 GiB
DATA_EXPECTED_MODEL="SN850X 4000GB"

SIZE_TOLERANCE_GB=50                # generous (covers reserved/aligned slack)

# ─── Helpers ─────────────────────────────────────────────────────────────────
fail() {
    echo "GUARD: FAIL — $*" >&2
    echo "GUARD: available NVMe by-id symlinks:" >&2
    ls -la /dev/disk/by-id/ 2>/dev/null | grep nvme >&2 || true
    exit 1
}

check_drive() {
    # Args: SYMLINK EXPECTED_SIZE_GB EXPECTED_MODEL_SUBSTR LABEL
    local symlink="$1" exp_gb="$2" exp_model="$3" label="$4"

    [ -e "$symlink" ] || fail "$label symlink $symlink does not resolve."

    local real basename size_bytes size_gb lo hi model_path model
    real=$(readlink -f "$symlink")
    basename=$(basename "$real")
    echo "GUARD:   $label symlink → $real"

    # Size in integer GiB.
    size_bytes=$(blockdev --getsize64 "$real")
    size_gb=$((size_bytes / 1024 / 1024 / 1024))
    lo=$((exp_gb - SIZE_TOLERANCE_GB))
    hi=$((exp_gb + SIZE_TOLERANCE_GB))
    if [ "$size_gb" -lt "$lo" ] || [ "$size_gb" -gt "$hi" ]; then
        fail "$label size ${size_gb} GiB outside expected [${lo}, ${hi}] GiB."
    fi
    echo "GUARD:   $label size OK: ${size_gb} GiB"

    # Model substring (sysfs reports 'WD_BLACK SN850X NNNNGB' on these drives).
    model_path="/sys/block/${basename}/device/model"
    [ -r "$model_path" ] || fail "$label model file unreadable: $model_path"
    model=$(tr -d '\n' < "$model_path" | sed 's/[[:space:]]*$//')
    if ! echo "$model" | grep -qF "$exp_model"; then
        fail "$label model '$model' does not contain '$exp_model'."
    fi
    echo "GUARD:   $label model OK: '$model'"
}

# ─── Run checks ──────────────────────────────────────────────────────────────
echo "GUARD: verifying install target…"
echo "GUARD:   target symlink  : $TARGET"
echo "GUARD:   target expected : ${TARGET_EXPECTED_SIZE_GB}±${SIZE_TOLERANCE_GB} GiB, model contains '${TARGET_EXPECTED_MODEL}'"

check_drive "$TARGET" "$TARGET_EXPECTED_SIZE_GB" "$TARGET_EXPECTED_MODEL" "TARGET"

# Defense against drive-swap: if the data drive's by-id symlink resolves,
# its identity must match too. (If the data drive is absent — e.g. it's been
# removed for replacement — we don't fail; the install can proceed and
# Ignition will halt later if storage.disks doesn't see it. That's a separate,
# clearer error path.)
if [ -e "$DATA" ]; then
    echo "GUARD: verifying data drive identity (defense against drive swap)…"
    check_drive "$DATA" "$DATA_EXPECTED_SIZE_GB" "$DATA_EXPECTED_MODEL" "DATA"
else
    echo "GUARD: data drive symlink absent — skipping data-drive identity check."
    echo "GUARD: (Ignition will halt later if storage.disks can't find it.)"
fi

echo "GUARD: all checks passed. Proceeding with install onto $TARGET."
exit 0
