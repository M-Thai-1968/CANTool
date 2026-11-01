"""Tests der Mehrsprachigkeit. Start: python -m unittest discover -s tests -v"""
import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import i18n
from check_lang import check_language
from help_content import build_topics

KEY = re.compile(r"^(app|tab|header|project|hw|conn|msg|col|data|trace|seq|graph|log|diag|help|warn|err|cli|steps)\.[a-z_0-9.]+$")
NOT_KEYS = {"project.json", "trace.csv"}
WANTED = ["de", "en", "fr", "es", "zh", "th"]


class LangTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("de")

    def test_all_languages_present_and_valid(self):
        t = i18n.Translator()
        codes = [c for c, _n in t.available()]
        self.assertEqual(codes[0], "de")
        for code in WANTED:
            self.assertIn(code, codes)
            self.assertEqual(check_language(code, t), [], code)

    def test_every_source_key_exists_in_german(self):
        texts = i18n.Translator().texts("de")
        missing = set()
        for f in ROOT.glob("*.py"):
            for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    v = node.value
                    if KEY.match(v) and not v.endswith(".") and v not in NOT_KEYS and v not in texts:
                        missing.add(v)
        self.assertEqual(sorted(missing), [])

    def test_switching_changes_texts_and_falls_back(self):
        self.assertTrue(i18n.set_language("en"))
        self.assertEqual(i18n.tr("tab.project"), "Project")
        i18n.set_language("de")
        self.assertEqual(i18n.tr("tab.project"), "Projekt")
        self.assertFalse(i18n.set_language("xx"))
        self.assertEqual(i18n.tr("gibt.es.nicht"), "gibt.es.nicht")
        self.assertEqual(i18n.tr("err.project.exists", name="x"), "Projekt 'x' existiert bereits")

    def test_help_and_tooltips_in_every_language(self):
        from sequence_runner import STEP_HELP, step_tooltip_html
        for code in WANTED:
            i18n.set_language(code)
            self.assertEqual(len(build_topics()), 16, code)
            for kind in STEP_HELP:
                self.assertIn(kind, step_tooltip_html(kind))

    def test_names_native(self):
        names = dict(i18n.Translator().available())
        self.assertEqual(names["de"], "Deutsch")
        self.assertEqual(names["th"], "ไทย")


class ManualTests(unittest.TestCase):
    def test_manual_html_cover_languages_and_images(self):
        import datetime as dt
        import manual_builder
        from help_content import FIGURE_TOPICS, IMAGE_DIR
        self.assertEqual(i18n.current_language(), "de")
        doc = manual_builder.build_manual_html(dt.date(2026, 10, 23))
        self.assertEqual(i18n.current_language(), "de")            # Sprache wird wiederhergestellt
        self.assertIn("CAN Tool", doc)
        self.assertEqual(doc.count("2026-10-23") >= 7, True)         # Deckblatt + je Sprache
        for name in ("Deutsch", "English", "Français", "Español", "中文(简体)", "ไทย"):
            self.assertIn(name, doc)
        for code in WANTED:
            for key in FIGURE_TOPICS + ["vflash", "graph_multi", "trace_custom", "trace_custom_edit", "builder_signal", "builder_scale", "builder_range"]:
                self.assertTrue((IMAGE_DIR / f"{key}_{code}.png").exists(), f"{key}_{code}")
        self.assertEqual(doc.count("<img"), len(WANTED) * (len(FIGURE_TOPICS) + 7))
        self.assertNotIn('src="trace_', doc)                         # nur absolute file://-Verweise
        self.assertEqual(manual_builder.default_file_name(dt.date(2026, 10, 23)), "CAN_Tool_Handbuch_2026-10-23.pdf")

    def test_every_figure_has_all_explanations(self):
        from help_content import FIGURE_TOPICS, figure_html
        for code in WANTED:
            i18n.set_language(code)
            for key in FIGURE_TOPICS:
                self.assertIn("<ol><li>", figure_html(key))
        i18n.set_language("de")


if __name__ == "__main__":
    unittest.main(verbosity=2)
