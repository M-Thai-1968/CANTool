"""cdd_manager.py - Diagnosebeschreibung (*.cdd) nach JSON/YAML wandeln.

WICHTIG: Das originale CANdelaStudio-Format ist umfangreich und herstellerabhaengig.
Dieser Konverter unterstuetzt
  1. das einfache XML-Format <DIAGDATA> (siehe projects/demo/cdd/diag.cdd) vollstaendig und
  2. fuer echte CANdelaStudio-Dateien nur eine heuristische Erkennung (Service-Namen/Request-Bytes).
Ergebnis pruefen und ggf. in cache/*.cdd.json von Hand ergaenzen (Antwortparameter, Adressen).
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from dbc_manager import FORMAT_VERSION, load_document, save_document


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].upper()


def _attr(el: ET.Element, *names: str, default: str = "") -> str:
    low = {k.lower(): v for k, v in el.attrib.items()}
    for n in names:
        if n.lower() in low:
            return low[n.lower()]
    return default


def _parse_simple(root: ET.Element) -> dict:
    ecus: dict[str, dict] = {}
    for ecu in root.iter():
        if _local(ecu.tag) != "ECU":
            continue
        name = _attr(ecu, "name", default="ECU")
        services: dict[str, dict] = {}
        for sv in ecu.iter():
            if _local(sv.tag) != "SERVICE":
                continue
            entry: dict = {"request": _attr(sv, "request"), "description": _attr(sv, "description"),
                           "params": [], "response": {"params": []}}
            for ch in sv:
                tag = _local(ch.tag)
                if tag == "PARAM":
                    entry["params"].append({"name": _attr(ch, "name"), "bytes": int(_attr(ch, "bytes", default="1")),
                                            "default": int(_attr(ch, "default", default="0"), 16),
                                            "base": "hex"})
                elif tag == "RESPONSE":
                    if _attr(ch, "prefix_len"):
                        entry["response"]["prefix_len"] = int(_attr(ch, "prefix_len"))
                    for p in ch:
                        if _local(p.tag) == "PARAM":
                            item = {"name": _attr(p, "name"), "offset": int(_attr(p, "offset", default="0")),
                                    "length": int(_attr(p, "length", default="1")),
                                    "type": _attr(p, "type", default="hex")}
                            for key, cast in (("factor", float), ("offset_val", float)):
                                if _attr(p, key):
                                    item[key] = cast(_attr(p, key))
                            if _attr(p, "unit"):
                                item["unit"] = _attr(p, "unit")
                            entry["response"]["params"].append(item)
            services[_attr(sv, "name", default=entry["request"])] = entry
        ecus[name] = {"request_id": _attr(ecu, "request_id", default="0x7E0"),
                      "response_id": _attr(ecu, "response_id", default="0x7E8"),
                      "extended": _attr(ecu, "extended").lower() == "true",
                      "fd": _attr(ecu, "fd").lower() == "true",
                      "services": services}
    return {"ecus": ecus, "warnings": []}


def _parse_heuristic(root: ET.Element) -> dict:
    """Sucht Elemente DIAGINST/DIAGSERVICE und uebernimmt Name + Request-Bytes, falls auffindbar."""
    services: dict[str, dict] = {}
    for el in root.iter():
        if _local(el.tag) not in ("DIAGINST", "DIAGSERVICE", "SERVICE"):
            continue
        name = _attr(el, "name", "qualifier", "qual")
        req = _attr(el, "request", "req", "id")
        for ch in el:
            if _local(ch.tag) in ("QUAL", "NAME") and ch.text and not name:
                name = ch.text.strip()
            if _local(ch.tag) in ("REQ", "REQUEST", "ID") and ch.text and not req:
                req = ch.text.strip()
        req = " ".join(re.findall(r"[0-9A-Fa-f]{2}", req)) if re.fullmatch(r"(?:0x)?[0-9A-Fa-f\s,]+", req or "") else ""
        if name:
            services[name] = {"request": req, "description": "", "params": [], "response": {"params": []}}
    warn = [{"key": "warn.cdd.heuristic", "args": {}}]
    if not services:
        warn.append({"key": "warn.cdd.no_services", "args": {}})
    return {"ecus": {"ECU": {"request_id": "0x7E0", "response_id": "0x7E8", "extended": False, "fd": False,
                             "services": services}}, "warnings": warn}


def parse_cdd_text(text: str) -> dict:
    root = ET.fromstring(text.encode("utf-8") if text.lstrip().startswith("<?xml") else text)
    doc = _parse_simple(root) if _local(root.tag) == "DIAGDATA" else _parse_heuristic(root)
    doc["format_version"] = FORMAT_VERSION
    return doc


def convert_cdd_file(cdd_path: Path, cache_dir: Path, fmt: str = "json", force: bool = False) -> Path:
    cdd_path, cache_dir = Path(cdd_path), Path(cache_dir)
    ext = "yaml" if fmt.lower() in ("yaml", "yml") else "json"
    out = cache_dir / f"{cdd_path.stem}.cdd.{ext}"
    raw = cdd_path.read_bytes()
    sha = hashlib.sha1(raw).hexdigest()
    if out.exists() and not force:
        try:
            if load_document(out).get("source_sha1") == sha:
                return out
        except Exception:
            pass
    doc = parse_cdd_text(raw.decode("utf-8-sig", errors="replace"))
    doc["source"], doc["source_sha1"] = cdd_path.name, sha
    save_document(doc, out)
    return out


def convert_all_cdds(cdd_dir: Path, cache_dir: Path, fmt: str = "json", force: bool = False) -> list[Path]:
    cdd_dir = Path(cdd_dir)
    if not cdd_dir.is_dir():
        return []
    return [convert_cdd_file(p, cache_dir, fmt, force) for p in sorted(cdd_dir.glob("*.cdd"))]
