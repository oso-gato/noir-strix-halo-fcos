# noir — changelog

## v1.0.1 (2026-06-14)

- **SSH keys are now pulled at build time, not baked.** The repo carries no key material. `build-iso.sh` fetches the account's current GitHub-published public keys (`github.com/oso-gato.keys`) and injects them into each ISO, tagging each by a short SHA256 fingerprint prefix (`oSo` / `Alchemist` / `Fatima`; unknown keys get a generic `oso-gato@github` tag). Change keys on GitHub → the next fortnightly build carries them. The build **hard-fails if it fetches zero keys** and verifies the baked ISO actually embeds them — passwordless `core` + key-only SSH means a keyless image would be an unreachable brick.
- Documentation/comment corrections from a post-publish audit (no functional change): docs now describe the shipped, public, credential-free v1.0.0 instead of design-phase placeholders.

## v1.0.0 (2026-06-13)

Initial public release. A declarative Fedora CoreOS install for **noir**, a headless homelab host (Minisforum MS-S1 MAX / AMD Strix Halo): Tailscale subnet router + Podman + Cockpit, built into two install ISOs (`noir-wipe.iso` / `noir-preserve.iso`) from one Butane source.

- **No baked credentials.** The `core` password and the Wi-Fi SSID/PSK are set at **first boot** via `noir-setup`; nothing secret ships in the repo or the published images. SSH public keys authorize access.
- **Wi-Fi:** three generic slots (`wifi-primary/secondary/tertiary`, all `route-metric=50`), filled at first boot and controlled by `noir-wifi`. The MediaTek MT7925 stack is rpm-ostree-layered on the first boot, so Wi-Fi is available only after the automatic reboot (boot 2).
- **Routing:** Tailscale's underlay is pinned to the wired bond (`bond0`) via `fwmark 0x80000` policy routing, so the tailnet stays on the physical link even when a Wi-Fi slot owns the internet default route. Subnet router for `10.0.50.0/24`.
- **Build:** `transpile.py` hand-transpiles `noir.bu` → Ignition; `sync_check.py` gates the two in lockstep. Fortnightly GitHub Actions builds and publishes both ISOs as a Release.
- **Base:** Fedora CoreOS *stable*, pinned in `build-iso.sh`.

Full design + runbook: **`BUILD-SPEC.md`** · hardware: **`HARDWARE.md`** · build rules for agents: **`AGENTS.md`**.
