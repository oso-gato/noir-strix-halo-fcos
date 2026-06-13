#!/usr/bin/env python3
"""
sync_check.py — verify noir.bu and noir-preserve.ign are in sync.  (v1.0)

Compares the Butane source against the transpiled Ignition output across
the surfaces that matter:

  1. Version headers (variant + spec versions)
  2. Users (groups + SSH keys; password_hash is normally absent — the gate
     asserts the two sides agree and, if a hash is ever present, that it is
     a valid bcrypt and not a leftover placeholder)
  3. Kernel arguments
  4. storage.disks (device, wipe_table, partitions)
  5. storage.filesystems (device, format, path, label, uuid, options)
  6. storage.files (path set + per-file mode + per-file body byte-for-byte,
     plus a REPLACE_WITH_* placeholder gate across all file bodies)
  7. systemd units (Butane with_mount_unit:true expands to <escaped>.mount,
                    so the verifier expands the Butane side before comparison)

Only noir-preserve.ign is checked — noir-wipe.ign is the same source with
four booleans flipped, so if preserve syncs, wipe syncs by construction.

Defensive gates (dormant under the credential-free design, which bakes no
secrets — they only fire if one is ever introduced):
  - bcrypt prefix: if a passwordHash is present it MUST start with $2a$, $2b$,
    or $2y$ (Cockpit's PAM stack expects bcrypt; SHA-512 ($6$) etc. is rejected).
  - REPLACE_WITH_*: any user/file body containing REPLACE_WITH_* is a
    forgotten substitution → fail-fast before flashing a non-functional host.

Exit codes:
  0  — clean
  1  — drift detected
  2  — pyyaml not installed (advisory; build-iso.sh treats this as skip-gate)
"""
import base64
import json
import os
import sys

try:
    import yaml
except ImportError:
    print(
        "sync_check: pyyaml not available, cannot verify "
        "noir.bu ↔ noir-preserve.ign drift.",
        file=sys.stderr,
    )
    print("  To enable this check, install pyyaml:", file=sys.stderr)
    print("    macOS:  python3 -m pip install pyyaml --user", file=sys.stderr)
    print("    Linux:  python3 -m pip install pyyaml --break-system-packages",
          file=sys.stderr)
    sys.exit(2)

HERE = os.path.dirname(os.path.abspath(__file__))
BU_PATH = os.path.join(HERE, "noir.bu")
IGN_PATH = os.path.join(HERE, "noir-preserve.ign")


def systemd_escape_path(p: str) -> str:
    p = p.lstrip("/")
    p = p.replace("-", r"\x2d")
    p = p.replace("/", "-")
    return p


with open(BU_PATH) as f:
    bu = yaml.safe_load(f)
with open(IGN_PATH) as f:
    ign = json.load(f)

problems = []

# ── 1. Versions ──────────────────────────────────────────────────────────────
print("═══ version ═══")
print(f"  butane variant/version : {bu.get('variant')} / {bu.get('version')}")
print(f"  ignition version       : {ign['ignition'].get('version')}")
if bu.get("variant") != "fcos" or bu.get("version") != "1.6.0":
    problems.append(f"unexpected butane spec: {bu.get('variant')}/{bu.get('version')}")
if ign["ignition"].get("version") != "3.5.0":
    problems.append(f"unexpected ignition version: {ign['ignition'].get('version')}")

# ── 2. Users ─────────────────────────────────────────────────────────────────
print("\n═══ users ═══")
bu_users = bu.get("passwd", {}).get("users", [])
ign_users = ign.get("passwd", {}).get("users", [])
if len(bu_users) != len(ign_users):
    problems.append(f"user count: bu={len(bu_users)} ign={len(ign_users)}")
