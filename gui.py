"""gui.py - PySide6-Oberflaeche (Rubriken: Projekt, Daten, Trace, Testsequenz, Graph, Logging, Diagnose, Hilfe).

Alle sichtbaren Texte kommen aus den Sprachdateien (lang/*.yaml, siehe i18n.py). Die Sprache laesst sich im laufenden
Betrieb umschalten (Dropdown rechts in der Rubrikzeile), ebenso zwischen hellem und dunklem Erscheinungsbild.

Benoetigt: PySide6, pyqtgraph, numpy (siehe requirements.txt).
"""
from __future__ import annotations

import csv
import math
import shutil
import html as html_lib
import json
import re
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import (QAbstractTableModel, QModelIndex, QObject, QPoint, QPointF, QRectF, QSignalBlocker, Qt,
                            QTimer, QUrl, Signal)
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPalette, QPen, QPixmap, QRadialGradient, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSpinBox, QSplitter, QTableView,
    QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser, QTextEdit, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

import builder_logic as bl
from a2l_manager import A2lDatabase
from xcp_manager import SimulatedXcpSlave, XcpMaster
from can_manager import (VALID_DLC, CanManager, Frame, SignalStore, build_raw_payload, make_key, parse_can_id,
                         split_key)
from dbc_manager import DbcDatabase
from help_content import IMAGE_DIR, build_topics, stylesheet as help_stylesheet
from manual_builder import default_file_name, export_pdf
from i18n import available_languages, current_language, set_language, tr
from iso_tp_manager import (DiagDefinition, IsoTpChannel, SimulatedEcu, bytes_to_hex,
                            hex_to_bytes)
from trace_logic import KIND_COL, UpdateTable
from platform_info import current_os, default_channel, detect_hardware, os_label, supported_hardware
from parquet_logger import ParquetLogger, default_log_name, parquet_available
from project_manager import Project, ProjectManager
from sequence_runner import (STEP_HELP, STEP_TEMPLATES, SequenceRunner, load_sequence, new_sequence,
                             save_sequence, step_example_json, step_summary, step_title, step_tooltip_html,
                             validate_sequence)
from user_settings import load_settings, save_setting
from vflash_manager import FlashPackError, FlashRunner, load_flashpack

CHECKED, UNCHECKED = Qt.CheckState.Checked, Qt.CheckState.Unchecked
USER_CHECK = Qt.ItemFlag.ItemIsUserCheckable


# =========================================================================== Hilfsmittel
class Theme:
    """Globaler Zustand des Erscheinungsbilds (von Trace, Sequenz, Graph, Hilfe ausgewertet)."""
    dark = False
    native_style: str | None = None


def dark_palette() -> QPalette:
    p, c = QPalette(), QColor
    roles = {
        QPalette.ColorRole.Window: c(45, 45, 48), QPalette.ColorRole.WindowText: c(230, 230, 230),
        QPalette.ColorRole.Base: c(30, 30, 30), QPalette.ColorRole.AlternateBase: c(45, 45, 48),
        QPalette.ColorRole.ToolTipBase: c(60, 60, 64), QPalette.ColorRole.ToolTipText: c(235, 235, 235),
        QPalette.ColorRole.Text: c(230, 230, 230), QPalette.ColorRole.Button: c(58, 58, 62),
        QPalette.ColorRole.ButtonText: c(230, 230, 230), QPalette.ColorRole.BrightText: c(255, 80, 80),
        QPalette.ColorRole.Link: c(110, 170, 255), QPalette.ColorRole.Highlight: c(42, 110, 190),
        QPalette.ColorRole.HighlightedText: c(255, 255, 255), QPalette.ColorRole.PlaceholderText: c(140, 140, 140),
    }
    for role, color in roles.items():
        p.setColor(role, color)
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, c(120, 120, 120))
    return p


def apply_theme(app: QApplication, dark: bool) -> None:
    """Dunkles bzw. natives helles Erscheinungsbild fuer die gesamte Anwendung."""
    if Theme.native_style is None:
        Theme.native_style = app.style().objectName()
    Theme.dark = dark
    if dark:
        app.setStyle("Fusion")
        app.setPalette(dark_palette())
        app.setStyleSheet("QToolTip { color: #ebebeb; background-color: #3c3c40; border: 1px solid #6a6a70; }")
    else:
        app.setStyleSheet("")
        if Theme.native_style:
            app.setStyle(Theme.native_style)
        app.setPalette(app.style().standardPalette())


class Binder:
    """Sammelt Aufrufe, die Texte setzen; run() wendet sie nach einem Sprachwechsel erneut an."""

    def __init__(self) -> None:
        self._calls: list = []

    def call(self, fn):
        self._calls.append(fn)
        fn()
        return fn

    def text(self, widget, key: str, **kw):
        self.call(lambda: widget.setText(tr(key, **kw)))
        return widget

    def title(self, widget, key: str):
        self.call(lambda: widget.setTitle(tr(key)))
        return widget

    def tip(self, widget, key: str):
        self.call(lambda: widget.setToolTip(tr(key)))
        return widget

    def placeholder(self, widget, key: str):
        self.call(lambda: widget.setPlaceholderText(tr(key)))
        return widget

    def tree_headers(self, tree: QTreeWidget, keys: list[str]) -> None:
        self.call(lambda: tree.setHeaderLabels([tr(k) if k else "" for k in keys]))

    def table_headers(self, table: QTableWidget, keys: list[str]) -> None:
        self.call(lambda: table.setHorizontalHeaderLabels([tr(k) if k else "" for k in keys]))

    def run(self) -> None:
        for fn in self._calls:
            fn()


def fmt_num(v: float) -> str:
    return f"{v:.6g}"


def signal_range(sig: dict) -> tuple[float, float]:
    lo, hi = sig.get("min"), sig.get("max")
    if lo is None or hi is None or hi <= lo:
        n, f, o = sig["length"], sig["factor"] or 1, sig["offset"] or 0
        rlo, rhi = ((-(1 << (n - 1)), (1 << (n - 1)) - 1) if sig["signed"] else (0, (1 << n) - 1))
        a, b = rlo * f + o, rhi * f + o
        lo, hi = min(a, b), max(a, b)
    return float(lo), float(hi)


def make_value_editor(sig: dict, current: float, on_change) -> QWidget:
    """Spinbox oder (bei Wertetabelle) Combobox fuer einen physikalischen Signalwert."""
    f, o = sig["factor"] or 1, sig["offset"] or 0
    choices = sig.get("choices") or {}
    if choices and len(choices) <= 128:
        box = QComboBox()
        sel = 0
        for i, (raw, text) in enumerate(sorted(choices.items())):
            phys = raw * f + o
            box.addItem(f"{raw} - {text}", phys)
            if abs(phys - current) < 1e-9:
                sel = i
        box.setCurrentIndex(sel)
        box.currentIndexChanged.connect(lambda _i, b=box: on_change(b.currentData()))
        return box
    spin = QDoubleSpinBox()
    lo, hi = signal_range(sig)
    spin.setRange(lo, hi)
    spin.setDecimals(0 if float(f).is_integer() else min(6, max(1, len(repr(float(f)).split(".")[-1]))))
    spin.setValue(current)
    spin.setSuffix(f" {sig['unit']}" if sig.get("unit") else "")
    spin.setKeyboardTracking(False)
    spin.valueChanged.connect(on_change)
    return spin


# =========================================================================== Kontext
class AppContext(QObject):
    """Gemeinsamer Zustand aller Rubriken."""

    projectChanged = Signal()
    selectionChanged = Signal()
    connectionChanged = Signal(bool)
    message = Signal(str)

    def __init__(self, base: Path) -> None:
        super().__init__()
        self.pm = ProjectManager(base / "projects")
        self.db = DbcDatabase()
        self.mgr = CanManager(self.db)
        self.store = SignalStore(self.db)
        self.mgr.add_listener(self.store.on_frame)
        self.project: Project | None = None
        self.tx_sel: dict[str, list[str] | None] = {}
        self.rx_sel: dict[str, list[str] | None] = {}
        self.sim_ecu: SimulatedEcu | None = None
        self.logger: ParquetLogger | None = None
        self.sequence = new_sequence()
        self.sequence_file: Path | None = None
        self.diag_def: DiagDefinition | None = None
        self._channel: IsoTpChannel | None = None
        self._channel_key: tuple | None = None

    # -- Projekt
    def open_project(self, name: str) -> None:
        self.disconnect()
        self.stop_logger()
        self.save_project()
        project = self.pm.load(name)
        self.project = project
        try:
            self.db = project.load_dbc()
        except Exception as exc:
            self.db = DbcDatabase()
            self.message.emit(tr("msg.dbc_error", error=exc))
        self.mgr.db = self.db
        self.store.db = self.db
        self.mgr.clear_tx()
        self.store.clear()
        s = project.settings
        self.tx_sel = {}
        for e in s["tx"]:
            if e["message"] in self.db.messages:
                self.mgr.add_tx_message(e["message"], e.get("cycle_ms"), e.get("values"))
                self.tx_sel[e["message"]] = e.get("signals")
        self.rx_sel = {m: sigs for m, sigs in s["rx"].items() if m in self.db.messages}
        self.apply_selection(emit=False)
        self.load_diag_def()
        self.sequence, self.sequence_file = new_sequence(), None
        if s.get("sequence") and (project.seq_dir / s["sequence"]).exists():
            try:
                self.sequence_file = project.seq_dir / s["sequence"]
                self.sequence = load_sequence(self.sequence_file)
            except Exception as exc:
                self.message.emit(tr("msg.sequence_load_error", error=exc))
        self.projectChanged.emit()

    def save_project(self) -> None:
        p = self.project
        if p is None:
            return
        s = p.settings
        s["tx"] = [{"message": n, "cycle_ms": e.cycle_ms, "enabled": False, "values": dict(e.values),
                    "signals": self.tx_sel.get(n)} for n, e in self.mgr.tx_entries.items()]
        s["rx"] = dict(self.rx_sel)
        s["sequence"] = self.sequence_file.name if self.sequence_file else ""
        try:
            p.save()
        except OSError as exc:
            self.message.emit(tr("msg.project_save_error", error=exc))

    def apply_selection(self, emit: bool = True) -> None:
        self.store.configure(self.tx_sel, self.rx_sel)
        if emit:
            self.selectionChanged.emit()

    def selected_keys(self) -> list[str]:
        keys = []
        for direction, sel in (("TX", self.tx_sel), ("RX", self.rx_sel)):
            for msg, sigs in sel.items():
                if msg not in self.db.messages:
                    continue
                for sig in (self.db.signal_names(msg) if sigs is None else sigs):
                    keys.append(make_key(direction, msg, sig))
        return keys

    # -- Diagnose
    def load_diag_def(self) -> None:
        self.diag_def = None
        self._invalidate_channel()
        if self.project is None:
            return
        try:
            files = self.project.diag_files()
            wanted = self.project.settings["diag"].get("cdd")
            path = next((f for f in files if f.name == wanted), files[0] if files else None)
            self.diag_def = DiagDefinition.load(path) if path else None
        except Exception as exc:
            self.message.emit(tr("msg.cdd_error", error=exc))

    def diag_config(self) -> dict:
        d = self.project.settings["diag"] if self.project else {}
        ecu_name = d.get("ecu", "")
        ecus = self.diag_def.ecus if self.diag_def else {}
        if ecu_name not in ecus and ecus:
            ecu_name = next(iter(ecus))
        ecu = ecus.get(ecu_name)
        req, resp = DiagDefinition.addr(ecu) if ecu else (0x7E0, 0x7E8)
        if d.get("request_id"):
            req = int(str(d["request_id"]), 0)
        if d.get("response_id"):
            resp = int(str(d["response_id"]), 0)
        return {"ecu": ecu_name, "request_id": req, "response_id": resp,
                "extended": bool(d.get("extended", ecu.get("extended", False) if ecu else False)),
                "fd": bool(d.get("fd", ecu.get("fd", False) if ecu else False)),
                "padding": d.get("padding", 0xAA)}

    def _invalidate_channel(self) -> None:
        if self._channel is not None:
            self._channel.detach()
        self._channel = self._channel_key = None

    def get_diag(self) -> tuple[IsoTpChannel, DiagDefinition | None, str]:
        cfg = self.diag_config()
        key = tuple(cfg.values()) + (id(self.mgr.backend),)
        if self._channel is None or key != self._channel_key:
            self._invalidate_channel()
            self._channel = IsoTpChannel(self.mgr, cfg["request_id"], cfg["response_id"], cfg["extended"],
                                         cfg["fd"], True, padding=cfg["padding"]).attach()
            self._channel_key = key
        return self._channel, self.diag_def, cfg["ecu"]

    # -- Verbindung
    def connect(self) -> None:
        hw = dict(self.project.settings["hardware"])
        if isinstance(hw.get("channel"), str) and hw["channel"].isdigit() and hw.get("type") == "vector":
            hw["channel"] = int(hw["channel"])
        cfg = self.diag_config()
        sim = self.project.settings["sim"]
        if hw.get("type") == "simulation" and sim.get("ecu"):
            self.sim_ecu = SimulatedEcu(hw.get("sim_network", "sim"), cfg["request_id"], cfg["response_id"],
                                        cfg["fd"], self.db, tuple(sim.get("traffic_senders", ["ECU"])))
            self.sim_ecu.start()
        try:
            self.mgr.connect(hw)
        except Exception:
            self._stop_sim()
            raise
        self.connectionChanged.emit(True)

    def _stop_sim(self) -> None:
        if self.sim_ecu is not None:
            self.sim_ecu.stop()
            self.sim_ecu = None

    def reinit(self) -> None:
        """Hardware neu initialisieren (schliessen und mit denselben Einstellungen neu oeffnen)."""
        self.disconnect()
        time.sleep(0.5)
        self.connect()

    def disconnect(self) -> None:
        if not self.mgr.connected and self.sim_ecu is None:
            return
        self.stop_logger()
        self.mgr.disconnect()
        self._invalidate_channel()
        self._stop_sim()
        self.connectionChanged.emit(False)

    # -- Logging
    def stop_logger(self) -> None:
        if self.logger is not None:
            self.mgr.remove_listener(self.logger.on_frame)
            try:
                self.logger.stop()
            except Exception as exc:
                self.message.emit(tr("msg.logger_error", error=exc))
            self.logger = None


