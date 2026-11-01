"""main.py - Startpunkt des CAN-Tools.

  python main.py                      GUI starten
  python main.py --project demo       GUI mit bestimmtem Projekt
  python main.py --list               Projekte auflisten
  python main.py --convert demo       DBC/CDD eines Projekts nach JSON/YAML wandeln (ohne GUI)
  python main.py --convert demo --force --format yaml
  python main.py --lang en --list     Ausgaben in anderer Sprache (de, en, fr, es, zh, th)
  python main.py --manual handbuch.pdf  Handbuch (Hilfe in allen Sprachen) als PDF schreiben
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from i18n import available_languages, set_language, tr
from user_settings import load_settings

BASE = Path(__file__).resolve().parent


def _console_utf8() -> None:
    """Windows-Konsolen nutzen oft cp1252: Umlaute/Sonderzeichen der Sprachen duerfen nicht zum Absturz fuehren."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _missing_xcb_cursor() -> bool:
    """Linux/X11: Qt >= 6.5 braucht libxcb-cursor0, sonst bricht das Programm mit 'xcb' ab (Speicherabzug)."""
    import ctypes.util
    import os
    if not sys.platform.startswith("linux"):
        return False
    if os.environ.get("QT_QPA_PLATFORM") not in (None, "", "xcb"):
        return False                                          # offscreen, wayland, ... brauchen die Bibliothek nicht
    if os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("QT_QPA_PLATFORM"):
        return False
    if not os.environ.get("DISPLAY"):
        return False
    return ctypes.util.find_library("xcb-cursor") is None


def main(argv: list[str] | None = None) -> int:
    _console_utf8()
    # Sprache vor dem Aufbau der Hilfetexte bestimmen: --lang > gespeicherte GUI-Sprache > Deutsch
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--lang")
    known, _rest = pre.parse_known_args(argv)
    wanted = known.lang or load_settings().get("language") or "de"
    if not set_language(wanted):
        set_language("de")
        if known.lang:
            codes = ", ".join(code for code, _name in available_languages())
            print(tr("cli.unknown_lang", lang=known.lang, codes=codes), file=sys.stderr)

    ap = argparse.ArgumentParser(description=tr("cli.description"))
    ap.add_argument("--project", help=tr("cli.help.project"))
    ap.add_argument("--list", action="store_true", help=tr("cli.help.list"))
    ap.add_argument("--convert", metavar="PROJECT", help=tr("cli.help.convert"))
    ap.add_argument("--format", choices=["json", "yaml"], help=tr("cli.help.format"))
    ap.add_argument("--force", action="store_true", help=tr("cli.help.force"))
    ap.add_argument("--lang", help=tr("cli.help.lang"))
    ap.add_argument("--manual", metavar="PDF", help=tr("cli.help.manual"))
    args = ap.parse_args(argv)

    if args.manual:
        import os
        if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from manual_builder import export_pdf
        try:
            print(tr("cli.manual_done", path=export_pdf(args.manual)))
        except ImportError as exc:
            print(tr("cli.missing_gui", error=exc), file=sys.stderr)
            return 1
        return 0

    from project_manager import ProjectManager
    pm = ProjectManager(BASE / "projects")

    if args.list:
        print("\n".join(pm.list()) or tr("cli.no_projects"))
        return 0

    if args.convert:
        project = pm.load(args.convert)
        if args.format:
            project.settings["cache_format"] = args.format
            project.save()
        dbcs, cdds = project.convert(force=args.force)
        for p in dbcs + cdds:
            print(tr("cli.converted", path=p.relative_to(BASE)))
        for p in project.a2l_files(args.force):
            print(tr("cli.converted", path=p.relative_to(BASE)))
        db = project.load_dbc()
        print(tr("cli.summary", messages=len(db.messages), files=len(db.sources)))
        for w in db.warnings:
            print(tr("cli.warning", text=w))
        return 0

    if _missing_xcb_cursor():
        print(tr("cli.missing_xcb_cursor"), file=sys.stderr)
        return 1
    try:
        from gui import run_gui
    except ImportError as exc:
        print(tr("cli.missing_gui", error=exc), file=sys.stderr)
        return 1
    return run_gui(BASE, args.project, known.lang)


if __name__ == "__main__":
    sys.exit(main())
