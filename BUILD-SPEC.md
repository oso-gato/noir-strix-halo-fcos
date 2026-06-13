# noir — design spec & runbook (v1.0.0)

Section 1 records the change-set that produced **v1.0.0** (every item shipped). Sections 2–3 are the as-built design + end-to-end runbook.

## 0. Goal
Make `oso-gato/noir-strix-halo-fcos` a **public** repo whose **fortnightly GitHub Actions build publishes two credential-free install ISOs**. No secret (password, Wi-Fi PSK) is ever baked or injected; the operator sets them on first boot. Hardware serials stay baked (accepted). Tailscale stays anchored to the wired bond; only general internet rides Wi-Fi when raised.

---

## 1. What v1.0.0 established, file by file

### Credentials
- **No build-time hash injection.** The fortnightly CI runner has no secret to inject, so there is none. `resolve_core_password_hash()` and the `.core-pw-hash` file are gone; `noir.bu`/`transpile.py` bake no `password_hash`, and `sync_check.py` simply asserts none is present.
- **`core` ships passwordless.** `noir.bu` carries **no** `password_hash`. SSH-key login + passwordless-sudo still work; Cockpit web login is disabled until first-boot setup sets the password.
- SSH keys are **not baked** — `build-iso.sh` fetches the account's current GitHub-published keys (`github.com/oso-gato.keys`) at build time and injects them into the `.ign` (each tagged by a short SHA256 fingerprint prefix; the build aborts on zero keys). Change keys on GitHub → the next build carries them.

### Wi-Fi
- **Replace the three SSID-named profiles** (`otherside`/`circus`/`zookeeper`) with **generic slots**, no SSID/PSK baked:
  - `wifi-primary` — autoconnect-priority 100, **route-metric 50**
  - `wifi-secondary` — autoconnect-priority 90, **route-metric 50**
  - `wifi-tertiary` — autoconnect-priority 80, **route-metric 50**
  - **All three supersede bond0** for noir's internet (metric 50 < bond0's 100). The priority chain (100/90/80) only decides *which* one connects when several are in range — only one Wi-Fi is active at a time on the single radio.
- **Rename helper** `noir-otherside` → `noir-wifi` (raises the highest-priority available slot), add `set-primary` subcommand.

### First-boot setup
- **`/usr/local/bin/noir-setup`** — interactive setter: Core password → each Wi-Fi slot's SSID + PSK (each skippable) → **Tailscale onboarding** (`tailscale up --hostname=noir --advertise-routes=10.0.50.0/24`; prints the auth URL to approve). Applies password via `chpasswd`, writes the NM keyfiles + `nmcli connection reload`. Then saves a copy of **all** of it — password hash, Wi-Fi keyfiles, **and `/var/lib/tailscale/tailscaled.state`** (the node identity) — to the data drive and writes a completion sentinel.
- **`noir-firstboot-setup.service`** ("Both" mode).
  - **Boot sequencing (critical — Wi-Fi isn't up on the first boot):** `noir-firstboot-install` rpm-ostree-layers `NetworkManager-wifi`/`wpa_supplicant`/`mt7xxx-firmware`/`wireless-regdb` on boot 1 and **reboots**, so `wlp99s0` only exists on the **post-layering (second) boot**. This service therefore gates on `ConditionPathExists=!/var/lib/noir/firstboot.stamp` + `After=noir-firstboot-enable.service NetworkManager-wait-online.service` — the **same gate the enable service uses** — so it fires only once Wi-Fi *and* `tailscaled` are live. **Do NOT use `ConditionFirstBoot`** (that's the too-early boot-1).
  - On that boot: persisted creds on the data drive (preserve) → restore silently, done;
  - else if a console is attached → run `noir-setup` on `tty1`;
  - else (headless) → MOTD banner: `ssh in && sudo noir-setup`.
  - A `flock` + completion **sentinel** make it first-one-wins (tty1 *or* SSH); the loser no-ops.
  - `noir-setup` also **probes Wi-Fi readiness** itself: if it's run during the brief boot-1 window (operator SSH'd in before the auto-reboot), it sets the **password** but **defers Wi-Fi** with "Wi-Fi support is still layering — the host will reboot once; re-run `sudo noir-setup` after it returns."
- **`noir-firstboot-restore`** path of the same service handles preserve restore.

### Routing (split, exit-node dropped)
- **Tailscale underlay pinned to bond0** — in the `bond0` NM keyfile (`[ipv4]`+`[ipv6]`):
  - `routing-rule: priority 5200 fwmark 0x80000/0xff0000 table 100`
  - `route: default via 10.0.50.1 dev bond0 table 100`
- **Drop `--advertise-exit-node`** from the `tailscale up` line; keep `--advertise-routes=10.0.50.0/24`.
- Subnet router (`10.0.50.0/24`) needs **no special routing** — LAN uses bond0's connected route automatically.
- `ip_forward` sysctl stays (subnet routing). **No `rp_filter` change** (that was only needed for the now-removed exit node).
- **Scope of this rule (important):** it pins **only Tailscale's own tailnet/underlay traffic** — the sockets `tailscaled` marks `0x80000` — to bond0. Nothing else is affected: noir's own internet, the LAN, and all other unmarked traffic follow the normal default route untouched.
- Any raised Wi-Fi slot (all metric 50) owns the **default route** for noir's own internet; bond0 carries it when no Wi-Fi is up.

### Gateway helper
- Table-100 default is hardcoded `via 10.0.50.1`; a small **`noir-table100.timer`/oneshot** reconciles it if the DHCP gateway ever changes.

