"""Tests fuer Befehlsreferenz (Tooltips) und Hilfe-Inhalte. Start: python -m unittest discover -s tests -v"""
import re
import shutil
import sys
import tempfile
import time
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from can_manager import CanManager
from cdd_manager import convert_cdd_file
from dbc_manager import DbcDatabase, convert_dbc_file
from help_content import build_topics
from i18n import set_language, tr
from iso_tp_manager import DiagDefinition, IsoTpChannel, SimulatedEcu
from sequence_runner import (STEP_HELP, STEP_TEMPLATES, SequenceRunner, step_example_json,
                             step_format, step_summary, step_title, step_tooltip_html, validate_sequence)

DEMO = ROOT / "projects" / "demo"
VOID = {"br", "hr", "img", "meta", "link", "input"}


class _Balance(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.errors, self.links = [], [], []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self.stack.append(tag)
        if tag == "a":
            self.links += [v for k, v in attrs if k == "href"]

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"</{tag}> passt nicht zu {self.stack[-1:]}")
            if tag in self.stack:
                while self.stack and self.stack.pop() != tag:
                    pass
        else:
            self.stack.pop()


def check_html(text):
    p = _Balance()
    p.feed(text)
    p.close()
    return p.errors + [f"ungeschlossen: {t}" for t in p.stack], p.links


class StepHelpTests(unittest.TestCase):
    def setUp(self):
        set_language("de")

    def test_every_step_documented(self):
        self.assertEqual(list(STEP_HELP), list(STEP_TEMPLATES))

    def test_examples_match_fields(self):
        for kind, h in STEP_HELP.items():
            ex = h["example"]
            self.assertEqual(ex["type"], kind)
            names = {n for n, _t, _r in h["fields"]}
            self.assertLessEqual(set(ex) - {"type"}, names, kind)
            required = {n for n, _t, req in h["fields"] if req}
            self.assertLessEqual(required, set(ex), kind)
            self.assertTrue(step_summary(kind) and step_format(kind) and step_title(kind))
            self.assertNotEqual(step_title(kind), f"steps.{kind}.title")
            self.assertIn(f'"type": "{kind}"', step_format(kind))
            for name, _t, _r in h["fields"]:
                self.assertNotEqual(tr(f"steps.{kind}.fields.{name}"), f"steps.{kind}.fields.{name}")

    def test_tooltip_content_and_wellformed(self):
        for kind, h in STEP_HELP.items():
            tip = step_tooltip_html(kind)
            errors, _ = check_html(tip)
            self.assertEqual(errors, [], kind)
            for needle in ("Format", "Elemente", "Beispiel", kind):
                self.assertIn(needle, tip)
            for name, _t, _r in h["fields"]:
                self.assertIn(name, tip)
            self.assertIn("&lt;", tip)                       # Platzhalter <...> sind maskiert
            self.assertTrue(step_example_json(kind).startswith("{"))


