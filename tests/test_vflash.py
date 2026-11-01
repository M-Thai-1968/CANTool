"""Tests fuer vFlash-Pack: Laden/Interpretieren und Ausfuehren gegen die simulierte ECU."""
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import i18n
from can_manager import CanManager
from dbc_manager import DbcDatabase
from iso_tp_manager import IsoTpChannel, SimulatedEcu
from make_demo_flashpack import build, intel_hex
from vflash_manager import (FlashPackError, FlashRunner, compute_key, load_flashpack, parse_intel_hex,
                            parse_srec)


def srec(address: int, data: bytes) -> str:
    rec = bytes([len(data) + 5]) + address.to_bytes(4, "big") + data
    return "S3" + (rec + bytes([(~sum(rec)) & 0xFF])).hex().upper() + "\nS70500000000FA\n"


class ParserTests(unittest.TestCase):
    def test_intel_hex_roundtrip_and_checksum(self):
        data = bytes(range(100))
        self.assertEqual(parse_intel_hex(intel_hex(0x0001FFF0, data)), [(0x0001FFF0, data)])   # ueber 64-kB-Grenze
        bad = intel_hex(0x100, data).replace(":10", ":11", 1)
        with self.assertRaises(FlashPackError):
            parse_intel_hex(bad, "x.hex")

    def test_srec(self):
        data = bytes(range(40))
        self.assertEqual(parse_srec(srec(0x8000_0000, data)), [(0x8000_0000, data)])
        with self.assertRaises(FlashPackError):
            parse_srec(srec(0x100, data).replace("S3", "S3 ", 1) + "S3xx\n", "x.s19")


class PackTests(unittest.TestCase):
    def setUp(self):
        i18n.set_language("de")
        self.tmp = Path(tempfile.mkdtemp())

    def test_demo_pack_interpretation(self):
        plan = load_flashpack(build(self.tmp / "demo.vflashpack"))
        self.assertEqual(plan.format, "manifest")
        self.assertEqual([(b.address, len(b.data)) for b in plan.blocks], [(0x10000, 1200), (0x20000, 300)])
        self.assertEqual((plan.request_id, plan.response_id, plan.alfid), (0x7E0, 0x7E8, 0x44))
        steps = plan.expanded_steps()
        self.assertEqual(len(steps), 10)                         # erase/download je Block aufgeteilt
        self.assertTrue(all(s["title"] for s in steps))
        sections = dict(plan.describe())
        self.assertEqual(len(sections["flash.tree.data"]), 2)
        self.assertEqual(plan.warnings, [])

    def test_real_vflash_like_pack_is_read_heuristically(self):
        path = self.tmp / "ecu.vflashpack"
        xml = '<Project><Ecu Name="MyEcu" RequestId="0x7E0" ResponseId="0x7E8"/></Project>'
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("ECU1.vflash", xml)
            zf.writestr("data/app.hex", intel_hex(0x1000, b"\x01\x02\x03\x04"))
            zf.writestr("sec/seedkey.dll", b"MZ")
        plan = load_flashpack(path)
        self.assertEqual((plan.format, plan.ecu, plan.request_id, plan.response_id), ("vflash", "MyEcu", 0x7E0, 0x7E8))
        self.assertEqual(len(plan.blocks), 1)
        self.assertGreaterEqual(len(plan.warnings), 3)           # heuristisch, generischer Ablauf, ignorierte Dateien
        self.assertIn("seedkey.dll", plan.info["ignored"])

    def test_xml_entities_and_bad_files_rejected(self):
        path = self.tmp / "evil.vflashpack"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("p.vflash", '<!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>')
            zf.writestr("app.hex", intel_hex(0, b"\x00"))
        self.assertEqual(load_flashpack(path).ecu, "")           # XML mit DTD wird ignoriert
        txt = self.tmp / "x.txt"
        txt.write_text("hi")
        with self.assertRaises(FlashPackError):
            load_flashpack(txt)

    def test_manifest_missing_file_and_standalone_image(self):
        path = self.tmp / "m.vflashpack"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("flashpack.json", '{"data": [{"file": "nope.hex"}]}')
        with self.assertRaises(FlashPackError):
            load_flashpack(path)
        hexfile = self.tmp / "a.hex"
        hexfile.write_text(intel_hex(0x100, b"abc"))
        plan = load_flashpack(hexfile)
        self.assertEqual(plan.format, "image")
        self.assertEqual(len(plan.effective_steps()), 6)         # ohne Security: 3 Sessions/Kommunikation + 3

    def test_compute_key(self):
        seed = bytes.fromhex("12345678")
        self.assertEqual(compute_key(seed, {"type": "xor", "value": "0xFFFFFFFF"}).hex(), "edcba987")
        self.assertEqual(compute_key(seed, {"type": "add", "value": 1}).hex(), "12345679")
        self.assertEqual(compute_key(seed, {"type": "constant", "value": "0xAABB", "length": 2}).hex(), "aabb")


