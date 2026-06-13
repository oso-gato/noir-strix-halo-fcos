# noir — custom Fedora CoreOS install (Minisforum MS-S1 MAX / Strix Halo)

**noir** is a headless **Fedora CoreOS** homelab host: a **Tailscale subnet router** (`10.0.50.0/24`) + Podman container host + Cockpit web admin. One Butane source (`noir.bu`) builds two install ISOs — **`noir-wipe.iso`** (formats the 4 TB data drive) and **`noir-preserve.iso`** (keeps it). It carries **no baked credentials**: the password and Wi-Fi are set at first boot.

Detailed docs: **[`BUILD-SPEC.md`](BUILD-SPEC.md)** (design + full runbook) · **[`HARDWARE.md`](HARDWARE.md)** (the hardware that dictates the build) · **[`AGENTS.md`](AGENTS.md)** (build rules for any AI agent maintaining this repo).

## Build principles (oso-gato house standard)
- **Package hierarchy:** ① Fedora official repos (`rpm-ostree` layering) → ② the vendor's own official RPM repo (`.repo` verbatim, `gpgcheck=1`, dated — e.g. Tailscale) → ③ worst case, the vendor's released `.rpm`/image. **Never** `curl|sh`, `pip`/`npm`/`cargo`/`go`/`gem`/`brew`, tarballs-to-PATH, or third-party repos (COPR/Flathub/snap).
- **Less is more** · **immutable base** (changes only by rebuilding from `noir.bu`) · **no baked secrets** · public IP = **key-only SSH only**, Cockpit/sensitive ports **tailnet-only**.

## Two facts that shape everything
1. **Wi-Fi is a *second-boot* thing.** FCOS ships no MT7925 firmware/NM-wifi plugin, so the first boot rpm-ostree-layers them and **reboots**; `wlp99s0` only exists on boot 2 — first-boot setup runs only then.
2. **`noir.bu` and `transpile.py` are kept byte-identical, gated by `sync_check.py`** — every change goes in both.

## Quick start
1. **Build** (macOS/Linux with podman): `./build-iso.sh` → `noir-preserve.iso` + `noir-wipe.iso`. (Fortnightly CI does this and publishes a Release.)
2. **Flash** each ISO to its own labelled USB (`sudo dd if=<iso> of=/dev/rdiskN bs=4m`).
3. **First boot:** the box auto-installs to the system drive, layers the Wi-Fi/Tailscale stack, **reboots once**, then comes up on Ethernet + SSH-by-key. On a wipe install it shows: `ssh in && sudo noir-setup`.
4. **Set it up:** `sudo noir-setup` (on the console *or* over SSH — first one wins) sets the Core password, the Wi-Fi slot SSIDs/PSKs, and runs `tailscale up`. All of it is saved to the data drive, so a **preserve** re-flash restores it with no re-auth.

## Wi-Fi control — `noir-wifi`
`sudo noir-wifi on` raises the highest-priority configured slot (any slot beats `bond0` for internet); `off` reverts to the wired link; `switch <primary|secondary|tertiary>` selects a network; `set-primary <slot|SSID>` promotes one; `status` / `list` inspect. Tailscale's tailnet traffic stays on `bond0` regardless.

## SSH access — keys are pulled at build, not baked
The repo bakes **no** SSH keys. At build time `build-iso.sh` fetches the account's **current** GitHub-published public keys (`https://github.com/oso-gato.keys`) and injects them into the image, each tagged by a short SHA256 fingerprint prefix; change the keys on GitHub and the next build picks them up. (The build hard-fails if it fetches zero keys, so it can never produce an unreachable image.) The private halves live in 1Password.

On macOS, with the 1Password **SSH agent** enabled (Settings → Developer), add a host entry — `ssh` presents the **public** key named in `IdentityFile`, and 1Password matches it to the vault item and signs (Touch ID), so the private key never touches disk:

```bash
printf '\n%s\n%s\n%s\n' \
  'Host noir noir.local' \
  '  User core' \
  '  IdentityFile ~/.ssh/oso-gato.pub' \
  >> ~/.ssh/config
```
Your `Host *` block should already point `IdentityAgent` at `~/Library/Group Containers/2BUA8C4S2C.com.1password/t/agent.sock`. Then `ssh core@noir.local`. (First boot is over the LAN; after Tailscale is up, `ssh noir` works via MagicDNS.)
