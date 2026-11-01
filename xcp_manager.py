"""xcp_manager.py - XCP-on-CAN-Master (Messen/Verstellen per Polling) und simulierter XCP-Slave.

Unterstuetzt: CONNECT, DISCONNECT, GET_STATUS, SYNCH, SET_MTA, UPLOAD, SHORT_UPLOAD, DOWNLOAD, SHORT_DOWNLOAD.
Nicht enthalten: DAQ-Listen (zyklisches Messen durch den Slave), Seed&Key, Kalibrierseiten, Flash-Programmierung.
Messwerte werden deshalb durch zyklisches Lesen (SHORT_UPLOAD) erfasst.
"""
from __future__ import annotations

import math
import queue
import struct
import threading
import time
from typing import Any

from a2l_manager import DATATYPES, A2lDatabase
from can_manager import CanManager, Frame
from dbc_manager import DbcDatabase
from i18n import tr

CMD = {"CONNECT": 0xFF, "DISCONNECT": 0xFE, "GET_STATUS": 0xFD, "SYNCH": 0xFC, "SET_MTA": 0xF6, "UPLOAD": 0xF5,
       "SHORT_UPLOAD": 0xF4, "DOWNLOAD": 0xF0, "SHORT_DOWNLOAD": 0xED}
ERRORS = {0x00: "ERR_CMD_SYNCH", 0x10: "ERR_CMD_BUSY", 0x11: "ERR_DAQ_ACTIVE", 0x12: "ERR_PGM_ACTIVE",
          0x20: "ERR_CMD_UNKNOWN", 0x21: "ERR_CMD_SYNTAX", 0x22: "ERR_OUT_OF_RANGE", 0x23: "ERR_WRITE_PROTECTED",
          0x24: "ERR_ACCESS_DENIED", 0x25: "ERR_ACCESS_LOCKED", 0x26: "ERR_PAGE_NOT_VALID", 0x27: "ERR_MODE_NOT_VALID",
          0x28: "ERR_SEGMENT_NOT_VALID", 0x29: "ERR_SEQUENCE", 0x2A: "ERR_DAQ_CONFIG", 0x30: "ERR_MEMORY_OVERFLOW",
          0x31: "ERR_GENERIC", 0x32: "ERR_VERIFY", 0x33: "ERR_RESOURCE_TEMPORARY_NOT_ACCESSIBLE"}


class XcpError(Exception):
    def __init__(self, text: str, code: int | None = None) -> None:
        super().__init__(text)
        self.code = code


