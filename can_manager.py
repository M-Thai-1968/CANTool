"""can_manager.py - CAN-Zugriff, zyklisches Senden, Empfangen, Signalspeicher.

Backends:
  * PythonCanBackend : Vector (VN1630A, VN7572, XL-Treiber) und PEAK (PCAN-USB Pro FD) ueber python-can
  * SimulationBackend: rein softwarebasierter Bus ohne Hardware (Demo/Tests)

Der CanManager verteilt alle Frames (rx und tx) an registrierte Listener
(Trace, Signalspeicher, Logger, ISO-TP). Listener laufen im Empfangs-Thread bzw. im
Sende-Thread und muessen schnell zurueckkehren.
"""
from __future__ import annotations

import queue
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from dbc_manager import DbcDatabase
from i18n import tr
from platform_info import (current_os, driver_available, hardware_supported, os_label, vector_devices,
                           vector_dll_name)

_FD_LENGTHS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64]


def round_up_dlc(n: int) -> int:
    """Kleinste gueltige CAN-FD-Datenlaenge >= n."""
    for length in _FD_LENGTHS:
        if length >= n:
            return length
    raise ValueError(tr("err.can.fd_too_long", n=n))


def make_key(direction: str, msg: str, sig: str) -> str:
    return f"{direction.upper()}:{msg}.{sig}"


def split_key(key: str) -> tuple[str, str, str]:
    direction, rest = key.split(":", 1)
    msg, sig = rest.split(".", 1)
    return direction, msg, sig


@dataclass
class Frame:
    timestamp: float
    arbitration_id: int
    data: bytes
    is_extended: bool = False
    is_fd: bool = False
    bitrate_switch: bool = False
    direction: str = "rx"          # "rx" | "tx"
    channel: str = ""
    is_error: bool = False         # Error-Frame des Busses (nur fuer den Trace sichtbar)

    def copy_as(self, direction: str) -> "Frame":
        return Frame(self.timestamp, self.arbitration_id, bytes(self.data), self.is_extended,
                     self.is_fd, self.bitrate_switch, direction, self.channel, self.is_error)


# --------------------------------------------------------------------------- Backends
class CanBackend:
    def open(self) -> None: ...
    def close(self) -> None: ...
    def send(self, frame: Frame) -> None: ...
    def recv(self, timeout: float) -> Frame | None: ...


class VirtualNetwork:
    """Gemeinsamer Software-Bus; alle SimulationBackends mit gleichem Namen sind verbunden."""
    _nets: dict[str, "VirtualNetwork"] = {}
    _lock = threading.Lock()

    def __init__(self) -> None:
        self.members: list[SimulationBackend] = []

    @classmethod
    def get(cls, name: str) -> "VirtualNetwork":
        with cls._lock:
            return cls._nets.setdefault(name, VirtualNetwork())


class SimulationBackend(CanBackend):
    """echo=True: eigene Frames kommen zusaetzlich als RX zurueck (Demo ohne Gegenstelle)."""

    def __init__(self, network: str = "sim", echo: bool = False) -> None:
        self.net = VirtualNetwork.get(network)
        self.echo = echo
        self.q: queue.Queue[Frame] = queue.Queue()
        self.name = f"sim:{network}"

    def open(self) -> None:
        self.net.members.append(self)

    def close(self) -> None:
        if self in self.net.members:
            self.net.members.remove(self)

    def send(self, frame: Frame) -> None:
        if self not in self.net.members:
            raise RuntimeError(tr("err.can.backend_closed"))
        for member in list(self.net.members):
            if member is not self or self.echo:
                member.q.put(frame.copy_as("rx"))

    def recv(self, timeout: float) -> Frame | None:
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None


def normalize_pcan_channel(value: Any) -> str:
    """Kanalangabe fuer PEAK vereinheitlichen: '', 1, '1', 'usb1', 'PCAN_USB1' -> 'PCAN_USBBUS1'.

    Eine leere oder ungueltige Angabe fuehrt in der PCAN-Basic-API zu "handle is invalid".
    Unveraendert bleiben z. B. PCAN_USBBUS2, PCAN_PCIBUS1, PCAN_LANBUS1.
    """
    text = str(value if value is not None else "").strip().upper().replace(" ", "")
    if not text:
        return "PCAN_USBBUS1"
    if text.isdigit():
        return f"PCAN_USBBUS{int(text)}"
    m = re.fullmatch(r"(?:PCAN_?)?USB(?:BUS)?_?(\d+)", text)
    if m:
        return f"PCAN_USBBUS{int(m.group(1))}"
    return text


