"""sequence_runner.py - Testsequenzen (JSON) laden, speichern, ausfuehren.

Format (sequences/<name>.json):
{
  "name": "startup", "description": "...",
  "steps": [
    {"type": "enable_tx", "message": "DriverCmd", "enabled": true},
    {"type": "set_signal", "message": "DriverCmd", "signal": "StartButton", "value": 1},
    {"type": "wait", "ms": 500},
    {"type": "wait_for", "direction": "RX", "message": "EngineData", "signal": "RPM",
     "op": ">", "value": 600, "timeout_ms": 3000},
    {"type": "check", "direction": "RX", "message": "EngineData", "signal": "RPM", "op": "<", "value": 9000},
    {"type": "send_once", "message": "DriverCmd"},
    {"type": "set_cycle", "message": "DriverCmd", "cycle_ms": 50},
    {"type": "diag", "service": "ReadVIN", "params": {}, "expect": "62 F1 90"},
    {"type": "diag_raw", "request": "22 F1 86", "expect": "62 F1 86"},
    {"type": "loop", "count": 3, "steps": [ ... ]},
    {"type": "comment", "text": "..."}
  ]
}
Ein Runner fuehrt immer genau eine Sequenz aus.
"""
from __future__ import annotations

import html
import json
import operator
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from can_manager import CanManager, Frame, build_raw_payload, make_key, parse_can_id
from i18n import tr
from iso_tp_manager import DiagDefinition, IsoTpChannel, bytes_to_hex, hex_to_bytes

OPS: dict[str, Callable[[float, float], bool]] = {
    "==": lambda a, b: abs(a - b) < 1e-9, "!=": lambda a, b: abs(a - b) >= 1e-9,
    "<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge,
}

STEP_TEMPLATES: dict[str, dict] = {
    "set_signal": {"type": "set_signal", "message": "", "signal": "", "value": 0},
    "enable_tx": {"type": "enable_tx", "message": "", "enabled": True},
    "set_cycle": {"type": "set_cycle", "message": "", "cycle_ms": 100},
    "send_once": {"type": "send_once", "message": ""},
    "send_raw": {"type": "send_raw", "id": "0x100", "data": "00 01 02 03 04 05 06 07", "dlc": 8,
                 "cyclic": False, "cycle_ms": 1000},
    "wait": {"type": "wait", "ms": 500},
    "wait_for": {"type": "wait_for", "direction": "RX", "message": "", "signal": "", "op": "==",
                 "value": 0, "timeout_ms": 3000},
    "check": {"type": "check", "direction": "RX", "message": "", "signal": "", "op": "==", "value": 0},
    "diag": {"type": "diag", "service": "", "params": {}, "expect": ""},
    "diag_raw": {"type": "diag_raw", "request": "3E 00", "expect": "7E 00"},
    "loop": {"type": "loop", "count": 2, "steps": []},
    "comment": {"type": "comment", "text": ""},
}


