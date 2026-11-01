"""dbc_manager.py - DBC-Verwaltung.

* parse_dbc_text()    : DBC-Text -> dict (reiner Python-Parser, keine Zusatzpakete)
* convert_dbc_file()  : DBC -> JSON/YAML im Cache-Ordner des Projekts (nur wenn veraltet)
* DbcDatabase         : laedt die konvertierten Dateien, Encode/Decode von Botschaften

Zur Laufzeit wird ausschliesslich mit den konvertierten JSON/YAML-Dateien gearbeitet.

Unterstuetzt: Intel/Motorola, vorzeichenbehaftet, Faktor/Offset, Wertetabellen (VAL_),
Float32/64 (SIG_VALTYPE_), einfaches Multiplexing (M / mN), Extended-IDs, CAN FD,
GenMsgCycleTime, Kommentare.
Nicht unterstuetzt: Extended Multiplexing (SG_MUL_VAL_), Umgebungsvariablen.
"""
from __future__ import annotations

import hashlib
import json
import re
import struct
from pathlib import Path
from typing import Any

from i18n import tr

FORMAT_VERSION = 1

_FRAME_FORMAT_DEFAULT = (["StandardCAN", "ExtendedCAN"] + ["reserved"] * 12
                         + ["StandardCAN_FD", "ExtendedCAN_FD"])

_RE_BO = re.compile(r"^BO_\s+(\d+)\s+(\w+)\s*:\s*(\d+)\s+(\w+)")
_RE_SG = re.compile(
    r"^SG_\s+(\w+)\s*(M|m\d+M?)?\s*:\s*(\d+)\|(\d+)@([01])([+-])\s*"
    r"\(\s*([^,\s]+)\s*,\s*([^)\s]+)\s*\)\s*\[\s*([^|\s]*)\s*\|\s*([^\]\s]*)\s*\]\s*"
    r'"([^"]*)"\s*(.*)$')
_RE_STR = r'"((?:[^"\\]|\\.)*)"'


def _num(text: str) -> int | float | None:
    text = text.strip()
    if not text:
        return None
    value = float(text)
    return int(value) if value.is_integer() and abs(value) < 1e15 else value


def read_text_file(path: Path) -> tuple[str, bytes]:
    """Liest eine Textdatei (UTF-8, sonst Windows-1252 - typisch fuer DBC)."""
    raw = Path(path).read_bytes()
    try:
        return raw.decode("utf-8-sig"), raw
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace"), raw