class PythonCanBackend(CanBackend):
    """Vector / PEAK / beliebige python-can-Schnittstelle (python-can >= 4.2)."""

    def __init__(self, hw: dict) -> None:
        self.hw = hw
        self.bus = None
        self.info = ""                      # welche Hardware/Zugriffsart tatsaechlich geoeffnet wurde (fuer die Statuszeile)
        self.name = (normalize_pcan_channel(hw.get("channel")) if hw.get("type") == "pcan"
                     else str(hw.get("channel", "")))
        if hw.get("type") == "vector" and str(hw.get("channel", "")).strip().isdigit():
            self.name = f"Vector CH{int(hw['channel']) + 1}"      # Kanalnummer 0 = CH1 (wie am Geraet beschriftet)

    def _bus_kwargs(self, override: dict[str, Any] | None = None) -> dict[str, Any]:
        hw = self.hw
        kind = hw.get("type", "vector")
        fd = bool(hw.get("fd"))
        bitrate = int(hw.get("bitrate", 500000))
        kw: dict[str, Any] = {}
        if kind == "vector":
            kw = {"interface": "vector", "channel": hw.get("channel", 0), "bitrate": bitrate}
            if hw.get("app_name"):
                kw["app_name"] = hw["app_name"]
            if fd:
                kw.update(fd=True, data_bitrate=int(hw.get("data_bitrate", 2000000)))
        elif kind == "pcan":
            kw = {"interface": "pcan", "channel": normalize_pcan_channel(hw.get("channel"))}
            if fd:
                # Standard: 80 MHz Takt, 500 kbit/s Nominal, 2 Mbit/s Daten. Andere Raten ueber "extra".
                kw.update(fd=True, f_clock_mhz=80, nom_brp=10, nom_tseg1=12, nom_tseg2=3, nom_sjw=1,
                          data_brp=4, data_tseg1=7, data_tseg2=2, data_sjw=1)
            else:
                kw["bitrate"] = bitrate
        else:
            kw = {"interface": hw.get("interface", "socketcan"), "channel": hw.get("channel", "can0"),
                  "bitrate": bitrate}
            if fd:
                kw.update(fd=True, data_bitrate=int(hw.get("data_bitrate", 2000000)))
        kw.update(override or {})
        kw.update(hw.get("extra") or {})
        return kw

    def _vector_attempts(self) -> list[dict[str, Any]]:
        """Zugriffsarten fuer Vector. Ohne App-Name gilt python-can-Standard 'CANalyzer' + Vector Hardware Config; ist dort
        kein Kanal zugewiesen ('not assigned to any interface'), scheitert das. Deshalb zuerst direkt ueber die
        Seriennummer des Geraets (Kanal = Nummer am Geraet, 0 = CH1), dann ueber den globalen Hardware-Index, zuletzt
        ueber die Hardware Config (python-can-Standard)."""
        extra = self.hw.get("extra") or {}
        if self.hw.get("app_name") or "serial" in extra or "app_name" in extra:
            return [{}]
        attempts: list[dict[str, Any]] = []
        real = [d for d in vector_devices() if not d["virtual"]]
        channel = self.hw.get("channel", 0)
        if real:
            attempts.append({"serial": real[0]["serial"], "channel": channel, "app_name": None})
        attempts += [{"app_name": None}, {}]
        return attempts

    def _describe_open(self, override: dict[str, Any]) -> str:
        if self.hw.get("type") != "vector":
            return ""
        serial = override.get("serial")
        dev = next((d for d in vector_devices() if d["serial"] == serial and str(d["hw_channel"]) == str(self.hw.get("channel", 0))),
                   None) if serial else None
        way = (tr("conn.via_serial") if serial else tr("conn.via_index") if "app_name" in override
               else tr("conn.via_hwconfig"))
        name = f"{dev['name']} (S/N {serial})" if dev else ""
        return f"{name} {way}".strip()

    def _open_error(self, errors: list[str]) -> str:
        kind = self.hw.get("type", "vector")
        hint = tr(f"err.can.hint.{kind}") if kind in ("vector", "pcan") else tr("err.can.hint.other")
        if kind == "vector":
            devs = vector_devices()
            items = "\n".join(f"  {d['name'] or '?'}  (S/N {d['serial']}, HW-Kanal {d['hw_channel']}, Index {d['channel_index']})"
                              + ("  [virtuell]" if d["virtual"] else "") for d in devs)
            hint += "\n" + (tr("err.can.vector_found", items=items) if devs else tr("err.can.vector_none"))
        return tr("err.can.open_failed", error=" | ".join(dict.fromkeys(errors)), hint=hint)

    def open(self) -> None:
        try:
            import can
        except ImportError as exc:
            raise RuntimeError(tr("err.can.missing_python_can")) from exc
        self._can = can
        if self.hw.get("type") == "vector" and current_os() == "windows" and driver_available("vector") is False:
            raise RuntimeError(tr("err.hw.vector_driver_missing", dll=vector_dll_name() + ".dll",
                                  bits=64 if sys.maxsize > 2**32 else 32))
        attempts = self._vector_attempts() if self.hw.get("type") == "vector" else [{}]
        errors: list[str] = []
        for override in attempts:
            try:
                self.bus = can.Bus(**self._bus_kwargs(override))
                self.info = self._describe_open(override)
                return
            except Exception as exc:                       # noqa: BLE001 - naechste Zugriffsart versuchen
                if "vxlapi" in str(exc).lower():
                    raise RuntimeError(tr("err.hw.vector_driver_missing", dll=vector_dll_name() + ".dll",
                                          bits=64 if sys.maxsize > 2**32 else 32) + f"\n({exc})") from exc
                errors.append(str(exc))
        raise RuntimeError(self._open_error(errors))

    def close(self) -> None:
        if self.bus is not None:
            try:
                self.bus.shutdown()
            finally:
                self.bus = None

    def send(self, frame: Frame) -> None:
        msg = self._can.Message(arbitration_id=frame.arbitration_id, data=frame.data,
                                is_extended_id=frame.is_extended, is_fd=frame.is_fd,
                                bitrate_switch=frame.bitrate_switch and frame.is_fd)
        self.bus.send(msg)

    def recv(self, timeout: float) -> Frame | None:
        m = self.bus.recv(timeout)
        if m is None or m.is_remote_frame:
            return None
        if m.is_error_frame:
            return Frame(time.time(), int(m.arbitration_id or 0), bytes(m.data or b""), bool(m.is_extended_id),
                         False, False, "rx", self.name, True)
        return Frame(time.time(), m.arbitration_id, bytes(m.data), bool(m.is_extended_id),
                     bool(m.is_fd), bool(getattr(m, "bitrate_switch", False)), "rx", self.name)


