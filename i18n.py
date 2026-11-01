"""i18n.py - Sprachverwaltung.

Jede Sprache liegt in einer eigenen Datei im Ordner ``lang/`` (``de.yaml``, ``en.yaml``, ... oder ``*.json``).
Aufbau einer Sprachdatei (Abschnitte dienen nur der Ordnung, alle Schluessel bilden einen gemeinsamen Namensraum):

    meta:   name (Anzeigename in der eigenen Sprache), order (Reihenfolge im Dropdown)
    ui:     Texte der Oberflaeche
    core:   Meldungen aus den Kernmodulen (Fehler, Warnungen, Sequenz-Ausgaben)
    steps:  Befehlsreferenz der Testsequenz (Tooltips, Hilfe-Anhang)
    help:   Kapitel der Hilfe (HTML)

Deutsch (``de``) ist die Primaersprache und Rueckfallebene: Fehlt ein Schluessel in der gewaehlten Sprache, wird der
deutsche Text verwendet. Platzhalter in Texten: ``{name}`` (str.format).

Neue Sprache hinzufuegen: ``lang/de.yaml`` kopieren, uebersetzen, als ``lang/<code>.yaml`` speichern und mit
``python tools/check_lang.py <code>`` pruefen. Sie erscheint danach automatisch im Sprach-Dropdown.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

LANG_DIR = Path(__file__).resolve().parent / "lang"
DEFAULT_LANG = "de"
META_SECTION = "meta"
SUFFIXES = (".yaml", ".yml", ".json")


def _flatten(data: dict, out: dict[str, str] | None = None, prefix: str = "") -> dict[str, str]:
    """Alle Abschnitte zu einem flachen Schluesselraum zusammenfassen (verschachtelte Dicts -> 'a.b')."""
    out = {} if out is None else out
    for key, value in data.items():
        full = f"{prefix}{key}"
        if isinstance(value, dict):
            _flatten(value, out, full + ".")
        else:
            out[full] = "" if value is None else str(value)
    return out


def read_language_file(path: Path) -> dict[str, Any]:
    """Liest eine Sprachdatei (YAML oder JSON) als Roh-Dictionary."""
    text = Path(path).read_text(encoding="utf-8")
    if Path(path).suffix.lower() == ".json":
        data = json.loads(text)
    else:
        import yaml
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: Oberste Ebene muss ein Mapping sein")
    return data


def flatten_language(data: dict[str, Any]) -> tuple[dict, dict[str, str]]:
    """-> (meta, flache Texte). Der Abschnitt 'meta' gehoert nicht zu den Texten."""
    meta = dict(data.get(META_SECTION) or {})
    body = {k: v for k, v in data.items() if k != META_SECTION}
    texts: dict[str, str] = {}
    for section, content in body.items():
        if isinstance(content, dict):
            _flatten(content, texts)
        else:
            texts[section] = "" if content is None else str(content)
    return meta, texts


class Translator:
    def __init__(self, lang_dir: Path = LANG_DIR, default: str = DEFAULT_LANG) -> None:
        self.lang_dir = Path(lang_dir)
        self.default = default
        self.lang = default
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[dict, dict[str, str]]] = {}
        self.errors: dict[str, str] = {}

    # -- Dateien
    def files(self) -> dict[str, Path]:
        found: dict[str, Path] = {}
        if self.lang_dir.is_dir():
            for suffix in SUFFIXES:                       # yaml hat Vorrang vor json
                for p in sorted(self.lang_dir.glob(f"*{suffix}")):
                    found.setdefault(p.stem, p)
        return found

    def _load(self, code: str) -> tuple[dict, dict[str, str]] | None:
        with self._lock:
            if code in self._cache:
                return self._cache[code]
            path = self.files().get(code)
            if path is None:
                return None
            try:
                loaded = flatten_language(read_language_file(path))
            except Exception as exc:
                self.errors[code] = f"{path.name}: {exc}"
                return None
            self._cache[code] = loaded
            return loaded

    def reload(self) -> None:
        with self._lock:
            self._cache.clear()
            self.errors.clear()

    # -- Sprachen
    def available(self) -> list[tuple[str, str]]:
        """[(Code, Anzeigename)] - Primaersprache zuerst, dann nach meta.order, dann nach Code."""
        items = []
        for code in self.files():
            loaded = self._load(code)
            if loaded is None:
                continue
            meta = loaded[0]
            order = meta.get("order", 100)
            items.append((0 if code == self.default else 1, order if isinstance(order, int) else 100,
                          code, str(meta.get("name", code))))
        items.sort()
        return [(code, name) for _a, _b, code, name in items]

    def set_language(self, code: str) -> bool:
        if self._load(code) is None:
            return False
        self.lang = code
        return True

    # -- Texte
    def texts(self, code: str) -> dict[str, str]:
        loaded = self._load(code)
        return loaded[1] if loaded else {}

    def has(self, key: str) -> bool:
        return key in self.texts(self.lang) or key in self.texts(self.default)

    def tr(self, _key: str, /, **kwargs: Any) -> str:
        text = self.texts(self.lang).get(_key)
        if text is None:
            text = self.texts(self.default).get(_key)
        if text is None:
            return _key
        if kwargs:
            try:
                return text.format(**kwargs)
            except (KeyError, IndexError, ValueError):
                return text
        return text


translator = Translator()


def tr(_key: str, /, **kwargs: Any) -> str:
    """Text zum Schluessel in der aktuellen Sprache."""
    return translator.tr(_key, **kwargs)


def set_language(code: str) -> bool:
    return translator.set_language(code)


def current_language() -> str:
    return translator.lang


def available_languages() -> list[tuple[str, str]]:
    return translator.available()