class XcpMaster:
    """XCP-Master auf CAN. tx_id = CAN_ID_MASTER (Kommandos), rx_id = CAN_ID_SLAVE (Antworten)."""

    def __init__(self, mgr: CanManager, tx_id: int, rx_id: int, extended: bool = False, timeout: float = 0.5,
                 on_log=None) -> None:
        self.mgr, self.tx_id, self.rx_id, self.extended, self.timeout = mgr, tx_id, rx_id, extended, timeout
        self.on_log = on_log
        self.max_cto = 8
        self.big_endian = False
        self.connected = False
        self._q: queue.Queue[bytes] = queue.Queue()
        self._lock = threading.Lock()
        self._op = threading.RLock()          # Lese-/Schreibzugriffe nicht verschachteln (SHORT_UPLOAD setzt den MTA)
        self._attached = False

    # -- Anbindung
    def attach(self) -> "XcpMaster":
        if not self._attached:
            self.mgr.add_listener(self._on_frame)
            self._attached = True
        return self

    def detach(self) -> None:
        if self._attached:
            self.mgr.remove_listener(self._on_frame)
            self._attached = False

    def _on_frame(self, frame: Frame) -> None:
        if frame.direction == "rx" and frame.arbitration_id == self.rx_id and frame.is_extended == self.extended:
            self._q.put(bytes(frame.data))

    def _log(self, direction: str, data: bytes) -> None:
        if self.on_log:
            self.on_log(direction, bytes(data))

    # -- Kommandos
    def command(self, payload: bytes) -> bytes:
        """Kommando senden und positive Antwort (ohne Paket-ID 0xFF) zurueckgeben; XcpError bei Fehler/Timeout."""
        with self._lock:
            while not self._q.empty():
                self._q.get_nowait()
            self._log("tx", payload)
            self.mgr.send_data(self.tx_id, bytes(payload).ljust(8, b"\x00")[:max(8, len(payload))], self.extended)
            try:
                resp = self._q.get(timeout=self.timeout)
            except queue.Empty:
                raise XcpError(tr("err.xcp.timeout", ms=int(self.timeout * 1000))) from None
            self._log("rx", resp)
        if resp[:1] == b"\xff":
            return resp[1:]
        if resp[:1] == b"\xfe":
            code = resp[1] if len(resp) > 1 else 0x31
            raise XcpError(tr("err.xcp.negative", name=ERRORS.get(code, "?"), code=f"{code:02X}"), code)
        raise XcpError(tr("err.xcp.bad_response", data=resp.hex(" ").upper()))

    def _u32(self, value: int) -> bytes:
        return struct.pack(">I" if self.big_endian else "<I", value)

    def connect(self) -> dict[str, Any]:
        r = self.command(bytes([CMD["CONNECT"], 0]))
        if len(r) < 7:
            raise XcpError(tr("err.xcp.bad_response", data=r.hex(" ").upper()))
        self.big_endian = bool(r[1] & 0x01)
        self.max_cto = r[2]
        max_dto = struct.unpack(">H" if self.big_endian else "<H", r[3:5])[0]
        self.connected = True
        return {"resources": r[0], "max_cto": self.max_cto, "max_dto": max_dto, "protocol": r[5], "transport": r[6],
                "byte_order": "big" if self.big_endian else "little"}

    def disconnect(self) -> None:
        try:
            if self.connected:
                self.command(bytes([CMD["DISCONNECT"]]))
        finally:
            self.connected = False

    def get_status(self) -> dict[str, int]:
        r = self.command(bytes([CMD["GET_STATUS"]]))
        return {"session": r[0], "protection": r[1] if len(r) > 1 else 0}

    def read(self, address: int, size: int, extension: int = 0) -> bytes:
        """size Bytes ab address lesen (mehrere SHORT_UPLOADs bei Bedarf)."""
        out = b""
        chunk_max = max(1, self.max_cto - 1)
        with self._op:
            while len(out) < size:
                n = min(chunk_max, size - len(out))
                r = self.command(bytes([CMD["SHORT_UPLOAD"], n, 0, extension]) + self._u32(address + len(out)))
                if len(r) < n:
                    raise XcpError(tr("err.xcp.bad_response", data=r.hex(" ").upper()))
                out += r[:n]
        return out

    def write(self, address: int, data: bytes, extension: int = 0) -> None:
        """SET_MTA + DOWNLOAD (Bloecke bis max_cto-2 Byte)."""
        with self._op:
            self.command(bytes([CMD["SET_MTA"], 0, 0, extension]) + self._u32(address))
            chunk_max = max(1, self.max_cto - 2)
            for i in range(0, len(data), chunk_max):
                part = bytes(data[i:i + chunk_max])
                self.command(bytes([CMD["DOWNLOAD"], len(part)]) + part)

    # -- A2L-Zugriff
    def read_item(self, db: A2lDatabase, name: str) -> tuple[float, float | str]:
        item = db.item(name)
        self._check(item, name)
        raw = self.read(item["address"], db.size(item), item.get("extension", 0))
        return db.decode(name, raw, self.big_endian)

    def write_item(self, db: A2lDatabase, name: str, phys: float | str) -> None:
        item = db.item(name)
        self._check(item, name)
        if "bit_mask" in item:
            raise XcpError(tr("err.xcp.bitmask_write", name=name))
        self.write(item["address"], db.encode(name, phys, self.big_endian), item.get("extension", 0))

    @staticmethod
    def _check(item: dict, name: str) -> None:
        if not item.get("supported") or item.get("address") is None:
            raise XcpError(tr("err.xcp.unsupported_item", name=name))


