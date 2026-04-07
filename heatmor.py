import json
import re
import subprocess
import time

import psutil
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

REFRESH = 2.0


def colorize_temp(val, warn=70, crit=85):
    if val >= crit:
        return f"[red]{val:.0f}°C[/red]"
    elif val >= warn:
        return f"[yellow]{val:.0f}°C[/yellow]"
    return f"[green]{val:.0f}°C[/green]"


def colorize_pct(val, warn=70, crit=90):
    if val >= crit:
        return f"[red]{val:.0f}%[/red]"
    elif val >= warn:
        return f"[yellow]{val:.0f}%[/yellow]"
    return f"[green]{val:.0f}%[/green]"


_IT8792_FAN_LABELS = {"fan1": "SYS_FAN5", "fan2": "SYS_FAN6", "fan3": "SYS_FAN4"}

_IT8792_VOLTAGE_LABELS = {
    "in0": "CPU Vcore",
    "in1": "DDR VTT",
    "in2": "Chipset",
}

# it8686 in6 = DRAM A/B voltage (only present after acpi_enforce_resources=lax + reboot)
_IT8686_VOLTAGE_LABELS = {
    "in6": "DRAM A/B",
}


def get_sensors():
    raw = subprocess.check_output(["sensors", "-j"], text=True, stderr=subprocess.DEVNULL)
    data = json.loads(raw)
    result = {}

    for key, val in data.items():
        if key.startswith("k10temp"):
            result["cpu_temp"] = val["Tctl"]["temp1_input"]
            break

    for key, val in data.items():
        if key.startswith("it8792"):
            # temp1=PCIEX8, temp3=System 2
            t1 = val.get("temp1", {}).get("temp1_input")
            t3 = val.get("temp3", {}).get("temp3_input")
            if t1 is not None:
                result["pciex8_temp"] = t1
            if t3 is not None:
                result["system2_temp"] = t3

            fans = {}
            for fan_key, label in _IT8792_FAN_LABELS.items():
                if fan_key in val:
                    rpm = val[fan_key].get(f"{fan_key}_input")
                    if rpm is not None and rpm > 0:
                        fans[label] = int(rpm)
            result["fans"] = fans

            voltages = {}
            for in_key, label in _IT8792_VOLTAGE_LABELS.items():
                if in_key in val:
                    v = val[in_key].get(f"{in_key}_input")
                    if v is not None:
                        voltages[label] = v
            result["voltages"] = voltages
            break

    for key, val in data.items():
        if key.startswith("it8686"):
            for in_key, label in _IT8686_VOLTAGE_LABELS.items():
                if in_key in val:
                    v = val[in_key].get(f"{in_key}_input")
                    if v is not None:
                        result.setdefault("voltages", {})[label] = v
            break

    for key, val in data.items():
        if key.startswith("nvme"):
            result["nvme_temp"] = val["Composite"]["temp1_input"]
            break

    return result


def get_gpu():
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total,clocks.current.graphics",
                "--format=csv,noheader,nounits",
            ],
            text=True,
        ).strip()
        temp, util, vram_used, vram_total, gpu_clock = [x.strip() for x in out.split(",")]
        return {
            "temp": float(temp),
            "util": float(util),
            "vram_used": int(vram_used),
            "vram_total": int(vram_total),
            "clock": int(gpu_clock),
        }
    except Exception:
        return None


def get_system():
    mem = psutil.virtual_memory()
    freq = psutil.cpu_freq()
    return {
        "cpu_pct": psutil.cpu_percent(interval=None),
        "cpu_mhz": int(freq.current) if freq else None,
        "ram_used": mem.used / 1024**3,
        "ram_total": mem.total / 1024**3,
        "ram_pct": mem.percent,
    }


def _get_dmi_field(text, field):
    m = re.search(rf'^\t{re.escape(field)}:\s*(.+)$', text, re.MULTILINE)
    return m.group(1).strip() if m else None


def _parse_memory_device(text):
    size = _get_dmi_field(text, "Size")
    if not size or size in ("No Module Installed", "Not Installed", "Unknown"):
        return None
    speed = _get_dmi_field(text, "Configured Memory Speed") or _get_dmi_field(text, "Speed")
    voltage = _get_dmi_field(text, "Configured Voltage")
    return {
        "slot": _get_dmi_field(text, "Locator") or "?",
        "size": size,
        "type": _get_dmi_field(text, "Type") or "",
        "speed": speed if speed and speed != "Unknown" else "N/A",
        "voltage": voltage if voltage and voltage != "Unknown" else "N/A",
        "manufacturer": _get_dmi_field(text, "Manufacturer") or "",
        "part": _get_dmi_field(text, "Part Number") or "",
    }