#: Sprachneutrale Befehlsdaten: Elemente (Name, Typ, Pflicht) und lauffaehige Beispiele gegen das Demo-Projekt
#: (tests/test_help.py fuehrt sie aus). Titel, Beschreibungen, Format und Hinweise stehen in lang/*.yaml (steps.*).
STEP_HELP: dict[str, dict] = {
    "set_signal": {
        "fields": [('message', 'text', True), ('signal', 'text', True), ('value', 'number_or_text', True)],
        "example": {"type": "set_signal", "message": "DriverCmd", "signal": "ThrottleCmd", "value": 40},
    },
    "enable_tx": {
        "fields": [('message', 'text', True), ('enabled', 'bool', True)],
        "example": {"type": "enable_tx", "message": "DriverCmd", "enabled": True},
    },
    "set_cycle": {
        "fields": [('message', 'text', True), ('cycle_ms', 'number', True)],
        "example": {"type": "set_cycle", "message": "DriverCmd", "cycle_ms": 20},
    },
    "send_once": {
        "fields": [('message', 'text', True)],
        "example": {"type": "send_once", "message": "DriverCmd"},
    },
    "send_raw": {
        "fields": [('id', 'number_or_text', True), ('data', 'hex', True), ('dlc', 'number', False),
                   ('cyclic', 'bool', False), ('cycle_ms', 'number', False), ('enabled', 'bool', False),
                   ('extended', 'bool', False), ('fd', 'bool', False)],
        "example": {"type": "send_raw", "id": "0x100", "data": "00 01 02 03 04 05 06 07", "dlc": 8,
                    "cyclic": True, "cycle_ms": 1000},
    },
    "wait": {
        "fields": [('ms', 'number', True)],
        "example": {"type": "wait", "ms": 500},
    },
    "wait_for": {
        "fields": [('direction', 'direction', True), ('message', 'text', True), ('signal', 'text', True), ('op', 'text', True), ('value', 'number', True), ('timeout_ms', 'number', False)],
        "example": {"type": "wait_for", "direction": "RX", "message": "EngineData", "signal": "RPM", "op": ">", "value": 100, "timeout_ms": 3000},
    },
    "check": {
        "fields": [('direction', 'direction', True), ('message', 'text', True), ('signal', 'text', True), ('op', 'text', True), ('value', 'number', True)],
        "example": {"type": "check", "direction": "RX", "message": "EngineData", "signal": "CoolantTemp", "op": "<", "value": 150},
    },
    "diag": {
        "fields": [('service', 'text', True), ('params', 'object', False), ('expect', 'hex', False), ('timeout_ms', 'number', False)],
        "example": {"type": "diag", "service": "ExtendedSession", "params": {"session": 3}, "expect": "50 03"},
    },
    "diag_raw": {
        "fields": [('request', 'hex', True), ('expect', 'hex', False), ('timeout_ms', 'number', False)],
        "example": {"type": "diag_raw", "request": "3E 00", "expect": "7E 00"},
    },
    "loop": {
        "fields": [('count', 'number', True), ('steps', 'list', True)],
        "example": {"type": "loop", "count": 3, "steps": [{"type": "set_signal", "message": "DriverCmd", "signal": "ThrottleCmd", "value": 20}, {"type": "wait", "ms": 200}]},
    },
    "comment": {
        "fields": [('text', 'text', False)],
        "example": {"type": "comment", "text": "Hier beginnt der Hochlauftest"},
    },
}


def step_title(kind: str) -> str:
    return tr(f"steps.{kind}.title")


def step_summary(kind: str) -> str:
    return tr(f"steps.{kind}.summary")


def step_format(kind: str) -> str:
    return tr(f"steps.{kind}.format")


def step_notes(kind: str) -> str:
    return tr(f"steps.{kind}.notes")


def step_example_json(kind: str) -> str:
    return json.dumps(STEP_HELP[kind]["example"], ensure_ascii=False)


def step_fields(kind: str) -> list[tuple[str, str, str]]:
    """[(Elementname, Typ-/Pflichttext, Beschreibung)] in der aktuellen Sprache."""
    out = []
    for name, ftype, required in STEP_HELP[kind]["fields"]:
        type_text = tr("steps.type_required", type=tr(f"steps.type.{ftype}"),
                       required=tr("steps.required" if required else "steps.optional"))
        out.append((name, type_text, tr(f"steps.{kind}.fields.{name}")))
    return out


def step_tooltip_html(kind: str, width: int = 520) -> str:
    """Rich-Text-Tooltip: Format, Beschreibung der Elemente, Beispiel (in der aktuellen Sprache)."""
    e = html.escape
    rows = "".join(
        f"<tr><td><b>{e(name)}</b>&nbsp;&nbsp;</td><td><i>{e(kind_)}</i>&nbsp;&nbsp;</td><td>{e(desc)}</td></tr>"
        for name, kind_, desc in step_fields(kind))
    notes = step_notes(kind)
    notes_html = f"<p><b>{e(tr('steps.label.note'))}</b> {e(notes)}</p>" if notes else ""
    return (f'<table width="{width}"><tr><td>'
            f"<p><b>{e(kind)}</b> &ndash; {e(step_title(kind))}</p>"
            f"<p>{e(step_summary(kind))}</p>"
            f"<p><b>{e(tr('steps.label.format'))}</b></p><pre>{e(step_format(kind))}</pre>"
            f"<p><b>{e(tr('steps.label.elements'))}</b></p>"
            f'<table border="0" cellspacing="0" cellpadding="2">{rows}</table>'
            f"<p><b>{e(tr('steps.label.example'))}</b></p><pre>{e(step_example_json(kind))}</pre>"
            f"{notes_html}</td></tr></table>")