# =========================================================================== Projekt
class ProjectTab(QWidget):
    HW_KEYS = ["simulation", "vector", "pcan", "socketcan"]

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.b = Binder()
        b = self.b
        lay = QVBoxLayout(self)

        box = QGroupBox()
        b.title(box, "project.group")
        row = QHBoxLayout(box)
        self.combo = QComboBox()
        self.combo.setMinimumWidth(200)
        b_new = b.text(QPushButton(), "project.new")
        b_imp = b.text(QPushButton(), "project.import")
        b_conv = b.text(QPushButton(), "project.reconvert")
        self.fmt = QComboBox()
        self.fmt.addItems(["json", "yaml"])
        for w in (b.text(QLabel(), "project.active"), self.combo, b_new, b_imp,
                  b.text(QLabel(), "project.cache_format"), self.fmt, b_conv):
            row.addWidget(w)
        row.addStretch()
        lay.addWidget(box)

        hw = QGroupBox()
        b.title(hw, "hw.group")
        form = QFormLayout(hw)
        self.hw_type = QComboBox()
        for key in self.HW_KEYS:
            self.hw_type.addItem("", key)
        b.call(self._hw_texts)
        self.channel = b.tip(QLineEdit(), "hw.channel_tip")
        self.bitrate = QSpinBox(); self.bitrate.setRange(10, 1000); self.bitrate.setSuffix(" kbit/s")
        self.fd = b.text(QCheckBox(), "hw.fd")
        self.data_bitrate = QSpinBox(); self.data_bitrate.setRange(500, 8000); self.data_bitrate.setSuffix(" kbit/s")
        self.app_name = b.tip(QLineEdit(), "hw.app_name_tip")
        self.sim_echo = b.text(QCheckBox(), "hw.sim_echo")
        self.sim_ecu = b.text(QCheckBox(), "hw.sim_ecu")
        form.addRow(b.text(QLabel(), "hw.interface"), self.hw_type)
        form.addRow(b.text(QLabel(), "hw.channel"), self.channel)
        form.addRow(b.text(QLabel(), "hw.bitrate"), self.bitrate)
        form.addRow("", self.fd)
        form.addRow(b.text(QLabel(), "hw.data_bitrate"), self.data_bitrate)
        form.addRow(b.text(QLabel(), "hw.app_name"), self.app_name)
        form.addRow("", self.sim_echo)
        form.addRow("", self.sim_ecu)
        note = b.text(QLabel(), "hw.peak_note")
        note.setWordWrap(True)
        form.addRow(note)
        os_note = b.text(QLabel(), f"hw.os_note.{current_os()}", os=os_label())
        os_note.setWordWrap(True)
        form.addRow(os_note)
        self.btn_detect = b.text(QPushButton(), "hw.detect")
        b.tip(self.btn_detect, "hw.detect_tip")
        self.btn_detect.clicked.connect(self.detect_hw)
        form.addRow("", self.btn_detect)
        lay.addWidget(hw)

        row2 = QHBoxLayout()
        self.btn_connect = QPushButton()
        self.btn_reinit = b.text(QPushButton(), "conn.reinit")
        b.tip(self.btn_reinit, "conn.reinit_tip")
        self.status = QLabel("")
        self.status.setWordWrap(True)
        row2.addWidget(self.btn_connect)
        row2.addWidget(self.btn_reinit)
        row2.addWidget(self.status, 1)
        lay.addLayout(row2)

        self.info = QTextEdit()
        self.info.setReadOnly(True)
        lay.addWidget(b.text(QLabel(), "project.info_label"))
        lay.addWidget(self.info, 1)

        b_new.clicked.connect(self.new_project)
        b_imp.clicked.connect(self.import_files)
        b_conv.clicked.connect(self.reconvert)
        self.combo.activated.connect(lambda _i: self.switch_project(self.combo.currentText()))
        self.btn_connect.clicked.connect(self.toggle_connect)
        self.btn_reinit.clicked.connect(self.reinit_hw)
        self.hw_type.currentIndexChanged.connect(self.store_hw)
        for w in (self.channel, self.app_name):
            w.editingFinished.connect(self.store_hw)
        for w in (self.bitrate, self.data_bitrate):
            w.editingFinished.connect(self.store_hw)
        for w in (self.fd, self.sim_echo, self.sim_ecu):
            w.toggled.connect(self.store_hw)
        self.fmt.activated.connect(lambda _i: self.reconvert())
        ctx.projectChanged.connect(self.reload)
        ctx.connectionChanged.connect(self.update_state)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_state)
        self.timer.start(500)
        self._loading = False
        self.refresh_list()
        self.update_state()

    def retranslate(self) -> None:
        self.b.run()
        self.refresh_info()
        self.update_state()

    def refresh_list(self, select: str | None = None) -> None:
        self.combo.clear()
        self.combo.addItems(self.ctx.pm.list())
        if select:
            self.combo.setCurrentText(select)

    def new_project(self) -> None:
        name, ok = QInputDialog.getText(self, tr("project.new_title"), tr("project.new_prompt"))
        if not ok or not name.strip():
            return
        try:
            self.ctx.pm.create(name)
        except Exception as exc:
            QMessageBox.warning(self, tr("project.dialog_title"), str(exc))
            return
        self.refresh_list(name.strip())
        self.switch_project(name.strip())

    def switch_project(self, name: str) -> None:
        if not name:
            return
        try:
            self.ctx.open_project(name)
        except Exception as exc:
            QMessageBox.critical(self, tr("project.dialog_title"), str(exc))

    def import_files(self) -> None:
        if self.ctx.project is None:
            return
        files, _ = QFileDialog.getOpenFileNames(self, tr("project.import_dialog"), "", tr("project.import_filter"))
        for f in files:
            try:
                self.ctx.project.import_file(Path(f))
            except Exception as exc:
                QMessageBox.warning(self, tr("project.import_title"), tr("project.import_error", file=f, error=exc))
        if files:
            self.reconvert()

    def reconvert(self) -> None:
        p = self.ctx.project
        if p is None:
            return
        p.settings["cache_format"] = self.fmt.currentText()
        try:
            p.convert(force=True)
            p.a2l_files(force=True)
        except Exception as exc:
            QMessageBox.warning(self, tr("project.convert_title"), str(exc))
            return
        self.ctx.save_project()
        self.ctx.open_project(p.name)

    def reload(self) -> None:
        p = self.ctx.project
        self._loading = True
        if p is not None:
            if self.combo.currentText() != p.name:
                self.combo.setCurrentText(p.name)
            s = p.settings
            hw = s["hardware"]
            self.hw_type.setCurrentIndex(max(0, self.hw_type.findData(hw.get("type", "simulation"))))
            self.channel.setText(str(hw.get("channel", "")))
            self.bitrate.setValue(int(hw.get("bitrate", 500000)) // 1000)
            self.data_bitrate.setValue(int(hw.get("data_bitrate", 2000000)) // 1000)
            self.fd.setChecked(bool(hw.get("fd")))
            self.app_name.setText(hw.get("app_name", ""))
            self.sim_echo.setChecked(bool(hw.get("sim_echo", True)))
            self.sim_ecu.setChecked(bool(s["sim"].get("ecu", True)))
            self.fmt.setCurrentText(s.get("cache_format", "json"))
        self._loading = False
        self.refresh_info()

    def _hw_texts(self) -> None:
        """Hardware-Auswahl beschriften; unter dem aktuellen Betriebssystem nicht nutzbare Typen sind markiert/gesperrt."""
        support = supported_hardware()
        model = self.hw_type.model()
        for i, k in enumerate(self.HW_KEYS):
            text = tr(f"hw.type.{k}")
            if not support.get(k, True):
                text += "  (" + tr("hw.only_linux" if k == "socketcan" else "hw.only_windows") + ")"
            self.hw_type.setItemText(i, text)
            item = model.item(i) if hasattr(model, "item") else None
            if item is not None:
                item.setEnabled(support.get(k, True))

    def detect_hw(self) -> None:
        """Vorhandene Schnittstellen suchen und bei genau einem Treffer Typ und Kanal uebernehmen."""
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            found = detect_hardware()
        finally:
            QApplication.restoreOverrideCursor()
        if not found:
            QMessageBox.information(self, tr("hw.detect"), tr("hw.detect_none", os=os_label()))
            return
        lines = []
        for c in found:
            cfg = c.get("vector_channel_config")
            extra = f"  ({getattr(cfg, 'name', '')}, S/N {c.get('serial')})" if cfg is not None else ""
            lines.append(f"{c.get('interface')}: {c.get('channel')}{extra}")
        QMessageBox.information(self, tr("hw.detect"), tr("hw.detect_found", items="\n".join(lines)))
        if len(found) == 1:
            kind = {"vector": "vector", "pcan": "pcan", "socketcan": "socketcan"}.get(str(found[0].get("interface")))
            idx = self.hw_type.findData(kind)
            if idx >= 0:
                self.hw_type.setCurrentIndex(idx)
                self.channel.setText(str(found[0].get("channel", default_channel(kind))))
                self.store_hw()

    def refresh_info(self) -> None:
        p = self.ctx.project
        if p is None:
            self.info.clear()
            return
        lines = [tr("project.info.dbc", sources=", ".join(self.ctx.db.sources) or "-", count=len(self.ctx.db.messages))]
        lines += [f"  {f.name}" for f in sorted(p.cache_dir.glob("*.*"))]
        lines += [tr("project.info.warning", text=w) for w in self.ctx.db.warnings]
        if self.ctx.diag_def:
            lines += [tr("project.info.cdd_warning", text=w) for w in self.ctx.diag_def.warnings]
        self.info.setPlainText("\n".join(lines))

    def store_hw(self, *_args) -> None:
        p = self.ctx.project
        if p is None or self._loading:
            return
        hw = p.settings["hardware"]
        hw.update(type=self.hw_type.currentData(), channel=self.channel.text().strip(),
                  bitrate=self.bitrate.value() * 1000, data_bitrate=self.data_bitrate.value() * 1000,
                  fd=self.fd.isChecked(), app_name=self.app_name.text().strip(),
                  sim_echo=self.sim_echo.isChecked())
        p.settings["sim"]["ecu"] = self.sim_ecu.isChecked()

    def toggle_connect(self) -> None:
        if self.ctx.project is None:
            return
        if self.ctx.mgr.connected:
            self.ctx.disconnect()
            return
        self.store_hw()
        try:
            self.ctx.connect()
        except Exception as exc:
            QMessageBox.critical(self, tr("conn.failed_title"), str(exc))

    def reinit_hw(self) -> None:
        if self.ctx.project is None:
            return
        self.store_hw()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.ctx.reinit()
        except Exception as exc:
            QMessageBox.critical(self, tr("conn.failed_title"), str(exc))
        finally:
            QApplication.restoreOverrideCursor()
            self.update_state()

    def update_state(self, *_args) -> None:
        m = self.ctx.mgr
        self.btn_reinit.setEnabled(self.ctx.project is not None)
        self.btn_connect.setText(tr("conn.disconnect") if m.connected else tr("conn.connect"))
        for w in (self.hw_type, self.channel, self.bitrate, self.fd, self.data_bitrate, self.app_name,
                  self.sim_echo, self.sim_ecu):
            w.setEnabled(not m.connected)
        text = (tr("conn.connected", channel=m.channel_name, rx=m.rx_count, tx=m.tx_count)
                if m.connected else tr("conn.disconnected"))
        if m.connected and m.connection_info:
            text += f"   [{m.connection_info}]"
        if (m.connected and m.rx_count == 0 and time.monotonic() - m.connected_at > 3.0
                and self.ctx.project is not None and self.ctx.project.settings["hardware"].get("type") != "simulation"):
            text += "\n" + tr("conn.no_rx", channel=m.channel_name)
        if m.last_error:
            text += f"   | {m.last_error}"
        self.status.setText(text)


# =========================================================================== Daten
class DataTab(QWidget):
    TX_HEADERS = ["col.tx_message_signal", "col.cyclic", "col.value", "col.cycle_ms", "col.select", ""]
    RX_HEADERS = ["col.rx_message_signal", "col.value", "col.unit", "col.text", "col.select"]

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.b = Binder()
        b = self.b
        self._building = False
        left = QWidget()
        ll = QVBoxLayout(left)
        self.filter = b.placeholder(QLineEdit(), "data.filter_placeholder")
        self.dbc_tree = QTreeWidget()
        b.tree_headers(self.dbc_tree, ["col.message_signal", "col.id", "col.unit"])
        self.dbc_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        b_tx = b.text(QPushButton(), "data.add_tx")
        b_rx = b.text(QPushButton(), "data.add_rx")
        row = QHBoxLayout()
        row.addWidget(b_tx)
        row.addWidget(b_rx)
        ll.addWidget(b.text(QLabel(), "data.available"))
        ll.addWidget(self.filter)
        ll.addWidget(self.dbc_tree)
        ll.addLayout(row)

        self.tx_tree = QTreeWidget()
        b.tree_headers(self.tx_tree, self.TX_HEADERS)
        self.rx_tree = QTreeWidget()
        b.tree_headers(self.rx_tree, self.RX_HEADERS)
        b_tx_del, b_rx_del = b.text(QPushButton(), "data.remove"), b.text(QPushButton(), "data.remove")
        tx_box, rx_box = QGroupBox(), QGroupBox()
        b.title(tx_box, "data.tx_group")
        b.title(rx_box, "data.rx_group")
        for box, tree, btn in ((tx_box, self.tx_tree, b_tx_del), (rx_box, self.rx_tree, b_rx_del)):
            v = QVBoxLayout(box)
            v.addWidget(tree)
            v.addWidget(btn, 0, Qt.AlignmentFlag.AlignLeft)
        right = QSplitter(Qt.Orientation.Vertical)
        right.addWidget(tx_box)
        right.addWidget(rx_box)
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([350, 800])
        QVBoxLayout(self).addWidget(split)

        self.filter.textChanged.connect(self.apply_filter)
        b_tx.clicked.connect(self.add_tx_selected)
        b_rx.clicked.connect(self.add_rx_selected)
        b_tx_del.clicked.connect(lambda: self.remove_selected(self.tx_tree, True))
        b_rx_del.clicked.connect(lambda: self.remove_selected(self.rx_tree, False))
        self.dbc_tree.itemDoubleClicked.connect(lambda it, _c: self.add_tx_selected())
        self.tx_tree.itemChanged.connect(self.tx_changed)
        self.rx_tree.itemChanged.connect(self.rx_changed)
        ctx.projectChanged.connect(self.reload)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_live)
        self.timer.start(200)

    def retranslate(self) -> None:
        self.b.run()
        for i in range(self.tx_tree.topLevelItemCount()):        # Senden-Schaltflaechen
            btn = self.tx_tree.itemWidget(self.tx_tree.topLevelItem(i), 5)
            if btn is not None:
                btn.setText(tr("data.send_button"))

    # -- Aufbau
    def reload(self) -> None:
        self._building = True
        db = self.ctx.db
        self.dbc_tree.clear()
        for name in db.message_names():
            m = db.messages[name]
            top = QTreeWidgetItem([name, f"0x{m['id']:X}{'x' if m['extended'] else ''}", m.get("sender", "")])
            top.setData(0, Qt.ItemDataRole.UserRole, name)
            for sig, d in m["signals"].items():
                top.addChild(QTreeWidgetItem([sig, "", d.get("unit", "")]))
            self.dbc_tree.addTopLevelItem(top)
        self.dbc_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tx_tree.clear()
        for name in self.ctx.mgr.tx_entries:
            self._build_tx(name)
        self.rx_tree.clear()
        for name in self.ctx.rx_sel:
            self._build_rx(name)
        for t in (self.tx_tree, self.rx_tree):
            t.expandAll()
            t.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._building = False
        self.apply_filter(self.filter.text())

    def apply_filter(self, text: str) -> None:
        text = text.strip().lower()
        for i in range(self.dbc_tree.topLevelItemCount()):
            top = self.dbc_tree.topLevelItem(i)
            hit_msg = not text or text in top.text(0).lower() or text in top.text(1).lower()
            any_child = False
            for j in range(top.childCount()):
                child = top.child(j)
                show = hit_msg or text in child.text(0).lower()
                child.setHidden(not show)
                any_child |= show
            top.setHidden(not (hit_msg or any_child))
            if text and (hit_msg or any_child):
                top.setExpanded(True)

    def _selected_messages(self) -> list[str]:
        names = []
        for it in self.dbc_tree.selectedItems():
            top = it.parent() or it
            name = top.data(0, Qt.ItemDataRole.UserRole)
            if name and name not in names:
                names.append(name)
        return names

    def add_tx_selected(self) -> None:
        for name in self._selected_messages():
            if name not in self.ctx.mgr.tx_entries:
                self.ctx.mgr.add_tx_message(name)
                self.ctx.tx_sel[name] = None
                self._building = True
                self._build_tx(name)
                self._building = False
        self.ctx.apply_selection()

    def add_rx_selected(self) -> None:
        for name in self._selected_messages():
            if name not in self.ctx.rx_sel:
                self.ctx.rx_sel[name] = None
                self._building = True
                self._build_rx(name)
                self._building = False
        self.ctx.apply_selection()

    def remove_selected(self, tree: QTreeWidget, is_tx: bool) -> None:
        for it in tree.selectedItems():
            top = it.parent() or it
            name = top.data(0, Qt.ItemDataRole.UserRole)
            if name is None:
                continue
            if is_tx:
                self.ctx.mgr.remove_tx(name)
                self.ctx.tx_sel.pop(name, None)
            else:
                self.ctx.rx_sel.pop(name, None)
            idx = tree.indexOfTopLevelItem(top)
            if idx >= 0:
                tree.takeTopLevelItem(idx)
        self.ctx.apply_selection()

    def _signal_check(self, item: QTreeWidgetItem, col: int, selected: bool) -> None:
        item.setFlags(item.flags() | USER_CHECK)
        item.setCheckState(col, CHECKED if selected else UNCHECKED)

    def _build_tx(self, name: str) -> None:
        entry = self.ctx.mgr.tx_entries[name]
        msg = self.ctx.db.messages[name]
        sel = self.ctx.tx_sel.get(name)
        top = QTreeWidgetItem([f"{name}  (0x{msg['id']:X})"])
        top.setData(0, Qt.ItemDataRole.UserRole, name)
        top.setFlags(top.flags() | USER_CHECK)
        top.setCheckState(1, CHECKED if entry.enabled else UNCHECKED)
        self.tx_tree.addTopLevelItem(top)
        cyc = QSpinBox()
        cyc.setRange(0, 60000)
        cyc.setValue(int(entry.cycle_ms))
        cyc.valueChanged.connect(lambda v, n=name: self.ctx.mgr.set_tx_cycle(n, v))
        self.tx_tree.setItemWidget(top, 3, cyc)
        btn = QPushButton(tr("data.send_button"))
        btn.clicked.connect(lambda _=False, n=name: self.send_once(n))
        self.tx_tree.setItemWidget(top, 5, btn)
        for sig_name, sig in msg["signals"].items():
            child = QTreeWidgetItem([sig_name])
            child.setData(0, Qt.ItemDataRole.UserRole + 1, sig_name)
            self._signal_check(child, 4, sel is None or sig_name in sel)
            top.addChild(child)
            cur = float(entry.values.get(sig_name, 0))
            editor = make_value_editor(
                sig, cur, lambda v, n=name, s=sig_name: self.ctx.mgr.set_tx_value(n, s, v))
            self.tx_tree.setItemWidget(child, 2, editor)

    def _build_rx(self, name: str) -> None:
        msg = self.ctx.db.messages[name]
        sel = self.ctx.rx_sel.get(name)
        top = QTreeWidgetItem([f"{name}  (0x{msg['id']:X})"])
        top.setData(0, Qt.ItemDataRole.UserRole, name)
        self.rx_tree.addTopLevelItem(top)
        for sig_name, sig in msg["signals"].items():
            child = QTreeWidgetItem([sig_name, "", sig.get("unit", ""), ""])
            child.setData(0, Qt.ItemDataRole.UserRole + 1, sig_name)
            self._signal_check(child, 4, sel is None or sig_name in sel)
            top.addChild(child)

    # -- Aenderungen
    def send_once(self, name: str) -> None:
        try:
            self.ctx.mgr.send_once(name)
        except Exception as exc:
            QMessageBox.warning(self, tr("data.send_title"), str(exc))

    @staticmethod
    def _checked_signals(top: QTreeWidgetItem) -> list[str] | None:
        sigs = [top.child(j).data(0, Qt.ItemDataRole.UserRole + 1) for j in range(top.childCount())
                if top.child(j).checkState(4) == CHECKED]
        return None if len(sigs) == top.childCount() else sigs

    def tx_changed(self, item: QTreeWidgetItem, col: int) -> None:
        if self._building:
            return
        if item.parent() is None and col == 1:
            name = item.data(0, Qt.ItemDataRole.UserRole)
            self.ctx.mgr.set_tx_enabled(name, item.checkState(1) == CHECKED)
        elif item.parent() is not None and col == 4:
            top = item.parent()
            self.ctx.tx_sel[top.data(0, Qt.ItemDataRole.UserRole)] = self._checked_signals(top)
            self.ctx.apply_selection()

    def rx_changed(self, item: QTreeWidgetItem, col: int) -> None:
        if self._building or item.parent() is None or col != 4:
            return
        top = item.parent()
        self.ctx.rx_sel[top.data(0, Qt.ItemDataRole.UserRole)] = self._checked_signals(top)
        self.ctx.apply_selection()

    def update_live(self) -> None:
        if not self.isVisible():
            return
        store, db = self.ctx.store, self.ctx.db
        self._building = True
        for i in range(self.rx_tree.topLevelItemCount()):
            top = self.rx_tree.topLevelItem(i)
            name = top.data(0, Qt.ItemDataRole.UserRole)
            for j in range(top.childCount()):
                child = top.child(j)
                sig = child.data(0, Qt.ItemDataRole.UserRole + 1)
                latest = store.latest("RX", name, sig)
                if latest is None:
                    continue
                child.setText(1, fmt_num(latest[1]))
                child.setText(3, db.choice_text(name, sig, latest[1]) or "")
        self._building = False


# =========================================================================== Trace
class TraceModel(QAbstractTableModel):
    HEAD_KEYS = ["trace.col.time", "trace.col.dir", "trace.col.channel", "trace.col.id", "trace.col.type",
                 "trace.col.len", "trace.col.data", "trace.col.message", "trace.col.signals", "trace.col.count"]
    # Farbmodus: (hell, dunkel) - Rx mittelblau, Tx mittelgruen, Fehler rot, ISO-TP mittelviolett
    KIND_COLORS = {"rx": ("#2f6fd0", "#6ea8ff"), "tx": ("#2e9e48", "#5cd17a"),
                   "error": ("#d32f2f", "#ff6b6b"), "isotp": ("#8e44ad", "#c08be0")}

    def __init__(self, max_rows: int = 5000) -> None:
        super().__init__()
        self.rows: list[tuple] = []          # 10 Anzeigespalten + Art ("rx" | "tx" | "error" | "isotp")
        self.max_rows = max_rows
        self.color_mode = False
        self.diff_mode = False               # True (nur im Aktualisierungsmodus): Zeitspalte = Diffzeit
        self.update_mode = False             # True: eine Zeile je Botschaft, bei erneutem Auftreten nur aktualisieren
        self._upd = UpdateTable(max_rows)

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self.HEAD_KEYS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            if section == 0 and self.update_mode and self.diff_mode:
                return tr("trace.col.diff")
            return tr(self.HEAD_KEYS[section])
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return row[index.column()]
        if role == Qt.ItemDataRole.ForegroundRole:
            if self.color_mode:
                return QBrush(QColor(self.KIND_COLORS[row[KIND_COL]][1 if Theme.dark else 0]))
            if row[1] == "Tx":
                return QBrush(QColor("#7fb2ff" if Theme.dark else "#1a5fb4"))
        return None

    def set_color_mode(self, on: bool) -> None:
        self.color_mode = on
        self.refresh_colors()

    def refresh_colors(self) -> None:
        if self.rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, len(self.HEAD_KEYS) - 1),
                                  [Qt.ItemDataRole.ForegroundRole])

    def set_update_mode(self, on: bool, diff: bool | None = None) -> None:
        self.update_mode = on
        if diff is not None:
            self.diff_mode = diff
        self._upd.diff = self.diff_mode
        self.clear()
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, 0)

    def append(self, new_rows: list[tuple]) -> None:
        if not new_rows:
            return
        if self.update_mode:
            start = len(self.rows)
            changed, added = self._upd.apply(new_rows)
            if added:
                self.rows = self._upd.rows[:start]          # Qt: Zeilenzahl erst nach beginInsertRows aendern
                self.beginInsertRows(QModelIndex(), start, start + added - 1)
                self.rows = self._upd.rows
                self.endInsertRows()
            self.rows = self._upd.rows
            for i in changed:
                self.dataChanged.emit(self.index(i, 0), self.index(i, len(self.HEAD_KEYS) - 1))
            return
        new_rows = new_rows[-self.max_rows:]
        overflow = len(self.rows) + len(new_rows) - self.max_rows
        if overflow > 0:
            self.beginRemoveRows(QModelIndex(), 0, overflow - 1)
            del self.rows[:overflow]
            self.endRemoveRows()
        start = len(self.rows)
        self.beginInsertRows(QModelIndex(), start, start + len(new_rows) - 1)
        self.rows.extend(new_rows)
        self.endInsertRows()

    def clear(self) -> None:
        self.beginResetModel()
        self._upd.clear()
        self.rows = [] if self.update_mode is False else self._upd.rows
        self.endResetModel()

    def retranslate(self) -> None:
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, len(self.HEAD_KEYS) - 1)


