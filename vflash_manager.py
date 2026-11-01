"""vflash_manager.py - vFlash-Pack laden, interpretieren und ueber ISO-TP/UDS ausfuehren.

Ausgefuehrt wird mit dem eigenen UDS-Flashablauf ueber die gewaehlte Hardware (python-can: Vector, PEAK, SocketCAN
oder Simulation) - die Vector-Software vFlash wird nicht benoetigt.

Unterstuetzte Quellen (load_flashpack):
  * ZIP-Paket (.vflashpack/.zip) mit ``flashpack.json|yaml`` (CAN-Tool-Manifest, siehe README/Hilfe) und Datendateien
  * ZIP-Paket ohne Manifest (echtes vFlash-Pack): HEX-/S-Record-/BIN-Dateien werden gefunden, das vFlash-Projekt (XML)
    wird heuristisch gelesen. Das interne Format von vFlash ist nicht oeffentlich dokumentiert - Ablauf, Adressen und
    Seed&Key muessen dann ggf. nachgetragen (Manifest) werden.
  * einzelne Manifest-Datei (.json/.yaml) oder Abbild (.hex/.s19/.srec/.mot/.bin)

Ohne eigenen Ablauf im Manifest wird ein generischer UDS-Standardablauf verwendet (nicht herstellerspezifisch!).
Es wird kein Code aus Paketen ausgefuehrt.
"""
from __future__ import annotations

import json
import re
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from i18n import tr
from iso_tp_manager import IsoTpChannel, IsoTpError, bytes_to_hex, hex_to_bytes

HEX_EXT = {".hex", ".ihex", ".ihx", ".h86"}
SREC_EXT = {".s19", ".s28", ".s37", ".srec", ".mot", ".s1", ".s2", ".s3", ".sx"}
BIN_EXT = {".bin"}
MANIFEST_NAMES = ("flashpack.json", "flashpack.yaml", "flashpack.yml")
MAX_PACK_BYTES = 512 * 1024 * 1024          # Schutz vor Zip-Bomben (entpackte Gesamtgroesse)
MAX_XML_BYTES = 20 * 1024 * 1024
MAX_CHUNK = 4093                            # ISO-TP (12 Bit): 4095 Byte Nutzdaten - SID - Blockzaehler


class FlashPackError(Exception):
    pass


class FlashStepError(Exception):
    pass


# --------------------------------------------------------------------------- Datenmodell
@dataclass
class DataBlock:
    address: int
    data: bytes
    name: str = ""
    source: str = ""