# --------------------------------------------------------------------------- Parser
def parse_dbc_text(text: str) -> dict:
    """Wandelt DBC-Text in das interne Dokument (JSON-faehig) um."""
    messages: dict[str, dict] = {}
    by_raw_id: dict[int, tuple[str, dict]] = {}
    nodes: list[str] = []
    warnings: list[dict] = []
    cur: dict | None = None
    cur_name = ""

    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("BU_:"):
            nodes = s[4:].split()
            cur = None
        elif s.startswith("BO_ "):
            m = _RE_BO.match(s)
            cur = None
            if not m or m.group(2) == "VECTOR__INDEPENDENT_SIG_MSG":
                continue
            raw_id, name, dlc, sender = int(m.group(1)), m.group(2), int(m.group(3)), m.group(4)
            cur_name = name
            cur = {
                "id": raw_id & 0x1FFFFFFF,
                "extended": bool(raw_id & 0x80000000),
                "dlc": dlc,
                "fd": dlc > 8,
                "sender": sender,
                "cycle_time_ms": None,
                "comment": "",
                "multiplexor": None,
                "signals": {},
            }
            messages[name] = cur
            by_raw_id[raw_id] = (name, cur)
        elif s.startswith("SG_ ") and cur is not None:
            m = _RE_SG.match(s)
            if not m:
                warnings.append({"key": "warn.dbc.unreadable_signal", "args": {"line": s[:80]}})
                continue
            sname, mux = m.group(1), m.group(2)
            sig: dict[str, Any] = {
                "start": int(m.group(3)),
                "length": int(m.group(4)),
                "byte_order": "little" if m.group(5) == "1" else "big",
                "signed": m.group(6) == "-",
                "factor": _num(m.group(7)),
                "offset": _num(m.group(8)),
                "min": _num(m.group(9)),
                "max": _num(m.group(10)),
                "unit": m.group(11),
                "receivers": [r for r in re.split(r"[,\s]+", m.group(12)) if r],
                "choices": {},
                "comment": "",
            }
            if mux == "M":
                cur["multiplexor"] = sname
            elif mux:
                sig["mux_value"] = int(mux[1:].rstrip("M"))
                if mux.endswith("M"):
                    warnings.append({"key": "warn.dbc.extended_mux", "args": {"signal": f"{cur_name}.{sname}"}})
            cur["signals"][sname] = sig
        else:
            cur = None

    # Zusatzinformationen (stehen nach den Botschaften)
    for m in re.finditer(r"^VAL_\s+(\d+)\s+(\w+)\s+(.*?);\s*$", text, re.M):
        entry = by_raw_id.get(int(m.group(1)))
        if entry and m.group(2) in entry[1]["signals"]:
            entry[1]["signals"][m.group(2)]["choices"] = {
                int(v): d for v, d in re.findall(r"(-?\d+)\s+" + _RE_STR, m.group(3))}

    for m in re.finditer(r"^SIG_VALTYPE_\s+(\d+)\s+(\w+)\s*:\s*(\d+)\s*;", text, re.M):
        entry = by_raw_id.get(int(m.group(1)))
        if entry and m.group(2) in entry[1]["signals"] and m.group(3) in ("1", "2"):
            entry[1]["signals"][m.group(2)]["float"] = 32 if m.group(3) == "1" else 64

    for m in re.finditer(r"CM_\s+BO_\s+(\d+)\s+" + _RE_STR + r"\s*;", text):
        entry = by_raw_id.get(int(m.group(1)))
        if entry:
            entry[1]["comment"] = m.group(2).replace('\\"', '"')
    for m in re.finditer(r"CM_\s+SG_\s+(\d+)\s+(\w+)\s+" + _RE_STR + r"\s*;", text):
        entry = by_raw_id.get(int(m.group(1)))
        if entry and m.group(2) in entry[1]["signals"]:
            entry[1]["signals"][m.group(2)]["comment"] = m.group(3).replace('\\"', '"')

    frame_formats = _FRAME_FORMAT_DEFAULT
    m = re.search(r'^BA_DEF_\s+BO_\s+"VFrameFormat"\s+ENUM\s+(.*?);', text, re.M)
    if m:
        frame_formats = re.findall(r'"([^"]*)"', m.group(1))
    for m in re.finditer(r'^BA_\s+"(\w+)"\s+BO_\s+(\d+)\s+(.+?);\s*$', text, re.M):
        entry = by_raw_id.get(int(m.group(2)))
        if not entry:
            continue
        attr, value = m.group(1), m.group(3).strip().strip('"')
        if attr == "GenMsgCycleTime" and value.lstrip("-").isdigit():
            entry[1]["cycle_time_ms"] = int(value) or None
        elif attr == "VFrameFormat":
            fmt = frame_formats[int(value)] if value.isdigit() and int(value) < len(frame_formats) else value
            if "FD" in fmt:
                entry[1]["fd"] = True

    return {"format_version": FORMAT_VERSION, "nodes": nodes, "messages": messages,
            "warnings": warnings}