class CustomTxEditor(QDialog):
    """Eingabe einer selbst definierten Sendebotschaft: ID (hex), DLC, Daten (hex), Zyklus, zyklisch."""

    def __init__(self, parent: QWidget | None = None, entry: dict | None = None) -> None:
        super().__init__(parent)
        e = entry or {"id": "100", "dlc": 8, "data": "00 01 02 03 04 05 06 07", "cycle_ms": 1000, "cyclic": False}
        self.setWindowTitle(tr("trace.custom.edit_title"))
        self.id = QLineEdit(str(e["id"]))
        self.dlc = QComboBox()
        for n_ in VALID_DLC:
            self.dlc.addItem(str(n_), n_)
        self.dlc.setCurrentText(str(e["dlc"]))
        self.data = QLineEdit(str(e["data"]))
        self.data.setMinimumWidth(280)
        self.ms = QSpinBox()
        self.ms.setRange(1, 3_600_000)
        self.ms.setValue(int(e["cycle_ms"]))
        self.ms.setSuffix(" ms")
        self.cyclic = QCheckBox(tr("trace.raw.cyclic"))
        self.cyclic.setChecked(bool(e["cyclic"]))
        self._id_text = str(e["id"])
        form = QFormLayout()
        form.addRow(tr("trace.raw.id"), self.id)
        form.addRow(tr("trace.raw.dlc"), self.dlc)
        form.addRow(tr("trace.raw.data"), self.data)
        form.addRow(tr("trace.raw.timing"), self.ms)
        form.addRow("", self.cyclic)
        ok, cancel = QPushButton(tr("flash.status.ok")), QPushButton(tr("flash.dlg.abort"))
        ok.setDefault(True)
        ok.clicked.connect(self._accept)
        cancel.clicked.connect(self.reject)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(ok)
        row.addWidget(cancel)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addLayout(row)

    def _accept(self) -> None:
        try:
            arb = parse_can_id(self.id.text())
            build_raw_payload(self.data.text(), self.dlc.currentData())
        except Exception as exc:
            QMessageBox.warning(self, tr("trace.custom.edit_title"), str(exc))
            return
        self._id_text = f"{arb:X}"
        self.accept()

    def result_entry(self) -> dict:
        return {"id": self._id_text, "dlc": self.dlc.currentData(), "data": self.data.text().strip(),
                "cycle_ms": self.ms.value(), "cyclic": self.cyclic.isChecked()}


class CustomTxDialog(QDialog):
    """Beliebig viele selbst definierte Sendebotschaften; Rechtsklick: Hinzufuegen, Aendern, Loeschen, Senden/Stopp."""

    COLS = ["trace.raw.id", "trace.raw.dlc", "trace.raw.data", "trace.raw.timing", "trace.raw.cyclic",
            "trace.custom.status"]

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.b = Binder()
        self.entries: list[dict] = []
        self.resize(760, 360)
        self.table = QTableWidget(0, len(self.COLS))
        self.b.table_headers(self.table, self.COLS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.menu)
        self.table.cellDoubleClicked.connect(lambda r, _c: self.edit(r))
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.b.text(self.hint, "trace.custom.tip")
        self.b.call(lambda: self.setWindowTitle(tr("trace.custom.title")))
        close = self.b.text(QPushButton(), "flash.dlg.close")
        close.clicked.connect(self.close)
        lay = QVBoxLayout(self)
        lay.addWidget(self.hint)
        lay.addWidget(self.table, 1)
        lay.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        ctx.projectChanged.connect(self.reload)
        ctx.connectionChanged.connect(lambda _c: self.refresh())
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_status)
        self.timer.start(500)
        self.reload()

    # --- Daten ---------------------------------------------------------
    def reload(self) -> None:
        self.stop_all()
        self.entries = [dict(e) for e in (self.ctx.project.settings.get("custom_tx", []) if self.ctx.project else [])]
        self.refresh()

    def _store(self) -> None:
        if self.ctx.project:
            self.ctx.project.settings["custom_tx"] = [dict(e) for e in self.entries]

    def _name(self, e: dict) -> str:
        arb = parse_can_id(e["id"])
        return self.ctx.mgr.raw_name(arb, arb > 0x7FF)

    def _running(self, e: dict) -> bool:
        try:
            return bool(e["cyclic"]) and self._name(e) in self.ctx.mgr.tx_entries
        except Exception:
            return False

    # --- Anzeige -------------------------------------------------------
    def refresh(self) -> None:
        self.table.setRowCount(len(self.entries))
        for r, e in enumerate(self.entries):
            vals = [e["id"], str(e["dlc"]), e["data"], str(e["cycle_ms"]), "x" if e["cyclic"] else ""]
            for c, v in enumerate(vals):
                self.table.setItem(r, c, QTableWidgetItem(v))
            self.table.setItem(r, 5, QTableWidgetItem(""))
        self.refresh_status()

    def refresh_status(self) -> None:
        for r, e in enumerate(self.entries):
            item = self.table.item(r, 5)
            if item is not None:
                item.setText(tr("trace.custom.running") if self._running(e) else "")

    # --- Aktionen ------------------------------------------------------
    def menu(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        m = QMenu(self)
        a_add = m.addAction(tr("trace.custom.add"))
        a_edit = a_del = a_send = None
        if 0 <= row < len(self.entries):
            self.table.selectRow(row)
            running = self._running(self.entries[row])
            a_send = m.addAction(tr("trace.raw.stop" if running else "trace.raw.send"))
            a_edit = m.addAction(tr("trace.custom.edit"))
            a_del = m.addAction(tr("trace.custom.delete"))
        chosen = m.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is a_add:
            self.add()
        elif chosen is a_edit:
            self.edit(row)
        elif chosen is a_del:
            self.delete(row)
        elif chosen is a_send:
            self.send(row)

    def add(self) -> None:
        dlg = CustomTxEditor(self)
        if dlg.exec():
            self.entries.append(dlg.result_entry())
            self._store()
            self.refresh()

    def edit(self, row: int) -> None:
        if not 0 <= row < len(self.entries):
            return
        self._stop(self.entries[row])
        dlg = CustomTxEditor(self, self.entries[row])
        if dlg.exec():
            self.entries[row] = dlg.result_entry()
            self._store()
            self.refresh()

    def delete(self, row: int) -> None:
        if 0 <= row < len(self.entries):
            self._stop(self.entries[row])
            del self.entries[row]
            self._store()
            self.refresh()

    def send(self, row: int) -> None:
        """Einmal senden bzw. zyklisch starten/stoppen."""
        if not 0 <= row < len(self.entries):
            return
        e = self.entries[row]
        try:
            if self._running(e):
                self._stop(e)
            else:
                self.ctx.mgr.tx_raw(e["id"], e["data"], e["dlc"], e["cyclic"], float(e["cycle_ms"]))
        except Exception as exc:
            QMessageBox.warning(self, tr("trace.raw.title"), str(exc))
        self.refresh_status()

    def _stop(self, e: dict) -> None:
        try:
            if self._running(e):
                self.ctx.mgr.remove_tx(self._name(e))
        except Exception:
            pass

    def stop_all(self) -> None:
        for e in self.entries:
            self._stop(e)

    def retranslate(self) -> None:
        self.b.run()
        self.refresh()


class TraceTab(QWidget):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.b = Binder()
        b = self.b
        self.buffer: deque[Frame] = deque(maxlen=50000)
        self.t0: float | None = None
        self.model = TraceModel()
        self.view = QTableView()
        self.view.setModel(self.model)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.verticalHeader().setDefaultSectionSize(20)
        self.view.verticalHeader().hide()
        self.view.horizontalHeader().setStretchLastSection(True)
        for i, w in enumerate((80, 45, 80, 80, 55, 40, 380, 150, 330)):
            self.view.setColumnWidth(i, w)
        self.pause = b.text(QCheckBox(), "trace.pause")
        self.decode = b.text(QCheckBox(), "trace.decode"); self.decode.setChecked(True)
        self.only_sel = b.text(QCheckBox(), "trace.only_selected")
        self.autoscroll = b.text(QCheckBox(), "trace.autoscroll"); self.autoscroll.setChecked(True)
        self.show_tx = b.text(QCheckBox(), "trace.tx"); self.show_tx.setChecked(True)
        self.show_rx = b.text(QCheckBox(), "trace.rx"); self.show_rx.setChecked(True)
        self.id_filter = b.placeholder(QLineEdit(), "trace.filter_placeholder")
        self.update_mode = b.text(QCheckBox(), "trace.update_mode")
        b.tip(self.update_mode, "trace.update_mode_tip")
        self.update_mode.toggled.connect(self._set_update_mode)
        self.diff_time = b.text(QCheckBox(), "trace.diff_time")
        b.tip(self.diff_time, "trace.diff_time_tip")
        self.diff_time.setEnabled(False)
        self.diff_time.toggled.connect(self._set_update_mode)
        self.color_btn = b.text(QPushButton(), "trace.color_mode")
        b.tip(self.color_btn, "trace.color_mode_tip")
        self.color_btn.setCheckable(True)
        b_clear, b_save = b.text(QPushButton(), "trace.clear"), b.text(QPushButton(), "trace.save_csv")
        bar = QHBoxLayout()
        for w in (self.pause, self.decode, self.only_sel, self.autoscroll, self.show_rx, self.show_tx,
                  self.id_filter, self.update_mode, self.diff_time, self.color_btn, b_clear, b_save):
            bar.addWidget(w)
        self.id_filter.setMinimumWidth(260)
        # Sende-Zeile fuer Rohframes (Standard: ID 100 hex, DLC 8, Daten 00..07, 1000 ms)
        self._raw_name: str | None = None
        self.raw_id = QLineEdit("100"); self.raw_id.setMaximumWidth(90)
        self.raw_dlc = QComboBox()
        for n in VALID_DLC:
            self.raw_dlc.addItem(str(n), n)
        self.raw_dlc.setCurrentText("8")
        self.raw_data = QLineEdit("00 01 02 03 04 05 06 07"); self.raw_data.setMinimumWidth(260)
        self.raw_ms = QSpinBox(); self.raw_ms.setRange(1, 3_600_000); self.raw_ms.setValue(1000); self.raw_ms.setSuffix(" ms")
        self.raw_cyclic = b.text(QCheckBox(), "trace.raw.cyclic")
        self.raw_btn = b.text(QPushButton(), "trace.raw.send")
        raw = QHBoxLayout()
        self.raw_labels = []
        for key, w in (("trace.raw.id", self.raw_id), ("trace.raw.dlc", self.raw_dlc), ("trace.raw.data", self.raw_data),
                       ("trace.raw.timing", self.raw_ms)):
            lbl = b.text(QLabel(), key)
            raw.addWidget(lbl)
            raw.addWidget(w)
            b.tip(w, "trace.raw.tip")
            b.tip(lbl, "trace.raw.tip")
        raw.addWidget(self.raw_cyclic)
        raw.addWidget(self.raw_btn)
        self.custom_btn = b.text(QPushButton(), "trace.custom.button")
        self.custom = CustomTxDialog(ctx, self)
        self.custom_btn.clicked.connect(lambda: (self.custom.show(), self.custom.raise_(), self.custom.activateWindow()))
        raw.addWidget(self.custom_btn)
        raw.addStretch(1)
        b.tip(self.raw_btn, "trace.raw.tip")
        b.tip(self.raw_cyclic, "trace.raw.tip")
        self.raw_btn.clicked.connect(self.raw_send)
        self.raw_cyclic.toggled.connect(lambda _c: self._raw_update_button())
        lay = QVBoxLayout(self)
        lay.addLayout(bar)
        lay.addLayout(raw)
        lay.addWidget(self.view)
        self.count_label = QLabel("")
        lay.addWidget(self.count_label)
        b_clear.clicked.connect(self.clear)
        b_save.clicked.connect(self.save_csv)
        self.color_btn.toggled.connect(self.model.set_color_mode)
        ctx.mgr.add_listener(self.on_frame, errors=True)
        ctx.connectionChanged.connect(self._on_connection)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.drain)
        self.timer.start(100)

    def _set_update_mode(self, _checked: bool = False) -> None:
        on = self.update_mode.isChecked()
        self.diff_time.setEnabled(on)
        self.model.set_update_mode(on, self.diff_time.isChecked())
        self.t0 = None if not self.model.rows else self.t0
        self.update_count()

    def _on_connection(self, connected: bool) -> None:
        if connected:
            self.clear()
        else:
            if self._raw_name is not None:
                self.ctx.mgr.remove_tx(self._raw_name)
            self._raw_name = None
            self._raw_update_button()
            self.custom.stop_all()

    def _raw_update_button(self) -> None:
        running = self._raw_name is not None
        self.raw_btn.setText(tr("trace.raw.stop" if running else "trace.raw.send"))

    def raw_send(self) -> None:
        """Rohframe senden: einmalig oder (mit Haken 'Zyklisch') starten/stoppen des zyklischen Sendens."""
        mgr = self.ctx.mgr
        try:
            if self._raw_name is not None:                      # laeuft zyklisch -> stoppen
                mgr.remove_tx(self._raw_name)
                self._raw_name = None
            else:
                name = mgr.tx_raw(self.raw_id.text(), self.raw_data.text(), self.raw_dlc.currentData(),
                                  self.raw_cyclic.isChecked(), float(self.raw_ms.value()))
                if self.raw_cyclic.isChecked():
                    self._raw_name = name
        except Exception as exc:
            QMessageBox.warning(self, tr("trace.raw.title"), str(exc))
        self._raw_update_button()

    def retranslate(self) -> None:
        self.b.run()
        self.custom.retranslate()
        self._raw_update_button()
        self.model.retranslate()
        self.model.refresh_colors()                  # Farben haengen vom Hell/Dunkel-Modus ab
        self.update_count()

    def _isotp_ids(self) -> set[int]:
        """CAN-IDs der Diagnose (Request/Response aus der Diagnose-Konfiguration, plus funktional 0x7DF)."""
        try:
            cfg = self.ctx.diag_config()
            ids = {cfg["request_id"], cfg["response_id"]}
        except Exception:
            ids = set()
        return ids | {0x7DF}

    def on_frame(self, frame: Frame) -> None:      # Bus-Thread
        self.buffer.append(frame)

    def clear(self) -> None:
        self.buffer.clear()
        self.model.clear()
        self.t0 = None

    def _filters(self) -> tuple[set[int], str]:
        ids, text = set(), ""
        for part in self.id_filter.text().replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                ids.add(int(part, 16))
            except ValueError:
                text = part.lower()
        return ids, text

    def _row(self, f: Frame, isotp_ids: frozenset | set = frozenset()) -> tuple | None:
        db = self.ctx.db
        if f.is_error:                                   # Bus-Fehler: immer anzeigen, keine Filter
            if self.t0 is None:
                self.t0 = f.timestamp
            return (f"{f.timestamp - self.t0:11.4f}", "Rx", f.channel, f"{f.arbitration_id:03X}", "ERR",
                    str(len(f.data)), bytes_to_hex(f.data), "", tr("trace.error_frame"), "", "error")
        name = db.find_by_id(f.arbitration_id, f.is_extended)
        if f.direction == "rx" and not self.show_rx.isChecked():
            return None
        if f.direction == "tx" and not self.show_tx.isChecked():
            return None
        if self.only_sel.isChecked():
            sel = self.ctx.tx_sel if f.direction == "tx" else self.ctx.rx_sel
            if name not in sel:
                return None
        ids, text = self._filters()
        if ids and f.arbitration_id not in ids:
            return None
        if text and text not in (name or "").lower():
            return None
        sigs = ""
        if self.decode.isChecked() and name:
            try:
                parts = []
                for k, v in db.decode(name, f.data).items():
                    unit = db.signal_def(name, k).get("unit", "")
                    parts.append(f"{k}={fmt_num(v)}{(' ' + unit) if unit else ''}")
                sigs = ", ".join(parts)
            except Exception:
                sigs = tr("trace.decode_error")
        if self.t0 is None:
            self.t0 = f.timestamp
        kind = "isotp" if (f.arbitration_id in isotp_ids and name is None) else f.direction
        return (f"{f.timestamp - self.t0:11.4f}", "Tx" if f.direction == "tx" else "Rx", f.channel,
                f"{f.arbitration_id:08X}" if f.is_extended else f"{f.arbitration_id:03X}",
                ("FD" + ("/BRS" if f.bitrate_switch else "")) if f.is_fd else "CAN", str(len(f.data)),
                bytes_to_hex(f.data), name or "", sigs, "", kind)

    def update_count(self) -> None:
        self.count_label.setText(tr("trace.count", rows=len(self.model.rows), queued=len(self.buffer)))

    def drain(self) -> None:
        if self.pause.isChecked() or not self.isVisible():
            return
        rows = []
        isotp_ids = self._isotp_ids()
        for _ in range(min(len(self.buffer), 2000)):
            row = self._row(self.buffer.popleft(), isotp_ids)
            if row:
                rows.append(row)
        if rows:
            self.model.append(rows)
            if self.autoscroll.isChecked() and not self.model.update_mode:
                self.view.scrollToBottom()
        self.update_count()

    def save_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, tr("trace.save_title"), "trace.csv", "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, delimiter=";")
            w.writerow([tr(k) for k in TraceModel.HEAD_KEYS])
            w.writerows(row[:KIND_COL] for row in self.model.rows)


