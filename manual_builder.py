"""manual_builder.py - Handbuch (Hilfe in allen verfuegbaren Sprachen) als HTML und PDF.

build_manual_html()  baut ein einziges HTML-Dokument: Deckblatt (Programmname, Ausgabedatum, Sprachen) und je Sprache
                     alle Hilfekapitel mit den kommentierten Abbildungen. Ohne Qt-Abhaengigkeit.
export_pdf()         schreibt das Dokument mit Qt (QTextDocument + QPdfWriter) als A4-PDF.
"""
from __future__ import annotations

import datetime as dt
import html
import re
from pathlib import Path

from help_content import IMAGE_DIR, build_topics, stylesheet
from i18n import available_languages, current_language, set_language, tr

PROGRAM_NAME = "CAN Tool"
PAGE_BREAK = ' style="page-break-before: always"'


def _absolute_images(body: str) -> str:
    """Bildverweise (nur Dateiname) in file://-URLs wandeln, damit jedes Ausgabeverfahren sie findet."""
    return re.sub(r'src="([^"/]+\.png)"', lambda m: f'src="{(IMAGE_DIR / m.group(1)).resolve().as_uri()}"', body)


PDF_MARGIN_MM = 8          # schmale Raender fuer bessere Darstellung (breitere Tabellen/Abbildungen)
PDF_IMG_WIDTH = 720        # Abbildungsbreite in px passend zur A4-Nutzbreite (194 mm bei 8 mm Rand)


def build_manual_html(date: dt.date | None = None, img_width: int = PDF_IMG_WIDTH) -> str:
    date = date or dt.date.today()
    stamp = date.isoformat()
    languages = available_languages()
    previous = current_language()
    cover_titles, sections = [], []
    try:
        for code, name in languages:
            set_language(code)
            cover_titles.append((name, tr("manual.title"), tr("manual.date", date=stamp)))
            topics = build_topics(figures=True, img_width=img_width)
            toc = "".join(f"<p>{html.escape(title)}</p>" for _k, title, _b in topics)
            parts = [f'<h1{PAGE_BREAK} align="center">{html.escape(name)} &ndash; {html.escape(tr("manual.title"))}</h1>'
                     f'<p align="center">{html.escape(PROGRAM_NAME)} &middot; {html.escape(tr("manual.date", date=stamp))}</p>'
                     f"{toc}"]
            for _key, _title, body in topics:
                body = body.replace("<h1>", f"<h1{PAGE_BREAK}>", 1) if "<h1>" in body else \
                    f"<h1{PAGE_BREAK}>{html.escape(_title)}</h1>" + body
                parts.append(_absolute_images(body))
            sections.append("".join(parts))
    finally:
        set_language(previous)

    lines = "".join(
        f'<p align="center" style="font-size: 12pt"><b>{html.escape(name)}</b>: {html.escape(title)} &ndash; '
        f"{html.escape(date_text)}</p>" for name, title, date_text in cover_titles)
    cover = (f'<p align="center" style="font-size: 12pt">&nbsp;</p><p align="center" style="font-size: 12pt">&nbsp;</p>'
             f'<p align="center" style="font-size: 12pt">&nbsp;</p><p align="center" style="font-size: 12pt">&nbsp;</p>'
             f'<p align="center" style="font-size: 44pt"><b>{html.escape(PROGRAM_NAME)}</b></p>'
             f'<p align="center" style="font-size: 22pt">{" / ".join(html.escape(t) for _n, t, _d in cover_titles)}</p>'
             f'<p align="center" style="font-size: 16pt">{html.escape(tr("manual.date", date=stamp))}</p>'
             f'<p align="center" style="font-size: 12pt">&nbsp;</p>'
             f'<p align="center" style="font-size: 12pt"><b>{html.escape(tr("manual.languages"))}</b></p>{lines}')
    css = stylesheet(False) + " body { font-family: sans-serif; } table { margin-bottom: 6px; }"
    return (f'<html><head><meta charset="utf-8"><title>{html.escape(PROGRAM_NAME)} &ndash; '
            f'{html.escape(tr("manual.title"))} {stamp}</title><style>{css}</style></head><body>'
            f"{cover}{''.join(sections)}</body></html>")


def default_file_name(date: dt.date | None = None) -> str:
    return f"CAN_Tool_Handbuch_{(date or dt.date.today()).isoformat()}.pdf"


def export_pdf(path: str | Path, date: dt.date | None = None) -> Path:
    """Handbuch mit Qt als A4-PDF schreiben (benoetigt PySide6; Schriften kommen vom System)."""
    from PySide6.QtCore import QMarginsF
    from PySide6.QtGui import QGuiApplication, QPageLayout, QPageSize, QPdfWriter, QTextDocument

    app = QGuiApplication.instance() or QGuiApplication([])          # noqa: F841  (fuer Schriften noetig)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = QTextDocument()
    doc.setHtml(build_manual_html(date, PDF_IMG_WIDTH))
    writer = QPdfWriter(str(path))
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setPageMargins(QMarginsF(PDF_MARGIN_MM, PDF_MARGIN_MM, PDF_MARGIN_MM, PDF_MARGIN_MM), QPageLayout.Unit.Millimeter)
    writer.setTitle(f"{PROGRAM_NAME} - {tr('manual.title')}")
    writer.setCreator(PROGRAM_NAME)
    doc.print_(writer)
    return path
