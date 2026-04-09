import json
import sqlite3
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
            # temp3=System 2 (motherboard)
            t3 = val.get("temp3", {}).get("temp3_input")
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
        "ram_used": (mem.total - mem.available) / 1024**3,
        "ram_total": mem.total / 1024**3,
        "ram_pct": mem.percent,
    }



RASDAEMON_DB = "/var/lib/rasdaemon/ras-mc_event.db"


def get_hw_errors():
    try:
        conn = sqlite3.connect(RASDAEMON_DB)
        cur = conn.cursor()
        errors = {}

        # Memory controller errors
        mc_ce = cur.execute("SELECT COALESCE(SUM(err_count), 0) FROM mc_event WHERE err_type = 'Corrected'").fetchone()[0]
        mc_ue = cur.execute("SELECT COALESCE(SUM(err_count), 0) FROM mc_event WHERE err_type = 'Uncorrected'").fetchone()[0]
        if mc_ce or mc_ue:
            errors["Memory"] = {"ce": mc_ce, "ue": mc_ue}

        # Machine Check Exceptions
        mce_count = cur.execute("SELECT COUNT(*) FROM mce_record").fetchone()[0]
        if mce_count:
            errors["MCE"] = {"count": mce_count}

        # PCIe AER errors
        aer_count = cur.execute("SELECT COUNT(*) FROM aer_event").fetchone()[0]
        if aer_count:
            errors["PCIe"] = {"count": aer_count}

        # Extended logs
        extlog_count = cur.execute("SELECT COUNT(*) FROM extlog_event").fetchone()[0]
        if extlog_count:
            errors["Extlog"] = {"count": extlog_count}

        # Recent errors across all tables (union of timestamps + source)
        recent = cur.execute(
            "SELECT timestamp, 'Memory' AS src, err_type || ': ' || err_msg AS detail FROM mc_event "
            "UNION ALL SELECT timestamp, 'MCE', error_msg FROM mce_record "
            "UNION ALL SELECT timestamp, 'PCIe', err_type || ': ' || err_msg FROM aer_event "
            "UNION ALL SELECT timestamp, 'Extlog', 'severity ' || severity FROM extlog_event "
            "ORDER BY timestamp DESC LIMIT 3"
        ).fetchall()

        conn.close()
        return {"errors": errors, "recent": recent}
    except Exception:
        return None


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


def build_display(sensors, gpu, system, hw_errors):
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

    # Health errors panel
    if hw_errors is None:
        health_text = "[dim]Unavailable (rasdaemon)[/dim]"
    elif not hw_errors["errors"]:
        health_text = "[green]No hardware errors[/green]"
    else:
        parts = []
        for src, info in hw_errors["errors"].items():
            if "ce" in info:
                ce_str = f"[yellow]CE:{info['ce']}[/yellow]" if info["ce"] else "CE:0"
                ue_str = f"[red]UE:{info['ue']}[/red]" if info["ue"] else "UE:0"
                parts.append(f"{src} {ce_str} {ue_str}")
            else:
                parts.append(f"[red]{src}: {info['count']}[/red]")
        health_text = "  ".join(parts)
        for ts, src, detail in hw_errors["recent"]:
            health_text += f"\n[dim]{ts}[/dim] [{src}] {detail}"

    health_panel = Panel(health_text, title="[bold]Errors[/bold]", border_style="blue", width=outer_width - 4)

    return Panel(
        Group(top, bottom, health_panel),
        title="[bold white]heatmor[/bold white]",
        border_style="bright_blue",
        width=outer_width,
    )


def main():
    psutil.cpu_percent(interval=None)  # prime the first reading
    with Live(refresh_per_second=0.5, screen=True) as live:
        while True:
            try:
                sensors = get_sensors()
                gpu = get_gpu()
                system = get_system()
                hw_errors = get_hw_errors()
                live.update(build_display(sensors, gpu, system, hw_errors))
            except Exception as e:
                live.update(f"[red]Error: {e}[/red]")
            time.sleep(REFRESH)


if __name__ == "__main__":
    main()
