# AGENTS.md — read this first (model-agnostic agent guide)

You are an AI agent (any model/tool) about to work on **noir**, a headless Fedora CoreOS homelab host. This file is the binding source of truth. **Read this, then `HARDWARE.md` and `BUILD-SPEC.md`, before changing anything.** `CLAUDE.md` just points here. The rules below are house standard for the `oso-gato` Fedora projects (`fedora-bootstrap`, `fedora-dev`, `fedora-xrdp`) — follow them verbatim.

## What noir is
A headless **Fedora CoreOS** host on a **Minisforum MS-S1 MAX** (AMD Strix Halo / Ryzen AI Max+ 395, 128 GB LPDDR5x, 2 TB + 4 TB WD_BLACK SN850X). Role: **Tailscale subnet router** for `10.0.50.0/24` + Podman container host + Cockpit web admin. The repo builds two install ISOs — `noir-wipe.iso` (formats the data drive) and `noir-preserve.iso` (keeps it) — from one Butane source.

## Read these next (in the repo)
- **`HARDWARE.md`** — exact hardware + the driver/firmware facts that *dictate* the build (and the boot-2 Wi-Fi sequencing).
- **`BUILD-SPEC.md`** — the full design spec + the end-to-end runbook (CI build → publish → first-boot wipe/preserve → Wi-Fi → Tailscale).

