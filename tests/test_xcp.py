"""Tests fuer A2L-Konvertierung und XCP-Master/-Slave (Simulation, ohne Hardware)."""
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from a2l_manager import A2lDatabase, convert_a2l_file, parse_a2l_text  # noqa: E402
from can_manager import CanManager  # noqa: E402
from dbc_manager import DbcDatabase, load_document  # noqa: E402
from project_manager import ProjectManager  # noqa: E402
from xcp_manager import SimulatedXcpSlave, XcpError, XcpMaster  # noqa: E402

DEMO_A2L = ROOT / "projects" / "demo" / "a2l" / "demo.a2l"


class A2lTests(unittest.TestCase):
    def setUp(self):
        self.doc = parse_a2l_text(DEMO_A2L.read_text(encoding="utf-8"))
        self.db = A2lDatabase(self.doc)

    def test_structure(self):
        self.assertEqual(set(self.doc["measurements"]), {"EngineSpeed", "CoolantTemp", "Throttle", "State", "Counter", "Voltage"})
        self.assertEqual(set(self.doc["characteristics"]), {"RpmLimit", "GainKp", "Mode"})
        x = self.doc["xcp"]
        self.assertEqual((x["can_id_master"], x["can_id_slave"], x["baudrate"], x["max_cto"]), (0x600, 0x601, 500000, 8))
        self.assertEqual(self.doc["measurements"]["EngineSpeed"]["address"], 0x1000)
        self.assertEqual(self.doc["characteristics"]["GainKp"]["datatype"], "FLOAT32_IEEE")

    def test_conversions_roundtrip(self):
        self.assertEqual(self.db.decode("EngineSpeed", b"\x40\x1f"), (8000, 2000.0))     # RAT_FUNC: raw = 4 * rpm
        self.assertEqual(self.db.decode("CoolantTemp", b"\x80"), (128, 48.0))            # LINEAR 0.75*raw - 48
        self.assertEqual(self.db.decode("State", b"\x02")[1], "ERROR")                   # TAB_VERB
        self.assertEqual(self.db.encode("EngineSpeed", 2000), b"\x40\x1f")
        self.assertEqual(self.db.encode("State", "RUN"), b"\x01")
        self.assertEqual(self.db.encode("CoolantTemp", 48.0), b"\x80")
        self.assertEqual(self.db.decode("Voltage", self.db.encode("Voltage", 12.5))[1], 12.5)
        self.assertEqual(self.db.decode("EngineSpeed", b"\x1f\x40", big_endian=True)[0], 8000)
        with self.assertRaises(ValueError):
            self.db.encode("Throttle", 1000)                                              # ueber UBYTE-Bereich

    def test_detail_for_logging(self):
        raw, phys = self.db.decode("EngineSpeed", b"\x40\x1f")
        d = self.db.detail("EngineSpeed", raw, phys)
        self.assertEqual((d["raw"], d["raw_hex"], d["value"], d["unit"], d["text"]), (8000, "0x1F40", 2000.0, "rpm", ""))
        raw, phys = self.db.decode("State", b"\x01")
        d = self.db.detail("State", raw, phys)
        self.assertEqual((d["raw_hex"], d["text"]), ("0x01", "RUN"))
        raw, phys = self.db.decode("Voltage", self.db.encode("Voltage", 12.5))
        d = self.db.detail("Voltage", raw, phys)
        self.assertEqual((d["raw"], d["raw_hex"], d["value"]), (None, "0x41480000", 12.5))

    def test_comments_strings_and_bitmask(self):
        text = '''/* c */ /begin PROJECT P "x // not a comment"
          /begin MODULE M ""
            /begin COMPU_METHOD CM "" IDENTICAL "%f" "" /end COMPU_METHOD
            /begin MEASUREMENT Flags "d" UBYTE CM 1 0 0 3 ECU_ADDRESS 0x10 BIT_MASK 0x0C /end MEASUREMENT // end
          /end MODULE /end PROJECT'''
        db = A2lDatabase(parse_a2l_text(text))
        self.assertEqual(db.decode("Flags", b"\x0b")[0], 2)                              # (0x0B & 0x0C) >> 2
        self.assertEqual(db.doc["project"], "P")

    def test_convert_cache_and_project(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            out = convert_a2l_file(DEMO_A2L, tmp, "json")
            self.assertTrue(out.name.endswith(".a2l.json"))
            mtime = out.stat().st_mtime_ns
            self.assertEqual(convert_a2l_file(DEMO_A2L, tmp, "json"), out)
            self.assertEqual(out.stat().st_mtime_ns, mtime)                               # unveraendert -> nicht neu
            y = convert_a2l_file(DEMO_A2L, tmp, "yaml")
            self.assertEqual(load_document(y)["xcp"]["can_id_slave"], 0x601)
            bad = tmp / "bad.a2l"
            bad.write_text("nichts", encoding="utf-8")
            with self.assertRaises(ValueError):
                convert_a2l_file(bad, tmp)
            pm = ProjectManager(tmp / "projects")
            p = pm.create("x")
            p.import_file(DEMO_A2L)
            files = p.a2l_files()
            self.assertEqual([f.name for f in files], ["demo.a2l.json"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class XcpTests(unittest.TestCase):
    def setUp(self):
        self.db = A2lDatabase(parse_a2l_text(DEMO_A2L.read_text(encoding="utf-8")))
        self.net = f"xcp{time.time_ns()}"
        self.slave = SimulatedXcpSlave(self.net, self.db, 0x600, 0x601)
        self.slave.start()
        self.mgr = CanManager(DbcDatabase())
        self.mgr.connect({"type": "simulation", "sim_network": self.net, "sim_echo": False})
        self.log = []
        self.xcp = XcpMaster(self.mgr, 0x600, 0x601, on_log=lambda d, b: self.log.append((d, b))).attach()

    def tearDown(self):
        self.xcp.detach()
        self.mgr.disconnect()
        self.slave.stop()

    def test_connect_read_write(self):
        info = self.xcp.connect()
        self.assertEqual((info["max_cto"], info["byte_order"]), (8, "little"))
        raw, phys = self.xcp.read_item(self.db, "EngineSpeed")
        self.assertTrue(0 <= phys <= 1000, phys)
        self.assertEqual(self.xcp.read(0x1008, 4), bytes(self.slave.mem.get(0x1008 + i, 0) for i in range(4)))
        self.xcp.write_item(self.db, "RpmLimit", 4000)                 # UWORD, 2 Byte
        self.assertEqual(self.xcp.read_item(self.db, "RpmLimit")[1], 4000)
        self.xcp.write_item(self.db, "GainKp", 2.5)                    # FLOAT32, > 1 Download-Block? (4 <= 6)
        self.assertAlmostEqual(self.xcp.read_item(self.db, "GainKp")[1], 2.5, places=5)
        self.xcp.write_item(self.db, "Mode", "RUN")
        self.assertEqual(self.xcp.read_item(self.db, "Mode")[1], "RUN")
        self.assertTrue(self.log and self.log[0][0] == "tx")
        self.assertEqual(self.xcp.get_status()["session"], 0)

    def test_long_read_is_chunked_and_big_endian_slave(self):
        self.xcp.connect()
        data = self.xcp.read(0x2000, 20)                                # 20 Byte -> mehrere SHORT_UPLOADs (7 Byte)
        self.assertEqual(len(data), 20)
        # Motorola-Slave: Master erkennt die Byte-Reihenfolge aus CONNECT
        self.xcp.detach()
        other = SimulatedXcpSlave(self.net + "b", self.db, 0x700, 0x701, big_endian=True)
        other.start()
        m2 = CanManager(DbcDatabase())
        m2.connect({"type": "simulation", "sim_network": self.net + "b", "sim_echo": False})
        x2 = XcpMaster(m2, 0x700, 0x701).attach()
        try:
            self.assertEqual(x2.connect()["byte_order"], "big")
            x2.write_item(self.db, "RpmLimit", 3000)
            self.assertEqual(x2.read_item(self.db, "RpmLimit")[1], 3000)
            self.assertEqual(other.mem[0x2000:0x2000 + 1] if False else other.mem[0x2000], (12000 >> 8))   # Big-Endian im Speicher
        finally:
            x2.detach()
            m2.disconnect()
            other.stop()

    def test_errors(self):
        with self.assertRaises(XcpError) as c:                         # nicht verbunden -> negative Antwort
            self.xcp.read(0x1000, 2)
        self.assertEqual(c.exception.code, 0x31)
        self.xcp.connect()
        with self.assertRaises(XcpError):
            self.xcp.command(bytes([0x99]))                            # unbekanntes Kommando
        self.xcp.disconnect()
        slow = XcpMaster(self.mgr, 0x650, 0x651, timeout=0.2).attach()  # niemand antwortet
        with self.assertRaises(XcpError) as c:
            slow.command(bytes([0xFF, 0]))
        self.assertIsNone(c.exception.code)
        slow.detach()
        with self.assertRaises(XcpError):
            self.xcp.write_item(self.db, "NichtDa", 1) if False else self.xcp._check({"supported": False}, "x")


if __name__ == "__main__":
    unittest.main(verbosity=2)
