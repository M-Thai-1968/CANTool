"""a2l_manager.py - A2L (ASAP2) einlesen, nach JSON/YAML konvertieren (wie DBC/CDD) und Werte umrechnen.

Konvertiert werden: MEASUREMENT, CHARACTERISTIC (Typ VALUE), COMPU_METHOD (IDENTICAL, LINEAR, RAT_FUNC mit a=d=0,
TAB_VERB/COMPU_VTAB), RECORD_LAYOUT (Datentyp), MOD_COMMON (Byte-Reihenfolge) und IF_DATA XCP (XCP_ON_CAN: CAN-IDs, Baudrate,
MAX_CTO). Nicht unterstuetzte Objekte (Kurven, Kennfelder, Felder ...) werden mit "supported": false uebernommen.
Cache: cache/<name>.a2l.json bzw. .yaml (mit SHA-1 der Quelle, wird nur bei Aenderung neu erzeugt).
"""
from __future__ import annotations

import hashlib
import re
import struct
from pathlib import Path
from typing import Any

from dbc_manager import load_document, save_document
from i18n import tr

FORMAT_VERSION = 1

# A2L-Datentyp -> (struct-Code, Bytes)
DATATYPES: dict[str, tuple[str, int]] = {
    "UBYTE": ("B", 1), "SBYTE": ("b", 1), "UWORD": ("H", 2), "SWORD": ("h", 2), "ULONG": ("I", 4), "SLONG": ("i", 4),
    "A_UINT64": ("Q", 8), "A_INT64": ("q", 8), "FLOAT32_IEEE": ("f", 4), "FLOAT64_IEEE": ("d", 8),
}


# --------------------------------------------------------------------------- Tokenizer und Baum
_TOKEN = re.compile(r'"(?:[^"\\]|\\.|"")*"|[^\s"]+')


