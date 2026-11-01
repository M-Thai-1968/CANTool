"""project_manager.py - Verwaltung mehrerer Projekte.

projects/<name>/
    project.json   Einstellungen (Hardware, TX/RX-Auswahl, Logging, Graph, ...)
    dbc/ cdd/ a2l/ sequences/   Quelldateien
    cache/         konvertierte JSON/YAML-Dateien (automatisch erzeugt)
    logs/          Parquet-Logs
"""
from __future__ import annotations

import copy
import json
import re
import shutil
from pathlib import Path

from a2l_manager import convert_all_a2l
from cdd_manager import convert_all_cdds
from dbc_manager import DbcDatabase, convert_all_dbcs
from i18n import tr

SUBDIRS = ("dbc", "cdd", "a2l", "sequences", "cache", "logs")

DEFAULT_SETTINGS: dict = {
    "hardware": {"type": "simulation", "channel": "", "bitrate": 500000, "fd": False,
                 "data_bitrate": 2000000, "app_name": "", "sim_network": "sim", "sim_echo": True},
    "cache_format": "json",
    "tx": [],               # [{"message", "cycle_ms", "enabled", "values": {}, "signals": [..]|None}]
    "rx": {},               # {"Msg": [Signale] | None}
    "logging": {"selection": {"TX": {}, "RX": {}}, "on_change_only": False, "min_interval_ms": 0,
                "log_raw": False, "raw_ids": ""},
    "graph": [],
    "custom_tx": [],        # [{"id", "dlc", "data", "cycle_ms", "cyclic"}] Trace > Selbst definiert
    "graph_multi": False,
    "builder": [],          # GUI Builder: [{"id","kind","x","y","w","h","signal","text",...}]
    "diag": {"cdd": "", "ecu": "", "request_id": "", "response_id": "", "fd": False,
             "padding": 170, "tester_present": False, "vflash_pack": ""},
    "xcp": {"a2l": "", "master_id": "", "slave_id": "", "extended": False, "poll_ms": 200, "watch": []},
    "sequence": "",
    "sim": {"ecu": True, "traffic_senders": ["ECU"]},
}

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\- ]{0,62}[A-Za-z0-9_\-]$|^[A-Za-z0-9]$")
# unter Windows reservierte Geraetenamen: Projektordner muss auf beiden Systemen anlegbar sein
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def _merge(base: dict, new: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in new.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) \
            and k in ("hardware", "logging", "diag", "sim", "xcp") else v
    return out


class Project:
    def __init__(self, root: Path, settings: dict) -> None:
        self.root = Path(root)
        self.settings = settings

    @property
    def name(self) -> str:
        return self.root.name

    @property
    def dbc_dir(self) -> Path: return self.root / "dbc"
    @property
    def cdd_dir(self) -> Path: return self.root / "cdd"
    @property
    def a2l_dir(self) -> Path: return self.root / "a2l"
    @property
    def seq_dir(self) -> Path: return self.root / "sequences"
    @property
    def cache_dir(self) -> Path: return self.root / "cache"
    @property
    def log_dir(self) -> Path: return self.root / "logs"

    def save(self) -> None:
        (self.root / "project.json").write_text(
            json.dumps(self.settings, indent=2, ensure_ascii=False), encoding="utf-8")

    def convert(self, force: bool = False) -> tuple[list[Path], list[Path]]:
        fmt = self.settings.get("cache_format", "json")
        return (convert_all_dbcs(self.dbc_dir, self.cache_dir, fmt, force),
                convert_all_cdds(self.cdd_dir, self.cache_dir, fmt, force))

    def load_dbc(self) -> DbcDatabase:
        db = DbcDatabase()
        for path in self.convert()[0]:
            db.add_file(path)
        return db

    def diag_files(self) -> list[Path]:
        self.convert()
        return sorted(p for p in self.cache_dir.glob("*.cdd.*") if p.suffix in (".json", ".yaml", ".yml"))

    def a2l_files(self, force: bool = False) -> list[Path]:
        """Konvertierte A2L-Dateien (cache/<name>.a2l.json|yaml); wird bei Bedarf neu konvertiert."""
        return convert_all_a2l(self.a2l_dir, self.cache_dir, self.settings.get("cache_format", "json"), force)

    def sequence_files(self) -> list[Path]:
        return sorted(self.seq_dir.glob("*.json"))

    def import_file(self, src: Path) -> Path:
        """Kopiert eine .dbc, .cdd oder .a2l in das Projekt."""
        src = Path(src)
        target_dir = {".dbc": self.dbc_dir, ".cdd": self.cdd_dir, ".a2l": self.a2l_dir}.get(src.suffix.lower())
        if target_dir is None:
            raise ValueError(tr("err.project.bad_ext"))
        target_dir.mkdir(parents=True, exist_ok=True)
        dest = target_dir / src.name
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
        return dest


class ProjectManager:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def list(self) -> list[str]:
        return sorted(p.parent.name for p in self.root.glob("*/project.json"))

    def create(self, name: str) -> Project:
        name = name.strip()
        if not _NAME_RE.match(name) or name.split(".")[0].upper() in _RESERVED:
            raise ValueError(tr("err.project.name"))
        path = self.root / name
        if path.exists():
            raise FileExistsError(tr("err.project.exists", name=name))
        for sub in SUBDIRS:
            (path / sub).mkdir(parents=True)
        project = Project(path, copy.deepcopy(DEFAULT_SETTINGS))
        project.save()
        return project

    def load(self, name: str) -> Project:
        path = self.root / name
        cfg = path / "project.json"
        if not cfg.exists():
            raise FileNotFoundError(tr("err.project.not_found", name=name))
        project = Project(path, _merge(DEFAULT_SETTINGS, json.loads(cfg.read_text(encoding="utf-8"))))
        for sub in SUBDIRS:
            (path / sub).mkdir(exist_ok=True)
        return project

    def delete(self, name: str) -> None:
        path = self.root / name
        if (path / "project.json").exists():
            shutil.rmtree(path)