for bu_u, ign_u in zip(bu_users, ign_users):
    print(f"  user: {bu_u['name']}")
    if bu_u["name"] != ign_u["name"]:
        problems.append(f"user name: bu={bu_u['name']} ign={ign_u['name']}")
    if sorted(bu_u.get("groups", [])) != sorted(ign_u.get("groups", [])):
        problems.append(
            f"groups for {bu_u['name']}: bu={bu_u.get('groups')} ign={ign_u.get('groups')}"
        )
    else:
        print(f"    groups: {bu_u.get('groups')}  ✓")
    if bu_u.get("ssh_authorized_keys", []) != ign_u.get("sshAuthorizedKeys", []):
        problems.append(f"ssh keys for {bu_u['name']} differ")
    else:
        print(f"    ssh keys: {len(bu_u.get('ssh_authorized_keys', []))} key(s)  ✓")

    # password_hash parity + (if present) bcrypt-prefix + placeholder gate.
    # The credential-free design bakes NO password (core is passwordless; the
    # password is set at first boot), so both sides are normally None and this
    # prints "(none) ✓". The gate only bites if a hash is ever added — then it
    # must match across both files and be a valid bcrypt, not a leftover
    # REPLACE_WITH_ placeholder.
    bu_pw = bu_u.get("password_hash")
    ign_pw = ign_u.get("passwordHash")
    if bu_pw != ign_pw:
        problems.append(
            f"password_hash for {bu_u['name']} differs: "
            f"bu={bu_pw!r} ign={ign_pw!r}"
        )
    elif bu_pw:
        if bu_pw.startswith("REPLACE_WITH_"):
            problems.append(
                f"password_hash for {bu_u['name']} is the placeholder string "
                f"({bu_pw!r}). Mint a real bcrypt hash on macOS with "
                f"`read -rs PW && printf '%s' \"$PW\" | "
                f"htpasswd -niB -C 12 {bu_u['name']} | cut -d: -f2; unset PW` "
                f"and substitute it in BOTH noir.bu AND transpile.py."
            )
        elif not bu_pw.startswith(("$2a$", "$2b$", "$2y$")):
            problems.append(
                f"password_hash for {bu_u['name']} is not bcrypt "
                f"(prefix must be $2a$/$2b$/$2y$, got {bu_pw[:4]!r}). "
                f"Cockpit's PAM stack expects bcrypt; SHA-512 ($6$) etc. "
                f"will be rejected at login."
            )
        else:
            print(f"    password_hash: bcrypt {bu_pw[:4]}…  ✓")
    else:
        print(f"    password_hash: (none)  ✓")

# ── 3. Kernel arguments ──────────────────────────────────────────────────────
print("\n═══ kernel arguments ═══")
bu_kargs = bu.get("kernel_arguments", {}).get("should_exist", [])
ign_kargs = ign.get("kernelArguments", {}).get("shouldExist", [])
if sorted(bu_kargs) != sorted(ign_kargs):
    problems.append(f"kargs: bu={bu_kargs} ign={ign_kargs}")
else:
    print(f"  should_exist: {bu_kargs}  ✓")

# ── 4. storage.disks ─────────────────────────────────────────────────────────
print("\n═══ storage.disks ═══")
bu_disks = bu.get("storage", {}).get("disks", [])
ign_disks = ign.get("storage", {}).get("disks", [])
if len(bu_disks) != len(ign_disks):
    problems.append(f"disk count: bu={len(bu_disks)} ign={len(ign_disks)}")
for i, (bd, id_) in enumerate(zip(bu_disks, ign_disks)):
    print(f"  disk[{i}]: {bd.get('device')}")
    if bd.get("device") != id_.get("device"):
        problems.append(f"disk[{i}].device mismatch")
    if bd.get("wipe_table", False) != id_.get("wipeTable", False):
        problems.append(
            f"disk[{i}].wipe_table bu={bd.get('wipe_table')} ign={id_.get('wipeTable')}"
        )
    else:
        print(f"    wipe_table: {bd.get('wipe_table')}  ✓")
    bu_parts = bd.get("partitions", [])
    ign_parts = id_.get("partitions", [])
    if len(bu_parts) != len(ign_parts):
        problems.append(
            f"disk[{i}] partition count: bu={len(bu_parts)} ign={len(ign_parts)}"
        )
    for j, (bp, ip) in enumerate(zip(bu_parts, ign_parts)):
        ok = (
            bp.get("label") == ip.get("label")
            and bp.get("number") == ip.get("number")
            and bp.get("size_mib") == ip.get("sizeMiB")
            and bp.get("resize", False) == ip.get("resize", False)
            and bp.get("wipe_partition_entry", False) == ip.get("wipePartitionEntry", False)
        )
        tag = "✓" if ok else "✗"
        print(
            f"    [{tag}] partition[{j}] "
            f"label={bp.get('label')} num={bp.get('number')} size_mib={bp.get('size_mib')}"
        )
        if not ok:
            problems.append(f"disk[{i}].partitions[{j}] mismatch:\n    bu : {bp}\n    ign: {ip}")

# ── 5. storage.filesystems ───────────────────────────────────────────────────
print("\n═══ storage.filesystems ═══")
bu_fss = bu.get("storage", {}).get("filesystems", [])
ign_fss = ign.get("storage", {}).get("filesystems", [])
if len(bu_fss) != len(ign_fss):
    problems.append(f"filesystem count: bu={len(bu_fss)} ign={len(ign_fss)}")
