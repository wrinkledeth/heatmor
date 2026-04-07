# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running

```bash
pip install -r requirements.txt
python heatmor.py
```

Refreshes every 2 seconds (`REFRESH = 2.0` constant). Press `Ctrl+C` to exit.

## Architecture

Single-file TUI app (`heatmor.py`) using `rich` for live terminal rendering.

**Data collection layer** — five independent functions, each wrapping an external source:
- `get_sensors()` — parses `sensors -j` JSON; hard-coded chip names: `k10temp` (AMD CPU), `it8792` (mobo/fans), `nvme-*` (NVMe)
- `get_gpu()` — calls `nvidia-smi`; returns `None` if unavailable (GPU panel is omitted)
- `get_system()` — uses `psutil` for CPU% and RAM
- `get_ram_details()` — calls `sudo -n dmidecode --type 17`; returns `None` if sudo fails (panel shows "requires root")
- `get_ram_errors()` — scans `journalctl -k` for MCE/EDAC patterns over the past 7 days

**Display layer** — `build_display()` takes collected data and returns a nested `rich` layout (Panel > Group > Columns > Panel > Table). The live loop in `main()` calls `live.update()` each iteration.

## Hardware Assumptions

Targeted at a **Gigabyte X470 Gaming 5** (AMD Ryzen). Sensor keys in `get_sensors()` are tuned to specific chips:

| Chip | Role |
|---|---|
| `k10temp` | AMD CPU die temp (Tctl) |
| `it8792-isa-0a60` | PCIe slot temp, System 2 temp, fan RPMs (SYS_FAN4/5/6), and secondary voltages (CPU Vcore, DDR VTT, Chipset, CPU Vdd18, DDR Vpp) |
| `it8686` | Primary voltage controller — `in6` = DRAM A/B voltage. Only appears after adding `acpi_enforce_resources=lax` to `GRUB_CMDLINE_LINUX_DEFAULT` and rebooting. |
| `nvme-*` | First NVMe Composite temp |

A sensors config at `/etc/sensors.d/x470-gaming5.conf` can apply labels to `it8792` channels (in0–in5, temp1/3, fan1–3).

RAM detail collection requires passwordless sudo for `dmidecode` — configure `/etc/sudoers` with `NOPASSWD: /usr/sbin/dmidecode` if needed.