class SequenceError(Exception):
    pass


class StepFailed(SequenceError):
    pass


def new_sequence(name: str = "neu") -> dict:
    return {"name": name, "description": "", "steps": []}


def load_sequence(path: Path) -> dict:
    seq = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(seq.get("steps"), list):
        raise SequenceError(tr("err.seq.no_steps"))
    seq.setdefault("name", Path(path).stem)
    seq.setdefault("description", "")
    return seq


def save_sequence(seq: dict, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(seq, indent=2, ensure_ascii=False), encoding="utf-8")


def validate_sequence(seq: dict, mgr: CanManager | None = None) -> list[str]:
    """Statische Pruefung; liefert Fehlertexte (leer = ok)."""
    problems: list[str] = []

    def walk(steps: list, prefix: str) -> None:
        for i, st in enumerate(steps, 1):
            where = f"{prefix}{i}"
            kind = st.get("type")
            if kind not in STEP_TEMPLATES:
                problems.append(tr("seq.problem.unknown_type", where=where, kind=kind))
                continue
            if kind in ("wait_for", "check") and st.get("op") not in OPS:
                problems.append(tr("seq.problem.bad_op", where=where, op=st.get("op")))
            if kind in ("set_signal", "enable_tx", "set_cycle", "send_once", "wait_for", "check") \
                    and mgr is not None and st.get("message") not in mgr.db.messages:
                problems.append(tr("seq.problem.no_message", where=where, message=st.get("message")))
            elif kind in ("set_signal", "wait_for", "check") and mgr is not None \
                    and st.get("signal") not in mgr.db.messages[st["message"]]["signals"]:
                problems.append(tr("seq.problem.no_signal", where=where, signal=st.get("signal"), message=st["message"]))
            if kind == "send_raw":
                try:
                    parse_can_id(st.get("id"))
                    build_raw_payload(st.get("data"), st.get("dlc"))
                    if st.get("cyclic") and st.get("enabled", True) and float(st.get("cycle_ms", 100)) <= 0:
                        raise ValueError(tr("err.can.bad_cycle", cycle_ms=st.get("cycle_ms")))
                except (ValueError, TypeError) as exc:
                    problems.append(tr("seq.problem.bad_raw", where=where, error=exc))
            if kind == "loop":
                walk(st.get("steps", []), f"{where}.")

    walk(seq.get("steps", []), "")
    return problems


@dataclass
class StepResult:
    index: str
    description: str
    status: str            # "ok" | "fail" | "error" | "skipped"
    message: str = ""
    duration_s: float = 0.0


@dataclass
class RunReport:
    name: str
    results: list[StepResult] = field(default_factory=list)
    status: str = "running"  # running | passed | failed | aborted

    def summary(self) -> str:
        ok = sum(r.status == "ok" for r in self.results)
        return tr("seq.summary", name=self.name, status=tr(f"seq.run_status.{self.status}"), ok=ok,
                  total=len(self.results))


