"""iso_tp_manager.py - ISO-TP (ISO 15765-2) ueber CAN / CAN FD plus UDS-Hilfen.

* IsoTpChannel   : Segmentierung/Reassemblierung (SF, FF, CF, FC), Classic und FD
* UdsResponse    : Antwort mit Positiv/Negativ-Auswertung
* DiagDefinition : Diagnose-Services aus der konvertierten CDD-Datei (JSON/YAML)
* SimulatedEcu   : Software-Steuergeraet (UDS) + Testverkehr fuer den Simulationsbus
"""
from __future__ import annotations

import math
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from can_manager import CanManager, Frame, round_up_dlc
from dbc_manager import DbcDatabase, load_document
from i18n import tr

VALID_DL = (8, 12, 16, 20, 24, 32, 48, 64)

NRC_TEXT = {
    0x10: "generalReject", 0x11: "serviceNotSupported", 0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat", 0x14: "responseTooLong", 0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect", 0x24: "requestSequenceError", 0x25: "noResponseFromSubnetComponent",
    0x31: "requestOutOfRange", 0x33: "securityAccessDenied", 0x35: "invalidKey",
    0x36: "exceededNumberOfAttempts", 0x37: "requiredTimeDelayNotExpired", 0x70: "uploadDownloadNotAccepted",
    0x78: "requestCorrectlyReceived-ResponsePending", 0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
}


class IsoTpError(Exception):
    pass


class IsoTpTimeout(IsoTpError):
    pass


def hex_to_bytes(text: str) -> bytes:
    return bytes.fromhex("".join(text.replace("0x", " ").replace(",", " ").split()))