# --------------------------------------------------------------------------- Konvertierung
def save_document(doc: dict, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() in (".yaml", ".yml"):
        import yaml
        path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    else:
        path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")


def load_document(path: Path) -> dict:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        import yaml
        return yaml.safe_load(text)
    return json.loads(text)


def convert_dbc_file(dbc_path: Path, cache_dir: Path, fmt: str = "json", force: bool = False) -> Path:
    """Konvertiert eine DBC-Datei; ueberspringt, wenn die Cache-Datei aktuell ist."""
    dbc_path, cache_dir = Path(dbc_path), Path(cache_dir)
    ext = "yaml" if fmt.lower() in ("yaml", "yml") else "json"
    out = cache_dir / f"{dbc_path.stem}.dbc.{ext}"
    text, raw = read_text_file(dbc_path)
    sha = hashlib.sha1(raw).hexdigest()
    if out.exists() and not force:
        try:
            if load_document(out).get("source_sha1") == sha:
                return out
        except Exception:
            pass
    doc = parse_dbc_text(text)
    doc["source"] = dbc_path.name
    doc["source_sha1"] = sha
    save_document(doc, out)
    return out


def convert_all_dbcs(dbc_dir: Path, cache_dir: Path, fmt: str = "json", force: bool = False) -> list[Path]:
    dbc_dir = Path(dbc_dir)
    if not dbc_dir.is_dir():
        return []
    return [convert_dbc_file(p, cache_dir, fmt, force) for p in sorted(dbc_dir.glob("*.dbc"))]


# --------------------------------------------------------------------------- Bit-Zugriff
def _extract_raw(data: bytes, start: int, length: int, little: bool) -> int:
    if little:
        return (int.from_bytes(data, "little") >> start) & ((1 << length) - 1)
    raw, pos = 0, start
    for _ in range(length):
        byte_i, bit_i = divmod(pos, 8)
        bit = (data[byte_i] >> bit_i) & 1 if byte_i < len(data) else 0
        raw = (raw << 1) | bit
        pos = pos + 15 if bit_i == 0 else pos - 1
    return raw


def _insert_raw(buf: bytearray, start: int, length: int, little: bool, raw: int) -> None:
    raw &= (1 << length) - 1
    if little:
        if start + length > len(buf) * 8:
            raise ValueError(tr("err.dbc.signal_too_long"))
        value = int.from_bytes(buf, "little")
        value = (value & ~(((1 << length) - 1) << start)) | (raw << start)
        buf[:] = value.to_bytes(len(buf), "little")
        return
    pos = start
    for i in range(length):
        byte_i, bit_i = divmod(pos, 8)
        if byte_i >= len(buf):
            raise ValueError(tr("err.dbc.signal_too_long"))
        if (raw >> (length - 1 - i)) & 1:
            buf[byte_i] |= 1 << bit_i
        else:
            buf[byte_i] &= ~(1 << bit_i) & 0xFF
        pos = pos + 15 if bit_i == 0 else pos - 1


# --------------------------------------------------------------------------- Datenbank
class DbcDatabase:
    """Summe aller geladenen DBC-Dokumente."""

    def __init__(self) -> None:
        self.messages: dict[str, dict] = {}
        self._by_id: dict[tuple[int, bool], str] = {}
        self._warn_items: list[tuple[str, dict]] = []
        self.sources: list[str] = []

    @property
    def warnings(self) -> list[str]:
        """Warnungen (Konvertierung und Zusammenfuehren) in der aktuellen Sprache."""
        return [tr(k, **a) for k, a in self._warn_items]

    # -- Laden
    def add_document(self, doc: dict, source: str = "") -> None:
        source = source or doc.get("source", "")
        self.sources.append(source)
        for w in doc.get("warnings", []):
            if isinstance(w, dict):
                self._warn_items.append((w["key"], {**w.get("args", {}), "source": source}))
            else:
                self._warn_items.append(("warn.dbc.generic", {"source": source, "text": str(w)}))
        for name, msg in doc.get("messages", {}).items():
            if name in self.messages:
                self._warn_items.append(("warn.dbc.duplicate_message", {"name": name, "source": source}))
                continue
            for sig in msg.get("signals", {}).values():
                sig["choices"] = {int(k): v for k, v in (sig.get("choices") or {}).items()}
            msg["source"] = source
            self.messages[name] = msg
            key = (msg["id"], bool(msg.get("extended")))
            if key in self._by_id:
                self._warn_items.append(("warn.dbc.duplicate_id", {
                    "id": f"{msg['id']:X}", "first": self._by_id[key], "second": name}))
            else:
                self._by_id[key] = name

    def add_file(self, path: Path) -> None:
        self.add_document(load_document(path), Path(path).name)

    @classmethod
    def from_project(cls, dbc_dir: Path, cache_dir: Path, fmt: str = "json") -> "DbcDatabase":
        """Konvertiert (falls noetig) alle DBCs eines Projekts und laedt die Ergebnisse."""
        db = cls()
        for path in convert_all_dbcs(dbc_dir, cache_dir, fmt):
            db.add_file(path)
        return db

    # -- Abfragen
    def message_names(self) -> list[str]:
        return sorted(self.messages)

    def signal_names(self, msg: str) -> list[str]:
        return list(self.messages[msg]["signals"])

    def find_by_id(self, arb_id: int, extended: bool = False) -> str | None:
        return self._by_id.get((arb_id, extended))

    def signal_def(self, msg: str, sig: str) -> dict:
        return self.messages[msg]["signals"][sig]

    def choice_text(self, msg: str, sig: str, value: float) -> str | None:
        d = self.signal_def(msg, sig)
        if not d.get("choices"):
            return None
        raw = round((value - (d["offset"] or 0)) / (d["factor"] or 1))
        return d["choices"].get(raw)

    # -- Encode / Decode
    @staticmethod
    def _to_raw(sig: dict, value: Any) -> int:
        if isinstance(value, str):
            inv = {v: k for k, v in sig.get("choices", {}).items()}
            if value in inv:
                return inv[value]
            value = float(value)
        factor, offset = sig["factor"] or 1, sig["offset"] or 0
        if sig.get("float"):
            fmt = ">" if sig["byte_order"] == "big" else "<"
            fmt += "f" if sig["float"] == 32 else "d"
            return int.from_bytes(struct.pack(fmt, (float(value) - offset) / factor), sig["byte_order"])
        raw = round((float(value) - offset) / factor)
        length = sig["length"]
        lo, hi = (-(1 << (length - 1)), (1 << (length - 1)) - 1) if sig["signed"] else (0, (1 << length) - 1)
        return max(lo, min(hi, raw))

    @staticmethod
    def _from_raw(sig: dict, raw: int) -> int | float:
        factor, offset = sig["factor"] or 1, sig["offset"] or 0
        length = sig["length"]
        if sig.get("float"):
            fmt = ">" if sig["byte_order"] == "big" else "<"
            fmt += "f" if sig["float"] == 32 else "d"
            value = struct.unpack(fmt, raw.to_bytes(length // 8, sig["byte_order"]))[0]
            return value * factor + offset
        if sig["signed"] and raw >= 1 << (length - 1):
            raw -= 1 << length
        if factor == 1 and offset == 0:
            return raw
        return round(raw * factor + offset, 10)

    def encode(self, msg_name: str, values: dict[str, Any] | None = None) -> bytes:
        """Physikalische Werte -> Nutzdaten. Nicht angegebene Signale: Rohwert 0.
        Bei Multiplexing werden nur Signale der aktiven Gruppe geschrieben."""
        msg = self.messages[msg_name]
        values = values or {}
        sigs = msg["signals"]
        buf = bytearray(msg["dlc"])
        mux_name = msg.get("multiplexor")
        active = self._to_raw(sigs[mux_name], values.get(mux_name, 0)) if mux_name else None
        for name, sig in sigs.items():
            mv = sig.get("mux_value")
            if mv is not None and mux_name and mv != active:
                continue
            raw = self._to_raw(sig, values[name]) if name in values else 0
            _insert_raw(buf, sig["start"], sig["length"], sig["byte_order"] == "little", raw)
        return bytes(buf)

    def decode(self, msg_name: str, data: bytes) -> dict[str, int | float]:
        msg = self.messages[msg_name]
        sigs = msg["signals"]
        mux_name = msg.get("multiplexor")
        active = None
        if mux_name:
            m = sigs[mux_name]
            active = _extract_raw(data, m["start"], m["length"], m["byte_order"] == "little")
        out: dict[str, int | float] = {}
        for name, sig in sigs.items():
            mv = sig.get("mux_value")
            if mv is not None and mux_name and mv != active:
                continue
            raw = _extract_raw(data, sig["start"], sig["length"], sig["byte_order"] == "little")
            out[name] = self._from_raw(sig, raw)
        return out

    def decode_detail(self, msg_name: str, data: bytes) -> dict[str, dict[str, Any]]:
        """Wie decode(), liefert je Signal Rohwert (vorzeichenbehaftet), Roh-Hex (Bitmuster), physikalischen Wert,
        Text (Wertetabelle, sonst "") und Einheit."""
        msg = self.messages[msg_name]
        sigs = msg["signals"]
        mux_name = msg.get("multiplexor")
        active = None
        if mux_name:
            m = sigs[mux_name]
            active = _extract_raw(data, m["start"], m["length"], m["byte_order"] == "little")
        out: dict[str, dict[str, Any]] = {}
        for name, sig in sigs.items():
            mv = sig.get("mux_value")
            if mv is not None and mux_name and mv != active:
                continue
            bits = _extract_raw(data, sig["start"], sig["length"], sig["byte_order"] == "little")
            value = self._from_raw(sig, bits)
            raw = bits
            if not sig.get("float") and sig["signed"] and bits >= 1 << (sig["length"] - 1):
                raw = bits - (1 << sig["length"])
            choices = sig.get("choices") or {}
            out[name] = {"raw": raw, "raw_hex": f"0x{bits:0{(sig['length'] + 3) // 4}X}", "value": value,
                         "text": str(choices.get(bits if not sig["signed"] else raw, "")) if choices else "",
                         "unit": sig.get("unit", "") or ""}
        return out

    def decode_frame(self, arb_id: int, extended: bool, data: bytes) -> tuple[str, dict] | None:
        name = self.find_by_id(arb_id, extended)
        return (name, self.decode(name, data)) if name else None