class SequenceRunner:
    """Fuehrt eine Sequenz in eigenem Thread aus. diag_channel_factory() -> (IsoTpChannel, DiagDefinition|None, ecu)."""

    def __init__(self, mgr: CanManager,
                 diag_factory: Callable[[], tuple[IsoTpChannel, DiagDefinition | None, str]] | None = None,
                 on_event: Callable[[str, Any], None] | None = None) -> None:
        self.mgr = mgr
        self.diag_factory = diag_factory
        self.on_event = on_event or (lambda kind, payload: None)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._latest: dict[str, tuple[float, float]] = {}
        self._watch: set[str] = set()
        self._lock = threading.Lock()
        self.report: RunReport | None = None
        self.stop_on_fail = True

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- Steuerung
    def start(self, seq: dict) -> None:
        if self.running:
            raise SequenceError(tr("err.seq.running"))
        if not self.mgr.connected:
            raise SequenceError(tr("err.seq.not_connected"))
        problems = validate_sequence(seq, self.mgr)
        if problems:
            raise SequenceError(tr("err.seq.invalid", problems="\n".join(problems)))
        self._stop.clear()
        self.report = RunReport(seq.get("name", "?"))
        self._thread = threading.Thread(target=self._run, args=(seq,), name="sequence", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    # -- Signalueberwachung (unabhaengig von der GUI-Auswahl)
    def _collect_watch(self, steps: list) -> None:
        for st in steps:
            if st.get("type") in ("wait_for", "check"):
                self._watch.add(f"{st['direction'].upper()}:{st['message']}")
            if st.get("type") == "loop":
                self._collect_watch(st.get("steps", []))

    def _on_frame(self, frame: Frame) -> None:
        name = self.mgr.db.find_by_id(frame.arbitration_id, frame.is_extended)
        if name is None or f"{frame.direction.upper()}:{name}" not in self._watch:
            return
        try:
            values = self.mgr.db.decode(name, frame.data)
        except Exception:
            return
        with self._lock:
            for sig, val in values.items():
                self._latest[make_key(frame.direction, name, sig)] = (frame.timestamp, val)

    # -- Ausfuehrung
    def _run(self, seq: dict) -> None:
        report = self.report
        self._latest.clear()
        self._watch = set()
        self._collect_watch(seq["steps"])
        self.mgr.add_listener(self._on_frame)
        self.on_event("started", report)
        try:
            self._exec_steps(seq["steps"], "", report)
            report.status = "aborted" if self._stop.is_set() else \
                ("failed" if any(r.status in ("fail", "error") for r in report.results) else "passed")
        except StepFailed:
            report.status = "failed"
        except Exception as exc:
            report.results.append(StepResult("-", tr("seq.runtime_error"), "error", repr(exc)))
            report.status = "failed"
        finally:
            self.mgr.remove_listener(self._on_frame)
            self.on_event("finished", report)

    def _sleep(self, seconds: float) -> None:
        self._stop.wait(seconds)

    def _exec_steps(self, steps: list, prefix: str, report: RunReport) -> None:
        for i, st in enumerate(steps, 1):
            if self._stop.is_set():
                return
            idx = f"{prefix}{i}"
            desc = self._describe(st)
            t0 = time.monotonic()
            try:
                if st["type"] == "loop":
                    report.results.append(StepResult(idx, desc, "ok"))
                    self.on_event("step", report.results[-1])
                    for n in range(int(st.get("count", 1))):
                        if self._stop.is_set():
                            return
                        self._exec_steps(st.get("steps", []), f"{idx}.{n + 1}.", report)
                    continue
                message = self._exec_one(st)
                result = StepResult(idx, desc, "ok", message, time.monotonic() - t0)
            except StepFailed as exc:
                result = StepResult(idx, desc, "fail", str(exc), time.monotonic() - t0)
            except Exception as exc:
                result = StepResult(idx, desc, "error", repr(exc), time.monotonic() - t0)
            report.results.append(result)
            self.on_event("step", result)
            if result.status != "ok" and self.stop_on_fail:
                raise StepFailed(result.message)

    @staticmethod
    def _describe(st: dict) -> str:
        t = st.get("type")
        if t == "set_signal":
            return tr("seq.desc.set_signal", message=st["message"], signal=st["signal"], value=st["value"])
        if t == "enable_tx":
            return tr("seq.desc.enable_on" if st["enabled"] else "seq.desc.enable_off", message=st["message"])
        if t == "set_cycle":
            return tr("seq.desc.set_cycle", message=st["message"], cycle_ms=st["cycle_ms"])
        if t == "send_once":
            return tr("seq.desc.send_once", message=st["message"])
        if t == "send_raw":
            ident = f"0x{parse_can_id(st['id']):X}"
            if st.get("cyclic") and not st.get("enabled", True):
                return tr("seq.desc.send_raw_stop", id=ident)
            if st.get("cyclic"):
                return tr("seq.desc.send_raw_cyclic", id=ident, cycle_ms=st.get("cycle_ms", 100), data=st.get("data", ""))
            return tr("seq.desc.send_raw_once", id=ident, data=st.get("data", ""))
        if t == "wait":
            return tr("seq.desc.wait", ms=st["ms"])
        if t in ("wait_for", "check"):
            return tr("seq.desc.wait_for" if t == "wait_for" else "seq.desc.check", direction=st["direction"],
                   message=st["message"], signal=st["signal"], op=st["op"], value=st["value"])
        if t == "diag":
            return tr("seq.desc.diag", service=st["service"])
        if t == "diag_raw":
            return tr("seq.desc.diag_raw", request=st["request"])
        if t == "loop":
            return tr("seq.desc.loop", count=st.get("count", 1))
        return st.get("text", "") or str(t)

    def _exec_one(self, st: dict) -> str:
        t, mgr = st["type"], self.mgr
        if t == "comment":
            return ""
        if t == "set_signal":
            self._ensure_tx(st["message"])
            mgr.set_tx_value(st["message"], st["signal"], st["value"])
            return ""
        if t == "enable_tx":
            self._ensure_tx(st["message"])
            mgr.set_tx_enabled(st["message"], bool(st["enabled"]))
            return ""
        if t == "set_cycle":
            self._ensure_tx(st["message"])
            mgr.set_tx_cycle(st["message"], float(st["cycle_ms"]))
            return ""
        if t == "send_once":
            self._ensure_tx(st["message"])
            mgr.send_once(st["message"])
            return ""
        if t == "send_raw":
            mgr.tx_raw(st["id"], st.get("data", ""), st.get("dlc"), bool(st.get("cyclic")),
                       float(st.get("cycle_ms", 100)), bool(st.get("enabled", True)), st.get("extended"), st.get("fd"))
            return ""
        if t == "wait":
            self._sleep(float(st["ms"]) / 1000.0)
            return ""
        if t == "check":
            return self._check(st, fresh_since=None)
        if t == "wait_for":
            start = time.time()
            deadline = time.monotonic() + float(st.get("timeout_ms", 3000)) / 1000.0
            last = tr("seq.msg.no_value")
            while time.monotonic() < deadline and not self._stop.is_set():
                try:
                    return self._check(st, fresh_since=start)
                except StepFailed as exc:
                    last = str(exc)
                self._stop.wait(0.01)
            raise StepFailed(tr("seq.msg.timeout", detail=last))
        if t in ("diag", "diag_raw"):
            return self._diag(st)
        raise StepFailed(tr("seq.msg.unknown_step", kind=t))

    def _ensure_tx(self, message: str) -> None:
        if message not in self.mgr.tx_entries:
            self.mgr.add_tx_message(message)

    def _check(self, st: dict, fresh_since: float | None) -> str:
        key = make_key(st["direction"], st["message"], st["signal"])
        with self._lock:
            entry = self._latest.get(key)
        if entry is None or (fresh_since is not None and entry[0] < fresh_since):
            raise StepFailed(tr("seq.msg.no_new_value", key=key))
        ts, val = entry
        if OPS[st["op"]](val, float(st["value"])):
            return f"{key} = {val}"
        raise StepFailed(tr("seq.msg.expected", key=key, val=val, op=st["op"], value=st["value"]))

    def _diag(self, st: dict) -> str:
        if self.diag_factory is None:
            raise StepFailed(tr("seq.msg.diag_not_configured"))
        channel, definition, ecu = self.diag_factory()
        if st["type"] == "diag":
            if definition is None:
                raise StepFailed(tr("seq.msg.no_diag_def"))
            service = definition.services(ecu).get(st["service"])
            if service is None:
                raise StepFailed(tr("seq.msg.no_service", service=st["service"]))
            request = definition.build_request(service, st.get("params"))
        else:
            request = hex_to_bytes(st["request"])
        resp = channel.request(request, timeout=float(st.get("timeout_ms", 1000)) / 1000.0)
        if resp is None:
            return tr("seq.msg.no_response_expected")
        expect = st.get("expect", "").strip()
        if expect:
            if not resp.raw.startswith(hex_to_bytes(expect)):
                raise StepFailed(tr("seq.msg.resp_mismatch", response=bytes_to_hex(resp.raw), expect=expect))
        elif not resp.positive:
            raise StepFailed(resp.text)
        return bytes_to_hex(resp.raw)