@dataclass
class FlashPlan:
    name: str
    source: str
    format: str                                  # "manifest" | "vflash" | "image"
    blocks: list[DataBlock] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)       # leer = Standardablauf
    ecu: str = ""
    request_id: int | None = None
    response_id: int | None = None
    extended: bool | None = None
    fd: bool | None = None
    security: dict = field(default_factory=lambda: {"type": "none", "level": 1})
    alfid: int = 0x44                            # addressAndLengthFormatIdentifier
    max_chunk: int = MAX_CHUNK
    info: dict[str, str] = field(default_factory=dict)
    _warn: list[tuple[str, dict]] = field(default_factory=list)

    def warn(self, key: str, **args: Any) -> None:
        self._warn.append((key, args))

    @property
    def warnings(self) -> list[str]:
        return [tr(k, **a) for k, a in self._warn]

    @property
    def total_bytes(self) -> int:
        return sum(len(b.data) for b in self.blocks)

    def effective_steps(self) -> list[dict]:
        """Ablauf des Manifests oder generischer Standardablauf."""
        if self.steps:
            return [dict(s) for s in self.steps]
        steps: list[dict] = [{"type": "session", "session": 3},
                             {"type": "comm_control", "control": 3, "comm_type": 1},
                             {"type": "session", "session": 2}]
        if self.security.get("type", "none") != "none":
            steps.append({"type": "security", "level": int(self.security.get("level", 1))})
        steps += [{"type": "erase", "block": "all"}, {"type": "download", "block": "all"},
                  {"type": "reset", "reset": 1}]
        return steps

    def expanded_steps(self) -> list[dict]:
        """Ablauf mit je einem Schritt pro Datenblock (erase/download mit block='all' werden aufgeloest)."""
        out: list[dict] = []
        for st in self.effective_steps():
            kind = st.get("type")
            if kind in ("erase", "download"):
                which = st.get("block", "all")
                idx = range(len(self.blocks)) if which == "all" else [int(which)]
                for i in idx:
                    out.append({**st, "block": i})
            else:
                out.append(st)
        for st in out:
            st["title"] = step_title(self, st)
        return out

    def describe(self) -> list[tuple[str, list[str]]]:
        """Interpretation fuer die Anzeige: [(Abschnittsschluessel, Zeilen)]."""
        sec = self.security
        sec_text = (tr("flash.security.none") if sec.get("type", "none") == "none"
                    else tr(f"flash.security.{sec['type']}", value=f"{int(str(sec.get('value', 0)), 0):X}",
                            level=sec.get("level", 1)))
        ids = (f"0x{self.request_id:X} / 0x{self.response_id:X}"
               if self.request_id is not None and self.response_id is not None else "-")
        general = [f"{tr('flash.info.source')}: {self.source}",
                   f"{tr('flash.info.format')}: {tr(f'flash.format.{self.format}')}",
                   f"{tr('flash.info.ecu')}: {self.ecu or '-'}", f"{tr('flash.info.ids')}: {ids}",
                   f"{tr('flash.info.security')}: {sec_text}", f"{tr('flash.info.alfid')}: 0x{self.alfid:02X}"]
        general += [f"{k}: {v}" for k, v in self.info.items()]
        data = [tr("flash.block", index=i, address=f"{b.address:08X}", length=len(b.data), source=b.name or b.source)
                for i, b in enumerate(self.blocks)]
        steps = [f"{i + 1}. {s['title']}" for i, s in enumerate(self.expanded_steps())]
        return [("flash.tree.general", general), ("flash.tree.data", data), ("flash.tree.steps", steps),
                ("flash.tree.warnings", self.warnings)]


# --------------------------------------------------------------------------- Datenformate
def merge_segments(segments: list[tuple[int, bytes]]) -> list[tuple[int, bytes]]:
    """Aneinandergrenzende Segmente zu Bloecken zusammenfassen."""
    out: list[tuple[int, bytearray]] = []
    for addr, data in sorted(segments, key=lambda s: s[0]):
        if out and out[-1][0] + len(out[-1][1]) == addr:
            out[-1][1].extend(data)
        else:
            out.append((addr, bytearray(data)))
    return [(a, bytes(d)) for a, d in out]


def parse_intel_hex(text: str, name: str = "") -> list[tuple[int, bytes]]:
    segments: list[tuple[int, bytes]] = []
    base = 0
    for no, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            if not line.startswith(":"):
                raise ValueError
            raw = bytes.fromhex(line[1:])
            if len(raw) < 5 or raw[0] != len(raw) - 5 or sum(raw) & 0xFF:
                raise ValueError
        except ValueError:
            raise FlashPackError(tr("flash.err.bad_record", file=name, line=no)) from None
        count, offset, rtype, payload = raw[0], (raw[1] << 8) | raw[2], raw[3], raw[4:4 + raw[0]]
        if rtype == 0x00 and count:
            segments.append((base + offset, bytes(payload)))
        elif rtype == 0x01:
            break
        elif rtype == 0x02:
            base = ((payload[0] << 8) | payload[1]) << 4
        elif rtype == 0x04:
            base = ((payload[0] << 8) | payload[1]) << 16
    return merge_segments(segments)


