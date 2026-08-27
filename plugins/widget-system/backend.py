from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

import psutil

from app.plugins.contracts import DashboardWidget, PluginTestResult


_last_disk_read_bytes: int | None = None
_last_disk_read_at: float | None = None
_METRICS_CACHE_TTL = 1.0
_metrics_cache: dict[str, Any] = {"at": 0.0, "value": None}


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _read_gpu_info() -> dict[str, Any]:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return {"available": False}
        name, util, mem_used, mem_total, temp, power = [
            value.strip() for value in result.stdout.strip().splitlines()[0].split(",")[:6]
        ]
        return {
            "available": True,
            "name": name,
            "gpu_util": _safe_float(util),
            "mem_used": round(_safe_float(mem_used) / 1024, 1),
            "mem_total": round(_safe_float(mem_total) / 1024, 1),
            "temp": _safe_float(temp),
            "power": _safe_float(power),
        }
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"available": False, "gpu_util": 0, "mem_used": 0, "mem_total": 0, "temp": 0, "power": 0}


def _read_cpu_temp() -> float:
    for path in (
        Path("/sys/class/thermal/thermal_zone0/temp"),
        Path("/sys/class/hwmon/hwmon0/temp1_input"),
    ):
        try:
            return round(int(path.read_text(encoding="utf-8").strip()) / 1000.0, 1)
        except (OSError, ValueError):
            continue
    return 0.0


def _read_disk_rate() -> float:
    global _last_disk_read_at, _last_disk_read_bytes
    counters = psutil.disk_io_counters()
    now = time.monotonic()
    current = int(counters.read_bytes) if counters else 0
    rate = 0.0
    if _last_disk_read_bytes is not None and _last_disk_read_at is not None:
        elapsed = now - _last_disk_read_at
        if elapsed > 0:
            rate = max(0.0, current - _last_disk_read_bytes) / elapsed / (1024**2)
    _last_disk_read_bytes = current
    _last_disk_read_at = now
    return round(rate, 1)


def build_metrics_payload() -> dict[str, Any]:
    now = time.monotonic()
    cached = _metrics_cache.get("value")
    if cached is not None and now - float(_metrics_cache.get("at") or 0.0) < _METRICS_CACHE_TTL:
        return cached
    memory = psutil.virtual_memory()
    payload = {
        "gpu": _read_gpu_info(),
        "cpu_mem": {
            "cpu_util": psutil.cpu_percent(interval=0.1),
            "mem_used": round(memory.used / (1024**3), 1),
            "mem_total": round(memory.total / (1024**3), 1),
            "cpu_temp": _read_cpu_temp(),
            "disk_read": _read_disk_rate(),
        },
    }
    _metrics_cache["value"] = payload
    _metrics_cache["at"] = now
    return payload


async def test(config: dict[str, Any]) -> PluginTestResult:
    return PluginTestResult(ok=True, message="system metrics ready", details=build_metrics_payload())


async def build_widget(config: dict[str, Any]) -> DashboardWidget | None:
    if config.get("show_dashboard_widget") is False:
        return None
    payload = build_metrics_payload()
    cpu = _safe_float(payload["cpu_mem"].get("cpu_util"))
    gpu = _safe_float(payload["gpu"].get("gpu_util"))
    return DashboardWidget(
        plugin_id="widget-system",
        key="system-overview",
        title="系统监控",
        badge=f"CPU {cpu:.0f}% · GPU {gpu:.0f}%",
        payload={"kind": "system_metrics", **payload},
    )


async def handle_action(action: str, config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    if action != "metrics":
        raise ValueError(f"unsupported action: {action}")
    return {
        "ok": True,
        "data": build_metrics_payload(),
        "poll_interval_ms": int(config.get("poll_interval_ms") or 5000),
    }