def create_backend(hw: dict) -> CanBackend:
    kind = hw.get("type", "simulation")
    if kind == "simulation":
        return SimulationBackend(hw.get("sim_network", "sim"), bool(hw.get("sim_echo", True)))
    if not hardware_supported(kind):                        # z. B. Vector unter Linux, SocketCAN unter Windows
        raise RuntimeError(tr(f"err.hw.{kind}_unsupported", os=os_label()))
    return PythonCanBackend(hw)


# --------------------------------------------------------------------------- Rohframes
VALID_DLC = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)


def parse_can_id(value: Any) -> int:
    """CAN-ID aus Zahl oder Text; Text ohne Praefix ist hexadezimal ('100' = 0x100), '0x..' ebenfalls."""
    if isinstance(value, bool):
        raise ValueError(tr("err.can.bad_id", value=value))
    if isinstance(value, int):
        number = value
    else:
        text = str(value).strip().lower().removesuffix("h")
        try:
            number = int(text, 16) if not text.startswith("0x") else int(text, 16)
        except ValueError:
            raise ValueError(tr("err.can.bad_id", value=value)) from None
    if not 0 <= number <= 0x1FFFFFFF:
        raise ValueError(tr("err.can.bad_id", value=value))
    return number


def build_raw_payload(data: Any, dlc: int | None = None) -> bytes:
    """Hex-Text -> Nutzdaten; mit DLC wird auf diese Laenge mit 00 aufgefuellt (zu lange Daten sind ein Fehler)."""
    text = "" if data is None else str(data)
    try:
        payload = bytes.fromhex("".join(text.replace("0x", " ").replace(",", " ").split()))
    except ValueError:
        raise ValueError(tr("err.can.bad_data", data=text)) from None
    if dlc is None:
        dlc = len(payload)
    dlc = int(dlc)
    if dlc not in VALID_DLC:
        raise ValueError(tr("err.can.bad_dlc", dlc=dlc))
    if len(payload) > dlc:
        raise ValueError(tr("err.can.data_longer_than_dlc", length=len(payload), dlc=dlc))
    return payload.ljust(dlc, b"\x00")