def parse_srec(text: str, name: str = "") -> list[tuple[int, bytes]]:
    segments: list[tuple[int, bytes]] = []
    addr_len = {"1": 2, "2": 3, "3": 4}
    for no, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            if len(line) < 4 or line[0] not in "Ss":
                raise ValueError
            kind, raw = line[1], bytes.fromhex(line[2:])
            if raw[0] != len(raw) - 1 or (sum(raw) + 1) & 0xFF:
                raise ValueError
        except (ValueError, IndexError):
            raise FlashPackError(tr("flash.err.bad_record", file=name, line=no)) from None
        if kind in addr_len:
            n = addr_len[kind]
            segments.append((int.from_bytes(raw[1:1 + n], "big"), bytes(raw[1 + n:-1])))
    return merge_segments(segments)


def parse_image(name: str, data: bytes, address: int | None, plan: FlashPlan) -> list[tuple[int, bytes]]:
    suffix = Path(name).suffix.lower()
    if suffix in HEX_EXT:
        return parse_intel_hex(data.decode("ascii", "replace"), name)
    if suffix in SREC_EXT:
        return parse_srec(data.decode("ascii", "replace"), name)
    if suffix in BIN_EXT:
        if address is None:
            address = 0
            plan.warn("flash.warn.bin_address", file=name)
        return [(address, bytes(data))]
    raise FlashPackError(tr("flash.err.unsupported_file", name=name))


def _int(value: Any) -> int:
    return int(str(value), 0)


# --------------------------------------------------------------------------- Laden
class _Source:
    """Lesezugriff auf Paketdateien (ZIP im Speicher, nie entpackt - keine Pfad-Tricks moeglich)."""

    def __init__(self, names: list[str], reader: Callable[[str], bytes]) -> None:
        self.names, self.read = names, reader

    def find(self, wanted: str) -> str | None:
        wanted_l = wanted.replace("\\", "/").lower()
        for n in self.names:
            if n.lower() == wanted_l:
                return n
        base = wanted_l.rsplit("/", 1)[-1]
        for n in self.names:
            if n.lower().rsplit("/", 1)[-1] == base:
                return n
        return None


def _zip_source(path: Path) -> _Source:
    zf = zipfile.ZipFile(path)
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if sum(i.file_size for i in infos) > MAX_PACK_BYTES:
        raise FlashPackError(tr("flash.err.too_big"))
    return _Source([i.filename for i in infos], zf.read)


def _dir_source(base: Path) -> _Source:
    def read(name: str) -> bytes:
        target = (base / name).resolve()
        if base.resolve() not in target.parents and target != base.resolve():
            raise FlashPackError(tr("flash.err.unsupported_file", name=name))
        return target.read_bytes()
    return _Source([p.name for p in base.iterdir() if p.is_file()], read)


def _parse_manifest(text: str, name: str) -> dict:
    try:
        if name.lower().endswith((".yaml", ".yml")):
            import yaml
            doc = yaml.safe_load(text)
        else:
            doc = json.loads(text)
    except Exception as exc:
        raise FlashPackError(tr("flash.err.bad_manifest", error=exc)) from exc
    if not isinstance(doc, dict):
        raise FlashPackError(tr("flash.err.bad_manifest", error="mapping expected"))
    return doc