## BUILD PRINCIPLES — BINDING (oso-gato house standard)
1. **TARGET:** Fedora CoreOS **stable**, pinned to an exact build (`ARG`/`BASE_ISO`), bumped deliberately.
2. **PACKAGE-INSTALL HIERARCHY — pick the highest applicable tier and stop:**
   - **Tier 1 — Fedora official repos**, via `rpm-ostree install --idempotent --allow-inactive`. The default. Keep the list minimal.
   - **Tier 2 — the vendor's / developer's OWN official RPM/dnf repo.** Only when Tier 1 has no package. Drop the `.repo` into `/etc/yum.repos.d/` **copied verbatim from the vendor, with a dated "verified" comment, `gpgcheck=1` + `repo_gpgcheck=1`**, then layer in the same `rpm-ostree` call. (noir's only Tier-2 case: Tailscale.)
   - **Tier 3 — worst case — the official vendor/developer's released artifact** (a vendor `.rpm`, or their published image). Record it.
   - **NEVER:** `curl | sh`, language package managers onto PATH (`pip`/`pipx`/`npm -g`/`cargo`/`go install`/`gem`/`brew`), tarballs onto PATH, or third-party repos (COPR, Flathub, snap). Exceptions require an explicit user waiver recorded in the Packages table. **Current waivers: none.** (`transpile.py`/`sync_check.py` are build-time generators run on the build host — they are not "installing software onto the target" and are exempt.)
3. **LESS IS MORE.** Install only what is required (`install_weak_deps=False` equivalent). Every layered package must be justifiable. Audit and remove the moment something is redundant. No cargo-culted flags/packages.
4. **IMMUTABLE BASE, declarative source of truth.** The host changes only by rebuilding the image from source; `rpm-ostree` layering is the only sanctioned (atomic, rollback-able) mutation path. `noir.bu` is the source of truth.
5. **VERIFY FIRST.** Fact-check any source/version/repo against the live upstream before changing it (and record the date).
6. **NO BAKED SECRETS.** No password, Wi-Fi PSK, or token in the repo or the built images. Credentials are set at first boot (see below). Serials are the **only** hardware identifier intentionally baked (a fingerprint, not a credential).
7. **EXPOSURE.** Public IP carries **key-only SSH** only. Cockpit and everything sensitive are **tailnet-only**.
8. **GUARDRAILS ARE CODE.** Agent rules live in this repo (this `AGENTS.md`, plus `HARDWARE.md`/`BUILD-SPEC.md`). Changing the rules = editing the repo. An undocumented change is a failure even if it works.

## Non-negotiable build facts — you WILL break the build if you ignore these
1. **Dual-maintenance, gated by `sync_check`.** `noir.bu` (Butane source) and `transpile.py` (a hand-written transpiler that emits the same Ignition) must be edited **in lockstep** — every keyfile, unit, and script body exists in **both**. A change only counts when `sync_check.py` prints clean. Never touch one without mirroring the other.
2. **Wi-Fi does NOT exist on the first boot — setup is a SECOND-boot event.** FCOS base ships no MediaTek MT7925 firmware and no NetworkManager-wifi plugin. `noir-firstboot-install` rpm-ostree-**layers** them (Tier-1 Fedora packages: `mt7xxx-firmware`/`NetworkManager-wifi`/`wpa_supplicant`/`wireless-regdb`) on **boot 1** and then **reboots** (rpm-ostree layers apply only on reboot). So `wlp99s0` only appears on **boot 2**. The first-boot setup (`noir-setup`) must therefore activate **only on boot 2** (`ConditionPathExists=!/var/lib/noir/firstboot.stamp` + `After=noir-firstboot-enable.service`), **never `ConditionFirstBoot`**. The wired NIC (RTL8127A, in-tree `r8169`) + SSH-by-key ARE up from boot 1.
3. **No credentials baked.** Core password + Wi-Fi SSID/PSK + Tailscale onboarding are all set at **first boot** by `noir-setup` (tty1 *or* SSH, `flock` + completion sentinel = first-one-wins), and persisted to the data drive so a *preserve* re-flash restores them. The CI build has no secret to inject — never reintroduce build-time credential injection.
4. **No secrets in history (maintained invariant).** The history was re-initialised to a single clean `v1.0.0` commit; no bcrypt hash or Wi-Fi PSK survives in any commit, and the public images bake none. Serials stay (a fingerprint, not a credential). Keep it that way — never reintroduce a baked hash/PSK or commit a secret back into history.

## The design (as built in v1.0.0)
See `BUILD-SPEC.md` for the full spec + runbook. Summary:
- **Passwordless `core`**; password set at first boot. Injection mechanism removed.
- **Wi-Fi = three generic slots** `wifi-primary/secondary/tertiary`, **all `route-metric=50`** (any beats bond0's 100; priority 100/90/80 only orders which connects). No SSID/PSK in the repo — `noir-setup` generates the keyfiles at first boot. Helper **`noir-wifi`**: `on|off|switch <slot>|status|list|set-primary <slot>`.
- **`noir-setup`** ("Both" mode, boot-2 gated): Core password → Wi-Fi slots → Tailscale onboarding; persists password hash + Wi-Fi keyfiles + `tailscaled.state` to the data drive.
- **Routing:** pin **only** Tailscale's underlay to bond0 (`fwmark 0x80000 → table 100 via 10.0.50.1`, v4+v6). **Exit-node dropped**, subnet router kept → no `iif tailscale0` rules, no `rp_filter` change.
- **SSH keys:** NOT baked — `build-iso.sh` fetches the account's **current** GitHub-published keys (`github.com/oso-gato.keys`) at build time and injects them, each tagged by a short SHA256 fingerprint prefix (the repo holds only the hash prefixes, never key material). The build **hard-fails on zero keys** (anti-brick: passwordless `core` + key-only SSH means a keyless ISO is unreachable).
- **CI:** fortnightly GitHub Actions builds both ISOs from the latest FCOS stable, publishes a Release (`v1.0.0`+). No secrets needed.

## Current state (keep this updated)
- Shipping code in `noir.bu`/`transpile.py` **is** the credential-free design above, released as **v1.0.0** (base FCOS `44.20260523.3.1`, pinned in `build-iso.sh`): passwordless `core` (no `password_hash`), generic `wifi-primary/secondary/tertiary` slots filled at first boot, no baked PSKs, no bcrypt hash in repo or history, exit-node dropped (subnet router kept). `sync_check.py` gates `noir.bu`↔`transpile.py` in lockstep — keep it green.
- GitHub: `oso-gato/noir-strix-halo-fcos` — **public**; released `v1.0.0` (2026-06-13) from a single clean re-initialised commit (old hash + PSKs purged from history). CI builds both ISOs from the latest FCOS stable and publishes each as a Release.