class RunTests(unittest.TestCase):
    def setUp(self):
        i18n.set_language("de")
        self.tmp = Path(tempfile.mkdtemp())
        net = f"fl{time.time_ns()}"
        self.ecu = SimulatedEcu(net, db=None)
        self.ecu.start()
        self.mgr = CanManager(DbcDatabase())
        self.mgr.connect({"type": "simulation", "sim_network": net, "sim_echo": False})
        self.ch = IsoTpChannel(self.mgr, 0x7E0, 0x7E8).attach()

    def tearDown(self):
        self.ch.detach()
        self.mgr.disconnect()
        self.ecu.stop()

    def run_plan(self, plan):
        events = []
        runner = FlashRunner(self.ch, lambda k, p: events.append((k, p)))
        runner.start(plan)
        runner.join(60)
        return runner.report, events

    def test_flash_demo_pack_into_simulated_ecu(self):
        plan = load_flashpack(build(self.tmp / "demo.vflashpack"))
        report, events = self.run_plan(plan)
        self.assertEqual(report.status, "passed", [r for r in report.results if r.status != "ok"])
        self.assertEqual(bytes(self.ecu.flash_memory[0x10000]), plan.blocks[0].data)
        self.assertEqual(bytes(self.ecu.flash_memory[0x20000]), plan.blocks[1].data)
        kinds = [k for k, _p in events]
        self.assertEqual(kinds[0], "started")
        self.assertEqual(kinds[-1], "finished")
        self.assertEqual(kinds.count("step_start"), 10)
        progress = [p for k, p in events if k == "progress"]
        self.assertGreater(len(progress), 5)                      # Blockgroesse 256 -> mehrere Transfers
        self.assertEqual(progress[-1]["bytes_done"], 1500)
        self.assertTrue(all(0 < p["done"] <= p["total"] for p in progress))

    def test_wrong_security_stops_and_skips_rest(self):
        plan = load_flashpack(build(self.tmp / "demo.vflashpack"))
        plan.security["value"] = "0x1"
        report, _events = self.run_plan(plan)
        self.assertEqual(report.status, "failed")
        statuses = [r.status for r in report.results]
        self.assertEqual(statuses[:4], ["ok", "ok", "ok", "fail"])
        self.assertTrue(all(s == "skipped" for s in statuses[4:]))
        self.assertEqual(self.ecu.flash_memory, {})               # nichts geschrieben

    def test_abort_between_chunks(self):
        plan = load_flashpack(build(self.tmp / "demo.vflashpack"))
        holder = {}

        def on_event(kind, payload):
            if kind == "progress" and payload["done"] >= 256 and "r" in holder:
                holder["r"].stop()
        runner = FlashRunner(self.ch, on_event)
        holder["r"] = runner
        runner.start(plan)
        runner.join(60)
        self.assertEqual(runner.report.status, "aborted")
        self.assertIn("aborted", [r.status for r in runner.report.results])
        self.assertLess(len(self.ecu.flash_memory.get(0x10000, b"")), 1200)

    def test_no_programming_session_is_rejected(self):
        plan = load_flashpack(build(self.tmp / "demo.vflashpack"))
        plan.steps = [{"type": "erase", "block": 0}]
        report, _ = self.run_plan(plan)
        self.assertEqual(report.status, "failed")
        self.assertIn("7E", report.results[0].message.upper())     # NRC 0x7E (Service in aktiver Session nicht unterstuetzt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