### Hardware / serials
- Both NVMe serials stay baked (`build-iso.sh` dest-device, `noir.bu` data disk, `guard.sh`).

### Mirroring & gate
- Every `noir.bu` change is **mirrored in `transpile.py`**; build gates on `sync_check.py` (must print clean).
- `diagnose-v1.0.sh` (no password/exit-node assertions; Wi-Fi-slot + routing checks), the MOTD, `README`, and `CHANGELOG` all reflect the credential-free design.

### CI
- Add `.github/workflows/build.yml` — fortnightly cron: pick latest FCOS stable, bump the pin, build both ISOs, publish as a GitHub Release. No secrets needed.

---

## 2. The two images
| ISO | Data drive (4 TB) | Use |
|---|---|---|
| `noir-wipe.iso` | **formats** it (fresh) | first install, or full reset |
| `noir-preserve.iso` | **keeps** it | reinstall OS, keep containers/home/logs **and** the saved password + Wi-Fi |

Both always reinstall the OS on the 2 TB system drive.

---

## 3. End-to-end runbook

### Stage A — Automated build & publish (GitHub Actions, every 2 weeks)
1. Cron triggers `.github/workflows/build.yml`.
2. Runner queries the FCOS **stable** stream, bumps the pin to the newest build.
3. Runs `build-iso.sh` (podman + coreos-installer) → `noir-wipe.iso` + `noir-preserve.iso`. **No drives, no secrets needed** — serials are baked literals; the customize step only embeds strings.
4. Publishes both ISOs as assets on a dated GitHub **Release**.

### Stage B — Flash
Download both ISOs from the Release. `dd` each to its own USB stick. **Label them** (wipe is destructive to the data drive).

### Stage C — First boot, WIPE (fresh box / reset)
1. Boot the wipe USB. `guard.sh` matches the 2 TB system drive by serial, coreos-installer auto-installs (no prompt), reboots.
2. **Boot 1:** Ignition formats the 4 TB and brings up **bond0 (Ethernet, DHCP) + SSH-by-key**. `noir-firstboot-install` **rpm-ostree-layers** tailscale + the Wi-Fi stack (`NetworkManager-wifi`/`wpa_supplicant`/`mt7xxx-firmware`/`wireless-regdb`) and **reboots once**. **Wi-Fi is not available on boot 1** — only Ethernet/SSH.
3. **Boot 2** (post-layering): the layered packages are now live → `wlp99s0` Wi-Fi up, `tailscaled` started, Cockpit enabled. No persisted creds → MOTD shows `ssh in && sudo noir-setup`.
4. You **SSH in** (or use the console) and run `sudo noir-setup`:
   - set the **Core password** (Cockpit/sudo);
   - enter **primary** Wi-Fi SSID + PSK (your offshore hotspot), and optionally secondary/tertiary.
   - It applies them and **saves a copy to the 4 TB data drive**.

### Stage D — First boot, PRESERVE (reinstall, keep data)
1. Boot the preserve USB. Installs OS to 2 TB, **keeps** the 4 TB data drive.
2. First boot finds the **saved creds on the data drive** → restores the Core password + Wi-Fi keyfiles + **Tailscale node identity** (`tailscaled.state`) **silently**. **No setup, no prompt, no re-auth.** Box is up, Cockpit + Wi-Fi configured, and it **rejoins the tailnet** automatically.

### Stage E — Wi-Fi (day-to-day)
- `sudo noir-wifi up` — turn on your phone hotspot first, then this associates the **primary** slot; its metric-50 default route takes over → general internet now goes via Wi-Fi (offshore, GFW-bypassed, fast).
- `sudo noir-wifi down` — drops it; internet falls back to bond0.
- `sudo noir-wifi status` — shows default route + primary state.
- `sudo noir-wifi set-primary <secondary|tertiary|new-ssid>` — promote another network into the primary metric-50 slot (persisted).
- **All** Wi-Fi slots supersede bond0 when raised (all metric 50); the priority chain just decides which one connects when several are in range.

### Stage F — Tailscale
- Done **as the last step of `noir-setup`** on a wipe install (it runs `tailscale up --hostname=noir --advertise-routes=10.0.50.0/24`, no exit-node, and prints the auth URL). On preserve, the node identity is restored from the data drive, so it rejoins **without** re-auth.
- Approve the subnet route at the admin console (first time only).
- noir is a **subnet router** for `10.0.50.0/24`. Its underlay (control/DERP/peers) is **pinned to bond0** and stays there even when Wi-Fi is up.
- Manual equivalent if needed: `sudo tailscale up --hostname=noir --advertise-routes=10.0.50.0/24`.

### Stage G — Routing behavior (steady state)
| Traffic | Path |
|---|---|
| Tailscale's own underlay (tailnet connectivity) | **bond0** always (pinned) |
| LAN / `10.0.50.0/24` subnet routing | **bond0** (connected route) |
| noir's own general internet | **primary Wi-Fi** when raised, else bond0 |
| Secondary/tertiary Wi-Fi | fallback only, never supersede |

Trade-off (accepted): because the underlay is pinned, if bond0's internet ever fully died, Tailscale would **not** auto-failover to Wi-Fi. Fine for the preference use case (bond0 always healthy).

---

## 4. Pre-public history scrub (completed at v1.0.0)
Before going public, the git history was re-initialised from the v1.0.0 tree to a single clean commit, so **no bcrypt hash or Wi-Fi PSK survives in any commit** (earlier private commits that carried a baked hash inside `.ign` files are gone). The drive serials in `README`/comments are intentionally kept (a fingerprint, not a credential). Keep this invariant — never commit a secret back into history.