# =========================================================================== Sequenz
class SequenceTab(QWidget):
    runEvent = Signal(str, object)

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.b = Binder()
        b = self.b
        self.runner = SequenceRunner(ctx.mgr, ctx.get_diag, lambda k, p: self.runEvent.emit(k, p))
        self._building = False
        self._status: tuple = ("text", "")          # ("text", str) | ("key", key, kwargs) | ("report", RunReport)
        self.files = QComboBox()
        b_new, b_open = b.text(QPushButton(), "seq.new"), b.text(QPushButton(), "seq.load")
        b_save, b_saveas = b.text(QPushButton(), "seq.save"), b.text(QPushButton(), "seq.save_as")
        top = QHBoxLayout()
        for w in (b.text(QLabel(), "seq.file"), self.files, b_open, b_new, b_save, b_saveas):
            top.addWidget(w)
        top.addStretch()
        self.name = QLineEdit()
        self.desc = QLineEdit()
        form = QFormLayout()
        form.addRow(b.text(QLabel(), "seq.name"), self.name)
        form.addRow(b.text(QLabel(), "seq.description"), self.desc)

        self.table = QTableWidget(0, 3)
        b.table_headers(self.table, ["seq.col.num", "seq.col.type", "seq.col.params"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().hide()
        self.kind = QComboBox()
        self.kind.setMinimumWidth(300)
        self.kind.setMaxVisibleItems(15)
        for key in STEP_TEMPLATES:
            self.kind.addItem("", key)
        b.call(self.fill_kinds)
        self.kind_hint = QLabel("")
        self.kind_hint.setWordWrap(True)
        self.kind_hint.setStyleSheet("font-style: italic;")
        self.kind.highlighted.connect(self.show_kind_hint)
        self.kind.currentIndexChanged.connect(self.show_kind_hint)
        b_add = b.text(QPushButton(), "seq.add_step")
        b_up, b_down = b.text(QPushButton(), "seq.up"), b.text(QPushButton(), "seq.down")
        b_dup, b_del = b.text(QPushButton(), "seq.duplicate"), b.text(QPushButton(), "seq.delete")
        self.sigbox = QComboBox()
        self.sigbox.setEditable(True)
        self.sigbox.setMinimumWidth(240)
        b_apply = b.text(QPushButton(), "seq.apply_signal")
        edit = QHBoxLayout()
        for w in (self.kind, b_add, b_up, b_down, b_dup, b_del):
            edit.addWidget(w)
        edit.addStretch()
        sig_row = QHBoxLayout()
        sig_row.addWidget(b.text(QLabel(), "seq.signal"))
        sig_row.addWidget(self.sigbox)
        sig_row.addWidget(b_apply)
        sig_row.addStretch()

        self.btn_run, self.btn_stop = b.text(QPushButton(), "seq.run"), b.text(QPushButton(), "seq.stop")
        self.btn_check = b.text(QPushButton(), "seq.check")
        self.status = QLabel("")
        run = QHBoxLayout()
        for w in (self.btn_run, self.btn_stop, self.btn_check):
            run.addWidget(w)
        run.addWidget(self.status, 1)
        self.results = QTableWidget(0, 4)
        b.table_headers(self.results, ["seq.res.step", "seq.res.description", "seq.res.status", "seq.res.message"])
        self.results.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.results.verticalHeader().hide()

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addLayout(form)
        lay.addLayout(edit)
        lay.addWidget(self.kind_hint)
        lay.addLayout(sig_row)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.table)
        split.addWidget(self.results)
        lay.addWidget(split, 1)
        lay.addLayout(run)

        b_new.clicked.connect(self.new)
        b_open.clicked.connect(self.load_selected)
        b_save.clicked.connect(self.save)
        b_saveas.clicked.connect(self.save_as)
        b_add.clicked.connect(self.add_step)
        b_up.clicked.connect(lambda: self.move(-1))
        b_down.clicked.connect(lambda: self.move(1))
        b_dup.clicked.connect(self.duplicate)
        b_del.clicked.connect(self.delete)
        b_apply.clicked.connect(self.apply_signal)
        self.table.itemChanged.connect(self.cell_changed)
        self.name.textEdited.connect(lambda t: self.ctx.sequence.__setitem__("name", t))
        self.desc.textEdited.connect(lambda t: self.ctx.sequence.__setitem__("description", t))
        self.btn_run.clicked.connect(self.run)
        self.btn_stop.clicked.connect(self.runner.stop)
        self.btn_check.clicked.connect(self.validate)
        self.runEvent.connect(self.on_event)
        ctx.projectChanged.connect(self.reload)
        ctx.connectionChanged.connect(lambda _c: self.update_buttons())
        self.update_buttons()
        self.show_kind_hint(0)

    # -- Sprache / Befehls-Dropdown
    def fill_kinds(self) -> None:
        """Eintraege und Detail-Tooltips (Format, Elemente, Beispiel) in der aktuellen Sprache setzen."""
        for i in range(self.kind.count()):
            key = self.kind.itemData(i)
            self.kind.setItemText(i, tr("seq.kind_item", key=key, title=step_title(key)))
            self.kind.setItemData(i, step_tooltip_html(key), Qt.ItemDataRole.ToolTipRole)

    def retranslate(self) -> None:
        self.b.run()
        self.show_kind_hint(self.kind.currentIndex())
        self.fill_table(self._row() if self._row() >= 0 else None)
        for r in range(self.results.rowCount()):                 # Statusspalte der Ergebnisse
            item = self.results.item(r, 2)
            if item is not None:
                item.setText(tr(f"seq.status.{item.data(Qt.ItemDataRole.UserRole)}"))
        self.render_status()

    def show_kind_hint(self, index: int) -> None:
        """Kurzbeschreibung des (hover-)markierten Befehls unter dem Dropdown."""
        key = self.kind.itemData(index) if index >= 0 else None
        if key in STEP_HELP:
            self.kind_hint.setText(tr("seq.hint", key=key, summary=step_summary(key), example=step_example_json(key)))

    def set_status(self, *state) -> None:
        self._status = state
        self.render_status()

    def render_status(self) -> None:
        kind = self._status[0]
        if kind == "key":
            self.status.setText(tr(self._status[1], **self._status[2]))
        elif kind == "report":
            self.status.setText(self._status[1].summary())
        else:
            self.status.setText(self._status[1])

    # -- Datei
    def reload(self) -> None:
        self.files.clear()
        if self.ctx.project:
            self.files.addItems([f.name for f in self.ctx.project.sequence_files()])
            if self.ctx.sequence_file:
                self.files.setCurrentText(self.ctx.sequence_file.name)
        self.refresh_all()

    def refresh_all(self) -> None:
        seq = self.ctx.sequence
        self.name.setText(seq.get("name", ""))
        self.desc.setText(seq.get("description", ""))
        self.sigbox.clear()
        self.sigbox.addItems([f"{m}.{s}" for m in self.ctx.db.message_names() for s in self.ctx.db.signal_names(m)])
        self.fill_table()

    def new(self) -> None:
        if self.runner.running:
            return
        self.ctx.sequence, self.ctx.sequence_file = new_sequence(), None
        self.refresh_all()

    def load_selected(self) -> None:
        if self.runner.running or not self.ctx.project or not self.files.currentText():
            return
        path = self.ctx.project.seq_dir / self.files.currentText()
        try:
            self.ctx.sequence = load_sequence(path)
            self.ctx.sequence_file = path
        except Exception as exc:
            QMessageBox.warning(self, tr("seq.load_title"), str(exc))
            return
        self.refresh_all()

    def save(self) -> None:
        if self.ctx.sequence_file is None:
            self.save_as()
            return
        save_sequence(self.ctx.sequence, self.ctx.sequence_file)
        self.set_status("key", "seq.saved", {"name": self.ctx.sequence_file.name})

    def save_as(self) -> None:
        if not self.ctx.project:
            return
        suggested = (self.ctx.sequence.get("name") or "neu") + ".json"
        path, _ = QFileDialog.getSaveFileName(self, tr("seq.save_title"), str(self.ctx.project.seq_dir / suggested),
                                              tr("seq.file_filter"))
        if not path:
            return
        self.ctx.sequence_file = Path(path)
        save_sequence(self.ctx.sequence, self.ctx.sequence_file)
        self.reload()

    # -- Tabelle
    @staticmethod
    def _params(step: dict) -> str:
        return json.dumps({k: v for k, v in step.items() if k != "type"}, ensure_ascii=False)

    def fill_table(self, select: int | None = None) -> None:
        self._building = True
        steps = self.ctx.sequence["steps"]
        self.table.setRowCount(len(steps))
        for i, st in enumerate(steps):
            num = QTableWidgetItem(str(i + 1))
            num.setFlags(num.flags() & ~Qt.ItemFlag.ItemIsEditable)
            typ = QTableWidgetItem(st.get("type", "?"))
            typ.setFlags(typ.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if st.get("type") in STEP_HELP:
                typ.setToolTip(step_tooltip_html(st["type"]))
            self.table.setItem(i, 0, num)
            self.table.setItem(i, 1, typ)
            self.table.setItem(i, 2, QTableWidgetItem(self._params(st)))
        self.table.resizeColumnToContents(0)
        self.table.resizeColumnToContents(1)
        if select is not None and 0 <= select < len(steps):
            self.table.selectRow(select)
        self._building = False

    def cell_changed(self, item: QTableWidgetItem) -> None:
        if self._building or item.column() != 2:
            return
        row = item.row()
        step = self.ctx.sequence["steps"][row]
        try:
            params = json.loads(item.text())
            if not isinstance(params, dict):
                raise ValueError(tr("seq.json_object_expected"))
        except Exception as exc:
            self.set_status("key", "seq.bad_json", {"row": row + 1, "error": exc})
            self.fill_table(row)
            return
        self.ctx.sequence["steps"][row] = {"type": step["type"], **params}
        self.set_status("text", "")

    def _row(self) -> int:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def add_step(self) -> None:
        steps = self.ctx.sequence["steps"]
        row = self._row()
        pos = row + 1 if row >= 0 else len(steps)
        steps.insert(pos, json.loads(json.dumps(STEP_TEMPLATES[self.kind.currentData()])))
        self.fill_table(pos)

    def move(self, delta: int) -> None:
        steps, row = self.ctx.sequence["steps"], self._row()
        new = row + delta
        if row < 0 or not 0 <= new < len(steps):
            return
        steps[row], steps[new] = steps[new], steps[row]
        self.fill_table(new)

    def duplicate(self) -> None:
        steps, row = self.ctx.sequence["steps"], self._row()
        if row >= 0:
            steps.insert(row + 1, json.loads(json.dumps(steps[row])))
            self.fill_table(row + 1)

    def delete(self) -> None:
        steps, row = self.ctx.sequence["steps"], self._row()
        if row >= 0:
            del steps[row]
            self.fill_table(min(row, len(steps) - 1))

    def apply_signal(self) -> None:
        row = self._row()
        text = self.sigbox.currentText().strip()
        if row < 0 or "." not in text:
            return
        msg, sig = text.split(".", 1)
        step = self.ctx.sequence["steps"][row]
        step["message"] = msg
        if step.get("type") in ("set_signal", "wait_for", "check"):
            step["signal"] = sig
        self.fill_table(row)

    # -- Ausfuehrung
    def validate(self) -> None:
        problems = validate_sequence(self.ctx.sequence, self.ctx.mgr)
        if problems:
            QMessageBox.warning(self, tr("seq.dialog_title"), "\n".join(problems))
        else:
            self.set_status("key", "seq.valid", {})

    def update_buttons(self) -> None:
        running = self.runner.running
        self.btn_run.setEnabled(self.ctx.mgr.connected and not running)
        self.btn_stop.setEnabled(running)
        for w in (self.table, self.name, self.desc):
            w.setEnabled(not running)

    def run(self) -> None:
        self.results.setRowCount(0)
        try:
            self.runner.start(self.ctx.sequence)
        except Exception as exc:
            QMessageBox.warning(self, tr("seq.dialog_title"), str(exc))
        self.update_buttons()

    def on_event(self, kind: str, payload) -> None:
        if kind == "started":
            self.set_status("key", "seq.running", {})
        elif kind == "step":
            r = self.results.rowCount()
            self.results.insertRow(r)
            if Theme.dark:
                colors = {"ok": "#285a2b", "fail": "#6b2a2a", "error": "#6b2a2a", "skipped": "#444444"}
                fg = QColor("#f0f0f0")
            else:
                colors = {"ok": "#d7f5d7", "fail": "#f8d0d0", "error": "#f8d0d0", "skipped": "#eeeeee"}
                fg = QColor("#000000")
            texts = (payload.index, payload.description, tr(f"seq.status.{payload.status}"), payload.message)
            for c, text in enumerate(texts):
                it = QTableWidgetItem(text)
                it.setBackground(QColor(colors.get(payload.status, "#ffffff")))
                it.setForeground(fg)
                if c == 2:
                    it.setData(Qt.ItemDataRole.UserRole, payload.status)
                self.results.setItem(r, c, it)
            self.results.resizeColumnToContents(1)
            self.results.scrollToBottom()
        elif kind == "finished":
            self.set_status("report", payload)
            self.update_buttons()


# =========================================================================== Graph
class GraphTab(QWidget):
    """Liniengraph: ein gemeinsamer Graph oder n Graphen (je Signal) mit gemeinsamer Zeitbasis. Auswahl nach Botschaft gruppiert."""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.b = Binder()
        b = self.b
        self.curves: dict[str, pg.PlotDataItem] = {}
        self.plots: list[pg.PlotWidget] = []
        self.legends: list = []
        self.checked: set[str] = set()
        self._building = False
        self.multi = False
        self.tree = QTreeWidget()
        b.tree_headers(self.tree, ["graph.signals"])
        self.tree.setMaximumWidth(320)
        self.window = QDoubleSpinBox(); self.window.setRange(1, 3600); self.window.setValue(30); self.window.setSuffix(" s")
        self.mode_btn = QPushButton()
        self.mode_btn.setCheckable(True)
        b.call(self._mode_text)
        b.tip(self.mode_btn, "graph.mode_tip")
        self.pause = b.text(QCheckBox(), "graph.pause")
        b_clear = b.text(QPushButton(), "graph.clear")
        self.host = QWidget()
        self.host_lay = QVBoxLayout(self.host)
        self.host_lay.setContentsMargins(0, 0, 0, 0)
        self.host_lay.setSpacing(2)
        left = QVBoxLayout()
        left.addWidget(b.text(QLabel(), "graph.signals"))
        left.addWidget(self.mode_btn)
        left.addWidget(self.tree, 1)
        form = QFormLayout()
        form.addRow(b.text(QLabel(), "graph.window"), self.window)
        left.addLayout(form)
        for w in (self.pause, b_clear):
            left.addWidget(w)
        lay = QHBoxLayout(self)
        lay.addLayout(left)
        lay.addWidget(self.host, 1)
        self.tree.itemChanged.connect(self.item_changed)
        self.mode_btn.toggled.connect(self.set_multi)
        b_clear.clicked.connect(ctx.store.clear)
        ctx.projectChanged.connect(self.reload)
        ctx.selectionChanged.connect(self.rebuild_list)
        self.rebuild_plots()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(100)

    def _mode_text(self) -> None:
        self.mode_btn.setText(tr("graph.mode_multi" if self.mode_btn.isChecked() else "graph.mode_single"))

    def retranslate(self) -> None:
        self.b.run()
        self._mode_text()
        for p in self.plots:
            p.setLabel("bottom", tr("graph.time"), units="s")

    def apply_theme(self) -> None:
        bg, fg = ("#1e1e1e", "#dcdcdc") if Theme.dark else ("#ffffff", "#202020")
        for plot in self.plots:
            plot.setBackground(bg)
            for axis in ("left", "bottom"):
                ax = plot.getAxis(axis)
                ax.setPen(fg)
                ax.setTextPen(fg)
        for legend in self.legends:
            try:
                legend.setLabelTextColor(fg)
            except Exception:
                pass

    # -- Auswahl (nach Botschaft gruppiert)
    def reload(self) -> None:
        proj = self.ctx.project
        self.checked = set(proj.settings.get("graph", [])) if proj else set()
        multi = bool(proj.settings.get("graph_multi", False)) if proj else False
        with QSignalBlocker(self.mode_btn):
            self.mode_btn.setChecked(multi)
        self.multi = multi
        self._mode_text()
        self.rebuild_list()

    def rebuild_list(self) -> None:
        self._building = True
        keys = self.ctx.selected_keys()
        self.tree.clear()
        groups: dict[tuple[str, str], QTreeWidgetItem] = {}
        for key in keys:
            direction, msg, sig = split_key(key)
            top = groups.get((direction, msg))
            if top is None:
                top = QTreeWidgetItem([f"{direction}: {msg}"])
                top.setFlags(top.flags() | USER_CHECK | Qt.ItemFlag.ItemIsAutoTristate)
                self.tree.addTopLevelItem(top)
                groups[(direction, msg)] = top
            unit = self.ctx.db.signal_def(msg, sig).get("unit", "") if msg in self.ctx.db.messages else ""
            child = QTreeWidgetItem([sig + (f"  [{unit}]" if unit else "")])
            child.setData(0, Qt.ItemDataRole.UserRole, key)
            child.setFlags(child.flags() | USER_CHECK)
            child.setCheckState(0, CHECKED if key in self.checked else UNCHECKED)
            top.addChild(child)
        self.tree.expandAll()
        self.checked &= set(keys)
        self._building = False
        self.rebuild_plots()

    def item_changed(self, _item: QTreeWidgetItem, _col: int = 0) -> None:
        if self._building:
            return
        checked = set()
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                child = top.child(j)
                if child.checkState(0) == CHECKED:
                    checked.add(child.data(0, Qt.ItemDataRole.UserRole))
        if checked != self.checked:
            self.checked = checked
            if self.ctx.project:
                self.ctx.project.settings["graph"] = sorted(self.checked)
            self.rebuild_plots()

    def set_multi(self, on: bool) -> None:
        self.multi = on
        self._mode_text()
        if self.ctx.project:
            self.ctx.project.settings["graph_multi"] = on
        self.rebuild_plots()

    # -- Diagramme
    def active_keys(self) -> list[str]:
        return [k for k in self.ctx.selected_keys() if k in self.checked]

    def _new_plot(self) -> pg.PlotWidget:
        plot = pg.PlotWidget()
        plot.showGrid(x=True, y=True, alpha=0.3)
        self.plots.append(plot)
        self.host_lay.addWidget(plot, 1)
        return plot

    def rebuild_plots(self) -> None:
        """Diagramme neu aufbauen: ein Graph mit allen Kurven oder je Signal ein Graph (gemeinsame Zeitachse)."""
        for plot in self.plots:
            self.host_lay.removeWidget(plot)
            plot.setParent(None)
            plot.deleteLater()
        self.plots, self.legends, self.curves = [], [], {}
        keys = self.active_keys()
        if not self.multi or not keys:
            plot = self._new_plot()
            self.legends.append(plot.addLegend())
            for i, key in enumerate(keys):
                self.curves[key] = plot.plot(name=key, pen=pg.mkPen(pg.intColor(i, hues=9, values=1), width=2))
        else:
            first = None
            for i, key in enumerate(keys):
                plot = self._new_plot()
                _direction, msg, sig = split_key(key)
                unit = self.ctx.db.signal_def(msg, sig).get("unit", "") if msg in self.ctx.db.messages else ""
                plot.setLabel("left", f"{msg}.{sig}", units=unit or None)
                plot.getAxis("left").setWidth(110)
                self.curves[key] = plot.plot(name=key, pen=pg.mkPen(pg.intColor(i, hues=9, values=1), width=2))
                if first is None:
                    first = plot
                else:
                    plot.setXLink(first)                       # gemeinsame Zeitbasis
                if i < len(keys) - 1:
                    plot.getAxis("bottom").setStyle(showValues=False)
        for plot in self.plots:
            plot.setLabel("bottom", tr("graph.time"), units="s")
        self.apply_theme()

    def refresh(self) -> None:
        if self.pause.isChecked() or not self.isVisible():
            return
        now = time.time()
        span = self.window.value()
        for key, curve in self.curves.items():
            t, v = self.ctx.store.history(key, now - span)
            if not t:
                curve.setData([], [])
                continue
            x, y = np.asarray(t) - now, np.asarray(v, dtype=float)
            curve.setData(x, y)
        if self.plots:
            self.plots[0].setXRange(-span, 0, padding=0)       # weitere Diagramme folgen ueber setXLink


# =========================================================================== Logging
class LoggingTab(QWidget):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.b = Binder()
        b = self.b
        self._building = False
        self._last_flush = time.monotonic()
        self.tree = QTreeWidget()
        b.tree_headers(self.tree, ["log.tree_header"])
        self.filename = QLineEdit()
        self.on_change = b.text(QCheckBox(), "log.on_change")
        self.min_interval = QSpinBox(); self.min_interval.setRange(0, 600000); self.min_interval.setSuffix(" ms")
        self.raw = b.text(QCheckBox(), "log.raw")
        self.raw_ids = b.placeholder(QLineEdit(), "log.raw_ids_placeholder")
        b.tip(self.raw_ids, "log.raw_ids_tip")
        self.btn = QPushButton()
        self.status = QLabel("")
        b_all, b_none = b.text(QPushButton(), "log.all"), b.text(QPushButton(), "log.none")
        sel_row = QHBoxLayout()
        sel_row.addWidget(b_all)
        sel_row.addWidget(b_none)
        sel_row.addStretch()
        form = QFormLayout()
        form.addRow(b.text(QLabel(), "log.filename"), self.filename)
        form.addRow(b.text(QLabel(), "log.min_interval"), self.min_interval)
        form.addRow("", self.on_change)
        form.addRow("", self.raw)
        form.addRow(b.text(QLabel(), "log.raw_ids"), self.raw_ids)
        lay = QVBoxLayout(self)
        lay.addWidget(self.tree, 1)
        lay.addLayout(sel_row)
        lay.addLayout(form)
        lay.addWidget(self.btn)
        lay.addWidget(self.status)
        b.call(self.update_button)
        if not parquet_available():
            self.btn.setEnabled(False)
        b_all.clicked.connect(lambda: self.set_all(CHECKED))
        b_none.clicked.connect(lambda: self.set_all(UNCHECKED))
        self.btn.clicked.connect(self.toggle)
        self.tree.itemChanged.connect(self.item_changed)
        ctx.projectChanged.connect(self.reload)
        ctx.selectionChanged.connect(self.rebuild)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_status)
        self.timer.start(500)
        self.update_status()

    def retranslate(self) -> None:
        self.b.run()
        self.update_status()

    def update_button(self) -> None:
        self.btn.setText(tr("log.stop") if self.ctx.logger is not None else tr("log.start"))

    def reload(self) -> None:
        self._building = True
        cfg = self.ctx.project.settings["logging"] if self.ctx.project else {}
        self.on_change.setChecked(bool(cfg.get("on_change_only")))
        self.min_interval.setValue(int(cfg.get("min_interval_ms", 0)))
        self.raw.setChecked(bool(cfg.get("log_raw")))
        self.raw_ids.setText(str(cfg.get("raw_ids", "")))
        self.filename.setText(default_log_name(self.ctx.project.name) if self.ctx.project else "")
        self._building = False
        self.rebuild()

    def rebuild(self) -> None:
        self._building = True
        stored = self.ctx.project.settings["logging"]["selection"] if self.ctx.project else {"TX": {}, "RX": {}}
        self.tree.clear()
        for direction, sel in (("TX", self.ctx.tx_sel), ("RX", self.ctx.rx_sel)):
            for msg, sigs in sel.items():
                if msg not in self.ctx.db.messages:
                    continue
                top = QTreeWidgetItem([f"{direction}: {msg}"])
                top.setData(0, Qt.ItemDataRole.UserRole, (direction, msg))
                top.setFlags(top.flags() | USER_CHECK | Qt.ItemFlag.ItemIsAutoTristate)
                self.tree.addTopLevelItem(top)
                saved = stored.get(direction, {}).get(msg, "absent")
                for sig in (self.ctx.db.signal_names(msg) if sigs is None else sigs):
                    child = QTreeWidgetItem([sig])
                    child.setFlags(child.flags() | USER_CHECK)
                    on = saved == "absent" or saved is None or sig in saved
                    child.setCheckState(0, CHECKED if on else UNCHECKED)
                    top.addChild(child)
        self.tree.expandAll()
        self._building = False

    def set_all(self, state) -> None:
        self._building = True
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setCheckState(0, state)
        self._building = False
        self.item_changed(None, 0)

    def selection(self, keep_empty: bool = False) -> dict[str, dict[str, list[str]]]:
        out: dict[str, dict[str, list[str]]] = {"TX": {}, "RX": {}}
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            direction, msg = top.data(0, Qt.ItemDataRole.UserRole)
            sigs = [top.child(j).text(0) for j in range(top.childCount())
                    if top.child(j).checkState(0) == CHECKED]
            if sigs or keep_empty:
                out[direction][msg] = sigs
        return out

    def item_changed(self, _item, _col=0) -> None:
        if self._building or not self.ctx.project:
            return
        self.ctx.project.settings["logging"]["selection"] = self.selection(keep_empty=True)

    def toggle(self) -> None:
        ctx = self.ctx
        if ctx.logger is not None:
            ctx.stop_logger()
            self.update_button()
            self.filename.setText(default_log_name(ctx.project.name))
            return
        if not ctx.mgr.connected:
            QMessageBox.information(self, tr("log.title"), tr("log.connect_first"))
            return
        sel = self.selection()
        has_other = bool(ctx.diag_def) or (ctx.project is not None and any(ctx.project.a2l_dir.glob("*.a2l")))
        if not sel["TX"] and not sel["RX"] and not self.raw.isChecked() and not has_other:
            QMessageBox.information(self, tr("log.title"), tr("log.no_data"))
            return
        name = self.filename.text().strip() or default_log_name(ctx.project.name)
        if not name.endswith(".parquet"):
            name += ".parquet"
        cfg = ctx.project.settings["logging"]
        cfg.update(selection=sel, on_change_only=self.on_change.isChecked(),
                   min_interval_ms=self.min_interval.value(), log_raw=self.raw.isChecked(),
                   raw_ids=self.raw_ids.text().strip())
        try:
            logger = ParquetLogger(ctx.db, ctx.project.log_dir / Path(name).name, sel, self.on_change.isChecked(),
                                   self.min_interval.value(), self.raw.isChecked(), raw_ids=self.raw_ids.text())
            logger.start()
        except Exception as exc:
            QMessageBox.critical(self, tr("log.title"), str(exc))
            return
        ctx.logger = logger
        ctx.mgr.add_listener(logger.on_frame, errors=self.raw.isChecked())
        self.update_button()

    def update_status(self) -> None:
        lg = self.ctx.logger
        if lg is None:
            self.status.setText("" if parquet_available() else tr("log.no_pyarrow"))
            self.update_button()
            return
        if time.monotonic() - self._last_flush > 5.0:      # regelmaessig auf Platte schreiben
            self._last_flush = time.monotonic()
            try:
                lg.flush()
            except Exception as exc:
                self.status.setText(tr("log.error", error=exc))
                return
        size = lg.path.stat().st_size if lg.path.exists() else 0
        self.status.setText(tr("log.active", name=lg.path.name, rows=lg.rows_written, size=f"{size / 1024:.0f}"))



# =========================================================================== vFlash
class FlashDialog(QDialog):
    """Fortschrittsfenster: Gesamtfortschritt, Status je Schritt, aktueller Datenblock, Protokoll."""
    runEvent = Signal(str, object)

    def __init__(self, parent, plan, channel) -> None:
        super().__init__(parent)
        self.plan, self.channel = plan, channel
        self.b = Binder()
        b = self.b
        self.setModal(True)
        self.resize(820, 620)
        self.total_steps = 0
        self.done_steps = 0
        self.finished_ok = False
        self.running = True
        self.warning = b.text(QLabel(), "flash.dlg.running")
        self.warning.setStyleSheet("font-weight: bold;")
        self.overall = QProgressBar()
        self.overall.setRange(0, 1000)
        self.current = QProgressBar()
        self.current.setRange(0, 1000)
        self.speed = QLabel("")
        self.table = QTableWidget(0, 4)
        b.table_headers(self.table, ["seq.col.num", "flash.dlg.col.step", "flash.dlg.col.status", "flash.dlg.col.detail"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        self.result = QLabel("")
        self.result.setStyleSheet("font-weight: bold;")
        self.btn_abort = b.text(QPushButton(), "flash.dlg.abort")
        self.btn_close = b.text(QPushButton(), "flash.dlg.close")
        self.btn_close.setEnabled(False)
        buttons = QHBoxLayout()
        buttons.addWidget(self.result, 1)
        buttons.addWidget(self.btn_abort)
        buttons.addWidget(self.btn_close)
        lay = QVBoxLayout(self)
        lay.addWidget(self.warning)
        lay.addWidget(b.text(QLabel(), "flash.dlg.overall"))
        lay.addWidget(self.overall)
        lay.addWidget(self.current)
        lay.addWidget(self.speed)
        lay.addWidget(self.table, 3)
        lay.addWidget(b.text(QLabel(), "flash.dlg.log"))
        lay.addWidget(self.log, 2)
        lay.addLayout(buttons)
        self.btn_abort.clicked.connect(self.abort)
        self.btn_close.clicked.connect(self.accept)
        self.runEvent.connect(self.on_event)
        self.runner = FlashRunner(channel, lambda k, p: self.runEvent.emit(k, p))
        b.call(lambda: self.setWindowTitle(tr("flash.dlg.title")))

    def start(self) -> None:
        self.runner.start(self.plan)

    def abort(self) -> None:
        self.runner.stop()
        self.btn_abort.setEnabled(False)
        self.log.appendPlainText(tr("flash.dlg.aborting"))

    def reject(self) -> None:                         # Fenster darf waehrend des Flashens nicht geschlossen werden
        if not self.running:
            super().reject()

    def closeEvent(self, event) -> None:
        event.ignore() if self.running else event.accept()

    def _colors(self, status: str) -> tuple[QColor, QColor]:
        if Theme.dark:
            table = {"ok": "#285a2b", "fail": "#6b2a2a", "aborted": "#6b4a1a", "skipped": "#444444", "running": "#1f4f7a"}
            return QColor(table.get(status, "#2d2d30")), QColor("#f0f0f0")
        table = {"ok": "#d7f5d7", "fail": "#f8d0d0", "aborted": "#fdebc8", "skipped": "#eeeeee", "running": "#d6e8fb"}
        return QColor(table.get(status, "#ffffff")), QColor("#000000")

    def _set_row(self, row: int, status: str, detail: str | None = None) -> None:
        bg, fg = self._colors(status)
        for col in range(4):
            item = self.table.item(row, col)
            if item is None:
                continue
            item.setBackground(bg)
            item.setForeground(fg)
        self.table.item(row, 2).setText(tr(f"flash.status.{status}"))
        if detail is not None:
            self.table.item(row, 3).setText(detail)

    def _update_overall(self, fraction_of_current: float = 0.0) -> None:
        if self.total_steps:
            self.overall.setValue(int(1000 * (self.done_steps + fraction_of_current) / self.total_steps))

    def on_event(self, kind: str, p: dict) -> None:
        if kind == "started":
            self.total_steps = len(p["steps"])
            self.table.setRowCount(self.total_steps)
            for i, title in enumerate(p["steps"]):
                for col, text in enumerate((str(i + 1), title, "", "")):
                    self.table.setItem(i, col, QTableWidgetItem(text))
                self._set_row(i, "pending")
            self.table.resizeColumnToContents(0)
            self.table.resizeColumnToContents(2)
        elif kind == "step_start":
            self.current.setValue(0)
            self.speed.setText("")
            self._set_row(p["index"], "running")
            self.table.scrollToItem(self.table.item(p["index"], 1))
            self.log.appendPlainText(f"[{p['index'] + 1}/{self.total_steps}] {p['title']}")
        elif kind == "progress":
            frac = p["done"] / p["total"] if p["total"] else 1.0
            self.current.setValue(int(1000 * frac))
            self.speed.setText(tr("flash.dlg.speed", done=f"{p['done'] / 1024:.1f}", total=f"{p['total'] / 1024:.1f}",
                                  speed=f"{p['speed'] / 1024:.1f}"))
            self._update_overall(frac)
        elif kind == "step_done":
            status = {"ok": "ok", "fail": "fail", "skipped": "skipped", "aborted": "aborted"}[p["status"]]
            self._set_row(p["index"], status, p["message"])
            if p["status"] == "ok":
                self.done_steps += 1
                self.current.setValue(1000)
            self._update_overall()
            if p["message"]:
                self.log.appendPlainText(f"    {p['message']}")
        elif kind == "log":
            self.log.appendPlainText(f"    {p['text']}")
        elif kind == "finished":
            rep = p["report"]
            self.running = False
            self.finished_ok = rep.status == "passed"
            if self.finished_ok:
                self.overall.setValue(1000)
            self.result.setText(tr(f"flash.result.{rep.status}", seconds=f"{rep.duration_s:.1f}"))
            self.result.setStyleSheet("font-weight: bold; color: " + (
                ("#5cd17a" if Theme.dark else "#1b7a34") if self.finished_ok else ("#ff6b6b" if Theme.dark else "#b3261e")))
            self.warning.setText("")
            self.btn_abort.setEnabled(False)
            self.btn_close.setEnabled(True)
            self.log.appendPlainText(self.result.text())


class FlashPanel(QWidget):
    """Reiter vFlash der Diagnose: Pack laden, Interpretation anzeigen, Ablauf mit Fortschrittsfenster starten."""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.b = Binder()
        b = self.b
        self.plan = None
        self.path: Path | None = None
        hint = b.text(QLabel(), "flash.hint")
        hint.setWordWrap(True)
        self.btn_load = b.text(QPushButton(), "flash.load")
        self.btn_reload = b.text(QPushButton(), "flash.reload")
        self.btn_start = b.text(QPushButton(), "flash.start")
        self.btn_start.setEnabled(False)
        self.btn_reload.setEnabled(False)
        self.path_label = QLabel("")
        self.status = QLabel("")
        self.tree = QTreeWidget()
        b.tree_headers(self.tree, ["flash.tree.header"])
        row = QHBoxLayout()
        for w in (self.btn_load, self.btn_reload, self.path_label):
            row.addWidget(w)
        row.addStretch()
        row.addWidget(self.btn_start)
        lay = QVBoxLayout(self)
        lay.addWidget(hint)
        lay.addLayout(row)
        lay.addWidget(self.status)
        lay.addWidget(self.tree, 1)
        self.btn_load.clicked.connect(self.choose)
        self.btn_reload.clicked.connect(lambda: self.load(self.path))
        self.btn_start.clicked.connect(self.start)
        self.refresh_texts()

    # -- Laden / Interpretieren
    def reload_project(self) -> None:
        self.plan, self.path = None, None
        saved = self.ctx.project.settings["diag"].get("vflash_pack", "") if self.ctx.project else ""
        if saved and Path(saved).exists():
            self.load(Path(saved), quiet=True)
        else:
            self.fill()

    def choose(self) -> None:
        start = str(self.path.parent) if self.path else ""
        path, _ = QFileDialog.getOpenFileName(self, tr("flash.load_title"), start, tr("flash.load_filter"))
        if path:
            self.load(Path(path))

    def load(self, path: Path | None, quiet: bool = False) -> None:
        if path is None:
            return
        try:
            self.plan, self.path = load_flashpack(path), Path(path)
        except (FlashPackError, OSError) as exc:
            self.plan = None
            if not quiet:
                QMessageBox.warning(self, tr("flash.load_title"), tr("flash.load_error", error=exc))
            self.fill()
            return
        if self.ctx.project:
            self.ctx.project.settings["diag"]["vflash_pack"] = str(path)
        self.fill()

    def fill(self) -> None:
        self.tree.clear()
        plan = self.plan
        self.btn_start.setEnabled(plan is not None)
        self.btn_reload.setEnabled(self.path is not None)
        self.path_label.setText(str(self.path) if self.path else "")
        if plan is None:
            self.status.setText(tr("flash.no_pack"))
            return
        self.status.setText(tr("flash.loaded", name=plan.name, blocks=len(plan.blocks),
                               kb=f"{plan.total_bytes / 1024:.1f}", steps=len(plan.expanded_steps())))
        for section, lines in plan.describe():
            if not lines:
                continue
            top = QTreeWidgetItem([tr(section)])
            for line in lines:
                top.addChild(QTreeWidgetItem([line]))
            self.tree.addTopLevelItem(top)
            top.setExpanded(True)

    def refresh_texts(self) -> None:
        self.fill()

    def retranslate(self) -> None:
        self.b.run()
        self.fill()

    # -- Ausfuehren
    def start(self) -> None:
        ctx, plan = self.ctx, self.plan
        if plan is None:
            return
        if not ctx.mgr.connected:
            QMessageBox.information(self, tr("flash.start"), tr("flash.not_connected"))
            return
        answer = QMessageBox.warning(
            self, tr("flash.confirm_title"),
            tr("flash.confirm_text", name=plan.name, blocks=len(plan.blocks), kb=f"{plan.total_bytes / 1024:.1f}"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        cfg = ctx.diag_config()
        pick = lambda value, default: default if value is None else value        # noqa: E731
        channel = IsoTpChannel(ctx.mgr, pick(plan.request_id, cfg["request_id"]),
                               pick(plan.response_id, cfg["response_id"]), pick(plan.extended, cfg["extended"]),
                               pick(plan.fd, cfg["fd"]), True, padding=cfg["padding"]).attach()
        dlg = FlashDialog(self, plan, channel)
        try:
            dlg.start()
            dlg.exec()
        finally:
            dlg.runner.stop()
            channel.detach()


# =========================================================================== Diagnose
class DiagTab(QWidget):
    logLine = Signal(str)
    busy = Signal(bool)

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.b = Binder()
        b = self.b
        self._building = False
        self.cdd = QComboBox()
        self.ecu = QComboBox()
        self.req_id, self.resp_id = QLineEdit(), QLineEdit()
        self.req_id.setMaximumWidth(90)
        self.resp_id.setMaximumWidth(90)
        self.ext = b.text(QCheckBox(), "diag.extended")
        self.fd = b.text(QCheckBox(), "diag.fd")
        self.padding = QLineEdit("AA")
        self.padding.setMaximumWidth(40)
        self.tp = b.text(QCheckBox(), "diag.tester_present")
        cfg = QHBoxLayout()
        for w in (b.text(QLabel(), "diag.cdd"), self.cdd, b.text(QLabel(), "diag.ecu"), self.ecu,
                  b.text(QLabel(), "diag.request_id"), self.req_id, b.text(QLabel(), "diag.response_id"),
                  self.resp_id, self.ext, self.fd, b.text(QLabel(), "diag.padding"), self.padding, self.tp):
            cfg.addWidget(w)
        cfg.addStretch()

        self.tree = QTreeWidget()
        b.tree_headers(self.tree, ["diag.col.service", "diag.col.request", "diag.col.description"])
        self.params = QTableWidget(0, 2)
        b.table_headers(self.params, ["diag.col.param", "diag.col.value"])
        self.params.horizontalHeader().setStretchLastSection(True)
        self.params.setMaximumHeight(150)
        self.btn_send = b.text(QPushButton(), "diag.send")
        self.raw = QLineEdit()
        b.placeholder(self.raw, "diag.raw_placeholder")
        self.btn_raw = b.text(QPushButton(), "diag.send_raw")
        raw_row = QHBoxLayout()
        raw_row.addWidget(self.raw, 1)
        raw_row.addWidget(self.btn_raw)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        b_clear = b.text(QPushButton(), "diag.clear_log")

        top = QWidget()
        tl = QVBoxLayout(top)
        tl.addWidget(self.tree, 1)
        tl.addWidget(self.params)
        tl.addWidget(self.btn_send)
        tl.addLayout(raw_row)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(top)
        lw = QWidget()
        ll = QVBoxLayout(lw)
        ll.addWidget(self.log)
        ll.addWidget(b_clear, 0, Qt.AlignmentFlag.AlignLeft)
        split.addWidget(lw)
        self.flash = FlashPanel(ctx)
        self.sub = QTabWidget()
        self.sub.addTab(split, "")
        self.sub.addTab(self.flash, "")
        b.call(lambda: (self.sub.setTabText(0, tr("diag.sub.services")), self.sub.setTabText(1, tr("diag.sub.vflash"))))
        lay = QVBoxLayout(self)
        lay.addLayout(cfg)
        lay.addWidget(self.sub, 1)

        self.cdd.activated.connect(lambda _i: self.cdd_changed())
        self.ecu.activated.connect(lambda _i: self.ecu_changed())
        for w in (self.req_id, self.resp_id, self.padding):
            w.editingFinished.connect(self.store)
        for w in (self.ext, self.fd):
            w.toggled.connect(self.store)
        self.tree.itemSelectionChanged.connect(self.fill_params)
        self.btn_send.clicked.connect(self.send_service)
        self.btn_raw.clicked.connect(self.send_raw)
        self.raw.returnPressed.connect(self.send_raw)
        b_clear.clicked.connect(self.log.clear)
        self.logLine.connect(self.log.appendPlainText)
        self.busy.connect(lambda v: (self.btn_send.setEnabled(not v), self.btn_raw.setEnabled(not v)))
        ctx.projectChanged.connect(self.reload)
        self.tp_timer = QTimer(self)
        self.tp_timer.timeout.connect(self.tester_present)
        self.tp.toggled.connect(lambda on: self.tp_timer.start(2000) if on else self.tp_timer.stop())

    def retranslate(self) -> None:
        self.b.run()
        self.flash.retranslate()

    # -- Konfiguration
    def reload(self) -> None:
        self.flash.reload_project()
        self._building = True
        p = self.ctx.project
        self.cdd.clear()
        if p:
            self.cdd.addItems([f.name for f in p.diag_files()])
            self.cdd.setCurrentText(p.settings["diag"].get("cdd", ""))
        self._fill_ecus()
        self._building = False
        self.fill_services()

    def _fill_ecus(self) -> None:
        self.ecu.clear()
        d = self.ctx.diag_def
        if d:
            self.ecu.addItems(d.ecu_names())
            self.ecu.setCurrentText(self.ctx.diag_config()["ecu"])
        cfg = self.ctx.diag_config()
        self.req_id.setText(f"{cfg['request_id']:X}")
        self.resp_id.setText(f"{cfg['response_id']:X}")
        self.ext.setChecked(cfg["extended"])
        self.fd.setChecked(cfg["fd"])
        self.padding.setText(f"{cfg['padding']:02X}" if cfg["padding"] is not None else "")

    def cdd_changed(self) -> None:
        if self.ctx.project:
            self.ctx.project.settings["diag"].update(cdd=self.cdd.currentText(), ecu="", request_id="", response_id="")
            self.ctx.load_diag_def()
            self._building = True
            self._fill_ecus()
            self._building = False
            self.fill_services()

    def ecu_changed(self) -> None:
        if self.ctx.project:
            self.ctx.project.settings["diag"].update(ecu=self.ecu.currentText(), request_id="", response_id="")
            self.ctx._invalidate_channel()
            self._building = True
            self._fill_ecus()
            self._building = False
            self.fill_services()

    def store(self, *_a) -> None:
        if self._building or not self.ctx.project:
            return
        d = self.ctx.project.settings["diag"]
        try:
            d.update(request_id=f"0x{int(self.req_id.text(), 16):X}", response_id=f"0x{int(self.resp_id.text(), 16):X}",
                     extended=self.ext.isChecked(), fd=self.fd.isChecked(),
                     padding=int(self.padding.text(), 16) if self.padding.text().strip() else None)
        except ValueError:
            self.logLine.emit(tr("diag.bad_address"))
            return
        self.ctx._invalidate_channel()

    # -- Services
    def fill_services(self) -> None:
        self.tree.clear()
        d = self.ctx.diag_def
        ecu = self.ecu.currentText()
        if d and ecu in d.ecus:
            for name, svc in d.services(ecu).items():
                it = QTreeWidgetItem([name, svc.get("request", ""), svc.get("description", "")])
                it.setData(0, Qt.ItemDataRole.UserRole, name)
                self.tree.addTopLevelItem(it)
        for c in range(3):
            self.tree.resizeColumnToContents(c)
        self.fill_params()

    def _current_service(self) -> tuple[str, dict] | None:
        items = self.tree.selectedItems()
        if not items or not self.ctx.diag_def:
            return None
        name = items[0].data(0, Qt.ItemDataRole.UserRole)
        return name, self.ctx.diag_def.services(self.ecu.currentText())[name]

    def fill_params(self) -> None:
        cur = self._current_service()
        params = cur[1].get("params", []) if cur else []
        self.params.setRowCount(len(params))
        for i, p in enumerate(params):
            n = QTableWidgetItem(p["name"])
            n.setFlags(n.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.params.setItem(i, 0, n)
            self.params.setItem(i, 1, QTableWidgetItem(f"{int(p.get('default', 0)):0{int(p.get('bytes', 1)) * 2}X}"))

    # -- Senden (im Hintergrund, GUI bleibt bedienbar)
    def send_service(self) -> None:
        cur = self._current_service()
        if not cur:
            return
        name, svc = cur
        values = {self.params.item(i, 0).text(): self.params.item(i, 1).text() for i in range(self.params.rowCount())}
        try:
            request = DiagDefinition.build_request(svc, values)
        except Exception as exc:
            self.logLine.emit(tr("diag.bad_request", error=exc))
            return
        self._start(request, name, svc)

    def send_raw(self) -> None:
        try:
            request = hex_to_bytes(self.raw.text())
            if not request:
                return
        except ValueError:
            self.logLine.emit(tr("diag.bad_hex"))
            return
        self._start(request, "RAW", None)

    def _start(self, request: bytes, name: str, svc: dict | None) -> None:
        if not self.ctx.mgr.connected:
            self.logLine.emit(tr("diag.not_connected"))
            return
        self.store()
        try:
            channel, _d, _e = self.ctx.get_diag()
        except Exception as exc:
            self.logLine.emit(tr("diag.config_error", error=exc))
            return
        self.busy.emit(True)
        threading.Thread(target=self._worker, args=(channel, request, name, svc), daemon=True).start()

    def _worker(self, channel: IsoTpChannel, request: bytes, name: str, svc: dict | None) -> None:
        stamp = time.strftime("%H:%M:%S")
        try:
            self.logLine.emit(f"{stamp}  >> {name}: {bytes_to_hex(request)}")
            resp = channel.request(request, timeout=2.0, pending_timeout=10.0)
            if resp is None:
                self.logLine.emit("      " + tr("diag.no_response_expected"))
            else:
                self.logLine.emit(f"      << {bytes_to_hex(resp.raw)}   [{resp.text}]")
                if resp.positive and svc:
                    for k, v in DiagDefinition.decode_response(svc, resp.raw, request).items():
                        self.logLine.emit(f"         {k}: {v}")
                    logger = self.ctx.logger                    # CDD: dekodierte Antwortparameter ins Parquet-Log
                    if logger is not None:
                        for d in DiagDefinition.decode_response_detail(svc, resp.raw, request):
                            logger.log_external("UDS", name, d["name"], d, "RX", message_hex=bytes_to_hex(resp.raw))
        except Exception as exc:
            self.logLine.emit("      " + tr("diag.error", error=exc))
        finally:
            self.busy.emit(False)

    def tester_present(self) -> None:
        if not self.ctx.mgr.connected:
            return
        try:
            channel, _d, _e = self.ctx.get_diag()
        except Exception:
            return
        threading.Thread(target=lambda: channel.request(bytes([0x3E, 0x80]), expect_response=False),
                         daemon=True).start()


# =========================================================================== XCP
class XcpTab(QWidget):
    """XCP on CAN: Messgroessen (zyklisch lesen) und Parameter (schreiben) aus einer konvertierten A2L-Datei."""
    logLine = Signal(str)
    valuesReady = Signal(object)

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.b = Binder()
        b = self.b
        self.db: A2lDatabase | None = None
        self.master: XcpMaster | None = None
        self.slave: SimulatedXcpSlave | None = None
        self._poll_stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._building = False
        self.watch_names_snapshot: list[str] = []
        self.a2l = QComboBox()
        self.a2l.setMinimumWidth(180)
        self.master_id, self.slave_id = QLineEdit(), QLineEdit()
        for w in (self.master_id, self.slave_id):
            w.setMaximumWidth(90)
        self.ext = b.text(QCheckBox(), "xcp.extended")
        self.btn_connect = QPushButton()
        self.status = QLabel("")
        cfg = QHBoxLayout()
        for w in (b.text(QLabel(), "xcp.a2l"), self.a2l, b.text(QLabel(), "xcp.master_id"), self.master_id,
                  b.text(QLabel(), "xcp.slave_id"), self.slave_id, self.ext, self.btn_connect):
            cfg.addWidget(w)
        cfg.addStretch()

        self.filter = b.placeholder(QLineEdit(), "xcp.filter_placeholder")
        self.tree = QTreeWidget()
        b.tree_headers(self.tree, ["xcp.col.name", "xcp.col.kind", "xcp.col.address", "xcp.col.unit"])
        self.tree.setRootIsDecorated(False)
        self.btn_add = b.text(QPushButton(), "xcp.add")
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.addWidget(b.text(QLabel(), "xcp.available"))
        ll.addWidget(self.filter)
        ll.addWidget(self.tree, 1)
        ll.addWidget(self.btn_add)

        self.watch = QTableWidget(0, 5)
        b.table_headers(self.watch, ["xcp.col.name", "xcp.col.kind", "xcp.col.raw", "xcp.col.value", "xcp.col.unit"])
        self.watch.horizontalHeader().setStretchLastSection(True)
        self.watch.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.watch.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.btn_remove = b.text(QPushButton(), "xcp.remove")
        self.poll = b.text(QCheckBox(), "xcp.poll")
        self.poll_ms = QSpinBox(); self.poll_ms.setRange(20, 10000); self.poll_ms.setValue(200); self.poll_ms.setSuffix(" ms")
        self.btn_read = b.text(QPushButton(), "xcp.read_once")
        self.write_value = b.placeholder(QLineEdit(), "xcp.write_placeholder")
        self.btn_write = b.text(QPushButton(), "xcp.write")
        ctl = QHBoxLayout()
        for w in (self.poll, self.poll_ms, self.btn_read, self.btn_remove):
            ctl.addWidget(w)
        ctl.addStretch()
        wr = QHBoxLayout()
        wr.addWidget(self.write_value, 1)
        wr.addWidget(self.btn_write)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.addWidget(b.text(QLabel(), "xcp.watch"))
        rl.addWidget(self.watch, 1)
        rl.addLayout(ctl)
        rl.addLayout(wr)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([450, 750])
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        self.log.setMaximumHeight(150)
        b_clear = b.text(QPushButton(), "diag.clear_log")
        lay = QVBoxLayout(self)
        lay.addLayout(cfg)
        lay.addWidget(self.status)
        lay.addWidget(split, 1)
        lay.addWidget(self.log)
        lay.addWidget(b_clear, 0, Qt.AlignmentFlag.AlignLeft)
        b.call(self.update_state)

        self.a2l.activated.connect(lambda _i: self.load_a2l(self.a2l.currentData()))
        self.btn_connect.clicked.connect(self.toggle_connect)
        self.filter.textChanged.connect(self.fill_tree)
        self.tree.itemDoubleClicked.connect(lambda *_a: self.add_selected())
        self.btn_add.clicked.connect(self.add_selected)
        self.btn_remove.clicked.connect(self.remove_selected)
        self.btn_read.clicked.connect(self.read_once)
        self.btn_write.clicked.connect(self.write_selected)
        self.poll.toggled.connect(self.toggle_poll)
        for w in (self.master_id, self.slave_id):
            w.editingFinished.connect(self.store)
        self.ext.toggled.connect(self.store)
        self.poll_ms.editingFinished.connect(self.store)
        b_clear.clicked.connect(self.log.clear)
        self.logLine.connect(self.log.appendPlainText)
        self.valuesReady.connect(self.show_values)
        ctx.projectChanged.connect(self.reload)
        ctx.connectionChanged.connect(self._on_connection)

    # -- Konfiguration
    def retranslate(self) -> None:
        self.b.run()
        self.fill_tree()
        self.refresh_kinds()

    def settings(self) -> dict:
        return self.ctx.project.settings["xcp"] if self.ctx.project else {}

    def reload(self) -> None:
        self.shutdown()
        self._building = True
        self.a2l.clear()
        self.db = None
        cfg = self.settings()
        files: list[Path] = []
        if self.ctx.project is not None:
            try:
                files = self.ctx.project.a2l_files()
            except Exception as exc:
                self.logLine.emit(tr("xcp.a2l_error", error=exc))
        for f in files:
            self.a2l.addItem(f.name.split(".a2l.")[0] + ".a2l", f)
        if files:
            idx = max(0, self.a2l.findText(cfg.get("a2l", "")))
            self.a2l.setCurrentIndex(idx)
        self.poll_ms.setValue(int(cfg.get("poll_ms", 200)))
        self._building = False
        self.load_a2l(self.a2l.currentData() if files else None)

    def load_a2l(self, path: Path | None) -> None:
        self.shutdown()
        self.db = None
        if path is not None:
            try:
                self.db = A2lDatabase.load(Path(path))
            except Exception as exc:
                self.logLine.emit(tr("xcp.a2l_error", error=exc))
        cfg = self.settings()
        self._building = True
        if self.db is not None:
            x = self.db.xcp
            cfg["a2l"] = self.a2l.currentText()
            master = cfg.get("master_id") or (f"{x['can_id_master']:X}" if "can_id_master" in x else "")
            slave = cfg.get("slave_id") or (f"{x['can_id_slave']:X}" if "can_id_slave" in x else "")
            self.master_id.setText(str(master))
            self.slave_id.setText(str(slave))
            self.ext.setChecked(bool(cfg.get("extended", x.get("extended", False))))
        self._building = False
        self.fill_tree()
        self.watch.setRowCount(0)
        for name in cfg.get("watch", []):
            if self.db is not None and (name in self.db.measurements or name in self.db.characteristics):
                self.add_watch(name, store=False)
        self.update_state()

    def store(self, *_a) -> None:
        if self._building or self.ctx.project is None:
            return
        cfg = self.settings()
        cfg.update(master_id=self.master_id.text().strip(), slave_id=self.slave_id.text().strip(),
                   extended=self.ext.isChecked(), poll_ms=self.poll_ms.value(),
                   watch=[self.watch.item(r, 0).text() for r in range(self.watch.rowCount())])

    def kind_text(self, kind: str) -> str:
        return tr(f"xcp.kind.{kind}")

    def fill_tree(self) -> None:
        self.tree.clear()
        if self.db is None:
            return
        text = self.filter.text().strip().lower()
        for name, kind in self.db.names():
            if text and text not in name.lower():
                continue
            item = self.db.item(name)
            addr = item.get("address")
            row = QTreeWidgetItem([name, self.kind_text(kind), f"0x{addr:X}" if addr is not None else "-",
                                   self.db.unit(item)])
            if not item.get("supported"):
                row.setToolTip(0, tr("xcp.unsupported_tip"))
                row.setDisabled(True)
            self.tree.addTopLevelItem(row)

    def refresh_kinds(self) -> None:
        if self.db is None:
            return
        for r in range(self.watch.rowCount()):
            name = self.watch.item(r, 0).text()
            self.watch.item(r, 1).setText(self.kind_text(self.db.item(name)["kind"]))

    # -- Beobachtungsliste
    def add_selected(self) -> None:
        for it in self.tree.selectedItems():
            self.add_watch(it.text(0))

    def add_watch(self, name: str, store: bool = True) -> None:
        if self.db is None or any(self.watch.item(r, 0).text() == name for r in range(self.watch.rowCount())):
            return
        item = self.db.item(name)
        r = self.watch.rowCount()
        self.watch.insertRow(r)
        for c, text in enumerate((name, self.kind_text(item["kind"]), "", "", self.db.unit(item))):
            self.watch.setItem(r, c, QTableWidgetItem(text))
        self._snapshot()
        if store:
            self.store()

    def remove_selected(self) -> None:
        for r in sorted({i.row() for i in self.watch.selectedIndexes()}, reverse=True):
            self.watch.removeRow(r)
        self._snapshot()
        self.store()

    def watch_names(self) -> list[str]:
        return [self.watch.item(r, 0).text() for r in range(self.watch.rowCount())]

    def show_values(self, values: dict) -> None:
        for r in range(self.watch.rowCount()):
            res = values.get(self.watch.item(r, 0).text())
            if res is None:
                continue
            if isinstance(res, str):
                self.watch.item(r, 2).setText("")
                self.watch.item(r, 3).setText(res)
            else:
                raw, phys = res
                self.watch.item(r, 2).setText(fmt_num(raw) if isinstance(raw, float) else str(raw))
                self.watch.item(r, 3).setText(fmt_num(phys) if isinstance(phys, (int, float)) else str(phys))

    # -- Verbindung
    def ids(self) -> tuple[int, int]:
        try:
            return int(self.master_id.text().strip(), 16), int(self.slave_id.text().strip(), 16)
        except ValueError:
            raise ValueError(tr("xcp.bad_ids")) from None

    def toggle_connect(self) -> None:
        if self.master is not None and self.master.connected:
            self.shutdown()
            self.update_state()
            return
        if self.db is None:
            QMessageBox.information(self, tr("xcp.title"), tr("xcp.no_a2l"))
            return
        if not self.ctx.mgr.connected:
            QMessageBox.information(self, tr("xcp.title"), tr("xcp.connect_first"))
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            tx, rx = self.ids()
            self.store()
            hw = self.ctx.project.settings["hardware"]
            if hw.get("type") == "simulation" and self.slave is None:
                self.slave = SimulatedXcpSlave(hw.get("sim_network", "sim"), self.db, tx, rx, self.ext.isChecked())
                self.slave.start()
            self.master = XcpMaster(self.ctx.mgr, tx, rx, self.ext.isChecked(),
                                    on_log=lambda d, data: self.logLine.emit(
                                        f"{'->' if d == 'tx' else '<-'} {data.hex(' ').upper()}")).attach()
            info = self.master.connect()
            self.logLine.emit(tr("xcp.connected_info", cto=info["max_cto"], dto=info["max_dto"], order=info["byte_order"]))
        except Exception as exc:
            QMessageBox.critical(self, tr("xcp.title"), str(exc))
            self.shutdown()
        finally:
            QApplication.restoreOverrideCursor()
        self.update_state()

    def _on_connection(self, connected: bool) -> None:
        if not connected:
            self.shutdown()
            self.update_state()

    def shutdown(self) -> None:
        """Zyklisches Lesen stoppen, XCP trennen, simulierten Slave beenden."""
        self._poll_stop.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=2.0)
            self._poll_thread = None
        if self.poll.isChecked():
            with QSignalBlocker(self.poll):
                self.poll.setChecked(False)
        if self.master is not None:
            try:
                self.master.disconnect()
            except Exception:
                pass
            self.master.detach()
            self.master = None
        if self.slave is not None:
            self.slave.stop()
            self.slave = None

    def update_state(self) -> None:
        on = self.master is not None and self.master.connected
        self.btn_connect.setText(tr("xcp.disconnect") if on else tr("xcp.connect"))
        self.status.setText(tr("xcp.state_on") if on else tr("xcp.state_off"))
        for w in (self.a2l, self.master_id, self.slave_id, self.ext):
            w.setEnabled(not on)
        for w in (self.poll, self.poll_ms, self.btn_read, self.btn_write, self.write_value):
            w.setEnabled(on)

    # -- Lesen / Schreiben
    def _read_all(self, names: list[str]) -> dict:
        out: dict = {}
        for name in names:
            try:
                out[name] = self.master.read_item(self.db, name)
                logger = self.ctx.logger                        # A2L: gelesene Werte ins Parquet-Log
                if logger is not None:
                    logger.log_external("XCP", self.db.doc.get("module") or "XCP", name,
                                        self.db.detail(name, *out[name]), "RX")
            except Exception as exc:
                out[name] = str(exc)
        return out

    def read_once(self) -> None:
        if self.master is None:
            return
        self.valuesReady.emit(self._read_all(self.watch_names()))

    def toggle_poll(self, on: bool) -> None:
        if not on:
            self._poll_stop.set()
            return
        if self.master is None:
            return
        self.store()
        self._poll_stop = threading.Event()
        stop, period = self._poll_stop, self.poll_ms.value() / 1000.0
        self._snapshot()

        def loop() -> None:
            while not stop.is_set():
                names = self.watch_names_snapshot
                if self.master is not None and names:
                    self.valuesReady.emit(self._read_all(names))
                stop.wait(period)
        self._poll_thread = threading.Thread(target=loop, name="xcp-poll", daemon=True)
        self._poll_thread.start()

    def _snapshot(self) -> None:
        """Namensliste fuer den Lese-Thread (nicht direkt auf Qt-Widgets aus dem Thread zugreifen)."""
        self.watch_names_snapshot = self.watch_names()

    def write_selected(self) -> None:
        rows = sorted({i.row() for i in self.watch.selectedIndexes()})
        if self.master is None or not rows:
            QMessageBox.information(self, tr("xcp.title"), tr("xcp.select_row"))
            return
        name, text = self.watch.item(rows[0], 0).text(), self.write_value.text().strip()
        try:
            value: float | str = text
            try:
                value = float(text)
            except ValueError:
                pass
            self.master.write_item(self.db, name, value)
            self.logLine.emit(tr("xcp.written", name=name, value=text))
            self.valuesReady.emit(self._read_all([name]))
        except Exception as exc:
            QMessageBox.warning(self, tr("xcp.title"), str(exc))


# =========================================================================== Hilfe
class HelpTab(QWidget):
    """Kapitelliste + Textanzeige mit Suche; Inhalte aus den Sprachdateien (help_content.py)."""

    def __init__(self) -> None:
        super().__init__()
        self.b = Binder()
        self.topics: list = []
        self._plain: list[str] = []
        self.list = QListWidget()
        self.list.setMaximumWidth(330)
        self.browser = QTextBrowser()
        self.browser.setOpenLinks(False)
        self.browser.setOpenExternalLinks(False)
        self.search = QLineEdit()
        self.b.placeholder(self.search, "help.search_placeholder")
        self.btn_next = self.b.text(QPushButton(), "help.next")
        self.btn_pdf = self.b.text(QPushButton(), "help.export_pdf")
        self.count = QLabel("")
        bar = QHBoxLayout()
        bar.addWidget(self.search, 1)
        bar.addWidget(self.btn_next)
        bar.addWidget(self.count)
        bar.addWidget(self.btn_pdf)
        self.browser.setSearchPaths([str(IMAGE_DIR)])
        right = QVBoxLayout()
        right.addLayout(bar)
        right.addWidget(self.browser, 1)
        lay = QHBoxLayout(self)
        lay.addWidget(self.list)
        lay.addLayout(right, 1)
        self.list.currentRowChanged.connect(self.show_topic)
        self.browser.anchorClicked.connect(self.on_link)
        self.search.textChanged.connect(self.apply_search)
        self.search.returnPressed.connect(self.find_next)
        self.btn_next.clicked.connect(self.find_next)
        self.btn_pdf.clicked.connect(self.save_manual)
        self.apply_theme()
        self.rebuild()

    def rebuild(self, keep_key: str | None = None) -> None:
        """Kapitel in der aktuellen Sprache neu aufbauen."""
        self.topics = build_topics()
        self._plain = [html_lib.unescape(re.sub(r"<[^>]+>", " ", body)).lower() for _k, _t, body in self.topics]
        with QSignalBlocker(self.list):
            self.list.clear()
            for key, title, _body in self.topics:
                item = QListWidgetItem(title)
                item.setData(Qt.ItemDataRole.UserRole, key)
                self.list.addItem(item)
        row = next((i for i, t in enumerate(self.topics) if t[0] == keep_key), 0)
        self.list.setCurrentRow(row)
        self.show_topic(row)
        self.apply_search(self.search.text())

    def retranslate(self) -> None:
        cur = self.list.currentItem()
        key = cur.data(Qt.ItemDataRole.UserRole) if cur else None
        self.b.run()
        self.rebuild(key)

    def apply_theme(self) -> None:
        """Farben der Hilfe explizit fuer Hell UND Dunkel setzen (kein Verlass auf vererbte Palette)."""
        bg, fg, link = ("#1e1e1e", "#e6e6e6", "#6eaaff") if Theme.dark else ("#ffffff", "#000000", "#0000ee")
        self.browser.setStyleSheet(f"QTextBrowser {{ padding: 6px; background-color: {bg}; color: {fg}; }}")
        pal = self.browser.palette()
        pal.setColor(QPalette.ColorRole.Base, QColor(bg))
        pal.setColor(QPalette.ColorRole.Text, QColor(fg))
        pal.setColor(QPalette.ColorRole.Link, QColor(link))
        self.browser.setPalette(pal)
        self.browser.document().setDefaultStyleSheet(help_stylesheet(Theme.dark))
        if self.topics:
            self.show_topic(self.list.currentRow())

    def show_topic(self, row: int) -> None:
        if 0 <= row < len(self.topics):
            self.browser.setHtml(self.topics[row][2])
            text = self.search.text().strip()
            if text:
                self.browser.find(text)

    def on_link(self, url: QUrl) -> None:
        text = url.toString()
        if text.startswith("topic:"):
            key = text.split(":", 1)[1]
            for i, (k, _t, _b) in enumerate(self.topics):
                if k == key:
                    self.search.clear()
                    self.list.setCurrentRow(i)
                    return

    def apply_search(self, text: str) -> None:
        text = text.strip().lower()
        hits = 0
        for i in range(self.list.count()):
            match = not text or text in self._plain[i]
            hits += bool(match and text)
            self.list.item(i).setHidden(not match)
        self.count.setText(tr("help.count", count=hits) if text else "")
        cur = self.list.currentItem()
        if cur is None or cur.isHidden():
            for i in range(self.list.count()):
                if not self.list.item(i).isHidden():
                    self.list.setCurrentRow(i)
                    return
        else:
            self.show_topic(self.list.currentRow())

    def save_manual(self) -> None:
        """Handbuch (alle Sprachen, mit Deckblatt) als PDF erzeugen und zum Speichern anbieten."""
        path, _ = QFileDialog.getSaveFileName(self, tr("help.export_title"), default_file_name(),
                                              tr("help.export_filter"))
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            export_pdf(path)
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, tr("help.export_title"), tr("help.export_error", error=exc))
            return
        QApplication.restoreOverrideCursor()
        QMessageBox.information(self, tr("help.export_title"), tr("help.export_done", path=path))

    def find_next(self) -> None:
        text = self.search.text().strip()
        if text and not self.browser.find(text):
            self.browser.moveCursor(QTextCursor.MoveOperation.Start)
            self.browser.find(text)


# =========================================================================== GUI Builder
ADD_ORDER = ("led", "text", "gauge", "background", "image")


class BuilderElement(QWidget):
    """Anzeigeelement (LED, Textfeld, Zeiger) auf der Zeichenflaeche; nur Ansicht, keine Werteeingabe."""

    GRIP = 14

    def __init__(self, tab: "BuilderTab", el: dict, parent: QWidget) -> None:
        super().__init__(parent)
        self.tab = tab
        self.el = el
        self.value: float | None = None
        self.unit = ""
        self.auto = bl.AutoScale()
        self.minmax = bl.MinMax()
        self._drag: tuple | None = None
        self._pix: QPixmap | None = None
        self._pix_path: object = "<unset>"
        self.setMouseTracking(True)
        self.setGeometry(el["x"], el["y"], el["w"], el["h"])

    # --- Werte ---------------------------------------------------------
    def set_value(self, value: float | None, unit: str) -> None:
        if self.el["kind"] == "gauge":
            self.auto.update(value)
            self.minmax.update(value)
        if value != self.value or unit != self.unit or self.el["kind"] == "gauge":
            self.value, self.unit = value, unit
            self.update()

    def scale(self) -> tuple[float, float]:
        if self.el.get("auto", True):
            return self.auto.range()
        return float(self.el["min"]), float(self.el["max"])

    # --- Zeichnen ------------------------------------------------------
    def paintEvent(self, _event) -> None:
        dark = Theme.dark
        fg = QColor("#e6e6e6" if dark else "#202020")
        panel = QColor("#2b2b2e" if dark else "#f4f4f4")
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect().adjusted(1, 1, -2, -2)
        kind = self.el["kind"]
        if kind == "background":
            col = QColor(self.el.get("color", bl.BG_COLOR))
            p.setPen(QPen(col.darker(125), 1))
            p.setBrush(col)
            p.drawRoundedRect(r, 8, 8)
        else:
            p.setPen(QPen(QColor("#6a6a70" if dark else "#b0b0b0"), 1))
            p.setBrush(panel if kind != "image" else Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r, 6, 6)
        if kind == "image":
            self._paint_image(p, r, fg)
        elif kind == "led":
            self._paint_led(p, r, fg)
        elif kind == "text":
            self._paint_text(p, r, fg)
        elif kind == "gauge":
            self._paint_gauge(p, r, fg)
        if self.tab.editing():
            p.setPen(QPen(QColor("#2a6ebe"), 1, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(self.rect().adjusted(0, 0, -1, -1))
            p.setBrush(QColor("#2a6ebe"))
            p.setPen(Qt.PenStyle.NoPen)
            g = self.GRIP
            p.drawPolygon([QPoint(self.width() - 1, self.height() - g), QPoint(self.width() - 1, self.height() - 1),
                           QPoint(self.width() - g, self.height() - 1)])
        p.end()

    def _pixmap(self) -> QPixmap | None:
        path = self.tab.resolve_image(self.el.get("path", ""))
        if path != self._pix_path:
            self._pix_path = path
            pm = QPixmap(str(path)) if path else QPixmap()
            self._pix = None if pm.isNull() else pm
        return self._pix

    def _paint_image(self, p: QPainter, r, fg: QColor) -> None:
        pm = self._pixmap()
        if pm is None:
            p.setPen(fg)
            msg = tr("builder.image_empty") if not self.el.get("path") else tr("builder.image_error")
            p.drawText(QRectF(r), Qt.AlignmentFlag.AlignCenter.value, msg)
            return
        box = r.adjusted(2, 2, -2, -2)
        scaled = pm.scaled(box.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        p.drawPixmap(box.left() + (box.width() - scaled.width()) // 2, box.top() + (box.height() - scaled.height()) // 2,
                     scaled)

    def _label(self) -> str:
        return self.el.get("text") or (self.el["signal"].split(":", 1)[-1] if self.el["signal"] else "")

    def _paint_led(self, p: QPainter, r, fg: QColor) -> None:
        base = QColor(bl.led_color(self.el, self.value))
        on = base.name().lower() != bl.NO_VALUE_COLOR
        d = min(r.height() - 14, 36)
        cx, cy = r.left() + 14 + d / 2, r.center().y()
        if on:
            glow = QRadialGradient(cx, cy, d)
            c0 = QColor(base)
            c0.setAlpha(120)
            c1 = QColor(base)
            c1.setAlpha(0)
            glow.setColorAt(0, c0)
            glow.setColorAt(1, c1)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(glow)
            p.drawEllipse(QPointF(cx, cy), d, d)
        p.setPen(QPen(base.darker(160), 2))
        p.setBrush(base)
        p.drawEllipse(QPointF(cx, cy), d / 2, d / 2)
        p.setPen(QColor(255, 255, 255, 110))
        p.setBrush(QColor(255, 255, 255, 110))
        p.drawEllipse(QPointF(cx - d / 6, cy - d / 6), d / 8, d / 8)
        p.setPen(fg)
        p.drawText(QRectF(cx + d / 2 + 8, r.top(), r.right() - cx - d / 2 - 10, r.height()),
                   Qt.AlignmentFlag.AlignVCenter.value | Qt.AlignmentFlag.AlignLeft.value, self._label())

    def _paint_text(self, p: QPainter, r, fg: QColor) -> None:
        p.setPen(fg)
        label = self.el.get("text", "")
        if self.el["signal"]:
            txt = bl.format_value(self.value, self.el.get("decimals", 2), self.unit)
            txt = f"{label} {txt}".strip() if label else txt
        else:
            txt = label
        p.drawText(QRectF(r).adjusted(8, 2, -8, -2),
                   Qt.AlignmentFlag.AlignVCenter.value | Qt.AlignmentFlag.AlignLeft.value | Qt.TextFlag.TextWordWrap.value, txt)

    def _paint_gauge(self, p: QPainter, r, fg: QColor) -> None:
        lo, hi = self.scale()
        label = self._label() if self.el.get("title_on", True) else ""
        top = 18 if label else 6
        cx = r.center().x()
        radius = min((r.width() - 40) / 2, r.height() - top - 34)
        radius = max(radius, 20)
        cy = r.top() + top + radius + 8
        arc = QRectF(cx - radius, cy - radius, 2 * radius, 2 * radius)
        p.setPen(QPen(fg, 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(arc, 0, 180 * 16)                       # Halbkreis: 180 Grad Skala
        if self.el.get("range_on"):
            band_rect = arc.adjusted(5, 5, -5, -5)
            for f0, f1, color in bl.gauge_bands(lo, hi, self.el.get("ranges", [])):
                p.setPen(QPen(QColor(color), 6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
                p.drawArc(band_rect, int((180 - 180 * f1) * 16), int(180 * (f1 - f0) * 16))
        font = p.font()
        small = QFont(font)
        small.setPointSizeF(max(6.5, font.pointSizeF() * 0.8))
        p.setFont(small)
        ticks = bl.tick_values(lo, hi, 5)
        for i, t in enumerate(ticks):
            ang = math.radians(bl.gauge_angle(i / (len(ticks) - 1)))
            x1, y1 = cx + radius * math.cos(ang), cy - radius * math.sin(ang)
            x2, y2 = cx + (radius - 8) * math.cos(ang), cy - (radius - 8) * math.sin(ang)
            p.setPen(QPen(fg, 2))
            p.drawLine(QPointF(x1, y1), QPointF(x2, y2))
            tx, ty = cx + (radius + 13) * math.cos(ang), cy - (radius + 9) * math.sin(ang)
            p.setPen(fg)
            p.drawText(QRectF(tx - 24, ty - 8, 48, 16), Qt.AlignmentFlag.AlignCenter.value, f"{t:g}")
        p.setFont(font)
        for flag, val, color in (("show_min", self.minmax.lo, "#1f77b4"), ("show_max", self.minmax.hi, "#ff7f0e")):
            if self.el.get(flag) and val is not None:                        # zweiter / dritter Zeiger: Minimum / Maximum
                a2 = math.radians(bl.gauge_angle(bl.gauge_fraction(val, lo, hi)))
                p.setPen(QPen(QColor(color), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLine(QPointF(cx, cy), QPointF(cx + (radius - 4) * math.cos(a2), cy - (radius - 4) * math.sin(a2)))
        frac = bl.gauge_fraction(self.value, lo, hi)
        ang = math.radians(bl.gauge_angle(frac))
        tip = QPointF(cx + (radius - 10) * math.cos(ang), cy - (radius - 10) * math.sin(ang))
        p.setPen(QPen(QColor("#d62728"), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(cx, cy), tip)
        p.setBrush(fg)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx, cy), 4, 4)
        p.setPen(fg)
        p.drawText(QRectF(r.left(), cy + 6, r.width(), 18), Qt.AlignmentFlag.AlignCenter.value,
                   bl.format_value(self.value, self.el.get("decimals", 1), self.unit))
        if label:
            p.drawText(QRectF(r.left(), r.top() + 1, r.width(), 16), Qt.AlignmentFlag.AlignCenter.value, label)

    # --- Verschieben / Groesse (nur im Bearbeitungsmodus) --------------
    def _in_grip(self, pos) -> bool:
        return pos.x() >= self.width() - self.GRIP and pos.y() >= self.height() - self.GRIP

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.tab.editing():
            mode = "size" if self._in_grip(event.position().toPoint()) else "move"
            self._drag = (mode, event.globalPosition().toPoint(), self.geometry())
            if self.el["kind"] != "background":
                self.raise_()

    def mouseMoveEvent(self, event) -> None:
        if not self.tab.editing():
            self.unsetCursor()
            return
        if self._drag is None:
            self.setCursor(Qt.CursorShape.SizeFDiagCursor if self._in_grip(event.position().toPoint())
                           else Qt.CursorShape.SizeAllCursor)
            return
        mode, start, geo = self._drag
        d = event.globalPosition().toPoint() - start
        if mode == "move":
            self.move(max(0, geo.x() + d.x()), max(0, geo.y() + d.y()))
        else:
            self.resize(max(40, geo.width() + d.x()), max(30, geo.height() + d.y()))

    def mouseReleaseEvent(self, _event) -> None:
        if self._drag is not None:
            self._drag = None
            self.el.update(x=self.x(), y=self.y(), w=self.width(), h=self.height())
            self.tab.store()

    def contextMenuEvent(self, event) -> None:
        self.tab.element_menu(self, event.globalPos())


class BuilderCanvas(QWidget):
    """Zeichenflaeche mit Raster im Bearbeitungsmodus."""

    def __init__(self, tab: "BuilderTab") -> None:
        super().__init__()
        self.tab = tab
        self.setMinimumSize(1600, 1000)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#1e1e1e" if Theme.dark else "#ffffff"))
        if self.tab.editing():
            p.setPen(QColor("#333338" if Theme.dark else "#ececec"))
            for x in range(0, self.width(), 20):
                p.drawLine(x, 0, x, self.height())
            for y in range(0, self.height(), 20):
                p.drawLine(0, y, self.width(), y)
        p.end()

    def contextMenuEvent(self, event) -> None:
        self.tab.canvas_menu(event.globalPos(), event.pos())


class BuilderTab(QWidget):
    """Eigene Anzeige aus LED, Textfeld und Zeiger fuer vorhandene Signale (nur Ansicht)."""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.b = Binder()
        b = self.b
        self.widgets: list[BuilderElement] = []
        self.edit = b.text(QCheckBox(), "builder.edit")
        self.edit.setChecked(True)
        b.tip(self.edit, "builder.tip")
        self.btns = {}
        bar = QHBoxLayout()
        bar.addWidget(self.edit)
        for kind in ADD_ORDER:
            btn = QPushButton()
            b.call(lambda btn=btn, kind=kind: btn.setText("+ " + tr(f"builder.add_{kind}")))
            btn.clicked.connect(lambda _c=False, kind=kind: self.add(kind))
            self.btns[kind] = btn
            bar.addWidget(btn)
        bar.addStretch(1)
        self.hint = QLabel()
        b.text(self.hint, "builder.hint")
        self.canvas = BuilderCanvas(self)
        scroll = QScrollArea()
        scroll.setWidget(self.canvas)
        scroll.setWidgetResizable(False)
        lay = QVBoxLayout(self)
        lay.addLayout(bar)
        lay.addWidget(self.hint)
        lay.addWidget(scroll, 1)
        self.edit.toggled.connect(self._edit_changed)
        ctx.projectChanged.connect(self.reload)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(100)
        self._edit_changed()

    def editing(self) -> bool:
        return self.edit.isChecked()

    def _edit_changed(self, _on: bool = True) -> None:
        for btn in self.btns.values():
            btn.setEnabled(self.editing())
        for w in self.widgets:
            w.update()
            w.unsetCursor()
        self.canvas.update()

    # --- Elemente ------------------------------------------------------
    def reload(self) -> None:
        for w in self.widgets:
            w.deleteLater()
        self.widgets = []
        els = bl.sanitize(self.ctx.project.settings.get("builder", [])) if self.ctx.project else []
        for el in els:
            self._make(el)
        self.hint.setVisible(not self.widgets)

    def _make(self, el: dict) -> BuilderElement:
        w = BuilderElement(self, el, self.canvas)
        w.show()
        self.widgets.append(w)
        self.hint.setVisible(False)
        self.restack()
        return w

    def restack(self) -> None:
        """Ebenen: Standard-Hintergrund < Hintergrundelemente < Funktionselemente."""
        for w in self.widgets:
            if w.el["kind"] == "background":
                w.lower()

    def resolve_image(self, path: str) -> Path | None:
        if not path:
            return None
        pth = Path(path)
        if not pth.is_absolute() and self.ctx.project:
            pth = self.ctx.project.root / pth
        return pth

    def add(self, kind: str, pos=None) -> None:
        n = len(self.widgets)
        x, y = (pos.x(), pos.y()) if pos is not None else (20 + 24 * (n % 10), 20 + 24 * (n % 10))
        el = bl.new_element(kind, x, y)
        if kind == "text":
            el["text"] = tr("builder.default_text")
        self._make(el)
        self.store()

    def store(self) -> None:
        if self.ctx.project:
            self.ctx.project.settings["builder"] = [dict(w.el) for w in self.widgets]

    def remove(self, w: BuilderElement) -> None:
        self.widgets.remove(w)
        w.deleteLater()
        self.hint.setVisible(not self.widgets)
        self.store()

    def tick(self) -> None:
        if not self.isVisible():
            return
        for w in self.widgets:
            sig = w.el["signal"]
            value, unit = None, ""
            if sig:
                try:
                    direction, msg, name = split_key(sig)
                    lat = self.ctx.store.latest(direction, msg, name)
                    value = lat[1] if lat else None
                    if msg in self.ctx.db.messages:
                        unit = self.ctx.db.signal_def(msg, name).get("unit", "") or ""
                except Exception:
                    value = None
            w.set_value(value, unit)

    # --- Rechtsklick-Menues --------------------------------------------
    def canvas_menu(self, global_pos, local_pos) -> None:
        if not self.editing():
            return
        m = QMenu(self)
        acts = {m.addAction("+ " + tr(f"builder.add_{k}")): k for k in ADD_ORDER}
        chosen = m.exec(global_pos)
        if chosen in acts:
            self.add(acts[chosen], local_pos)

    def _ask_color(self, el: dict, key: str, default: str) -> bool:
        col = QColorDialog.getColor(QColor(el.get(key, default)), self, tr("builder.color_title"))
        if col.isValid():
            el[key] = col.name()
            return True
        return False

    def _ask_float(self, el: dict, key: str, title_key: str) -> bool:
        v, ok = QInputDialog.getDouble(self, tr(title_key), tr("builder.x_prompt"), float(el.get(key, 0.0)),
                                       -1e12, 1e12, 3)
        if ok:
            el[key] = v
        return ok

    def _choose_image(self, w: BuilderElement) -> None:
        start = str(self.ctx.project.root) if self.ctx.project else ""
        file, _f = QFileDialog.getOpenFileName(self, tr("builder.image_title"), start, tr("builder.image_filter"))
        if not file:
            return
        target = file
        if self.ctx.project:                                # Kopie im Projekt: Projektordner bleibt austauschbar
            try:
                img_dir = self.ctx.project.root / "images"
                img_dir.mkdir(exist_ok=True)
                dest = img_dir / Path(file).name
                if dest.resolve() != Path(file).resolve():
                    stem, suffix, i = dest.stem, dest.suffix, 1
                    while dest.exists():
                        dest = img_dir / f"{stem}_{i}{suffix}"
                        i += 1
                    shutil.copy2(file, dest)
                target = f"images/{dest.name}"
            except OSError:
                target = file
        w.el["path"] = target

    def element_menu(self, w: BuilderElement, global_pos) -> None:
        el = w.el
        kind = el["kind"]
        m = QMenu(self)
        acts: dict = {}

        def add(key: str, text_key: str, checkable: bool | None = None) -> None:
            act = m.addAction(tr(text_key))
            if checkable is not None:
                act.setCheckable(True)
                act.setChecked(checkable)
            acts[act] = key

        if kind in bl.FUNCTION_KINDS:
            add("signal", "builder.menu.signal")
            add("label", "builder.menu.label" if kind != "text" else "builder.menu.text")
        if kind == "background":
            add("bg_color", "builder.menu.bg_color")
        elif kind == "image":
            add("image", "builder.menu.image")
            if el.get("path"):
                add("image_clear", "builder.menu.image_clear")
        elif kind == "led":
            m.addSeparator()
            ranged = el.get("mode", "digital") == "range"
            add("led_digital", "builder.menu.led_digital", not ranged)
            add("led_analog", "builder.menu.led_analog", ranged)
            if ranged:
                for i in range(bl.RANGE_COUNT):
                    add(f"range{i}", f"builder.menu.range{i + 1}")
            else:
                add("c0", "builder.menu.c0")
                add("c1", "builder.menu.c1")
        elif kind == "text":
            add("decimals", "builder.menu.decimals")
        elif kind == "gauge":
            add("decimals", "builder.menu.decimals")
            add("title_on", "builder.menu.title_on", bool(el.get("title_on", True)))
            m.addSeparator()
            add("auto", "builder.menu.scale_auto", bool(el.get("auto", True)))
            add("manual", "builder.menu.scale_manual")
            m.addSeparator()
            add("show_min", "builder.menu.show_min", bool(el.get("show_min")))
            add("show_max", "builder.menu.show_max", bool(el.get("show_max")))
            add("minmax_reset", "builder.menu.minmax_reset")
            m.addSeparator()
            add("range_on", "builder.menu.range_on", bool(el.get("range_on")))
            if el.get("range_on"):
                for i in range(bl.RANGE_COUNT):
                    add(f"range{i}", f"builder.menu.range{i + 1}")
        if self.editing():
            m.addSeparator()
            add("delete", "builder.menu.delete")
        chosen = m.exec(global_pos)
        action = acts.get(chosen)
        if action is None:
            return
        if action == "delete":
            self.remove(w)
            return
        if action == "signal":
            self._pick_signal(w)
        elif action == "label":
            text, ok = QInputDialog.getText(self, tr("builder.text_title"), tr("builder.text_prompt"), text=el.get("text", ""))
            if ok:
                el["text"] = text
        elif action == "bg_color":
            self._ask_color(el, "color", bl.BG_COLOR)
        elif action == "image":
            self._choose_image(w)
        elif action == "image_clear":
            el["path"] = ""
        elif action == "led_digital":
            el["mode"] = "digital"
        elif action == "led_analog":
            el["mode"] = "range"
        elif action in ("c0", "c1"):
            self._ask_color(el, action, "#888888")
        elif action.startswith("range") and action != "range_on":
            self._edit_range(w, int(action[5:]))
        elif action == "range_on":
            el["range_on"] = not el.get("range_on")
        elif action in ("show_min", "show_max"):
            el[action] = not el.get(action)
            w.minmax.reset()
        elif action == "minmax_reset":
            w.minmax.reset()
        elif action == "title_on":
            el["title_on"] = not el.get("title_on", True)
        elif action == "decimals":
            v, ok = QInputDialog.getInt(self, tr("builder.menu.decimals"), tr("builder.decimals_prompt"),
                                        int(el.get("decimals", 2)), 0, 9)
            if ok:
                el["decimals"] = v
        elif action == "auto":
            el["auto"] = not el.get("auto", True)
            w.auto.reset()
        elif action == "manual":
            self._manual_scale(w)
        self.store()
        w.update()

    def _edit_range(self, w: BuilderElement, index: int) -> None:
        """Bereich n der Skalenanzeige: min <= x < max mit Farbe."""
        rng = dict(w.el["ranges"][index])
        dlg = QDialog(self)
        dlg.setWindowTitle(tr("builder.range_title", n=index + 1))
        on = QCheckBox(tr("builder.range_active"))
        on.setChecked(bool(rng.get("on", True)))
        lo, hi = QDoubleSpinBox(), QDoubleSpinBox()
        for sp, v in ((lo, rng["min"]), (hi, rng["max"])):
            sp.setRange(-1e12, 1e12)
            sp.setDecimals(3)
            sp.setValue(v)
        color = {"value": rng.get("color", "#888888")}
        cbtn = QPushButton("          ")

        def paint_btn() -> None:
            cbtn.setStyleSheet(f"background-color: {color['value']};")
        paint_btn()

        def pick() -> None:
            col = QColorDialog.getColor(QColor(color["value"]), dlg, tr("builder.color_title"))
            if col.isValid():
                color["value"] = col.name()
                paint_btn()
        cbtn.clicked.connect(pick)
        form = QFormLayout()
        form.addRow("", QLabel(tr("builder.range_rule")))
        form.addRow("", on)
        form.addRow(tr("builder.min"), lo)
        form.addRow(tr("builder.max"), hi)
        form.addRow(tr("builder.range_color"), cbtn)
        ok, cancel = QPushButton(tr("flash.status.ok")), QPushButton(tr("flash.dlg.abort"))
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(ok)
        row.addWidget(cancel)
        v = QVBoxLayout(dlg)
        v.addLayout(form)
        v.addLayout(row)
        cancel.clicked.connect(dlg.reject)

        def accept() -> None:
            try:
                bl.manual_range(lo.value(), hi.value())
            except ValueError as exc:
                QMessageBox.warning(dlg, tr("builder.range_title", n=index + 1), tr("builder.scale_error") + f" ({exc})")
                return
            dlg.accept()
        ok.clicked.connect(accept)
        if dlg.exec():
            w.el["ranges"][index] = {"on": on.isChecked(), "min": lo.value(), "max": hi.value(), "color": color["value"]}

    def _pick_signal(self, w: BuilderElement) -> None:
        none = tr("builder.no_signal")
        items = [none] + self.ctx.selected_keys()
        cur = items.index(w.el["signal"]) if w.el["signal"] in items else 0
        text, ok = QInputDialog.getItem(self, tr("builder.signal_title"), tr("builder.signal_prompt"), items, cur, False)
        if ok:
            w.el["signal"] = "" if text == none else text
            w.auto.reset()
            w.minmax.reset()
            w.value = None

    def _manual_scale(self, w: BuilderElement) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle(tr("builder.scale_title"))
        lo = QDoubleSpinBox()
        hi = QDoubleSpinBox()
        cur_lo, cur_hi = w.scale()
        for sp, v in ((lo, cur_lo), (hi, cur_hi)):
            sp.setRange(-1e12, 1e12)
            sp.setDecimals(3)
            sp.setValue(v)
        form = QFormLayout()
        form.addRow(tr("builder.min"), lo)
        form.addRow(tr("builder.max"), hi)
        ok, cancel = QPushButton(tr("flash.status.ok")), QPushButton(tr("flash.dlg.abort"))
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(ok)
        row.addWidget(cancel)
        v = QVBoxLayout(dlg)
        v.addLayout(form)
        v.addLayout(row)
        cancel.clicked.connect(dlg.reject)

        def accept() -> None:
            try:
                bl.manual_range(lo.value(), hi.value())
            except ValueError as exc:
                QMessageBox.warning(dlg, tr("builder.scale_title"), tr("builder.scale_error") + f" ({exc})")
                return
            dlg.accept()
        ok.clicked.connect(accept)
        if dlg.exec():
            w.el.update(auto=False, min=lo.value(), max=hi.value())

    def apply_theme(self) -> None:
        self.canvas.update()
        for w in self.widgets:
            w.update()

    def retranslate(self) -> None:
        self.b.run()
        self.apply_theme()


# =========================================================================== Hauptfenster
TAB_KEYS = ["tab.project", "tab.data", "tab.trace", "tab.sequence", "tab.graph", "tab.builder", "tab.logging", "tab.diag",
            "tab.xcp", "tab.help"]


class MainWindow(QMainWindow):
    def __init__(self, base: Path, start_project: str | None = None) -> None:
        super().__init__()
        self.resize(1300, 800)
        self.ctx = AppContext(base)
        self.tabs = QTabWidget()
        self.project_tab = ProjectTab(self.ctx)
        self.page_widgets = [self.project_tab, DataTab(self.ctx), TraceTab(self.ctx), SequenceTab(self.ctx),
                             GraphTab(self.ctx), BuilderTab(self.ctx), LoggingTab(self.ctx), DiagTab(self.ctx), XcpTab(self.ctx), HelpTab()]
        for widget in self.page_widgets:
            self.tabs.addTab(widget, "")
        self.setCentralWidget(self.tabs)
        self._build_corner()
        self.retranslate()
        self.ctx.message.connect(lambda t: self.statusBar().showMessage(t, 8000))
        projects = self.ctx.pm.list()
        if not projects:
            self.ctx.pm.create("neues_projekt")
            projects = self.ctx.pm.list()
        self.project_tab.refresh_list()
        name = start_project if start_project in projects else projects[0]
        self.project_tab.refresh_list(name)
        self.ctx.open_project(name)

    # -- rechter Rand der Rubrikzeile: Dark Mode + Sprache
    def _build_corner(self) -> None:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(4, 0, 8, 0)
        self.dark_check = QCheckBox()
        self.dark_check.setChecked(Theme.dark)
        self.dark_check.toggled.connect(self.set_dark)
        self.lang_label = QLabel()
        self.lang_combo = QComboBox()
        for code, name in available_languages():
            self.lang_combo.addItem(name, code)
        self.lang_combo.setCurrentIndex(max(0, self.lang_combo.findData(current_language())))
        self.lang_combo.activated.connect(lambda _i: self.set_language(self.lang_combo.currentData()))
        for w in (self.dark_check, self.lang_label, self.lang_combo):
            row.addWidget(w)
        self.tabs.setCornerWidget(box, Qt.Corner.TopRightCorner)

    def set_dark(self, on: bool) -> None:
        apply_theme(QApplication.instance(), on)
        save_setting("dark", bool(on))
        for w in self.page_widgets:
            if hasattr(w, "apply_theme"):
                w.apply_theme()
        for w in self.page_widgets:                      # Tabellen-/Statusfarben neu setzen
            if hasattr(w, "retranslate"):
                w.retranslate()

    def set_language(self, code: str) -> None:
        if code and set_language(code):
            save_setting("language", code)
            self.retranslate()

    def retranslate(self) -> None:
        self.setWindowTitle(f"{tr('app.title')}  [{os_label()}]")
        for i, key in enumerate(TAB_KEYS):
            self.tabs.setTabText(i, tr(key))
        self.dark_check.setText(tr("header.dark"))
        self.dark_check.setToolTip(tr("header.dark_tip"))
        self.lang_label.setText(tr("header.language"))
        self.lang_combo.setToolTip(tr("header.language_tip"))
        for w in self.page_widgets:
            if hasattr(w, "retranslate"):
                w.retranslate()

    def closeEvent(self, event) -> None:
        self.ctx.disconnect()
        self.ctx.save_project()
        super().closeEvent(event)


def run_gui(base: Path, project: str | None = None, lang: str | None = None) -> int:
    app = QApplication.instance() or QApplication([])
    settings = load_settings()
    wanted = lang or settings.get("language") or "de"
    if not set_language(wanted):
        set_language("de")
    apply_theme(app, bool(settings.get("dark", False)))
    win = MainWindow(base, project)
    win.show()
    return app.exec()