for i, (bf, ifs) in enumerate(zip(bu_fss, ign_fss)):
    print(f"  fs[{i}]: {bf.get('device')} → {bf.get('path', '(no path)')}")
    checks = [
        ("device",          bf.get("device"),          ifs.get("device")),
        ("format",          bf.get("format"),          ifs.get("format")),
        ("path",            bf.get("path"),            ifs.get("path")),
        ("label",           bf.get("label"),           ifs.get("label")),
        ("uuid",            bf.get("uuid"),            ifs.get("uuid")),
        ("wipe_filesystem", bf.get("wipe_filesystem", False), ifs.get("wipeFilesystem", False)),
        ("mount_options",   bf.get("mount_options"),   ifs.get("mountOptions")),
    ]
    for field, a, b in checks:
        if a != b:
            problems.append(f"fs[{i}].{field}: bu={a!r} ign={b!r}")
        else:
            print(f"    {field:16s}: {a!r}  ✓")

# ── 6. storage.files ─────────────────────────────────────────────────────────
print("\n═══ storage.files ═══")
bu_files = {f["path"]: f for f in bu.get("storage", {}).get("files", [])}
ign_files = {f["path"]: f for f in ign.get("storage", {}).get("files", [])}

only_bu = set(bu_files) - set(ign_files)
only_ign = set(ign_files) - set(bu_files)
if only_bu:
    problems.append(f"files in noir.bu not in noir-preserve.ign: {sorted(only_bu)}")
if only_ign:
    problems.append(f"files in noir-preserve.ign not in noir.bu: {sorted(only_ign)}")

for path in sorted(set(bu_files) & set(ign_files)):
    bu_body = bu_files[path].get("contents", {}).get("inline", "")
    src = ign_files[path].get("contents", {}).get("source", "")
    if not src.startswith("data:;base64,"):
        problems.append(f"{path}: ignition source not base64 data url: {src[:40]}")
        continue
    ign_body = base64.b64decode(src.split("base64,", 1)[1]).decode("utf-8")

    bu_mode = bu_files[path].get("mode")
    ign_mode = ign_files[path].get("mode")
    mode_ok = bu_mode == ign_mode
    body_ok = bu_body == ign_body

    tag = "✓" if (mode_ok and body_ok) else "✗"
    print(
        f"  [{tag}] {path}  "
        f"mode(bu={oct(bu_mode) if bu_mode else '?'}, "
        f"ign={oct(ign_mode) if ign_mode else '?'}), "
        f"body {len(bu_body)} vs {len(ign_body)} B"
    )

    if not mode_ok:
        problems.append(f"{path}: mode bu={oct(bu_mode)} ign={oct(ign_mode)}")
    if not body_ok:
        bu_lines = bu_body.splitlines()
        ign_lines = ign_body.splitlines()
        line_diff_found = False
        for i, (a, b) in enumerate(zip(bu_lines, ign_lines)):
            if a != b:
                problems.append(
                    f"{path}: first diff at line {i+1}\n    bu : {a!r}\n    ign: {b!r}"
                )
                line_diff_found = True
                break
        if not line_diff_found:
            if len(bu_lines) != len(ign_lines):
                problems.append(
                    f"{path}: line count bu={len(bu_lines)} ign={len(ign_lines)}"
                )
            else:
                # Bodies differ at the byte level (e.g. trailing newline only)
                # but every line matches and line counts match. Catch it
                # explicitly so trailing-newline-only divergences fail the gate.
                problems.append(
                    f"{path}: byte-level body mismatch "
                    f"(sizes {len(bu_body)} vs {len(ign_body)} B); "
                    f"line content identical, likely trailing-newline difference"
                )

    # Placeholder-substitution gate (defensive). Any REPLACE_WITH_* token still
    # present in a file body means a value was left unsubstituted and would
    # flash a non-functional host. The credential-free design uses none.
    if "REPLACE_WITH_" in bu_body:
        problems.append(
            f"{path}: contains a REPLACE_WITH_ placeholder — substitute "
            f"the real value before building the ISO"
        )

# ── 7. systemd units ─────────────────────────────────────────────────────────
print("\n═══ systemd units ═══")

bu_unit_names = {u["name"] for u in bu.get("systemd", {}).get("units", [])}
bu_auto_mount_names = set()
for fs in bu_fss:
    if fs.get("with_mount_unit") and fs.get("path"):
        bu_auto_mount_names.add(f"{systemd_escape_path(fs['path'])}.mount")