def bytes_to_hex(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


@dataclass
class UdsResponse:
    request: bytes
    raw: bytes

    @property
    def positive(self) -> bool:
        return bool(self.raw) and self.raw[0] == (self.request[0] + 0x40) & 0xFF

    @property
    def nrc(self) -> int | None:
        return self.raw[2] if len(self.raw) >= 3 and self.raw[0] == 0x7F else None

    @property
    def text(self) -> str:
        if self.positive:
            return tr("diag.resp.positive", data=bytes_to_hex(self.raw))
        if self.nrc is not None:
            return tr("diag.resp.negative", nrc=f"{self.nrc:02X}", text=NRC_TEXT.get(self.nrc, "")).strip()
        return tr("diag.resp.unexpected", data=bytes_to_hex(self.raw))


class IsoTpChannel:
    """Ein ISO-TP-Kanal (feste Request-/Response-ID). Nicht fuer parallele Requests gedacht."""

    def __init__(self, mgr: CanManager, tx_id: int, rx_id: int, extended: bool = False,
                 fd: bool = False, brs: bool = True, tx_dl: int | None = None,
                 padding: int | None = 0xAA, block_size: int = 0, st_min: int = 0,
                 n_bs: float = 1.0, n_cr: float = 1.0) -> None:
        self.mgr, self.tx_id, self.rx_id, self.extended = mgr, tx_id, rx_id, extended
        self.fd, self.brs = fd, brs and fd
        self.tx_dl = tx_dl or (64 if fd else 8)
        if self.tx_dl not in VALID_DL or (not fd and self.tx_dl != 8):
            raise ValueError(tr("err.isotp.bad_dl", dl=self.tx_dl))
        self.padding, self.block_size, self.st_min = padding, block_size, st_min
        self.n_bs, self.n_cr = n_bs, n_cr
        self._q: queue.Queue[bytes] = queue.Queue()
        self._lock = threading.Lock()
        self._attached = False

    # -- Anbindung
    def attach(self) -> "IsoTpChannel":
        if not self._attached:
            self.mgr.add_listener(self._on_frame)
            self._attached = True
        return self

    def detach(self) -> None:
        if self._attached:
            self.mgr.remove_listener(self._on_frame)
            self._attached = False

    def _on_frame(self, frame: Frame) -> None:
        if frame.direction == "rx" and frame.arbitration_id == self.rx_id \
                and frame.is_extended == self.extended:
            self._q.put(bytes(frame.data))

    def flush(self) -> None:
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return

    def _get(self, timeout: float) -> bytes:
        try:
            return self._q.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            raise IsoTpTimeout(tr("err.isotp.timeout")) from None

    def _tx(self, payload: bytes) -> None:
        if self.fd:
            payload = payload.ljust(round_up_dlc(max(len(payload), 8)), bytes([self.padding or 0]))
        elif self.padding is not None:
            payload = payload.ljust(8, bytes([self.padding]))
        self.mgr.send_data(self.tx_id, payload, self.extended, self.fd, self.brs)

    # -- Senden
    @staticmethod
    def _st_min_seconds(value: int) -> float:
        if value <= 0x7F:
            return value / 1000.0
        if 0xF1 <= value <= 0xF9:
            return (value - 0xF0) / 10000.0
        return 0.127

    def _wait_fc(self) -> tuple[int, float]:
        end = time.monotonic() + self.n_bs
        while True:
            data = self._get(end - time.monotonic())
            if data[0] >> 4 != 3:
                continue
            fs = data[0] & 0xF
            if fs == 0:
                return (data[1] if len(data) > 1 else 0,
                        self._st_min_seconds(data[2] if len(data) > 2 else 0))
            if fs == 1:                              # WAIT
                end = time.monotonic() + self.n_bs
                continue
            raise IsoTpError(tr("err.isotp.overflow"))

    def send(self, data: bytes) -> None:
        data = bytes(data)
        n, dl = len(data), self.tx_dl
        if n == 0:
            raise ValueError(tr("err.isotp.empty"))
        sf_max = 7 if dl <= 8 else dl - 2
        if n <= sf_max:
            self._tx((bytes([n]) if n <= 7 else bytes([0, n])) + data)
            return
        if n <= 4095:
            header = bytes([0x10 | (n >> 8), n & 0xFF])
        else:
            header = bytes([0x10, 0]) + n.to_bytes(4, "big")
        first = dl - len(header)
        self._tx(header + data[:first])
        pos, sn, sent_in_block = first, 1, 0
        bs, gap = self._wait_fc()
        while pos < n:
            chunk = data[pos:pos + dl - 1]
            self._tx(bytes([0x20 | sn]) + chunk)
            pos += len(chunk)
            sn = (sn + 1) & 0xF
            if pos >= n:
                break
            sent_in_block += 1
            if bs and sent_in_block >= bs:
                bs, gap = self._wait_fc()
                sent_in_block = 0
            if gap > 0:
                time.sleep(gap)

    # -- Empfangen
    def recv(self, timeout: float = 1.0) -> bytes:
        end = time.monotonic() + timeout
        while True:
            data = self._get(end - time.monotonic())
            kind = data[0] >> 4
            if kind == 0:                            # Single Frame
                n = data[0] & 0xF
                if n:
                    return data[1:1 + n]
                return data[2:2 + data[1]]
            if kind == 1:                            # First Frame
                n = ((data[0] & 0xF) << 8) | data[1]
                off = 2
                if n == 0:
                    n, off = int.from_bytes(data[2:6], "big"), 6
                buf = bytearray(data[off:])
                self._tx(bytes([0x30, self.block_size, self.st_min]))
                return self._recv_cf(buf, n)
            # CF/FC ausserhalb einer Uebertragung ignorieren

    def _recv_cf(self, buf: bytearray, total: int) -> bytes:
        sn, in_block = 1, 0
        while len(buf) < total:
            data = self._get(self.n_cr)
            if data[0] >> 4 != 2:
                continue
            if data[0] & 0xF != sn:
                raise IsoTpError(tr("err.isotp.sequence", expected=sn, got=data[0] & 0xF))
            buf += data[1:]
            sn = (sn + 1) & 0xF
            in_block += 1
            if self.block_size and in_block >= self.block_size and len(buf) < total:
                self._tx(bytes([0x30, self.block_size, self.st_min]))
                in_block = 0
        return bytes(buf[:total])

    # -- UDS-Request
    def request(self, data: bytes, timeout: float = 1.0, pending_timeout: float = 5.0,
                expect_response: bool = True) -> UdsResponse | None:
        data = bytes(data)
        with self._lock:
            self.flush()
            self.send(data)
            if not expect_response or (data[0] == 0x3E and len(data) > 1 and data[1] & 0x80):
                return None
            end = time.monotonic() + timeout
            while True:
                raw = self.recv(end - time.monotonic())
                if len(raw) >= 3 and raw[0] == 0x7F and raw[1] == data[0] and raw[2] == 0x78:
                    end = time.monotonic() + pending_timeout
                    continue
                return UdsResponse(data, raw)


# --------------------------------------------------------------------------- Diagnose-Definition
class DiagDefinition:
    """Services aus der konvertierten CDD (cache/<name>.cdd.json|yaml)."""

    def __init__(self, doc: dict) -> None:
        self.ecus: dict[str, dict] = doc.get("ecus", {})
        self._warn_items: list = doc.get("warnings", [])

    @property
    def warnings(self) -> list[str]:
        """Warnungen der Konvertierung in der aktuellen Sprache."""
        return [tr(w["key"], **w.get("args", {})) if isinstance(w, dict) else str(w) for w in self._warn_items]

    @classmethod
    def load(cls, path: Path) -> "DiagDefinition":
        return cls(load_document(path))

    def ecu_names(self) -> list[str]:
        return list(self.ecus)

    def services(self, ecu: str) -> dict[str, dict]:
        return self.ecus[ecu].get("services", {})

    @staticmethod
    def addr(ecu: dict) -> tuple[int, int]:
        return int(str(ecu["request_id"]), 0), int(str(ecu["response_id"]), 0)

    @staticmethod
    def build_request(service: dict, params: dict[str, Any] | None = None) -> bytes:
        text = service["request"]
        values = dict(params or {})
        for p in service.get("params", []):
            raw = values.get(p["name"], p.get("default", 0))
            if isinstance(raw, str):
                raw = int(raw, 16) if p.get("base", "hex") == "hex" else int(raw, 0)
            width = int(p.get("bytes", 1))
            text = text.replace("{" + p["name"] + "}", f"{int(raw):0{width * 2}X}")
        if "{" in text:
            raise ValueError(tr("err.diag.unresolved", text=text))
        return hex_to_bytes(text)

    @staticmethod
    def decode_response(service: dict, raw: bytes, request: bytes) -> dict[str, Any]:
        """Wertet Antwortparameter aus; Offsets zaehlen ab dem Ende des Service-/DID-Praefix."""
        spec = service.get("response") or {}
        prefix_len = int(spec.get("prefix_len", len(request) if request[0] == 0x22 else 2))
        body = raw[prefix_len:]
        out: dict[str, Any] = {}
        for p in spec.get("params", []):
            off, length = int(p.get("offset", 0)), int(p.get("length", 1))
            chunk = body[off:off + length]
            kind = p.get("type", "hex")
            if kind == "ascii":
                val: Any = chunk.decode("ascii", errors="replace").rstrip("\x00 ")
            elif kind in ("uint", "int"):
                v = int.from_bytes(chunk, "big", signed=(kind == "int"))
                val = round(v * float(p.get("factor", 1)) + float(p.get("offset_val", 0)), 6)
                if val == int(val):
                    val = int(val)
            else:
                val = bytes_to_hex(chunk)
            out[p["name"] + (f" [{p['unit']}]" if p.get("unit") else "")] = val
        return out


    @staticmethod
    def decode_response_detail(service: dict, raw: bytes, request: bytes) -> list[dict[str, Any]]:
        """Wie decode_response, aber je Parameter Name, Einheit, Roh-Hex, Wert (Zahl oder None) und Text."""
        spec = service.get("response") or {}
        prefix_len = int(spec.get("prefix_len", len(request) if request[0] == 0x22 else 2))
        body = raw[prefix_len:]
        out: list[dict[str, Any]] = []
        for p in spec.get("params", []):
            off, length = int(p.get("offset", 0)), int(p.get("length", 1))
            chunk = body[off:off + length]
            kind = p.get("type", "hex")
            value: float | None = None
            text = ""
            if kind == "ascii":
                text = chunk.decode("ascii", errors="replace").rstrip("\x00 ")
            elif kind in ("uint", "int"):
                value = round(int.from_bytes(chunk, "big", signed=(kind == "int")) * float(p.get("factor", 1))
                              + float(p.get("offset_val", 0)), 6)
            else:
                text = bytes_to_hex(chunk)
            out.append({"name": p["name"], "unit": p.get("unit", "") or "", "raw_hex": "0x" + chunk.hex().upper(),
                        "value": value, "text": text})
        return out


# --------------------------------------------------------------------------- Simulation
class SimulatedEcu:
    """Software-ECU am Simulationsbus: UDS-Antworten (ISO-TP) und optional zyklischer Testverkehr."""

    VIN = b"WDD2050421F123456"

    def __init__(self, network: str, request_id: int = 0x7E0, response_id: int = 0x7E8,
                 fd: bool = False, db: DbcDatabase | None = None,
                 traffic_senders: tuple[str, ...] = ("ECU",)) -> None:
        self.mgr = CanManager(db or DbcDatabase())
        self.network, self.fd = network, fd
        self.channel = IsoTpChannel(self.mgr, response_id, request_id, fd=fd)
        self.session = 1
        self.unlocked = False
        self.flash_memory: dict[int, bytearray] = {}      # Adresse -> programmierte Daten (fuer Tests/Demo)
        self._erased: set[int] = set()
        self._dl: dict | None = None
        self.db, self.traffic_senders = db, traffic_senders
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._t0 = time.time()

    def start(self) -> None:
        self.mgr.connect({"type": "simulation", "sim_network": self.network, "sim_echo": False})
        self.channel.attach()
        self._stop.clear()
        self._threads = [threading.Thread(target=self._serve, name="sim-ecu", daemon=True)]
        if self.db:
            for name, msg in self.db.messages.items():
                if msg.get("sender") in self.traffic_senders:
                    self.mgr.add_tx_message(name, enabled=True)
            if self.mgr.tx_entries:
                self._threads.append(threading.Thread(target=self._traffic, name="sim-traffic", daemon=True))
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=1.0)
        self.channel.detach()
        self.mgr.disconnect()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                req = self.channel.recv(0.1)
            except IsoTpTimeout:
                continue
            except IsoTpError:
                continue
            try:
                self._answer(req)
            except Exception:
                pass

    def _answer(self, req: bytes) -> None:
        reply = self.handle(req)
        if reply is not None:
            self.channel.send(reply)

    def handle(self, req: bytes) -> bytes | None:
        sid = req[0]
        nrc = lambda code: bytes([0x7F, sid, code])
        flash = self._handle_flash(req, nrc)
        if flash is not False:
            return flash
        if sid == 0x10 and len(req) >= 2:
            self.session = req[1] & 0x7F
            self.unlocked = self.unlocked and self.session == 2
            return bytes([0x50, self.session, 0x00, 0x32, 0x01, 0xF4]) if not req[1] & 0x80 else None
        if sid == 0x3E:
            return None if len(req) > 1 and req[1] & 0x80 else bytes([0x7E, 0x00])
        if sid == 0x11 and len(req) >= 2:
            return bytes([0x51, req[1]])
        if sid == 0x14:
            return bytes([0x54])
        if sid == 0x19 and len(req) >= 2:
            if req[1] == 0x02:
                return bytes([0x59, 0x02, 0xFF, 0x01, 0x23, 0x45, 0x2F, 0x04, 0x56, 0x78, 0x09])
            return nrc(0x12)
        if sid == 0x27 and len(req) >= 2:
            if req[1] == 0x01:
                return bytes([0x67, 0x01, 0x12, 0x34, 0x56, 0x78])
            if req[1] == 0x02:
                if req[2:] == bytes([0xED, 0xCB, 0xA9, 0x87]):
                    self.unlocked = True
                    return bytes([0x67, 0x02])
                return nrc(0x35)
            return nrc(0x12)
        if sid == 0x22 and len(req) >= 3:
            did = (req[1] << 8) | req[2]
            head = bytes([0x62, req[1], req[2]])
            if did == 0xF190:
                return head + self.VIN
            if did == 0xF186:
                return head + bytes([self.session])
            if did == 0xF18C:
                return head + b"SN-0042-4711"
            if did == 0xF40D:                        # Fahrzeuggeschwindigkeit (Demo)
                return head + bytes([int(60 + 20 * math.sin(time.time() - self._t0))])
            if did == 0xF1FF:                        # absichtlich langsam -> 0x78 pending
                self.channel.send(bytes([0x7F, 0x22, 0x78]))
                time.sleep(0.4)
                return head + b"\x01"
            return nrc(0x31)
        return nrc(0x11)

    def _handle_flash(self, req: bytes, nrc):
        """Minimaler Flash-Ablauf (0x28, 0x31 FF00/FF01, 0x34, 0x36, 0x37) fuer Demo und Tests; sonst False."""
        sid = req[0]
        if sid == 0x28 and len(req) >= 3:
            return bytes([0x68, req[1]])
        if sid == 0x31 and len(req) >= 4 and req[2:4] in (b"\xff\x00", b"\xff\x01"):
            if req[2:4] == b"\xff\x01":
                return bytes([0x71, req[1], 0xFF, 0x01, 0x00])
            if self.session != 2:
                return nrc(0x7E)
            if not self.unlocked:
                return nrc(0x33)
            n_len, n_addr = req[4] >> 4, req[4] & 0x0F
            addr = int.from_bytes(req[5:5 + n_addr], "big")
            self._erased.add(addr)
            self.flash_memory[addr] = bytearray()
            time.sleep(0.05)
            return bytes([0x71, req[1], 0xFF, 0x00, 0x00])
        if sid == 0x34 and len(req) >= 3:
            if self.session != 2 or not self.unlocked:
                return nrc(0x33)
            n_len, n_addr = req[2] >> 4, req[2] & 0x0F
            addr = int.from_bytes(req[3:3 + n_addr], "big")
            length = int.from_bytes(req[3 + n_addr:3 + n_addr + n_len], "big")
            if addr not in self._erased:
                return nrc(0x70)
            self._dl = {"addr": addr, "length": length, "seq": 1}
            return bytes([0x74, 0x20, 0x01, 0x00])             # maxNumberOfBlockLength = 256
        if sid == 0x36 and len(req) >= 2:
            if self._dl is None:
                return nrc(0x24)
            if req[1] != self._dl["seq"]:
                return nrc(0x73)
            self.flash_memory[self._dl["addr"]].extend(req[2:])
            self._dl["seq"] = (self._dl["seq"] + 1) & 0xFF
            return bytes([0x76, req[1]])
        if sid == 0x37:
            if self._dl is None:
                return nrc(0x24)
            ok = len(self.flash_memory[self._dl["addr"]]) == self._dl["length"]
            self._dl = None
            return bytes([0x77]) if ok else nrc(0x72)
        return False

    def _traffic(self) -> None:
        while not self._stop.wait(0.05):
            t = time.time() - self._t0
            for name in list(self.mgr.tx_entries):
                values = {}
                for i, (sig_name, sig) in enumerate(self.db.messages[name]["signals"].items()):
                    if sig.get("mux_value") is not None:
                        continue
                    lo, hi = sig.get("min"), sig.get("max")
                    if lo is None or hi is None or hi <= lo:
                        f, o, n = sig["factor"] or 1, sig["offset"] or 0, sig["length"]
                        lo, hi = ((-(1 << (n - 1)), (1 << (n - 1)) - 1) if sig["signed"] else (0, (1 << n) - 1))
                        lo, hi = lo * f + o, hi * f + o
                    if sig["length"] == 1:
                        values[sig_name] = int(math.sin(t / 2 + i) > 0)
                        continue
                    phase = 0.9 * i
                    values[sig_name] = lo + (hi - lo) * (0.5 + 0.45 * math.sin(2 * math.pi * t / (8 + 2 * i) + phase))
                self.mgr.tx_entries[name].values.update(values)
