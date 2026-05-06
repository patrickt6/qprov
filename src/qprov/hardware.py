"""Hardware and environment introspection.

Used at @tracked decoration time to record the machine the computation ran on.
Soft-imports psutil and pynvml so the module stays importable without GPU
deps.
"""
from __future__ import annotations

import os
import platform
import socket
import sys
from dataclasses import dataclass


@dataclass
class HardwareInfo:
    hostname: str
    cpu_model: str | None
    ram_gb: float | None
    gpu_model: str | None
    python_version: str
    sage_version: str | None
    os_info: str


def _cpu_model() -> str | None:
    """Best-effort CPU model. Falls back across platforms."""
    try:
        if sys.platform == "linux":
            with open("/proc/cpuinfo", "r") as f:
                for line in f:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
        if sys.platform == "darwin":
            import subprocess
            out = subprocess.check_output(
                ["sysctl", "-n", "machdep.cpu.brand_string"], text=True
            )
            return out.strip()
        if sys.platform == "win32":
            return platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER")
    except Exception:
        pass
    return platform.processor() or None


def _ram_gb() -> float | None:
    try:
        import psutil  # type: ignore
        return round(psutil.virtual_memory().total / (1024**3), 2)
    except Exception:
        return None


def _gpu_model() -> str | None:
    try:
        import pynvml  # type: ignore
        pynvml.nvmlInit()
        try:
            count = pynvml.nvmlDeviceGetCount()
            if count == 0:
                return None
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            name = pynvml.nvmlDeviceGetName(handle)
            return name.decode("utf-8") if isinstance(name, bytes) else str(name)
        finally:
            pynvml.nvmlShutdown()
    except Exception:
        return None


def _sage_version() -> str | None:
    try:
        import sage.version  # type: ignore
        return getattr(sage.version, "version", None)
    except Exception:
        return None


def collect() -> HardwareInfo:
    return HardwareInfo(
        hostname=socket.gethostname(),
        cpu_model=_cpu_model(),
        ram_gb=_ram_gb(),
        gpu_model=_gpu_model(),
        python_version=platform.python_version(),
        sage_version=_sage_version(),
        os_info=f"{platform.system()} {platform.release()}",
    )
