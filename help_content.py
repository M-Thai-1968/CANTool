"""help_content.py - Hilfe-Inhalte (Qt-Rich-Text/HTML, ohne Qt-Abhaengigkeit).

Die Texte stehen in den Sprachdateien (lang/*.yaml, Schluessel help.topic.<kapitel>.title / .html).
Platzhalter in den HTML-Texten:
    [[pre:<name>]]          sprachneutrales Codebeispiel aus help_snippets.py
    [[commands_table]]      Befehlsuebersicht der Testsequenz (aus steps.*)
    [[commands_reference]]  vollstaendige Befehlsreferenz (aus steps.*)
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

from help_snippets import SNIPPETS
from i18n import current_language, tr
from sequence_runner import (STEP_HELP, step_fields, step_format, step_notes,
                             step_summary, step_title)

TOPIC_KEYS = ["general", "files", "project", "data", "trace", "sequence", "graph", "logging", "diag",
              "header", "help", "xcp", "builder", "appendix_a", "appendix_b", "appendix_c"]

IMAGE_DIR = Path(__file__).resolve().parent / "help_images"
EXTRA_FIGURES = {"diag": ["vflash"], "graph": ["graph_multi"], "trace": ["trace_custom", "trace_custom_edit"],
                 "builder": ["builder_signal", "builder_scale", "builder_range"]}            # weitere Abbildungen im selben Kapitel
FIGURE_TOPICS = ["project", "data", "trace", "sequence", "graph", "builder", "logging", "diag", "header", "help"]

_e = html.escape
_TABLE = '<table border="1" cellspacing="0" cellpadding="4" width="100%">'


def _pre(text: str) -> str:
    return f"<pre>{_e(text.strip(chr(10)))}</pre>"


def _table(header: list[str], rows: list[list[str]], widths: list[int] | None = None) -> str:
    out = [_TABLE, "<tr>"]
    for i, h in enumerate(header):
        w = f' width="{widths[i]}%"' if widths else ""
        out.append(f'<th{w} align="left">{_e(h)}</th>')
    out.append("</tr>")
    for row in rows:
        out.append("<tr>" + "".join(f"<td valign='top'>{cell}</td>" for cell in row) + "</tr>")
    out.append("</table>")
    return "".join(out)


def commands_table() -> str:
    rows = [[f"<code>{_e(k)}</code>", _e(step_title(k)), _e(step_summary(k))] for k in STEP_HELP]
    return _table([tr("steps.label.col_command"), tr("steps.label.col_title"), tr("steps.label.col_summary")],
                  rows, [18, 27, 55])


def commands_reference() -> str:
    parts = []
    for kind in STEP_HELP:
        rows = [[f"<code>{_e(n)}</code>", _e(t), _e(d)] for n, t, d in step_fields(kind)]
        parts.append(f"<h2>{_e(kind)} - {_e(step_title(kind))}</h2><p>{_e(step_summary(kind))}</p>")
        parts.append(f"<p><b>{_e(tr('steps.label.format'))}</b></p>" + _pre(step_format(kind)))
        parts.append(f"<p><b>{_e(tr('steps.label.elements'))}</b></p>" + _table(
            [tr("steps.label.col_element"), tr("steps.label.col_type"), tr("steps.label.col_desc")], rows, [18, 22, 60]))
        example = _pre(json.dumps(STEP_HELP[kind]["example"], ensure_ascii=False, indent=2))
        parts.append(f"<p><b>{_e(tr('steps.label.example'))}</b></p>" + example)
        notes = step_notes(kind)
        if notes:
            parts.append(f"<p><b>{_e(tr('steps.label.note'))}</b> {_e(notes)}</p>")
    return "".join(parts)


def figure_file(key: str, lang: str | None = None) -> str | None:
    """Dateiname der Abbildung zum Kapitel in der gewaehlten Sprache (Rueckfall: en, de) oder None."""
    for code in dict.fromkeys([lang or current_language(), "en", "de"]):
        name = f"{key}_{code}.png"
        if (IMAGE_DIR / name).exists():
            return name
    return None


def figure_html(key: str, img_width: int = 760) -> str:
    """Kommentierte Abbildung(en) mit nummerierten Erklaerungen (leer, wenn keine Abbildung vorhanden)."""
    if key not in FIGURE_TOPICS:
        return ""
    out = []
    for shot in [key] + EXTRA_FIGURES.get(key, []):
        name = figure_file(shot)
        if name is None:
            continue
        items = []
        i = 1
        while f"help.shot.{shot}.{i}" in _shot_keys():
            items.append(f"<li>{_e(tr(f'help.shot.{shot}.{i}'))}</li>")
            i += 1
        out.append(f"<h2>{_e(tr('help.shot.title'))}</h2>"
                   f'<p><img src="{name}" width="{img_width}"></p>'
                   f"<p><i>{_e(tr('help.shot.caption'))}</i></p><ol>{''.join(items)}</ol>")
    return "".join(out)


def _shot_keys() -> set[str]:
    from i18n import translator
    return {k for k in translator.texts("de") if k.startswith("help.shot.")}


def render_topic(key: str, figures: bool = True, img_width: int = 760) -> str:
    body = tr(f"help.topic.{key}.html")
    body = re.sub(r"\[\[pre:([a-z_]+)\]\]", lambda m: _pre(SNIPPETS[m.group(1)]), body)
    body = body.replace("[[commands_table]]", commands_table())
    body = body.replace("[[commands_reference]]", commands_reference())
    if figures:
        fig = figure_html(key, img_width)
        if fig:
            cut = body.find("</p>")
            body = (body[:cut + 4] + fig + body[cut + 4:]) if cut >= 0 else body + fig
    return body


def build_topics(figures: bool = True, img_width: int = 760) -> list[tuple[str, str, str]]:
    """[(Schluessel, Titel, HTML)] in der aktuellen Sprache."""
    return [(key, tr(f"help.topic.{key}.title"), render_topic(key, figures, img_width)) for key in TOPIC_KEYS]


def stylesheet(dark: bool) -> str:
    """Standard-Stylesheet fuer QTextDocument (Hell/Dunkel)."""
    pre_bg, th_bg = ("#2b2b2b", "#3a3a3a") if dark else ("#f2f2f2", "#e4e4e4")
    fg, link = ("#e6e6e6", "#6eaaff") if dark else ("#000000", "#0000ee")
    return (f"body {{ color: {fg}; }} a {{ color: {link}; }} pre {{ background-color: {pre_bg}; color: {fg}; }} "
            f"th {{ background-color: {th_bg}; }}")
