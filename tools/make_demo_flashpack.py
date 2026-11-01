"""make_demo_flashpack.py - erzeugt projects/demo/flash/demo.vflashpack (ZIP mit Manifest, HEX und BIN) fuer die Simulation.

    python tools/make_demo_flashpack.py [ausgabe.vflashpack]

Das Paket dient als Vorlage fuer eigene Packs: flashpack.json (Manifest), app.hex (Intel HEX), cal.bin (Rohdaten).
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def intel_hex(address: int, data: bytes, width: int = 16) -> str:
    lines, base = [], None
    for off in range(0, len(data), width):
        chunk = data[off:off + width]
        addr = address + off
        if addr >> 16 != base:
            base = addr >> 16
            rec = bytes([2, 0, 0, 4, base >> 8, base & 0xFF])
            lines.append(":" + (rec + bytes([(-sum(rec)) & 0xFF])).hex().upper())
        rec = bytes([len(chunk), (addr >> 8) & 0xFF, addr & 0xFF, 0]) + chunk
        lines.append(":" + (rec + bytes([(-sum(rec)) & 0xFF])).hex().upper())
    lines.append(":00000001FF")
    return "\n".join(lines) + "\n"


MANIFEST = {
    "format": "canTool-flashpack-1", "name": "Demo Application", "ecu": "Engine_ECU",
    "request_id": "0x7E0", "response_id": "0x7E8", "extended": False, "fd": False,
    "security": {"type": "xor", "level": 1, "value": "0xFFFFFFFF"}, "address_format": "0x44",
    "data": [{"file": "app.hex", "name": "Application"},
             {"file": "cal.bin", "address": "0x00020000", "name": "Calibration"}],
    "steps": [{"type": "session", "session": 3}, {"type": "comm_control", "control": 3, "comm_type": 1},
              {"type": "session", "session": 2}, {"type": "security", "level": 1},
              {"type": "erase", "block": "all"}, {"type": "download", "block": "all"},
              {"type": "check", "routine": "FF01"}, {"type": "reset", "reset": 1}],
}


def build(path: Path) -> Path:
    app = bytes((i * 7 + 3) & 0xFF for i in range(1200))
    cal = bytes((255 - i) & 0xFF for i in range(300))
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("flashpack.json", json.dumps(MANIFEST, indent=2))
        zf.writestr("app.hex", intel_hex(0x00010000, app))
        zf.writestr("cal.bin", cal)
    return path


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "projects" / "demo" / "flash" / "demo.vflashpack"
    print(build(out))