def get_ram_details():
    result = subprocess.run(
        ["sudo", "-n", "dmidecode", "--type", "17"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    stanzas = re.split(r'(?=^Memory Device)', result.stdout, flags=re.MULTILINE)
    devices = [_parse_memory_device(s) for s in stanzas]
    return [d for d in devices if d is not None]




def _row_count(table):
    """Number of data rows in a Rich Table."""
    return len(table.rows)


def _measure_panel(table, title):
    """Return the natural (width, height) of a panel wrapping the given table."""
    console = Console()
    p = Panel(table, title=f"[bold]{title}[/bold]", border_style="blue")
    w = p.__rich_measure__(console, console.options).maximum
    # height = 2 (panel border) + rows
    h = _row_count(table) + 2
    return w, h


def build_display(sensors, gpu, system, ram_details=None):
    temps = Table(show_header=False, box=None, padding=(0, 1))
    temps.add_column(style="dim")
    temps.add_column(justify="right")
    temps.add_row("CPU", colorize_temp(sensors.get("cpu_temp", 0), warn=70, crit=85))
    if gpu:
        temps.add_row("GPU", colorize_temp(gpu["temp"], warn=70, crit=85))
    temps.add_row("NVMe", colorize_temp(sensors.get("nvme_temp", 0), warn=50, crit=65))
    if "system2_temp" in sensors:
        temps.add_row("MOBO", colorize_temp(sensors["system2_temp"], warn=50, crit=65))

    usage = Table(show_header=False, box=None, padding=(0, 1))
    usage.add_column(style="dim")
    usage.add_column(justify="right")
    usage.add_column(justify="right", style="dim")
    cpu_freq_str = f"{system['cpu_mhz']} MHz" if system.get("cpu_mhz") else ""
    usage.add_row("CPU", colorize_pct(system['cpu_pct']), cpu_freq_str)
    if gpu:
        usage.add_row("GPU", colorize_pct(gpu['util']), f"{gpu['clock']} MHz")
        vram_pct = gpu["vram_used"] / gpu["vram_total"] * 100
        usage.add_row("VRAM", colorize_pct(vram_pct), f"{gpu['vram_used']}/{gpu['vram_total']} MB")
    usage.add_row("RAM", colorize_pct(system['ram_pct']), f"{system['ram_used']:.1f}/{system['ram_total']:.0f} GB")

    fans = Table(show_header=False, box=None, padding=(0, 1))
    fans.add_column(style="dim")
    fans.add_column(justify="right")
    for name, rpm in sensors.get("fans", {}).items():
        fans.add_row(name, f"[cyan]{rpm}[/cyan]")

    volts = Table(show_header=False, box=None, padding=(0, 1))
    volts.add_column(style="dim")
    volts.add_column(justify="right")
    for label, v in sensors.get("voltages", {}).items():
        volts.add_row(label, f"[cyan]{v:.3f} V[/cyan]")

    # Measure all panels
    titles = {"temps": "Temps", "usage": "Usage", "fans": "Fans (RPM)", "volts": "Voltages"}
    tw, th = _measure_panel(temps, titles["temps"])
    uw, uh = _measure_panel(usage, titles["usage"])
    fw, fh = _measure_panel(fans, titles["fans"])
    vw, vh = _measure_panel(volts, titles["volts"])

    # Left panels (temps/fans) share tight width; right panels share wider width
    wl = max(tw, fw)
    wr = max(uw, vw)
    # Paired panels share height
    ht = max(th, uh)
    hb = max(fh, vh)

    def _panel(table, title, width, height):
        return Panel(table, title=f"[bold]{title}[/bold]", border_style="blue", width=width, height=height)

    top = Table.grid()
    top.add_column()
    top.add_column()
    top.add_row(_panel(temps, titles["temps"], wl, ht), _panel(usage, titles["usage"], wr, ht))

    bottom = Table.grid()
    bottom.add_column()
    bottom.add_column()
    bottom.add_row(_panel(fans, titles["fans"], wl, hb), _panel(volts, titles["volts"], wr, hb))

    # Outer panel width = left + right panel widths + 2 border + 2 padding
    outer_width = wl + wr + 4

    return Panel(
        Group(top, bottom),
        title="[bold white]heatmor[/bold white]",
        border_style="bright_blue",
        width=outer_width,
    )


def main():
    psutil.cpu_percent(interval=None)  # prime the first reading
    ram_details = get_ram_details()
    with Live(refresh_per_second=0.5, screen=True) as live:
        while True:
            try:
                sensors = get_sensors()
                gpu = get_gpu()
                system = get_system()
                live.update(build_display(sensors, gpu, system, ram_details))
            except Exception as e:
                live.update(f"[red]Error: {e}[/red]")
            time.sleep(REFRESH)


if __name__ == "__main__":
    main()