bu_expected = bu_unit_names | bu_auto_mount_names

ign_unit_names = {u["name"] for u in ign.get("systemd", {}).get("units", [])}

only_bu = bu_expected - ign_unit_names
only_ign = ign_unit_names - bu_expected
if only_bu:
    problems.append(f"units expected from noir.bu but missing in ignition: {sorted(only_bu)}")
if only_ign:
    problems.append(f"units in ignition but not implied by noir.bu: {sorted(only_ign)}")

bu_units_by_name = {u["name"]: u for u in bu.get("systemd", {}).get("units", [])}
ign_units_by_name = {u["name"]: u for u in ign.get("systemd", {}).get("units", [])}

for name in sorted(bu_unit_names & ign_unit_names):
    bu_u = bu_units_by_name[name]
    ign_u = ign_units_by_name[name]
    bu_enabled = bu_u.get("enabled")
    ign_enabled = ign_u.get("enabled")
    bu_body = bu_u.get("contents", "") or ""
    ign_body = ign_u.get("contents", "") or ""
    enabled_ok = bu_enabled == ign_enabled
    body_ok = bu_body == ign_body

    bu_dropins = {d["name"]: d.get("contents", "") or "" for d in bu_u.get("dropins", []) or []}
    ign_dropins = {d["name"]: d.get("contents", "") or "" for d in ign_u.get("dropins", []) or []}
    dropin_names_ok = set(bu_dropins) == set(ign_dropins)
    dropin_bodies_ok = all(
        bu_dropins[n] == ign_dropins[n] for n in bu_dropins if n in ign_dropins
    )

    overall_ok = enabled_ok and body_ok and dropin_names_ok and dropin_bodies_ok
    tag = "✓" if overall_ok else "✗"
    body_note = (
        f"body {len(bu_body)} vs {len(ign_body)} B" if (bu_body or ign_body) else "no body"
    )
    dropin_note = (
        f", {len(bu_dropins)} dropin(s)" if (bu_dropins or ign_dropins) else ""
    )
    print(
        f"  [{tag}] {name}  enabled(bu={bu_enabled}, ign={ign_enabled}), "
        f"{body_note}{dropin_note}"
    )

    if not enabled_ok:
        problems.append(f"unit {name}: enabled bu={bu_enabled} ign={ign_enabled}")
    if not body_ok:
        bu_lines = bu_body.splitlines()
        ign_lines = ign_body.splitlines()
        line_diff_found = False
        for i, (a, b) in enumerate(zip(bu_lines, ign_lines)):
            if a != b:
                problems.append(
                    f"unit {name}: first diff at line {i+1}\n    bu : {a!r}\n    ign: {b!r}"
                )
                line_diff_found = True
                break
        if not line_diff_found:
            if len(bu_lines) != len(ign_lines):
                problems.append(
                    f"unit {name}: line count bu={len(bu_lines)} ign={len(ign_lines)}"
                )
            else:
                problems.append(
                    f"unit {name}: byte-level body mismatch "
                    f"(sizes {len(bu_body)} vs {len(ign_body)} B); "
                    f"line content identical, likely trailing-newline difference"
                )
    if not dropin_names_ok:
        problems.append(
            f"unit {name}: dropin names bu={sorted(bu_dropins)} ign={sorted(ign_dropins)}"
        )
    if not dropin_bodies_ok:
        for n in sorted(bu_dropins):
            if n in ign_dropins and bu_dropins[n] != ign_dropins[n]:
                problems.append(
                    f"unit {name} dropin '{n}': body differs\n"
                    f"    bu : {bu_dropins[n]!r}\n    ign: {ign_dropins[n]!r}"
                )

for name in sorted(bu_auto_mount_names & ign_unit_names):
    ign_u = ign_units_by_name[name]
    enabled = ign_u.get("enabled")
    body_size = len(ign_u.get("contents") or "")
    tag = "✓" if enabled else "✗"
    print(
        f"  [{tag}] {name}  (auto-generated from with_mount_unit, "
        f"enabled={enabled}, body {body_size} B)"
    )
    if not enabled:
        problems.append(f"auto mount unit {name} not enabled in ignition")

# ── Verdict ──────────────────────────────────────────────────────────────────
print("\n═══ verdict ═══")
if problems:
    print(f"  ✗ {len(problems)} problem(s):")
    for p in problems:
        print(f"    - {p}")
    sys.exit(1)
else:
    print("  ✓ noir.bu and noir-preserve.ign are fully in sync")
    sys.exit(0)