# --------------------------------------------------------------------------- Sendeliste
@dataclass
class TxEntry:
    name: str
    arb_id: int
    extended: bool = False
    fd: bool = False
    brs: bool = False
    cycle_ms: float = 100.0
    enabled: bool = False
    message: str | None = None                  # DBC-Botschaft; None = Rohframe
    values: dict[str, Any] = field(default_factory=dict)
    raw_data: bytes = b""
    count: int = 0
    next_due: float | None = None


class CanManager:
    def __init__(self, db: DbcDatabase | None = None) -> None:
        self.db = db or DbcDatabase()
        self.backend: CanBackend | None = None
        self.rx_count = 0
        self.tx_count = 0
        self.last_error = ""
        self.tx_entries: dict[str, TxEntry] = {}
        self._listeners: list[Callable[[Frame], None]] = []
        self._error_listeners: list[Callable[[Frame], None]] = []
        self._lock = threading.Lock()          # Listener-Liste
        self._tx_lock = threading.RLock()      # Sendeliste
        self._stop = threading.Event()
        self._hw: dict = {}
        self._rx_thread: threading.Thread | None = None
        self._tx_thread: threading.Thread | None = None
        self.channel_name = ""
        self.connection_info = ""
        self.connected_at = 0.0

    # -- Listener
    def add_listener(self, fn: Callable[[Frame], None], errors: bool = False) -> None:
        """errors=True: Listener bekommt zusaetzlich Error-Frames (sonst nur gueltige Botschaften)."""
        with self._lock:
            self._listeners = self._listeners + [fn]
            if errors:
                self._error_listeners = self._error_listeners + [fn]

    def remove_listener(self, fn: Callable[[Frame], None]) -> None:
        with self._lock:
            self._listeners = [f for f in self._listeners if f != fn]
            self._error_listeners = [f for f in self._error_listeners if f != fn]

    def _dispatch(self, frame: Frame) -> None:
        for fn in self._listeners:
            if frame.is_error and fn not in self._error_listeners:
                continue
            try:
                fn(frame)
            except Exception as exc:  # ein defekter Listener darf den Bus nicht stoppen
                self.last_error = tr("err.can.listener", error=repr(exc))

    # -- Verbindung
    @property
    def connected(self) -> bool:
        return self.backend is not None

    def connect(self, hw: dict) -> None:
        if self.backend is not None:
            self.disconnect()
        self._hw = dict(hw)
        backend = create_backend(hw)
        backend.open()
        self.backend = backend
        self.channel_name = getattr(backend, "name", "")
        self.connection_info = getattr(backend, "info", "")
        self.connected_at = time.monotonic()
        self.rx_count = self.tx_count = 0
        self.last_error = ""
        self._stop = stop = threading.Event()        # pro Verbindung neu: haengende Alt-Threads laufen nie wieder an
        self._rx_thread = threading.Thread(target=self._rx_loop, args=(backend, stop), name="can-rx", daemon=True)
        self._tx_thread = threading.Thread(target=self._tx_loop, args=(stop,), name="can-tx", daemon=True)
        self._rx_thread.start()
        self._tx_thread.start()

    def disconnect(self) -> None:
        self._stop.set()
        for t in (self._rx_thread, self._tx_thread):
            if t is not None and t is not threading.current_thread():
                t.join(timeout=2.0)
        backend, self.backend = self.backend, None
        if backend is not None:
            self._close_backend(backend)
        with self._tx_lock:
            for e in self.tx_entries.values():
                e.next_due = None

    def _close_backend(self, backend: CanBackend, timeout: float = 3.0) -> None:
        """Treiber schliessen, ohne dass eine haengende Hardware den Aufrufer blockiert."""
        def close() -> None:
            try:
                backend.close()
            except Exception as exc:                       # noqa: BLE001 - Schliessen darf Re-Init nicht verhindern
                self.last_error = tr("err.can.close", error=repr(exc))
        t = threading.Thread(target=close, name="can-close", daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            self.last_error = tr("err.can.close_timeout", seconds=timeout)

    def reinit(self, hw: dict | None = None) -> None:
        """Hardware neu initialisieren: schliessen, kurz warten, mit denselben Einstellungen neu oeffnen.

        Die Sendeliste bleibt erhalten; zyklisch aktive Eintraege senden danach weiter.
        """
        hw = dict(hw or self._hw or {})
        if not hw:
            raise RuntimeError(tr("err.can.not_connected"))
        self.disconnect()
        time.sleep(0.5)
        self.connect(hw)

    def _rx_loop(self, backend: CanBackend, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                frame = backend.recv(0.05)
            except Exception as exc:
                self.last_error = tr("err.can.rx", error=repr(exc))
                time.sleep(0.2)
                continue
            if frame is None:
                continue
            frame.channel = self.channel_name
            self.rx_count += 1
            self._dispatch(frame)

    # -- Senden
    def send(self, frame: Frame) -> None:
        if self.backend is None:
            raise RuntimeError(tr("err.can.not_connected"))
        if not frame.is_fd and len(frame.data) > 8:
            raise ValueError(tr("err.can.classic_limit"))
        frame.direction = "tx"
        frame.channel = self.channel_name
        frame.timestamp = time.time()
        self.backend.send(frame)
        self.tx_count += 1
        self._dispatch(frame)

    def send_data(self, arb_id: int, data: bytes, extended: bool = False, fd: bool = False,
                  brs: bool = False) -> None:
        if fd:
            data = bytes(data).ljust(round_up_dlc(len(data)), b"\x00")
        self.send(Frame(time.time(), arb_id, bytes(data), extended, fd, brs and fd))

    # -- Sendeliste (zyklisch)
    def add_tx_message(self, msg_name: str, cycle_ms: float | None = None,
                       values: dict[str, Any] | None = None, enabled: bool = False,
                       brs: bool = True) -> TxEntry:
        msg = self.db.messages[msg_name]
        entry = TxEntry(msg_name, msg["id"], bool(msg.get("extended")), bool(msg.get("fd")),
                        bool(msg.get("fd")) and brs,
                        float(cycle_ms or msg.get("cycle_time_ms") or 100), enabled, msg_name,
                        dict(values or {}))
        with self._tx_lock:
            self.tx_entries[msg_name] = entry
        return entry

    def add_tx_raw(self, arb_id: int, data: bytes, cycle_ms: float = 100, extended: bool = False,
                   fd: bool = False, name: str | None = None) -> TxEntry:
        name = name or f"RAW_0x{arb_id:X}"
        entry = TxEntry(name, arb_id, extended, fd, fd, float(cycle_ms), False, None, {}, bytes(data))
        with self._tx_lock:
            self.tx_entries[name] = entry
        return entry

    @staticmethod
    def raw_name(arb_id: int, extended: bool = False) -> str:
        return f"RAW_{'X' if extended else ''}0x{arb_id:X}"

    def tx_raw(self, arb_id: Any, data: Any, dlc: int | None = None, cyclic: bool = False, cycle_ms: float = 100,
               enabled: bool = True, extended: bool | None = None, fd: bool | None = None) -> str:
        """Rohframe einmalig oder zyklisch senden; enabled=False beendet das zyklische Senden dieser ID.

        extended/fd werden automatisch gesetzt (ID > 0x7FF bzw. DLC > 8), wenn nicht vorgegeben. Rueckgabe: Name.
        """
        arb = parse_can_id(arb_id)
        ext = bool(extended) if extended is not None else arb > 0x7FF
        name = self.raw_name(arb, ext)
        if cyclic and not enabled:
            self.remove_tx(name)
            return name
        payload = build_raw_payload(data, dlc)
        use_fd = bool(fd) if fd is not None else len(payload) > 8
        if len(payload) > 8 and not use_fd:
            raise ValueError(tr("err.can.classic_limit"))
        if not cyclic:
            self.send_data(arb, payload, ext, use_fd, use_fd)
            return name
        if float(cycle_ms) <= 0:
            raise ValueError(tr("err.can.bad_cycle", cycle_ms=cycle_ms))
        if not self.connected:
            raise RuntimeError(tr("err.can.not_connected"))
        entry = self.add_tx_raw(arb, payload, float(cycle_ms), ext, use_fd, name)
        entry.enabled = True
        return name

    def remove_tx(self, name: str) -> None:
        with self._tx_lock:
            self.tx_entries.pop(name, None)

    def clear_tx(self) -> None:
        with self._tx_lock:
            self.tx_entries.clear()

    def set_tx_value(self, name: str, signal: str, value: Any) -> None:
        with self._tx_lock:
            entry = self.tx_entries.get(name)
            if entry is None:
                raise KeyError(tr("err.can.not_in_txlist", name=name))
            if entry.message is None:
                raise ValueError(tr("err.can.raw_no_signals"))
            if signal not in self.db.messages[entry.message]["signals"]:
                raise KeyError(tr("err.can.no_signal", signal=signal, name=name))
            entry.values[signal] = value

    def get_tx_values(self, name: str) -> dict[str, Any]:
        with self._tx_lock:
            return dict(self.tx_entries[name].values)

    def set_tx_enabled(self, name: str, enabled: bool) -> None:
        with self._tx_lock:
            entry = self.tx_entries[name]
            entry.enabled = enabled
            entry.next_due = None

    def set_tx_cycle(self, name: str, cycle_ms: float) -> None:
        with self._tx_lock:
            self.tx_entries[name].cycle_ms = float(cycle_ms)

    def send_once(self, name: str) -> None:
        with self._tx_lock:
            entry = self.tx_entries[name]
        self._send_entry(entry)

    def _send_entry(self, e: TxEntry) -> None:
        with self._tx_lock:
            data = e.raw_data if e.message is None else self.db.encode(e.message, dict(e.values))
            e.count += 1
        self.send_data(e.arb_id, data, e.extended, e.fd, e.brs)

    def _tx_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            now = time.perf_counter()
            wake = now + 0.05
            with self._tx_lock:
                entries = list(self.tx_entries.values())
            for e in entries:
                if not e.enabled or e.cycle_ms <= 0:
                    e.next_due = None
                    continue
                if e.next_due is None:
                    e.next_due = now
                if now >= e.next_due:
                    try:
                        self._send_entry(e)
                    except Exception as exc:
                        self.last_error = tr("err.can.tx", name=e.name, error=repr(exc))
                        e.enabled = False
                    e.next_due += e.cycle_ms / 1000.0
                    if e.next_due < now - 0.5:      # nach langer Pause nicht aufholen
                        e.next_due = now + e.cycle_ms / 1000.0
                wake = min(wake, e.next_due)
            delay = wake - time.perf_counter()
            if delay > 0:
                stop.wait(delay)


# --------------------------------------------------------------------------- Signalspeicher
class SignalStore:
    """Letzte Werte und Verlauf der ausgewaehlten TX/RX-Signale (Schluessel: 'RX:Msg.Sig')."""

    def __init__(self, db: DbcDatabase, max_points: int = 50000) -> None:
        self.db = db
        self.max_points = max_points
        self._lock = threading.Lock()
        self._latest: dict[str, tuple[float, float]] = {}
        self._hist: dict[str, deque] = {}
        self._select: dict[str, dict[str, set[str] | None]] = {"TX": {}, "RX": {}}

    def configure(self, tx: dict[str, Iterable[str] | None], rx: dict[str, Iterable[str] | None]) -> None:
        """tx/rx: {Botschaft: [Signale] oder None (= alle Signale)}."""
        def norm(d):
            return {m: (None if s is None else set(s)) for m, s in d.items()}
        with self._lock:
            self._select = {"TX": norm(tx), "RX": norm(rx)}

    def keys(self) -> list[str]:
        with self._lock:
            return sorted(self._hist)

    def clear(self) -> None:
        with self._lock:
            self._latest.clear()
            self._hist.clear()

    def on_frame(self, frame: Frame) -> None:
        direction = frame.direction.upper()
        name = self.db.find_by_id(frame.arbitration_id, frame.is_extended)
        if name is None:
            return
        sel = self._select.get(direction, {})
        if name not in sel:
            return
        try:
            values = self.db.decode(name, frame.data)
        except Exception:
            return
        wanted = sel[name]
        with self._lock:
            for sig, val in values.items():
                if wanted is not None and sig not in wanted:
                    continue
                key = make_key(direction, name, sig)
                self._latest[key] = (frame.timestamp, val)
                h = self._hist.get(key)
                if h is None:
                    h = self._hist[key] = deque(maxlen=self.max_points)
                h.append((frame.timestamp, val))

    def latest(self, direction: str, msg: str, sig: str) -> tuple[float, float] | None:
        with self._lock:
            return self._latest.get(make_key(direction, msg, sig))

    def history(self, key: str, since: float = 0.0) -> tuple[list[float], list[float]]:
        with self._lock:
            h = list(self._hist.get(key, ()))
        pts = [p for p in h if p[0] >= since]
        return [p[0] for p in pts], [p[1] for p in pts]