def _plan_from_manifest(doc: dict, src: _Source, source: str, fmt: str = "manifest") -> FlashPlan:
    plan = FlashPlan(name=str(doc.get("name") or Path(source).stem), source=source, format=fmt)
    plan.ecu = str(doc.get("ecu", ""))
    for attr, key in (("request_id", "request_id"), ("response_id", "response_id")):
        if doc.get(key) not in (None, ""):
            setattr(plan, attr, _int(doc[key]))
    for attr in ("extended", "fd"):
        if doc.get(attr) is not None:
            setattr(plan, attr, bool(doc[attr]))
    if isinstance(doc.get("security"), dict):
        plan.security = {"type": "none", "level": 1, **doc["security"]}
        if plan.security["type"] not in ("none", "xor", "add", "constant"):
            raise FlashPackError(tr("flash.err.bad_manifest", error=f"security.type={plan.security['type']}"))
    if doc.get("address_format") is not None:
        plan.alfid = _int(doc["address_format"])
    if doc.get("max_chunk"):
        plan.max_chunk = max(1, min(MAX_CHUNK, _int(doc["max_chunk"])))
    entries = doc.get("data") or []
    if not isinstance(entries, list):
        raise FlashPackError(tr("flash.err.bad_manifest", error="data must be a list"))
    for entry in entries:
        entry = {"file": entry} if isinstance(entry, str) else dict(entry)
        found = src.find(str(entry.get("file", "")))
        if found is None:
            raise FlashPackError(tr("flash.err.missing_file", name=entry.get("file")))
        address = _int(entry["address"]) if entry.get("address") not in (None, "") else None
        for addr, data in parse_image(found, src.read(found), address, plan):
            plan.blocks.append(DataBlock(addr, data, str(entry.get("name", "")), found))
    if isinstance(doc.get("steps"), list):
        for st in doc["steps"]:
            if not isinstance(st, dict) or st.get("type") not in STEP_TYPES:
                raise FlashPackError(tr("flash.err.bad_manifest", error=f"step {st!r}"))
        plan.steps = [dict(s) for s in doc["steps"]]
    else:
        plan.warn("flash.warn.generic_sequence")
    return plan


_ID_KEYS = (("request_id", re.compile(r"(request|phys|tester|tx).*id|^reqid$", re.I)),
            ("response_id", re.compile(r"(response|resp|ecu|rx).*id|^resid$", re.I)))


def _scan_vflash_xml(src: _Source, plan: FlashPlan) -> None:
    """Heuristik: vFlash-Projekt (XML) nach Namen, CAN-IDs und Dateiverweisen durchsuchen (nur Anzeige/IDs)."""
    import xml.etree.ElementTree as ET
    candidates = [n for n in src.names if n.lower().endswith((".vflash", ".vflashproj", ".xml"))]
    for name in candidates:
        raw = src.read(name)
        if len(raw) > MAX_XML_BYTES or b"<!ENTITY" in raw or b"<!DOCTYPE" in raw:
            continue
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            continue
        plan.info.setdefault("vFlash", name)
        for el in root.iter():
            tag = el.tag.split("}")[-1]
            pairs = [(tag, (el.text or "").strip())] + [(k.split("}")[-1], v) for k, v in el.attrib.items()]
            for key, value in pairs:
                if not value:
                    continue
                if re.fullmatch(r"(ecu)?name", key, re.I) and "ecu" not in plan.info and not plan.ecu:
                    plan.ecu = value
                for attr, rx in _ID_KEYS:
                    if rx.search(key) and getattr(plan, attr) is None:
                        try:
                            setattr(plan, attr, _int(value if value.lower().startswith("0x") or value.isdigit()
                                                     else "0x" + value))
                        except ValueError:
                            pass
        break


def load_flashpack(path: str | Path) -> FlashPlan:
    """Pack/Manifest/Abbild laden und in einen FlashPlan interpretieren (liest nur, schreibt/fuehrt nichts aus)."""
    path = Path(path)
    suffix = path.suffix.lower()
    try:
        if zipfile.is_zipfile(path):
            src = _zip_source(path)
            manifest = next((n for n in src.names for m in MANIFEST_NAMES if n.lower().rsplit("/", 1)[-1] == m), None)
            if manifest:
                plan = _plan_from_manifest(_parse_manifest(src.read(manifest).decode("utf-8"), manifest), src,
                                           path.name)
                if not plan.info.get("vFlash"):
                    _scan_vflash_xml(src, FlashPlan("", "", "vflash"))        # nur um Fehler frueh zu erkennen
                return _finish(plan)
            return _finish(_plan_from_vflash(src, path))
        if suffix in (".json", ".yaml", ".yml"):
            src = _dir_source(path.parent)
            return _finish(_plan_from_manifest(_parse_manifest(path.read_text(encoding="utf-8"), path.name), src,
                                               path.name))
        if suffix in HEX_EXT | SREC_EXT | BIN_EXT:
            plan = FlashPlan(name=path.stem, source=path.name, format="image")
            plan.warn("flash.warn.generic_sequence")
            for addr, data in parse_image(path.name, path.read_bytes(), None, plan):
                plan.blocks.append(DataBlock(addr, data, path.name, path.name))
            return _finish(plan)
    except (zipfile.BadZipFile, OSError) as exc:
        raise FlashPackError(str(exc)) from exc
    raise FlashPackError(tr("flash.err.unsupported_file", name=path.name))


