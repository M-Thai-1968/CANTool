"""platform_info.py - Betriebssystem-Erkennung und OS-abhaengige Hardware-Unterstuetzung (Windows / Linux).

Alles, was sich zwischen Windows und Linux unterscheidet, steht hier:
- welche Hardware-Typen sinnvoll sind (Vector-XL-Treiber nur unter Windows, SocketCAN nur unter Linux),
- Erkennung vorhandener Schnittstellen (python-can detect_available_configs, SocketCAN-Geraete unter /sys/class/net),
- Pruefung, ob die noetigen Treiberbibliotheken gefunden werden.
Die Funktionen sind frei von Qt und werfen keine Ausnahmen (ausser wo dokumentiert).
"""
from __future__ import annotations

import ctypes.util
import platform
import sys
from pathlib import Path
from typing import Any

WINDOWS, LINUX, MACOS, OTHER = "windows", "linux", "macos", "other"


def current_os() -> str:
    """'windows', 'linux', 'macos' oder 'other' (zur Laufzeit erkannt)."""
    p = sys.platform
    if p.startswith("win") or p == "cygwin":
        return WINDOWS
    if p.startswith("linux"):
        return LINUX
    if p == "darwin":
        return MACOS
    return OTHER


def os_label() -> str:
    """Kurzbezeichnung fuer Titelzeile und Info, z. B. 'Windows 11' oder 'Linux 6.8'."""
    name = {WINDOWS: "Windows", LINUX: "Linux", MACOS: "macOS"}.get(current_os(), platform.system() or "?")
    release = platform.release()
    return f"{name} {release}".strip()


def supported_hardware(os_name: str | None = None) -> dict[str, bool]:
    """Hardware-Typ -> unter diesem Betriebssystem nutzbar?

    - vector: python-can nutzt den Vector-XL-Treiber (vxlapi) -> nur Windows.
    - socketcan: Linux-Kernel-Schnittstelle -> nur Linux.
    - pcan: Windows (PCAN-Basic-DLL) und Linux (PCAN-Basic-Bibliothek oder Kernel-Treiber via SocketCAN).
    """
    o = os_name or current_os()
    return {"simulation": True, "vector": o == WINDOWS, "pcan": o in (WINDOWS, LINUX, MACOS),
            "socketcan": o == LINUX}


def hardware_supported(hw_type: str, os_name: str | None = None) -> bool:
    return supported_hardware(os_name).get(hw_type, True)


def default_channel(hw_type: str, os_name: str | None = None) -> str:
    """Sinnvoller Standardkanal je Hardware-Typ (nur als Vorschlag)."""
    if hw_type == "pcan":
        return "PCAN_USBBUS1"
    if hw_type == "socketcan":
        return "can0"
    if hw_type == "vector":
        return "0"
    return ""


def socketcan_interfaces() -> list[str]:
    """CAN-Netzwerkgeraete unter Linux (z. B. can0, vcan0): Typ 280 = ARPHRD_CAN."""
    found = []
    base = Path("/sys/class/net")
    try:
        for dev in sorted(base.iterdir()):
            try:
                if (dev / "type").read_text().strip() == "280":
                    found.append(dev.name)
            except OSError:
                continue
    except OSError:
        pass
    return found


def vector_dll_name() -> str:
    """Name der Vector-XL-Bibliothek: 64-bit-Python braucht vxlapi64, 32-bit-Python vxlapi."""
    return "vxlapi64" if sys.maxsize > 2**32 else "vxlapi"


def add_driver_search_dirs(base: Path | None = None) -> list[str]:
    """Windows: Ordner des Tools (und ./drivers) in die DLL-Suche aufnehmen, damit eine dort abgelegte
    vxlapi64.dll bzw. PCANBasic.dll gefunden wird. Rueckgabe: hinzugefuegte Ordner."""
    import os
    if current_os() != WINDOWS:
        return []
    root = base or Path(__file__).resolve().parent
    added = []
    for d in (root, root / "drivers"):
        if d.is_dir() and any((d / n).exists() for n in (vector_dll_name() + ".dll", "PCANBasic.dll")):
            if str(d) not in os.environ.get("PATH", ""):
                os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
            try:
                os.add_dll_directory(str(d))
            except (AttributeError, OSError):
                pass
            added.append(str(d))
    return added


def driver_available(hw_type: str, os_name: str | None = None) -> bool | None:
    """True/False, ob die Treiberbibliothek gefunden wird; None = nicht pruefbar."""
    o = os_name or current_os()
    if hw_type == "vector":
        if o != WINDOWS:
            return False
        add_driver_search_dirs()
        return bool(ctypes.util.find_library(vector_dll_name()))
    if hw_type == "pcan":
        if o == WINDOWS:
            return bool(ctypes.util.find_library("PCANBasic"))
        if o == LINUX:
            return bool(ctypes.util.find_library("pcanbasic") or socketcan_interfaces())
        return None
    if hw_type == "socketcan":
        return o == LINUX
    return None


def vector_devices() -> list[dict[str, Any]]:
    """Vector-Kanaele laut Treiber: [{'serial', 'hw_channel', 'channel_index', 'name', 'virtual'}]; leer bei Fehler/ohne Treiber."""
    try:
        import can
        configs = can.detect_available_configs(["vector"])
    except Exception:                                      # noqa: BLE001 - fehlender Treiber/Hardware
        return []
    out = []
    for c in configs:
        cfg = c.get("vector_channel_config")
        name = str(getattr(cfg, "name", "") or c.get("name", ""))
        serial = int(c.get("serial") or getattr(cfg, "serial_number", 0) or 0)
        out.append({"serial": serial, "hw_channel": c.get("channel"), "channel_index": c.get("channel_index"),
                    "name": name, "virtual": serial == 0 or "virtual" in name.lower()})
    return out


def detect_hardware() -> list[dict[str, Any]]:
    """Gefundene Schnittstellen als python-can-Konfigurationen ({'interface', 'channel', ...}); leer bei Fehler.

    Nur Schnittstellen, die auf dem aktuellen Betriebssystem vorkommen koennen, werden abgefragt.
    """
    try:
        import can
    except ImportError:
        return []
    ifaces = [i for i, t in (("vector", "vector"), ("pcan", "pcan"), ("socketcan", "socketcan"))
              if hardware_supported(t)]
    try:
        configs = can.detect_available_configs(ifaces)
    except Exception:                                      # noqa: BLE001 - fehlende Treiber duerfen nicht stoeren
        configs = []
    found = [dict(c) for c in configs]
    if hardware_supported("socketcan") and not any(c.get("interface") == "socketcan" for c in found):
        found += [{"interface": "socketcan", "channel": n} for n in socketcan_interfaces()]
    return found