class HelpContentTests(unittest.TestCase):
    def setUp(self):
        set_language("de")

    def test_topics(self):
        topics = build_topics()
        keys = [k for k, _t, _b in topics]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(keys[:2], ["general", "files"])
        titles = " ".join(t for _k, t, _b in topics)
        for part in ("Projekt", "Daten", "Trace", "Testsequenz", "Graph", "Logging", "Diagnose", "Hilfe"):
            self.assertIn(f"Rubrik {part}", titles)
        all_links = []
        for key, title, body in topics:
            errors, links = check_html(body)
            self.assertEqual(errors, [], f"{key}: {errors[:3]}")
            self.assertGreater(len(body), 500, key)
            all_links += links
        for link in all_links:
            self.assertTrue(link.startswith("topic:") and link[6:] in keys, link)

    def test_appendix_covers_all_commands(self):
        body = dict((k, b) for k, _t, b in build_topics())["appendix_a"]
        for kind in STEP_HELP:
            self.assertIn(f"<h2>{kind} - ", body)
        seq = dict((k, b) for k, _t, b in build_topics())["sequence"]
        for kind in STEP_HELP:
            self.assertIn(f"<code>{kind}</code>", seq)

    def test_documented_dbc_and_cdd_snippets_parse(self):
        """Die Beispiele aus Kapitel 2 muessen von den Parsern wirklich verstanden werden."""
        from cdd_manager import parse_cdd_text
        from dbc_manager import parse_dbc_text
        body = dict((k, b) for k, _t, b in build_topics())["files"]
        import html as h
        blocks = [h.unescape(m) for m in re.findall(r'<pre[^>]*>(.*?)</pre>', body, re.S)]
        dbc = next(b for b in blocks if b.lstrip().startswith("VERSION"))
        doc = parse_dbc_text(dbc)
        self.assertEqual(doc["messages"]["EngineData"]["cycle_time_ms"], 10)
        self.assertEqual(doc["messages"]["EngineData"]["signals"]["CoolantTemp"]["offset"], -40)
        cdd = next(b for b in blocks if "<DIAGDATA>" in b)
        d = parse_cdd_text(cdd.strip())
        svc = d["ecus"]["Engine_ECU"]["services"]
        self.assertEqual(set(svc), {"ExtendedSession", "ReadVIN", "ReadSpeed"})
        self.assertEqual(DiagDefinition.build_request(svc["ExtendedSession"], {"session": 3}), bytes([0x10, 3]))
        import json
        for b in blocks:                                      # alle JSON-Beispiele gueltig
            if b.lstrip().startswith("{") and '"steps"' in b or '"format_version"' in b or '"cache_format"' in b:
                json.loads(b)


class ExamplesRunTests(unittest.TestCase):
    """Alle Beispiele aus den Tooltips laufen gegen das Demo-Projekt durch."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = DbcDatabase()
        for p in sorted((DEMO / "dbc").glob("*.dbc")):
            self.db.add_file(convert_dbc_file(p, self.tmp))
        self.defn = DiagDefinition.load(convert_cdd_file(DEMO / "cdd" / "diag.cdd", self.tmp))
        net = f"h{time.time_ns()}"
        self.ecu = SimulatedEcu(net, db=self.db)
        self.ecu.start()
        self.mgr = CanManager(self.db)
        self.mgr.connect({"type": "simulation", "sim_network": net, "sim_echo": True})
        self.ch = IsoTpChannel(self.mgr, 0x7E0, 0x7E8).attach()

    def tearDown(self):
        self.mgr.disconnect()
        self.ecu.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_all_examples_pass(self):
        order = ["comment", "enable_tx", "set_cycle", "set_signal", "send_once", "send_raw", "wait", "wait_for",
                 "check", "diag", "diag_raw", "loop"]
        self.assertEqual(set(order), set(STEP_HELP))
        seq = {"name": "examples", "steps": [STEP_HELP[k]["example"] for k in order]}
        self.assertEqual(validate_sequence(seq, self.mgr), [])
        runner = SequenceRunner(self.mgr, lambda: (self.ch, self.defn, "Engine_ECU"))
        runner.start(seq)
        runner.join(30)
        bad = [r for r in runner.report.results if r.status != "ok"]
        self.assertEqual(runner.report.status, "passed", bad)

    def test_set_signal_with_text_value(self):
        seq = {"name": "t", "steps": [
            {"type": "set_signal", "message": "VehicleSpeed", "signal": "Gear", "value": "R"},
            {"type": "send_once", "message": "VehicleSpeed"}]}
        runner = SequenceRunner(self.mgr)
        runner.start(seq)
        runner.join(5)
        self.assertEqual(runner.report.status, "passed")
        self.assertEqual(self.db.decode("VehicleSpeed", self.mgr.db.encode(
            "VehicleSpeed", self.mgr.tx_entries["VehicleSpeed"].values))["Gear"], 15)


if __name__ == "__main__":
    unittest.main(verbosity=2)
