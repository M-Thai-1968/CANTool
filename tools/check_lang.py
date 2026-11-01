"""check_lang.py - prueft Sprachdateien gegen die Primaersprache (Deutsch).

    python tools/check_lang.py            alle Sprachen pruefen
    python tools/check_lang.py en fr      nur diese Sprachen

Geprueft wird: identische Schluessel, nicht leere Texte, gleiche {Platzhalter}, identische HTML-Struktur
(Tag-Folge, <code>-Inhalte, Links, [[Platzhalter]]) in den Hilfetexten. Rueckgabewert 1 bei Fehlern.
"""
from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from i18n import DEFAULT_LANG, LANG_DIR, Translator  # noqa: E402

PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?:[!:][^}]*)?\}")
BLOCK = re.compile(r"\[\[[^\]]+\]\]")


class _Skeleton(HTMLParser):
    """Sammelt Tag-Folge, <code>-Inhalte und Link-Ziele eines HTML-Textes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.tags: list[str] = []
        self.codes: list[str] = []
        self.links: list[str] = []
        self._in_code = False
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        for k, v in attrs:
            if k in ("href", "width", "border", "cellspacing", "cellpadding", "valign", "align"):
                (self.links if k == "href" else self.tags).append(f"{k}={v}")
        if tag == "code":
            self._in_code, self._buf = True, []

    def handle_endtag(self, tag):
        self.tags.append("/" + tag)
        if tag == "code":
            self._in_code = False
            self.codes.append("".join(self._buf))

    def handle_data(self, data):
        if self._in_code:
            self._buf.append(data)

    def handle_entityref(self, name):
        if self._in_code:
            self._buf.append(f"&{name};")

    def handle_charref(self, name):
        if self._in_code:
            self._buf.append(f"&#{name};")


def skeleton(text: str) -> _Skeleton:
    p = _Skeleton()
    p.feed(text)
    p.close()
    return p


def is_html(key: str, value: str) -> bool:
    return key.startswith("help.topic.") and key.endswith(".html")


def check_language(code: str, tr_: Translator | None = None, limit: int = 40) -> list[str]:
    tr_ = tr_ or Translator()
    problems: list[str] = []
    ref, cur = tr_.texts(DEFAULT_LANG), tr_.texts(code)
    if not cur:
        return [f"{code}: Datei nicht gefunden oder nicht lesbar ({tr_.errors.get(code, 'unbekannt')})"]
    missing = sorted(set(ref) - set(cur))
    extra = sorted(set(cur) - set(ref))
    problems += [f"{code}: Schluessel fehlt: {k}" for k in missing]
    problems += [f"{code}: Unbekannter Schluessel: {k}" for k in extra]
    for key in sorted(set(ref) & set(cur)):
        r, c = ref[key], cur[key]
        if r.strip() and not c.strip():
            problems.append(f"{code}: Leerer Text: {key}")
            continue
        if sorted(PLACEHOLDER.findall(r)) != sorted(PLACEHOLDER.findall(c)) and not is_html(key, r):
            problems.append(f"{code}: Platzhalter in '{key}' abweichend: "
                            f"{sorted(set(PLACEHOLDER.findall(r)))} <-> {sorted(set(PLACEHOLDER.findall(c)))}")
        if is_html(key, r):
            if sorted(BLOCK.findall(r)) != sorted(BLOCK.findall(c)):
                problems.append(f"{code}: [[...]]-Platzhalter in '{key}' abweichend")
            sr, sc = skeleton(r), skeleton(c)
            if sr.tags != sc.tags:
                problems.append(f"{code}: HTML-Struktur in '{key}' abweichend ({_first_diff(sr.tags, sc.tags)})")
            if sr.codes != sc.codes:
                problems.append(f"{code}: <code>-Inhalte in '{key}' abweichend ({_first_diff(sr.codes, sc.codes)})")
            if sr.links != sc.links:
                problems.append(f"{code}: Links in '{key}' abweichend")
        elif key.startswith("steps.") and key.endswith(".format"):
            for token in re.findall(r'"type": "[a-z_]+"', r):
                if token not in c:
                    problems.append(f"{code}: Format in '{key}' enthaelt {token} nicht")
    return problems[:limit] + ([f"{code}: ... {len(problems) - limit} weitere"] if len(problems) > limit else [])


def _first_diff(a: list, b: list) -> str:
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return f"Position {i}: '{x}' <-> '{y}'"
    return f"Laenge {len(a)} <-> {len(b)}"


def main(argv: list[str]) -> int:
    t = Translator(LANG_DIR)
    codes = argv or [c for c, _n in t.available() if c != DEFAULT_LANG]
    bad = 0
    for code in codes:
        problems = check_language(code, t)
        print(f"[{'OK' if not problems else 'FEHLER'}] {code}: {len(t.texts(code))} Texte")
        for p in problems:
            print("   ", p)
        bad += bool(problems)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