def _plan_from_vflash(src: _Source, path: Path) -> FlashPlan:
    """Echtes vFlash-Pack ohne Manifest: Datendateien suchen, Projekt-XML heuristisch auswerten."""
    plan = FlashPlan(name=path.stem, source=path.name, format="vflash")
    ignored = []
    for name in sorted(src.names):
        suffix = Path(name).suffix.lower()
        if suffix in HEX_EXT | SREC_EXT | BIN_EXT:
            for addr, data in parse_image(name, src.read(name), None, plan):
                plan.blocks.append(DataBlock(addr, data, "", name))
        elif suffix not in (".vflash", ".vflashproj", ".xml"):
            ignored.append(name)
    _scan_vflash_xml(src, plan)
    plan.warn("flash.warn.vflash_heuristic")
    plan.warn("flash.warn.generic_sequence")
    if ignored:
        plan.info["ignored"] = ", ".join(ignored[:8]) + (" ..." if len(ignored) > 8 else "")
        plan.warn("flash.warn.ignored_files", count=len(ignored))
    plan.warn("flash.warn.vflash_security")
    return plan


def _finish(plan: FlashPlan) -> FlashPlan:
    if not plan.blocks and any(s.get("type") in ("erase", "download") for s in plan.effective_steps()):
        plan.warn("flash.warn.no_data")
    return plan


# --------------------------------------------------------------------------- Schritte
STEP_TYPES = ("session", "comm_control", "security", "routine", "erase", "download", "check", "request",
              "reset", "wait", "comment")


def step_title(plan: FlashPlan, st: dict) -> str:
    kind = st.get("type", "?")
    args: dict[str, Any] = {k: v for k, v in st.items() if k != "type"}
    if kind in ("erase", "download"):
        blk = plan.blocks[int(st["block"])] if 0 <= int(st["block"]) < len(plan.blocks) else None
        args.update(address=f"{blk.address:08X}" if blk else "?", length=len(blk.data) if blk else 0)
    for key in ("session", "reset", "control"):
        if key in args and isinstance(args[key], int):
            args[key] = f"{args[key]:02X}"
    args.setdefault("level", 1)
    args.setdefault("routine", "FF01" if kind == "check" else "")
    args.setdefault("data", "")
    args.setdefault("text", "")
    args.setdefault("ms", 0)
    args.setdefault("block", 0)
    args.setdefault("control", "")
    args.setdefault("session", "")
    args.setdefault("reset", "")
    args.setdefault("comm_type", 1)
    if kind not in STEP_TYPES:
        return tr("flash.step.unknown", kind=kind)
    try:
        return tr(f"flash.step.{kind}", **args)
    except (KeyError, IndexError):
        return kind


def compute_key(seed: bytes, sec: dict) -> bytes:
    kind = sec.get("type", "none")
    n = len(seed)
    value = _int(sec.get("value", 0))
    if kind == "xor":
        key = (int.from_bytes(seed, "big") ^ value) & ((1 << (8 * n)) - 1)
    elif kind == "add":
        key = (int.from_bytes(seed, "big") + value) & ((1 << (8 * n)) - 1)
    elif kind == "constant":
        return value.to_bytes(int(sec.get("length", n)), "big")
    else:
        raise FlashStepError(tr("flash.err.no_algorithm"))
    return key.to_bytes(n, "big")


