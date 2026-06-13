# noir — hardware reference (Minisforum MS-S1 MAX, AMD Strix Halo)

> This file is the single source of truth for **what noir physically is**, and — more importantly for anyone continuing the build — **why the FCOS image is layered and sequenced the way it is**. Every config decision in `noir.bu` (layered packages, the boot-1→boot-2 Wi-Fi sequence, the serial-pinned install target) traces back to a fact on this page.
>
> Verified June 2026 against Minisforum's official spec page + ServeTheHome's review + AMD/NotebookCheck/TechPowerUp. Confidence noted where it matters. **This is uniquely the noir hardware** — a different chassis would change the layering set.

## Identity
- **Model:** Minisforum **MS-S1 MAX** (announced 2025-09-16; Strix Halo mini-workstation, deployable desktop or 2U).
- **Platform:** AMD **Strix Halo** — a multi-chip APU/SiP (Zen 5 CCD chiplet(s) + a large I/O die carrying the GPU, NPU, memory controllers, and Infinity Cache on one package). **There is no discrete motherboard chipset** (no B650/X670-class chip) — all platform I/O is integrated into the APU.

## Core spec
| Component | Spec | Notes / source |
|---|---|---|
| **APU** | AMD **Ryzen AI Max+ 395** — 16C / 32T, Zen 5, TSMC 4 nm. Base 3.0 GHz, boost 5.1 GHz. 16 MB L2 + 64 MB L3 + 32 MB Infinity (memory-side) cache. | AMD / NotebookCheck |
| **iGPU** | **Radeon 8060S** — 40 RDNA 3.5 CUs (2560 shaders), ~2.9 GHz; ~RTX 4070-Laptop class | ServeTheHome |
| **NPU** | **XDNA 2** — 50 TOPS (INT8). **Combined CPU+GPU+NPU = 126 TOPS** | AMD |
| **Power (silicon)** | default TDP 55 W; cTDP 45–120 W | AMD |
| **Power (this chassis)** | **130 W sustained / 160 W peak** PPT; selectable perf modes; **320 W internal PSU** | Minisforum |
| **Memory** | **128 GB LPDDR5X-8000**, **soldered / on-package, non-upgradeable**. 256-bit bus (Minisforum calls it quad-channel = 4×64-bit; PHY is eight 32-bit sub-channels). ~256 GB/s theoretical, **~215 GB/s measured**. Unified (UMA) across CPU/GPU/NPU — no PCIe copy. | AMD / TechPowerUp / STH |
| **System drive** | 2 TB WD_BLACK SN850X NVMe — serial `25281F806642` (FCOS install target) | noir config |
| **Data drive** | 4 TB WD_BLACK SN850X NVMe — serial `25278B803296` (XFS: `/var/home` / `/var/lib/containers` / `/var/log`) | noir config |
| **BIOS** | AMI Aptio; IOMMU enabled by default (Strix Halo NPU SVA paths require it) | noir config |

### Storage slots — IMPORTANT asymmetry
Two M.2 2280 NVMe slots, **not equal**:
- **Slot 1 — PCIe 4.0 x4** (~7 GB/s) — up to 8 TB
- **Slot 2 — PCIe 4.0 x1** (~2 GB/s) — up to 8 TB
- RAID 0/1 supported.

**Consequence:** one of noir's two SN850X drives sits in the **x1** slot and is throttled to ~¼ of its rated speed. Confirm physically which drive is where; the I/O-heavier role (data drive / containers) ideally takes the **x4** slot.

## Networking chipsets
| | Chipset | Linux driver | Kernel floor | Notes |
|---|---|---|---|---|
| **Wired** | 2× **Realtek RTL8127** 10 GbE RJ45 (kernel ID: RTL8127**A**) | **`r8169`** (in-tree) | **6.16** (RTL8127A added to r8169); suspend/shutdown hang fixed in **6.18** | Consumer silicon — **no RoCEv2/RDMA**. Minisforum's own guide pushes an out-of-tree `r8127-dkms` (needs Secure Boot off) — **noir does not need it**: FCOS 44's kernel 6.19 has in-tree support. The LACP bond is an OS choice (kernel bonding driver), not a NIC feature. |
| **Wireless** | **MediaTek MT7925** (Filogic 360) — Wi-Fi 7 / 802.11be, 2×2 tri-band (2.4/5/6 GHz), 160 MHz, 4K-QAM, MLO | **`mt7925e`** (mt76 family) | **6.7** | **Requires firmware blobs** (see below). |
| **Bluetooth** | Bluetooth **5.4** — same MT7925 combo module | `btusb`/mt7925 | 6.7 | Shares antenna with Wi-Fi |

## Ports / I/O (full map)
**No Thunderbolt at all** — Strix Halo exposes **USB4** (royalty-free superset), not Intel Thunderbolt. There are **four** USB-C/USB4 ports across two speed tiers:

**Rear:** 2× **USB4 V2** Type-C (**80 Gbps**, DP-Alt 2.0, PD-out 15 W) · 1× USB-A 10 Gbps (USB 3.2 Gen2) · 2× USB-A 2.0 · 2× **10 GbE** RJ45 · 1× **HDMI 2.1 FRL** (8K@60 / 4K@120) · anti-theft lock · reset.
**Front:** 2× **USB4** Type-C (**40 Gbps**, DP-Alt 2.0, PD-out 15 W) · 1× USB-A 10 Gbps · 3.5 mm audio combo · 2× DMIC (AI mics) · power.
**Internal:** 1× PCIe **x16 mechanical / PCIe 4.0 x4 electrical** slot (half-height cards / SFF GPU). **No** dedicated DisplayPort (DP 2.0 via USB4 alt-mode only). **No** SD reader. **No** OCuLink.

### "Thunderbolt 2 / Thunderbolt 4 Version 2" — resolved
The machine has **no Thunderbolt-branded ports**. Mapping the colloquial names to reality:
- **"Thunderbolt 4 Version 2"** = the rear **2× USB4 V2 (80 Gbps)** ports. (USB4 v2's 80 Gbps matches Thunderbolt 5 bandwidth, hence the confusion — but it's USB4 v2, not TB.)
- **"Thunderbolt 2"** = the front **2× USB4 v1 (40 Gbps)** ports (Thunderbolt-4-class bandwidth).

## FCOS / Linux implications — why the build looks the way it does
This is the part that bites if you don't know the hardware. FCOS is an immutable, minimal base; **this hardware forces specific rpm-ostree layering**, and one of those layers creates the boot sequencing trap.

1. **Wi-Fi is the reason for the two-boot first-run.** The `mt7925e` *driver* is in-tree (FCOS 44 = kernel 6.19 ≥ 6.7 ✓), but FCOS base ships **no MediaTek Wi-Fi firmware** and **no NM Wi-Fi plugin**. So `noir-firstboot-install` rpm-ostree-layers, on **boot 1**:
   - `mt7xxx-firmware` → `/lib/firmware/mediatek/mt7925/{WIFI_MT7925_PATCH_MCU_1_1_hdr.bin, WIFI_RAM_CODE_MT7925_1_1.bin, BT_RAM_CODE_MT7925_1_1_hdr.bin}` — **without these, `mt7925e` won't initialize and there is no `wlp99s0`**.
   - `NetworkManager-wifi` (the NM Wi-Fi plugin; without it NM reports `wlp99s0` *unmanaged*) + `wpa_supplicant` (WPA3-SAE backend) + `wireless-regdb` (6 GHz / Wi-Fi 7 channel regulatory).
   - rpm-ostree layers apply **only on reboot**, so the service then `systemctl reboot`s.
   - **⇒ Wi-Fi (`wlp99s0`) does not exist until boot 2 (the post-layering reboot).** Anything that touches Wi-Fi — `noir-setup`, `noir-wifi` — **must gate on boot 2+** (`ConditionPathExists=!/var/lib/noir/firstboot.stamp` + `After=noir-firstboot-enable.service`). Never `ConditionFirstBoot` (that's the too-early boot 1). This is the single most important sequencing fact for the build.
2. **Wired NIC needs nothing layered.** RTL8127A is in `r8169` since 6.16; FCOS 44's 6.19 has it (and the 6.18 suspend-hang fix). Ethernet + SSH are up from boot 1 — which is exactly why the operator can SSH in to run `noir-setup` even before Wi-Fi exists.
3. **Secure Boot can stay on.** Because noir uses the in-tree `r8169`/`mt7925e` (not Minisforum's out-of-tree `r8127-dkms`, which would require Secure Boot off), Secure Boot doesn't have to be disabled for networking.
4. **Kernel floor (all satisfied by FCOS 44 / 6.19):** `mt7925e` ≥ 6.7 · RTL8127A in `r8169` ≥ 6.16 · RTL8127 suspend fix ≥ 6.18.
5. **Install-target identity is serial-pinned** because the dual NVMe + the PCIe-x1/x4 asymmetry mean device-name (`/dev/nvme*`) ordering isn't stable; `guard.sh` matches `/dev/disk/by-id/` serial + size + model before any write.
6. **IOMMU on** (BIOS default) for the XDNA 2 NPU's SVA paths.

## Sources
Minisforum official (`minisforum.com/products/ms-s1-max`, `store.minisforum.com`) · ServeTheHome MS-S1 MAX review · AMD Ryzen AI Max+ 395 product page · NotebookCheck · TechPowerUp Strix Halo analysis · kernel.org / linux-firmware (mt7925, r8169 RTL8127A).