# --------------------------------------------------------------------------- simulierter Slave
class SimulatedXcpSlave:
    """XCP-Slave am Simulationsbus: Speicher aus der A2L, Messgroessen laufen als Sinus, Parameter sind beschreibbar."""

    def __init__(self, network: str, db: A2lDatabase, master_id: int, slave_id: int, extended: bool = False,
                 big_endian: bool = False, max_cto: int = 8) -> None:
        self.mgr = CanManager(DbcDatabase())
        self.network, self.db = network, db
        self.master_id, self.slave_id, self.extended = master_id, slave_id, extended
        self.big_endian, self.max_cto = big_endian, max_cto
        self.mem: dict[int, int] = {}
        self.mta = 0
        self.connected = False
        self._q: queue.Queue[bytes] = queue.Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._t0 = time.time()
        for name, item in db.characteristics.items():
            if item.get("supported"):
                mid = (item["lower"] + item["upper"]) / 2 if item["upper"] > item["lower"] else item["lower"]
                self._store(item, db.to_raw(item, mid))
        self._update_measurements()

    def _store(self, item: dict, raw: float) -> None:
        try:
            data = self.db.pack(item, raw, self.big_endian)
        except (ValueError, struct.error):
            return
        for i, b in enumerate(data):
            self.mem[item["address"] + i] = b

    def _update_measurements(self) -> None:
        t = time.time() - self._t0
        for k, (name, item) in enumerate(self.db.measurements.items()):
            if not item.get("supported") or item.get("bit_mask"):
                continue
            lo, hi = item["lower"], item["upper"]
            if hi <= lo:
                hi = lo + 100
            phys = lo + (hi - lo) * (0.5 + 0.45 * math.sin(t * (0.5 + 0.2 * k) + k))
            self._store(item, round(self.db.to_raw(item, phys)) if item["datatype"] not in ("FLOAT32_IEEE", "FLOAT64_IEEE")
                        else self.db.to_raw(item, phys))

    def start(self) -> None:
        self.mgr.connect({"type": "simulation", "sim_network": self.network, "sim_echo": False})
        self.mgr.add_listener(self._on_frame)
        self._stop.clear()
        self._threads = [threading.Thread(target=self._serve, name="sim-xcp", daemon=True),
                         threading.Thread(target=self._tick, name="sim-xcp-tick", daemon=True)]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=1.0)
        self.mgr.remove_listener(self._on_frame)
        self.mgr.disconnect()

    def _on_frame(self, frame: Frame) -> None:
        if frame.direction == "rx" and frame.arbitration_id == self.master_id and frame.is_extended == self.extended:
            self._q.put(bytes(frame.data))

    def _tick(self) -> None:
        while not self._stop.wait(0.05):
            self._update_measurements()

    def _u32(self, data: bytes) -> int:
        return struct.unpack(">I" if self.big_endian else "<I", bytes(data[:4]))[0]

    def _reply(self, payload: bytes) -> None:
        self.mgr.send_data(self.slave_id, payload.ljust(8, b"\x00"), self.extended)

    def _neg(self, code: int) -> bytes:
        return bytes([0xFE, code])

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                req = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._reply(self.handle(req))
            except Exception:                                # noqa: BLE001 - Slave darf nie abstuerzen
                self._reply(self._neg(0x31))

    def handle(self, req: bytes) -> bytes:
        cmd = req[0]
        if cmd == CMD["CONNECT"]:
            self.connected = True
            dto = struct.pack(">H" if self.big_endian else "<H", self.max_cto)
            return bytes([0xFF, 0x00, 0x01 if self.big_endian else 0x00, self.max_cto]) + dto + bytes([0x01, 0x01])
        if not self.connected:
            return self._neg(0x31) if cmd in CMD.values() else self._neg(0x20)
        if cmd == CMD["DISCONNECT"]:
            self.connected = False
            return bytes([0xFF])
        if cmd == CMD["GET_STATUS"]:
            return bytes([0xFF, 0x00, 0x00, 0x00, 0x00, 0x00])
        if cmd == CMD["SYNCH"]:
            return self._neg(0x00)
        if cmd == CMD["SET_MTA"]:
            self.mta = self._u32(req[4:8])
            return bytes([0xFF])
        if cmd == CMD["UPLOAD"]:
            n = req[1]
            if n > self.max_cto - 1:
                return self._neg(0x22)
            data = bytes(self.mem.get(self.mta + i, 0) for i in range(n))
            self.mta += n
            return bytes([0xFF]) + data
        if cmd == CMD["SHORT_UPLOAD"]:
            n, addr = req[1], self._u32(req[4:8])
            if n > self.max_cto - 1:
                return self._neg(0x22)
            return bytes([0xFF]) + bytes(self.mem.get(addr + i, 0) for i in range(n))
        if cmd == CMD["DOWNLOAD"]:
            n = req[1]
            if n > self.max_cto - 2:
                return self._neg(0x22)
            for i in range(n):
                self.mem[self.mta + i] = req[2 + i]
            self.mta += n
            return bytes([0xFF])
        if cmd == CMD["SHORT_DOWNLOAD"]:
            n, addr = req[1], self._u32(req[4:8])
            for i in range(n):
                self.mem[addr + i] = req[8 + i] if 8 + i < len(req) else 0
            return bytes([0xFF])
        return self._neg(0x20)


__all__ = ["XcpMaster", "XcpError", "SimulatedXcpSlave", "DATATYPES"]