# --------------------------------------------------------------------------- Ausfuehrung
@dataclass
class StepResult:
    index: int
    title: str
    status: str                    # ok | fail | skipped | aborted
    message: str = ""
    duration_s: float = 0.0


@dataclass
class FlashReport:
    status: str = "running"        # running | passed | failed | aborted
    results: list[StepResult] = field(default_factory=list)
    bytes_done: int = 0
    duration_s: float = 0.0


class FlashRunner:
    """Fuehrt einen FlashPlan in eigenem Thread aus. Ereignisse: started, step_start, progress, step_done, log, finished."""

    def __init__(self, channel: IsoTpChannel, on_event: Callable[[str, dict], None] | None = None) -> None:
        self.channel = channel
        self.on_event = on_event or (lambda _k, _p: None)
        self.report = FlashReport()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, plan: FlashPlan) -> None:
        if self.running:
            raise FlashStepError(tr("flash.err.running"))
        self._stop.clear()
        self.report = FlashReport()
        self._thread = threading.Thread(target=self._run, args=(plan,), name="flash-runner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    # -- intern
    def _emit(self, kind: str, **payload: Any) -> None:
        try:
            self.on_event(kind, payload)
        except Exception:                    # noqa: BLE001 - GUI-Fehler darf den Flashvorgang nicht abbrechen
            pass

    def _run(self, plan: FlashPlan) -> None:
        steps = plan.expanded_steps()
        rep = self.report
        t_all = time.monotonic()
        self._emit("started", steps=[s["title"] for s in steps], total_bytes=plan.total_bytes)
        failed = aborted = False
        for i, st in enumerate(steps):
            if failed or aborted:
                rep.results.append(StepResult(i, st["title"], "skipped"))
                self._emit("step_done", index=i, status="skipped", message="")
                continue
            self._emit("step_start", index=i, title=st["title"])
            t0 = time.monotonic()
            try:
                msg = self._exec(plan, i, st)
                status = "ok"
            except FlashAbort:
                status, msg, aborted = "aborted", tr("flash.err.aborted"), True
            except (FlashStepError, IsoTpError) as exc:
                status, msg, failed = "fail", str(exc), True
            except Exception as exc:         # noqa: BLE001
                status, msg, failed = "fail", repr(exc), True
            rep.results.append(StepResult(i, st["title"], status, msg, time.monotonic() - t0))
            self._emit("step_done", index=i, status=status, message=msg)
        rep.status = "aborted" if aborted else "failed" if failed else "passed"
        rep.duration_s = time.monotonic() - t_all
        self._emit("finished", report=rep)

    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise FlashAbort

    def _ask(self, data: bytes, timeout: float = 2.0, pending: float = 10.0) -> bytes:
        self._check_stop()
        self._emit("log", text=f">> {bytes_to_hex(data[:24])}{' ...' if len(data) > 24 else ''}")
        resp = self.channel.request(data, timeout=timeout, pending_timeout=pending)
        if resp is None:
            raise FlashStepError(tr("flash.err.no_response"))
        self._emit("log", text=f"<< {bytes_to_hex(resp.raw[:24])}")
        if not resp.positive:
            raise FlashStepError(resp.text)
        return resp.raw

    def _exec(self, plan: FlashPlan, index: int, st: dict) -> str:
        kind = st["type"]
        if kind == "session":
            self._ask(bytes([0x10, int(st["session"])]))
        elif kind == "comm_control":
            self._ask(bytes([0x28, int(st.get("control", 3)), int(st.get("comm_type", 1))]))
        elif kind == "security":
            self._security(plan, int(st.get("level", 1)))
        elif kind in ("routine", "check"):
            rid = _int("0x" + str(st.get("routine", "FF01")).replace("0x", ""))
            data = hex_to_bytes(str(st.get("data", ""))) if st.get("data") else b""
            self._ask(bytes([0x31, int(st.get("control", 1)), rid >> 8, rid & 0xFF]) + data,
                      timeout=float(st.get("timeout_ms", 5000)) / 1000, pending=60.0)
        elif kind == "erase":
            blk = self._block(plan, st)
            rid = _int("0x" + str(st.get("routine", "FF00")).replace("0x", ""))
            self._ask(bytes([0x31, 0x01, rid >> 8, rid & 0xFF]) + self._alfid(plan, blk.address, len(blk.data)),
                      timeout=float(st.get("timeout_ms", 10000)) / 1000, pending=120.0)
        elif kind == "download":
            return self._download(plan, index, self._block(plan, st))
        elif kind == "request":
            raw = self._ask(hex_to_bytes(str(st["data"])), timeout=float(st.get("timeout_ms", 2000)) / 1000)
            expect = hex_to_bytes(str(st["expect"])) if st.get("expect") else b""
            if not raw.startswith(expect):
                raise FlashStepError(tr("flash.err.expected", expect=bytes_to_hex(expect), got=bytes_to_hex(raw)))
        elif kind == "reset":
            self._ask(bytes([0x11, int(st.get("reset", 1))]), timeout=3.0)
        elif kind == "wait":
            for _ in range(max(1, int(st.get("ms", 0)) // 50)):
                self._check_stop()
                time.sleep(0.05)
        return ""

    def _block(self, plan: FlashPlan, st: dict) -> DataBlock:
        i = int(st["block"])
        if not 0 <= i < len(plan.blocks):
            raise FlashStepError(tr("flash.err.block_missing", block=i))
        return plan.blocks[i]

    @staticmethod
    def _alfid(plan: FlashPlan, address: int, length: int) -> bytes:
        n_len, n_addr = plan.alfid >> 4, plan.alfid & 0x0F
        if address >> (8 * n_addr) or length >> (8 * n_len):
            raise FlashStepError(tr("flash.err.alfid_too_small", alfid=f"{plan.alfid:02X}"))
        return bytes([plan.alfid]) + address.to_bytes(n_addr, "big") + length.to_bytes(n_len, "big")

    def _security(self, plan: FlashPlan, level: int) -> None:
        raw = self._ask(bytes([0x27, level]))
        seed = raw[2:]
        if not seed:
            raise FlashStepError(tr("flash.err.no_seed"))
        if not any(seed):                            # Seed 0 = bereits entsperrt
            self._emit("log", text=tr("flash.log.already_unlocked"))
            return
        self._ask(bytes([0x27, level + 1]) + compute_key(seed, plan.security))

    def _download(self, plan: FlashPlan, index: int, blk: DataBlock) -> str:
        raw = self._ask(bytes([0x34, 0x00]) + self._alfid(plan, blk.address, len(blk.data)), timeout=5.0, pending=30.0)
        n = (raw[1] >> 4) if len(raw) > 1 else 0
        max_len = int.from_bytes(raw[2:2 + n], "big") if n and len(raw) >= 2 + n else 0
        chunk = max(1, min(plan.max_chunk, (max_len - 2) if max_len > 2 else 256))
        total, done, seq = len(blk.data), 0, 1
        t0 = time.monotonic()
        while done < total:
            part = blk.data[done:done + chunk]
            self._ask(bytes([0x36, seq]) + part, timeout=3.0, pending=30.0)
            done += len(part)
            self.report.bytes_done += len(part)
            seq = (seq + 1) & 0xFF
            self._emit("progress", index=index, done=done, total=total, speed=done / max(time.monotonic() - t0, 1e-6),
                       bytes_done=self.report.bytes_done)
        self._ask(bytes([0x37]), timeout=5.0, pending=60.0)
        return tr("flash.msg.transferred", length=total, chunk=chunk)


class FlashAbort(Exception):
    pass