def _strip_comments(text: str) -> str:
    """/* ... */ und // ... entfernen, Zeichenketten bleiben unberuehrt."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 1
            out.append(" ")
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j - 1
        else:
            out.append(c)
        i += 1
    return "".join(out)


class Block:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.tokens: list[str] = []
        self.children: list[Block] = []

    def find(self, kind: str) -> list["Block"]:
        return [c for c in self.children if c.kind == kind]


def parse_blocks(text: str) -> Block:
    """A2L-Text -> Baum aus /begin ... /end-Bloecken (Wurzel: Block 'ROOT')."""
    root = Block("ROOT")
    stack = [root]
    tokens = _TOKEN.findall(_strip_comments(text))
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "/begin" and i + 1 < len(tokens):
            blk = Block(tokens[i + 1])
            stack[-1].children.append(blk)
            stack.append(blk)
            i += 2
            continue
        if t == "/end":
            if len(stack) > 1:
                stack.pop()
            i += 2
            continue
        stack[-1].tokens.append(t)
        i += 1
    return root


def _s(tok: str) -> str:
    """Zeichenkette ohne Anfuehrungszeichen."""
    return tok[1:-1].replace('""', '"') if len(tok) >= 2 and tok[0] == tok[-1] == '"' else tok


def _num(tok: str, default: float = 0.0) -> float:
    try:
        return float(int(tok, 16)) if tok.lower().startswith(("0x", "-0x")) else float(tok)
    except (TypeError, ValueError):
        return default


def _int(tok: str, default: int = 0) -> int:
    return int(_num(tok, default))


def _kw(tokens: list[str], key: str, count: int = 1) -> list[str] | None:
    """Werte nach einem Schluesselwort innerhalb der Tokenliste."""
    if key in tokens:
        i = tokens.index(key)
        return tokens[i + 1:i + 1 + count]
    return None


# --------------------------------------------------------------------------- Umwandlung in ein Dokument
def _compu_method(b: Block, vtabs: dict[str, dict[str, str]]) -> dict[str, Any]:
    t = b.tokens
    cm: dict[str, Any] = {"type": t[2] if len(t) > 2 else "IDENTICAL", "format": _s(t[3]) if len(t) > 3 else "",
                          "unit": _s(t[4]) if len(t) > 4 else ""}
    co = _kw(t, "COEFFS", 6)
    if co and len(co) == 6:
        cm["coeffs"] = [_num(x) for x in co]
    cl = _kw(t, "COEFFS_LINEAR", 2)
    if cl and len(cl) == 2:
        cm["coeffs_linear"] = [_num(x) for x in cl]
    ref = _kw(t, "COMPU_TAB_REF", 1)
    if ref:
        cm["vtab"] = vtabs.get(ref[0], {})
    return cm


def _vtab(b: Block) -> dict[str, str]:
    t = b.tokens
    n = _int(t[3]) if len(t) > 3 else 0
    table: dict[str, str] = {}
    for k in range(n):
        base = 4 + 2 * k
        if base + 1 < len(t):
            table[str(_int(t[base]))] = _s(t[base + 1])
    return table


def _record_layout(b: Block) -> str:
    t = b.tokens
    for key in ("FNC_VALUES", "FNC_VALUES"):
        if key in t:
            i = t.index(key)
            if i + 2 < len(t):
                return t[i + 2]
    return ""


def parse_a2l_text(text: str) -> dict[str, Any]:
    root = parse_blocks(text)
    projects = root.find("PROJECT")
    project = projects[0] if projects else root
    modules = project.find("MODULE")
    module = modules[0] if modules else project
    doc: dict[str, Any] = {"format_version": FORMAT_VERSION,
                           "project": _s(project.tokens[0]) if project.tokens else "",
                           "module": _s(module.tokens[0]) if module.tokens else "",
                           "byte_order": "MSB_LAST", "xcp": {}, "compu_methods": {}, "measurements": {},
                           "characteristics": {}}
    for mc in module.find("MOD_COMMON"):
        bo = _kw(mc.tokens, "BYTE_ORDER", 1)
        if bo:
            doc["byte_order"] = bo[0]
    vtabs = {b.tokens[0]: _vtab(b) for b in module.find("COMPU_VTAB") if b.tokens}
    for b in module.find("COMPU_METHOD"):
        if b.tokens:
            doc["compu_methods"][b.tokens[0]] = _compu_method(b, vtabs)
    layouts = {b.tokens[0]: _record_layout(b) for b in module.find("RECORD_LAYOUT") if b.tokens}
    for b in module.find("MEASUREMENT"):
        t = b.tokens
        if len(t) < 8:
            continue
        item = {"description": _s(t[1]), "datatype": t[2], "conversion": t[3], "lower": _num(t[6]), "upper": _num(t[7]),
                "supported": t[2] in DATATYPES}
        addr = _kw(t, "ECU_ADDRESS", 1)
        item["address"] = _int(addr[0]) if addr else None
        ext = _kw(t, "ECU_ADDRESS_EXTENSION", 1)
        item["extension"] = _int(ext[0]) if ext else 0
        mask = _kw(t, "BIT_MASK", 1)
        if mask:
            item["bit_mask"] = _int(mask[0])
        item["supported"] = item["supported"] and item["address"] is not None
        doc["measurements"][t[0]] = item
    for b in module.find("CHARACTERISTIC"):
        t = b.tokens
        if len(t) < 9:
            continue
        layout = layouts.get(t[4], "")
        item = {"description": _s(t[1]), "type": t[2], "address": _int(t[3]), "layout": t[4], "datatype": layout,
                "conversion": t[6], "lower": _num(t[7]), "upper": _num(t[8]),
                "supported": t[2] == "VALUE" and layout in DATATYPES}
        ext = _kw(t, "ECU_ADDRESS_EXTENSION", 1)
        item["extension"] = _int(ext[0]) if ext else 0
        doc["characteristics"][t[0]] = item
    for b in module.find("IF_DATA"):
        if b.tokens and b.tokens[0] == "XCP":
            doc["xcp"] = _xcp_if_data(b)
            break
    return doc


def _xcp_if_data(b: Block) -> dict[str, Any]:
    info: dict[str, Any] = {}
    for c in b.children:
        if c.kind == "PROTOCOL_LAYER":
            t = c.tokens
            # Version, T1..T7, MAX_CTO, MAX_DTO, BYTE_ORDER, ADDRESS_GRANULARITY
            if len(t) >= 11:
                info["max_cto"], info["max_dto"] = _int(t[8]), _int(t[9])
            for tok in t:
                if tok.startswith("BYTE_ORDER_MSB"):
                    info["byte_order"] = "big" if tok.endswith("FIRST") else "little"
        elif c.kind == "XCP_ON_CAN":
            t = c.tokens
            for key, name in (("CAN_ID_BROADCAST", "can_id_broadcast"), ("CAN_ID_MASTER", "can_id_master"),
                              ("CAN_ID_SLAVE", "can_id_slave"), ("BAUDRATE", "baudrate")):
                v = _kw(t, key, 1)
                if v:
                    info[name] = _int(v[0])
            info["extended"] = any(info.get(k, 0) > 0x7FF for k in ("can_id_master", "can_id_slave"))
            info["transport"] = "CAN"
    return info


def convert_a2l_file(a2l_path: Path, cache_dir: Path, fmt: str = "json", force: bool = False) -> Path:
    """A2L -> cache/<name>.a2l.json|yaml; ueberspringt, wenn die Cache-Datei zur Quelle passt."""
    a2l_path, cache_dir = Path(a2l_path), Path(cache_dir)
    ext = "yaml" if fmt.lower() in ("yaml", "yml") else "json"
    out = cache_dir / f"{a2l_path.stem}.a2l.{ext}"
    raw = a2l_path.read_bytes()
    sha = hashlib.sha1(raw).hexdigest()
    if out.exists() and not force:
        try:
            if load_document(out).get("source_sha1") == sha:
                return out
        except Exception:
            pass
    text = raw.decode("utf-8-sig", errors="replace")
    if "/begin" not in text:
        raise ValueError(tr("err.a2l.invalid", file=a2l_path.name))
    doc = parse_a2l_text(text)
    doc["source"], doc["source_sha1"] = a2l_path.name, sha
    save_document(doc, out)
    return out


def convert_all_a2l(a2l_dir: Path, cache_dir: Path, fmt: str = "json", force: bool = False) -> list[Path]:
    a2l_dir = Path(a2l_dir)
    if not a2l_dir.is_dir():
        return []
    return [convert_a2l_file(p, cache_dir, fmt, force) for p in sorted(a2l_dir.glob("*.a2l"))]


# --------------------------------------------------------------------------- Datenbank und Umrechnung
class A2lDatabase:
    """Konvertiertes A2L-Dokument mit Dekodierung/Kodierung von Rohbytes."""

    def __init__(self, doc: dict[str, Any]) -> None:
        self.doc = doc
        self.measurements: dict[str, dict] = doc.get("measurements", {})
        self.characteristics: dict[str, dict] = doc.get("characteristics", {})
        self.methods: dict[str, dict] = doc.get("compu_methods", {})
        self.xcp: dict[str, Any] = doc.get("xcp", {})
        self.name = doc.get("source", "")

    @classmethod
    def load(cls, path: Path) -> "A2lDatabase":
        return cls(load_document(path))

    def item(self, name: str) -> dict:
        if name in self.measurements:
            return {**self.measurements[name], "kind": "measurement"}
        if name in self.characteristics:
            return {**self.characteristics[name], "kind": "characteristic"}
        raise KeyError(tr("err.a2l.unknown_item", name=name))

    def names(self) -> list[tuple[str, str]]:
        return ([(n, "measurement") for n in sorted(self.measurements)]
                + [(n, "characteristic") for n in sorted(self.characteristics)])

    def method(self, item: dict) -> dict:
        return self.methods.get(item.get("conversion", ""), {"type": "IDENTICAL"})

    def unit(self, item: dict) -> str:
        return self.method(item).get("unit", "")

    def size(self, item: dict) -> int:
        return DATATYPES[item["datatype"]][1]

    # -- Rohwert <-> Bytes
    def unpack(self, item: dict, data: bytes, big_endian: bool = False) -> float:
        code, size = DATATYPES[item["datatype"]]
        if len(data) < size:
            raise ValueError(tr("err.a2l.short_data", name=item.get("datatype", ""), size=size))
        raw = struct.unpack((">" if big_endian else "<") + code, bytes(data[:size]))[0]
        mask = item.get("bit_mask")
        if mask and isinstance(raw, int):
            shift = (mask & -mask).bit_length() - 1
            raw = (raw & mask) >> shift
        return raw

    def pack(self, item: dict, raw: float, big_endian: bool = False) -> bytes:
        code, size = DATATYPES[item["datatype"]]
        if code in "fd":
            return struct.pack((">" if big_endian else "<") + code, float(raw))
        value = int(round(raw))
        bits = size * 8
        lo, hi = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if code.islower() else (0, (1 << bits) - 1)
        if not lo <= value <= hi:
            raise ValueError(tr("err.a2l.out_of_range", value=value, lo=lo, hi=hi))
        return struct.pack((">" if big_endian else "<") + code, value)

    # -- Rohwert <-> physikalisch
    def to_physical(self, item: dict, raw: float) -> float | str:
        m = self.method(item)
        kind = m.get("type", "IDENTICAL")
        if kind == "LINEAR" and m.get("coeffs_linear"):
            a, b = m["coeffs_linear"]
            return a * raw + b
        if kind == "RAT_FUNC" and m.get("coeffs"):
            a, b, c, d, e, f = m["coeffs"]
            if a == 0 and d == 0:
                den = b - e * raw
                return (f * raw - c) / den if den else float("nan")
            return raw                                     # quadratische Funktionen: Rohwert anzeigen
        if kind in ("TAB_VERB", "TAB_NOINTP") and m.get("vtab"):
            return m["vtab"].get(str(int(raw)), raw)
        return raw

    def to_raw(self, item: dict, phys: float | str) -> float:
        m = self.method(item)
        kind = m.get("type", "IDENTICAL")
        if kind in ("TAB_VERB", "TAB_NOINTP") and m.get("vtab"):
            if isinstance(phys, str):
                for key, text in m["vtab"].items():
                    if text == phys.strip():
                        return float(key)
            return float(phys)
        phys = float(phys)
        if kind == "LINEAR" and m.get("coeffs_linear"):
            a, b = m["coeffs_linear"]
            return (phys - b) / a if a else phys
        if kind == "RAT_FUNC" and m.get("coeffs"):
            a, b, c, d, e, f = m["coeffs"]
            if a == 0 and d == 0:
                den = e * phys + f
                return (b * phys + c) / den if den else float("nan")
        return phys

    def detail(self, name: str, raw: float, phys: float | str) -> dict[str, Any]:
        """Protokollzeile fuer einen gelesenen Wert: Rohwert, Roh-Hex, physikalischer Wert, Text, Einheit."""
        item = self.item(name)
        size = self.size(item)
        is_float = item["datatype"].startswith("FLOAT")
        bits = (int.from_bytes(struct.pack("<f" if size == 4 else "<d", raw), "little") if is_float
                else int(raw) & ((1 << (8 * size)) - 1))
        number = isinstance(phys, (int, float))
        return {"raw": None if is_float or int(raw) >= 1 << 63 else int(raw), "raw_hex": f"0x{bits:0{size * 2}X}",
                "value": float(phys) if number else float(raw), "text": "" if number else str(phys),
                "unit": self.unit(item)}

    def decode(self, name: str, data: bytes, big_endian: bool = False) -> tuple[float, float | str]:
        item = self.item(name)
        raw = self.unpack(item, data, big_endian)
        return raw, self.to_physical(item, raw)

    def encode(self, name: str, phys: float | str, big_endian: bool = False) -> bytes:
        item = self.item(name)
        return self.pack(item, self.to_raw(item, phys), big_endian)
