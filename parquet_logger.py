"""parquet_logger.py - Parquet-Logging ausgewaehlter Signale (Langformat) mit Filter.

Spalten (Signaldatei, aufgeloeste Signale je Frame): timestamp (Epoch-s), direction, message, signal, value (physikalisch),
source (CAN | XCP | UDS), can_id, raw (Rohwert, vorzeichenbehaftet), raw_hex (Bitmuster), text (Wertetabelle/Text), unit,
message_hex (Frame-Nutzdaten als Hex). Quellen: DBC (CAN-Frames), A2L (XCP-Werte) und CDD (UDS-Antworten) - soweit eingebunden.
Optional (log_raw=True) zusaetzlich <datei>_raw.parquet mit ALLEN Frames des Busses (auch ohne DBC, inkl. Fehlerframes):
timestamp, direction, channel, can_id, extended, fd, brs, error, dlc, data (binary), data_hex, message (DBC-Name oder "").
Optional raw_ids: ID-Filter fuer die Rohdaten, z. B. "100, 7E0-7EF" (hex; leer = alle).
Filter: Auswahl {"RX": {"Msg": ["Sig", ...] | None}, "TX": {...}}, nur bei Aenderung loggen,
Mindestabstand je Signal. Benoetigt pyarrow.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Iterable

from can_manager import Frame
from dbc_manager import DbcDatabase
from i18n import tr


def parse_id_filter(text: str | None) -> list[tuple[int, int]]:
    """'100, 7E0-7EF, 0x18FF0000' -> [(lo, hi), ...] (hexadezimal); leer = kein Filter."""
    ranges = []
    for part in (text or "").replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            lo, _, hi = part.partition("-")
            lo_i = int(lo, 16)
            hi_i = int(hi, 16) if hi else lo_i
        except ValueError:
            raise ValueError(tr("err.log.bad_id_filter", value=part)) from None
        if lo_i > hi_i or not 0 <= lo_i <= hi_i <= 0x1FFFFFFF:
            raise ValueError(tr("err.log.bad_id_filter", value=part))
        ranges.append((lo_i, hi_i))
    return ranges


def parquet_available() -> bool:
    try:
        import pyarrow  # noqa: F401
        import pyarrow.parquet  # noqa: F401
        return True
    except ImportError:
        return False


class ParquetLogger:
    def __init__(self, db: DbcDatabase, path: Path, selection: dict[str, dict[str, Iterable[str] | None]],
                 on_change_only: bool = False, min_interval_ms: float = 0.0, log_raw: bool = False,
                 flush_rows: int = 20000, compression: str = "zstd", raw_ids: str = "") -> None:
        self.db, self.path = db, Path(path)
        self.sel = {d.upper(): {m: (None if s is None else set(s)) for m, s in msgs.items()}
                    for d, msgs in selection.items()}
        self.on_change_only, self.min_interval = on_change_only, min_interval_ms / 1000.0
        self.log_raw, self.flush_rows, self.compression = log_raw, flush_rows, compression
        self.raw_filter = parse_id_filter(raw_ids)
        self.rows_written = self.raw_rows_written = 0
        self.running = False
        self._buf: dict[str, list] = self._empty()
        self._raw: dict[str, list] = self._empty_raw()
        self._last: dict[tuple, tuple[float, float]] = {}
        self._lock = threading.Lock()
        self._writer = self._raw_writer = None
        self._schema = self._raw_schema = None

    @staticmethod
    def _empty() -> dict[str, list]:
        return {"timestamp": [], "direction": [], "message": [], "signal": [], "value": [], "source": [], "can_id": [],
                "raw": [], "raw_hex": [], "text": [], "unit": [], "message_hex": []}

    @staticmethod
    def _empty_raw() -> dict[str, list]:
        return {"timestamp": [], "direction": [], "channel": [], "can_id": [], "extended": [], "fd": [], "brs": [],
                "error": [], "dlc": [], "data": [], "data_hex": [], "message": [], "signals": []}

    @property
    def raw_path(self) -> Path:
        return self.path.with_name(self.path.stem + "_raw.parquet")

    def start(self) -> None:
        if not parquet_available():
            raise RuntimeError(tr("err.log.pyarrow"))
        import pyarrow as pa
        import pyarrow.parquet as pq
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._schema = pa.schema([("timestamp", pa.float64()), ("direction", pa.string()),
                                  ("message", pa.string()), ("signal", pa.string()), ("value", pa.float64()),
                                  ("source", pa.string()), ("can_id", pa.uint32()), ("raw", pa.int64()),
                                  ("raw_hex", pa.string()), ("text", pa.string()), ("unit", pa.string()),
                                  ("message_hex", pa.string())])
        self._writer = pq.ParquetWriter(self.path, self._schema, compression=self.compression)
        if self.log_raw:
            self._raw_schema = pa.schema([("timestamp", pa.float64()), ("direction", pa.string()),
                                          ("channel", pa.string()), ("can_id", pa.uint32()),
                                          ("extended", pa.bool_()), ("fd", pa.bool_()), ("brs", pa.bool_()),
                                          ("error", pa.bool_()), ("dlc", pa.uint8()), ("data", pa.binary()),
                                          ("data_hex", pa.string()), ("message", pa.string()),
                                          ("signals", pa.string())])
            self._raw_writer = pq.ParquetWriter(self.raw_path, self._raw_schema, compression=self.compression)
        self.running = True

    def _raw_wanted(self, can_id: int) -> bool:
        return not self.raw_filter or any(lo <= can_id <= hi for lo, hi in self.raw_filter)

    @staticmethod
    def _signal_text(detail: dict) -> str:
        """Alle Signale eines Frames als lesbarer Text: Name=Roh-Hex/Wert Einheit [Text]."""
        parts = []
        for sig, d in detail.items():
            v = d["value"]
            val = f"{v:.10g}" if isinstance(v, float) else str(v)
            parts.append(f"{sig}={d['raw_hex']}/{val}" + (f" {d['unit']}" if d["unit"] else "")
                         + (f" [{d['text']}]" if d["text"] else ""))
        return "; ".join(parts)

    def on_frame(self, frame: Frame) -> None:
        if not self.running:
            return
        direction = frame.direction.upper()
        name = None if frame.is_error else self.db.find_by_id(frame.arbitration_id, frame.is_extended)
        detail = None
        wanted = None
        selected = False
        log_raw = self.log_raw and self._raw_wanted(frame.arbitration_id)
        if name is not None:
            selected = name in self.sel.get(direction, {})
            if selected or log_raw:
                try:
                    detail = self.db.decode_detail(name, frame.data)
                except Exception:
                    detail = None
            if selected:
                wanted = self.sel[direction][name]       # None = alle Signale der Botschaft
        if detail is None and not log_raw:
            return
        flush = False
        data = bytes(frame.data)
        with self._lock:
            if not self.running:
                return
            if log_raw:                                  # alle Frames, unabhaengig von DBC und Signalauswahl
                r = self._raw
                r["timestamp"].append(frame.timestamp)
                r["direction"].append(direction)
                r["channel"].append(str(frame.channel or ""))
                r["can_id"].append(frame.arbitration_id)
                r["extended"].append(bool(frame.is_extended))
                r["fd"].append(bool(frame.is_fd))
                r["brs"].append(bool(frame.bitrate_switch))
                r["error"].append(bool(frame.is_error))
                r["dlc"].append(len(data))
                r["data"].append(data)
                r["data_hex"].append(data.hex(" ").upper())
                r["message"].append(name or "")
                r["signals"].append(self._signal_text(detail) if detail else "")
                flush = len(r["timestamp"]) >= self.flush_rows
            for sig, d in (detail or {}).items():
                if not selected:                         # nur fuer die Rohdatei dekodiert, keine Signalzeilen
                    break
                if wanted is not None and sig not in wanted:
                    continue
                if self._skip((direction, name, sig), frame.timestamp, d["value"]):
                    continue
                self._add_signal(frame.timestamp, direction, "CAN", name, frame.arbitration_id, sig, d,
                                 data.hex(" ").upper())
            flush = flush or len(self._buf["timestamp"]) >= self.flush_rows
        if flush:
            self.flush()

    def _skip(self, key: tuple, ts: float, value: Any) -> bool:
        """on_change_only / Mindestabstand je Signal; merkt sich den zuletzt geschriebenen Wert."""
        last = self._last.get(key)
        if last is not None:
            if self.on_change_only and last[1] == value:
                return True
            if self.min_interval and ts - last[0] < self.min_interval:
                return True
        self._last[key] = (ts, value)
        return False

    def _add_signal(self, ts: float, direction: str, source: str, message: str, can_id: int | None, signal: str,
                    d: dict, message_hex: str) -> None:
        b = self._buf
        v = d["value"]
        b["timestamp"].append(ts)
        b["direction"].append(direction)
        b["message"].append(message)
        b["signal"].append(signal)
        b["value"].append(None if v is None else float(v))
        b["source"].append(source)
        b["can_id"].append(can_id)
        b["raw"].append(d.get("raw"))
        b["raw_hex"].append(d.get("raw_hex", ""))
        b["text"].append(d.get("text", ""))
        b["unit"].append(d.get("unit", ""))
        b["message_hex"].append(message_hex)

    def log_external(self, source: str, message: str, signal: str, detail: dict, direction: str = "RX",
                     timestamp: float | None = None, message_hex: str = "") -> None:
        """Wert aus einer anderen Quelle protokollieren (XCP-Lesen aus A2L, UDS-Antwort aus CDD).

        detail: {raw, raw_hex, value, text, unit} wie bei DbcDatabase.decode_detail.
        """
        if not self.running:
            return
        ts = time.time() if timestamp is None else timestamp
        flush = False
        with self._lock:
            if not self.running:
                return
            if not self._skip((direction, source, message, signal), ts, detail.get("value")):
                self._add_signal(ts, direction, source, message, None, signal, detail, message_hex)
            flush = len(self._buf["timestamp"]) >= self.flush_rows
        if flush:
            self.flush()

    def flush(self) -> None:
        import pyarrow as pa
        with self._lock:
            buf, self._buf = self._buf, self._empty()
            raw, self._raw = self._raw, self._empty_raw()
            if self._writer is not None and buf["timestamp"]:
                self._writer.write_table(pa.Table.from_pydict(buf, schema=self._schema))
                self.rows_written += len(buf["timestamp"])
            if self._raw_writer is not None and raw["timestamp"]:
                self._raw_writer.write_table(pa.Table.from_pydict(raw, schema=self._raw_schema))
                self.raw_rows_written += len(raw["timestamp"])

    def stop(self) -> None:
        if not self.running:
            return
        self.flush()
        with self._lock:
            self.running = False
            for w in (self._writer, self._raw_writer):
                if w is not None:
                    w.close()
            self._writer = self._raw_writer = None


def default_log_name(project: str) -> str:
    return f"{project}_{time.strftime('%Y%m%d_%H%M%S')}.parquet"
