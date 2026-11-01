"""user_settings.py - benutzerbezogene Einstellungen (Sprache, Hell/Dunkel), unabhaengig von Qt.

Gespeichert in ``~/.can_tool/settings.json`` (Pfad per Umgebungsvariable ``CAN_TOOL_SETTINGS`` aenderbar).
Lesen/Schreiben schlaegt nie fehl: bei Problemen gelten die Standardwerte.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def settings_path() -> Path:
    env = os.environ.get("CAN_TOOL_SETTINGS")
    return Path(env) if env else Path.home() / ".can_tool" / "settings.json"


def load_settings() -> dict[str, Any]:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_setting(key: str, value: Any) -> None:
    try:
        data = load_settings()
        data[key] = value
        path = settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
