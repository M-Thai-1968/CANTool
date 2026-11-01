"""Headless-Tests des Kerns (ohne GUI, ohne Hardware, ohne python-can/pyarrow).
Start:  python -m unittest discover -s tests -v   (im Ordner can_tool)
"""
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from can_manager import CanManager, SignalStore, Frame
from dbc_manager import DbcDatabase, convert_dbc_file, load_document
from iso_tp_manager import DiagDefinition, IsoTpChannel, SimulatedEcu
from project_manager import ProjectManager
from sequence_runner import SequenceRunner, load_sequence, validate_sequence
from cdd_manager import convert_cdd_file

DEMO = ROOT / "projects" / "demo"


class DbcTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = DbcDatabase()
        for p in sorted((DEMO / "dbc").glob("*.dbc")):
            self.db.add_file(convert_dbc_file(p, self.tmp))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_convert_is_cached_and_json_yaml(self):
        p = DEMO / "dbc" / "vehicle.dbc"
        out = convert_dbc_file(p, self.tmp)
        mtime = out.stat().st_mtime_ns
        self.assertEqual(convert_dbc_file(p, self.tmp), out)
        self.assertEqual(out.stat().st_mtime_ns, mtime)          # nicht neu geschrieben
        y = convert_dbc_file(p, self.tmp, "yaml")
        self.assertEqual(load_document(y)["messages"].keys(), load_document(out)["messages"].keys())

    def test_messages_and_attributes(self):
        m = self.db.messages
        self.assertEqual(set(m), {"VehicleSpeed", "DriverCmd", "EngineData", "EngineStatus", "DiagCounter", "FdSensor"})
        self.assertEqual(m["VehicleSpeed"]["cycle_time_ms"], 20)
        self.assertTrue(m["FdSensor"]["fd"])
        self.assertEqual(m["VehicleSpeed"]["signals"]["Gear"]["choices"][15], "R")

    def test_roundtrip_intel_offset_signed(self):
        vals = {"RPM": 3000.25, "CoolantTemp": 85, "Torque": -123.4}
        back = self.db.decode("EngineData", self.db.encode("EngineData", vals))
        self.assertAlmostEqual(back["RPM"], 3000.25)
        self.assertEqual(back["CoolantTemp"], 85)
        self.assertAlmostEqual(back["Torque"], -123.4, places=6)

    def test_motorola_known_vector(self):
        # Pressure: Start-Bit 7 (MSB in Byte 0), 12 Bit big endian, Faktor 0.1
        data = self.db.encode("EngineStatus", {"Pressure": 0x123 * 0.1, "State": 5, "Load": 50})
        self.assertEqual(data[0], 0x12)
        self.assertEqual(data[1] >> 4, 0x3)
        back = self.db.decode("EngineStatus", data)
        self.assertAlmostEqual(back["Pressure"], 29.1)
        self.assertEqual(back["State"], 5)
        self.assertAlmostEqual(back["Load"], 50)

    def test_multiplex(self):
        a = self.db.decode("DiagCounter", self.db.encode("DiagCounter", {"MuxSel": 0, "CounterA": 1234}))
        self.assertEqual(a, {"MuxSel": 0, "CounterA": 1234})
        b = self.db.decode("DiagCounter", self.db.encode("DiagCounter", {"MuxSel": 1, "CounterB": 100.5}))
        self.assertEqual(b, {"MuxSel": 1, "CounterB": 100.5})

    def test_fd_message_and_choices(self):
        data = self.db.encode("FdSensor", {"Voltage": 12.345, "Current": -5.5, "Temp": -7})
        self.assertEqual(len(data), 16)
        back = self.db.decode("FdSensor", data)
        self.assertAlmostEqual(back["Voltage"], 12.345)
        self.assertAlmostEqual(back["Current"], -5.5)
        self.assertEqual(back["Temp"], -7)
        self.assertEqual(self.db.encode("VehicleSpeed", {"Gear": "R"})[2] & 0x0F, 15)

    def test_clamping_and_lookup(self):
        self.assertEqual(self.db.decode("DriverCmd", self.db.encode("DriverCmd", {"ThrottleCmd": 1000}))["ThrottleCmd"], 102.0)
        self.assertEqual(self.db.find_by_id(0x200), "EngineData")


class BusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = DbcDatabase()
        for p in sorted((DEMO / "dbc").glob("*.dbc")):
            self.db.add_file(convert_dbc_file(p, self.tmp))
        self.net = f"t{time.time_ns()}"
        self.mgr = CanManager(self.db)
        self.mgr.connect({"type": "simulation", "sim_network": self.net, "sim_echo": True})

    def tearDown(self):
        self.mgr.disconnect()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cyclic_tx_rate_and_store(self):
        store = SignalStore(self.db)
        store.configure({"DriverCmd": ["ThrottleCmd"]}, {"DriverCmd": ["ThrottleCmd"]})
        self.mgr.add_listener(store.on_frame)
        self.mgr.add_tx_message("DriverCmd", cycle_ms=20, values={"ThrottleCmd": 40}, enabled=True)
        time.sleep(1.0)
        self.mgr.set_tx_enabled("DriverCmd", False)
        n = self.mgr.tx_entries["DriverCmd"].count
        self.assertTrue(40 <= n <= 55, n)                       # 20 ms -> ~50/s
        t, v = store.history("TX:DriverCmd.ThrottleCmd")
        self.assertAlmostEqual(v[-1], 40.0)
        self.assertGreater(len(store.history("RX:DriverCmd.ThrottleCmd")[0]), 30)   # Echo
        gaps = [b - a for a, b in zip(t, t[1:])]
        self.assertLess(max(gaps), 0.1)
        self.mgr.set_tx_enabled("DriverCmd", True)
        self.mgr.set_tx_value("DriverCmd", "ThrottleCmd", 80)
        time.sleep(0.2)
        self.assertAlmostEqual(store.latest("TX", "DriverCmd", "ThrottleCmd")[1], 80.0)

    def test_classic_limit(self):
        with self.assertRaises(ValueError):
            self.mgr.send(Frame(0, 0x100, bytes(9)))


class DiagTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.net = f"d{time.time_ns()}"
        self.ecus = []

    def tearDown(self):
        for e in self.ecus:
            e.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pair(self, fd: bool, **kw):
        ecu = SimulatedEcu(self.net, fd=fd)
        ecu.start()
        self.ecus.append(ecu)
        mgr = CanManager()
        mgr.connect({"type": "simulation", "sim_network": self.net, "sim_echo": False})
        self.mgr = mgr
        ch = IsoTpChannel(mgr, 0x7E0, 0x7E8, fd=fd, **kw).attach()
        return mgr, ch

    def _check_all(self, ch):
        r = ch.request(bytes([0x22, 0xF1, 0x90]))
        self.assertTrue(r.positive)
        self.assertEqual(r.raw[3:], SimulatedEcu.VIN)                     # Multiframe 20 Byte
        self.assertFalse(ch.request(bytes([0x22, 0x00, 0x01])).positive)
        self.assertEqual(ch.request(bytes([0x22, 0x00, 0x01])).nrc, 0x31)
        self.assertEqual(ch.request(bytes([0x3E, 0x00])).raw, bytes([0x7E, 0x00]))
        self.assertIsNone(ch.request(bytes([0x3E, 0x80])))
        self.assertTrue(ch.request(bytes([0x22, 0xF1, 0xFF]), timeout=0.2, pending_timeout=2).positive)  # 0x78
        self.assertEqual(ch.request(bytes([0x31, 0x01])).nrc, 0x11)

    def test_classic(self):
        mgr, ch = self._pair(False)
        self._check_all(ch)
        mgr.disconnect()

    def test_classic_blocksize_stmin(self):
        mgr, ch = self._pair(False, block_size=2, st_min=1)
        r = ch.request(bytes([0x22, 0xF1, 0x90]))
        self.assertEqual(r.raw[3:], SimulatedEcu.VIN)
        mgr.disconnect()

    def test_fd(self):
        mgr, ch = self._pair(True)
        self._check_all(ch)
        mgr.disconnect()

    def test_long_send_both_directions(self):
        """Selbsttest 100 Byte Multiframe-Senden (FD und Classic) ueber zwei Kanaele."""
        for fd in (False, True):
            net = f"l{fd}{time.time_ns()}"
            a, b = CanManager(), CanManager()
            for m in (a, b):
                m.connect({"type": "simulation", "sim_network": net, "sim_echo": False})
            ca = IsoTpChannel(a, 0x1, 0x2, fd=fd).attach()
            cb = IsoTpChannel(b, 0x2, 0x1, fd=fd).attach()
            payload = bytes(range(100)) * 3                              # 300 Byte
            import threading
            result = []
            t = threading.Thread(target=lambda: result.append(cb.recv(3)))
            t.start()
            ca.send(payload)
            t.join()
            self.assertEqual(result[0], payload, f"fd={fd}")
            a.disconnect(); b.disconnect()

    def test_cdd_and_definition(self):
        p = convert_cdd_file(DEMO / "cdd" / "diag.cdd", self.tmp)
        d = DiagDefinition.load(p)
        self.assertIn("Engine_ECU", d.ecu_names())
        svc = d.services("Engine_ECU")
        self.assertEqual(DiagDefinition.build_request(svc["ExtendedSession"], {"session": 3}), bytes([0x10, 0x03]))
        self.assertEqual(DiagDefinition.build_request(svc["ReadDid"], {"did": "F186"}), bytes([0x22, 0xF1, 0x86]))
        mgr, ch = self._pair(False)
        r = ch.request(DiagDefinition.build_request(svc["ReadVIN"]))
        dec = DiagDefinition.decode_response(svc["ReadVIN"], r.raw, r.request)
        self.assertEqual(dec["VIN"], SimulatedEcu.VIN.decode())
        mgr.disconnect()


class SequenceRawTests(unittest.TestCase):
    def test_send_raw_step(self):
        from sequence_runner import SequenceRunner, validate_sequence
        mgr = CanManager(DbcDatabase())
        got = []
        mgr.add_listener(got.append)
        mgr.connect({"type": "simulation", "sim_network": f"s{time.time_ns()}", "sim_echo": True})
        try:
            seq = {"name": "raw", "steps": [
                {"type": "send_raw", "id": "0x100", "data": "00 01 02 03 04 05 06 07", "dlc": 8},
                {"type": "send_raw", "id": "0x200", "data": "11 22", "cyclic": True, "cycle_ms": 50},
                {"type": "wait", "ms": 400},
                {"type": "send_raw", "id": "0x200", "cyclic": True, "enabled": False, "data": ""}]}
            self.assertEqual(validate_sequence(seq, mgr), [])
            runner = SequenceRunner(mgr)
            runner.start(seq)
            runner.join(10)
            self.assertEqual(runner.report.status, "passed")
            time.sleep(0.1)
            self.assertEqual(len([f for f in got if f.arbitration_id == 0x100 and f.direction == "rx"]), 1)
            n = len([f for f in got if f.arbitration_id == 0x200 and f.direction == "rx"])
            self.assertGreaterEqual(n, 4)
            time.sleep(0.3)
            self.assertEqual(len([f for f in got if f.arbitration_id == 0x200 and f.direction == "rx"]), n)
            bad = {"name": "b", "steps": [{"type": "send_raw", "id": "xyz", "data": "00"}]}
            self.assertEqual(len(validate_sequence(bad, mgr)), 1)
        finally:
            mgr.disconnect()


class SequenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = DbcDatabase()
        for p in sorted((DEMO / "dbc").glob("*.dbc")):
            self.db.add_file(convert_dbc_file(p, self.tmp))
        self.defn = DiagDefinition.load(convert_cdd_file(DEMO / "cdd" / "diag.cdd", self.tmp))
        self.net = f"s{time.time_ns()}"
        self.ecu = SimulatedEcu(self.net, db=self.db)
        self.ecu.start()
        self.mgr = CanManager(self.db)
        self.mgr.connect({"type": "simulation", "sim_network": self.net, "sim_echo": True})
        self.ch = IsoTpChannel(self.mgr, 0x7E0, 0x7E8).attach()
        self.runner = SequenceRunner(self.mgr, lambda: (self.ch, self.defn, "Engine_ECU"))

    def tearDown(self):
        self.mgr.disconnect()
        self.ecu.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, seq):
        self.runner.start(seq)
        self.runner.join(30)
        return self.runner.report

    def test_demo_sequences_pass(self):
        for name in ("startup", "driving"):
            rep = self._run(load_sequence(DEMO / "sequences" / f"{name}.json"))
            self.assertEqual(rep.status, "passed", [r for r in rep.results if r.status != "ok"])

    def test_failure_and_timeout(self):
        seq = {"name": "x", "steps": [
            {"type": "wait_for", "direction": "RX", "message": "EngineData", "signal": "RPM",
             "op": ">", "value": 999999, "timeout_ms": 300},
            {"type": "wait", "ms": 10}]}
        rep = self._run(seq)
        self.assertEqual(rep.status, "failed")
        self.assertEqual(len(rep.results), 1)                    # bricht nach erstem Fehler ab

    def test_validation_and_stop(self):
        bad = {"steps": [{"type": "set_signal", "message": "Nope", "signal": "x", "value": 1},
                         {"type": "bogus"}]}
        self.assertEqual(len(validate_sequence(bad, self.mgr)), 2)
        rep_seq = {"name": "long", "steps": [{"type": "wait", "ms": 5000}]}
        self.runner.start(rep_seq)
        time.sleep(0.2)
        self.runner.stop()
        self.runner.join(2)
        self.assertEqual(self.runner.report.status, "aborted")


class ProjectTests(unittest.TestCase):
    def test_create_load(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            pm = ProjectManager(tmp)
            p = pm.create("A1")
            p.settings["sequence"] = "x.json"
            p.save()
            self.assertEqual(pm.list(), ["A1"])
            self.assertEqual(pm.load("A1").settings["sequence"], "x.json")
            self.assertEqual(pm.load("A1").settings["hardware"]["type"], "simulation")
            with self.assertRaises(ValueError):
                pm.create("../x")
            with self.assertRaises(FileExistsError):
                pm.create("A1")
            src = DEMO / "dbc" / "vehicle.dbc"
            p.import_file(src)
            self.assertEqual(len(p.load_dbc().messages), 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class NoDefinitionAndPcanTests(unittest.TestCase):
    def test_trace_works_without_dbc_and_cdd(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            pm = ProjectManager(tmp)
            pm.create("leer")
            db = pm.load("leer").load_dbc()
            self.assertEqual(len(db.messages), 0)
            mgr = CanManager(db)
            got = []
            mgr.add_listener(got.append)
            mgr.connect({"type": "simulation", "sim_network": f"n{time.time_ns()}", "sim_echo": True})
            mgr.send_data(0x100, bytes(range(8)))
            time.sleep(0.3)
            mgr.disconnect()
            self.assertTrue(any(f.arbitration_id == 0x100 and f.direction == "rx" for f in got))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_pcan_channel_normalization(self):
        from can_manager import PythonCanBackend, normalize_pcan_channel as n
        for raw, want in (("", "PCAN_USBBUS1"), (None, "PCAN_USBBUS1"), ("1", "PCAN_USBBUS1"), (2, "PCAN_USBBUS2"),
                          ("usb2", "PCAN_USBBUS2"), ("PCAN_USB1", "PCAN_USBBUS1"), ("pcan_usbbus2", "PCAN_USBBUS2")):
            self.assertEqual(n(raw), want)
        kw = PythonCanBackend({"type": "pcan", "channel": "", "fd": False, "bitrate": 500000})._bus_kwargs()
        self.assertEqual(kw, {"interface": "pcan", "channel": "PCAN_USBBUS1", "bitrate": 500000})


class UdsDetailTests(unittest.TestCase):
    def test_decode_response_detail(self):
        svc = {"request": "22F190", "response": {"prefix_len": 3, "params": [
            {"name": "VIN", "offset": 0, "length": 4, "type": "ascii"},
            {"name": "Speed", "offset": 4, "length": 2, "type": "uint", "factor": 0.5, "unit": "km/h"}]}}
        resp = bytes([0x62, 0xF1, 0x90]) + b"ABCD" + bytes([0x01, 0x00])
        d = DiagDefinition.decode_response_detail(svc, resp, bytes([0x22, 0xF1, 0x90]))
        self.assertEqual((d[0]["name"], d[0]["text"], d[0]["raw_hex"], d[0]["value"]), ("VIN", "ABCD", "0x41424344", None))
        self.assertEqual((d[1]["value"], d[1]["unit"], d[1]["raw_hex"]), (128.0, "km/h", "0x0100"))


class TraceUpdateTests(unittest.TestCase):
    def test_update_mode_one_row_per_message(self):
        from trace_logic import COUNT_COL, UpdateTable
        def row(t, d, ident, data, kind="rx"):
            return (f"{t:.4f}", d, "CH1", ident, "CAN", "8", data, "", "", "", kind)
        u = UpdateTable()
        ch, added = u.apply([row(0.1, "Rx", "100", "00"), row(0.2, "Rx", "200", "01"), row(0.3, "Tx", "100", "02", "tx")])
        self.assertEqual((ch, added, len(u.rows)), (set(), 3, 3))
        ch, added = u.apply([row(1.1, "Rx", "100", "AA"), row(1.2, "Rx", "100", "BB"), row(1.3, "Rx", "300", "03")])
        self.assertEqual((ch, added, len(u.rows)), ({0}, 1, 4))
        self.assertEqual((u.rows[0][0], u.rows[0][6], u.rows[0][COUNT_COL]), ("1.2000", "BB", "3"))
        self.assertEqual(u.rows[2][COUNT_COL], "1")                 # Tx 100 ist eine eigene Zeile
        self.assertEqual([r[3] for r in u.rows], ["100", "200", "100", "300"])   # Reihenfolge = Erstauftreten
        u.clear()
        self.assertEqual(u.rows, [])


    def test_diff_time_per_message(self):
        from trace_logic import UpdateTable
        def row(t, ident):
            return (f"{t:11.4f}", "Rx", "CH1", ident, "CAN", "8", "00", "", "", "", "rx")
        u = UpdateTable(diff=True)
        u.apply([row(1.0, "100"), row(1.5, "200")])
        self.assertEqual([float(r[0]) for r in u.rows], [0.0, 0.0])
        u.apply([row(1.1, "100"), row(1.62, "200"), row(1.3, "100")])
        self.assertAlmostEqual(float(u.rows[0][0]), 0.2, places=3)       # 1.3 - 1.1
        self.assertAlmostEqual(float(u.rows[1][0]), 0.12, places=3)      # 1.62 - 1.5


class PlatformTests(unittest.TestCase):
    def test_os_detection_and_hardware_matrix(self):
        import platform_info as pi
        self.assertIn(pi.current_os(), ("windows", "linux", "macos", "other"))
        self.assertTrue(pi.os_label())
        win, lin = pi.supported_hardware("windows"), pi.supported_hardware("linux")
        self.assertTrue(win["vector"] and win["pcan"] and win["simulation"] and not win["socketcan"])
        self.assertTrue(lin["socketcan"] and lin["pcan"] and lin["simulation"] and not lin["vector"])
        for name, want in (("win32", "windows"), ("cygwin", "windows"), ("linux", "linux"), ("darwin", "macos")):
            old = sys.platform
            try:
                sys.platform = name
                self.assertEqual(pi.current_os(), want)
            finally:
                sys.platform = old

    def test_unsupported_hardware_gives_clear_error(self):
        import platform_info as pi
        from can_manager import create_backend
        bad = "vector" if not pi.hardware_supported("vector") else "socketcan"
        if pi.hardware_supported(bad):
            self.skipTest("alle Typen unterstuetzt")
        with self.assertRaises(RuntimeError) as cm:
            create_backend({"type": bad})
        self.assertIn(pi.os_label().split()[0], str(cm.exception))
        self.assertIsNotNone(create_backend({"type": "simulation"}))

    def test_vector_driver_missing_message(self):
        import types
        import can_manager as cm
        fake = types.ModuleType("can")

        def bus(**_kw):
            raise OSError("Could not import vxlapi: Vector XL library not found: vxlapi64")
        fake.Bus = bus
        saved = sys.modules.get("can")
        sys.modules["can"] = fake
        old = cm.current_os, cm.driver_available
        try:
            cm.current_os = lambda: "windows"
            cm.driver_available = lambda _k, _o=None: True        # DLL vorhanden, python-can scheitert trotzdem
            with self.assertRaises(RuntimeError) as c1:
                cm.PythonCanBackend({"type": "vector", "channel": 0}).open()
            cm.driver_available = lambda _k, _o=None: False       # DLL fehlt
            with self.assertRaises(RuntimeError) as c2:
                cm.PythonCanBackend({"type": "vector", "channel": 0}).open()
        finally:
            cm.current_os, cm.driver_available = old
            if saved is None:
                sys.modules.pop("can", None)
            else:
                sys.modules["can"] = saved
        for e in (c1, c2):
            self.assertIn("vxlapi64.dll" if sys.maxsize > 2**32 else "vxlapi.dll", str(e.exception))
            self.assertIn("vector.com", str(e.exception))

    def test_vector_vn7572_uses_same_strategy(self):
        """Die PCIe-Karte VN7572 wird wie die VN1630A ueber Typ 'vector' (Seriennummer + Kanalindex) geoeffnet."""
        import types
        import can_manager as cm
        calls = []
        cfg = lambda n, s: types.SimpleNamespace(name=n, serial_number=s)       # noqa: E731
        fake = types.ModuleType("can")
        fake.detect_available_configs = lambda _i: [
            {"channel": 0, "serial": 777, "channel_index": 0, "vector_channel_config": cfg("VN7572 Channel 1", 777)},
            {"channel": 1, "serial": 777, "channel_index": 1, "vector_channel_config": cfg("VN7572 Channel 2", 777)}]
        fake.Bus = lambda **kw: calls.append(kw) or object()
        saved = sys.modules.get("can")
        sys.modules["can"] = fake
        try:
            cm.PythonCanBackend({"type": "vector", "channel": 1, "bitrate": 500000}).open()
            self.assertEqual((calls[0]["serial"], calls[0]["channel"], calls[0]["app_name"]), (777, 1, None))
        finally:
            if saved is None:
                sys.modules.pop("can", None)
            else:
                sys.modules["can"] = saved

    def test_vector_open_strategy_serial_then_fallbacks(self):
        import types
        import can_manager as cm
        calls = []
        cfg = lambda n, s: types.SimpleNamespace(name=n, serial_number=s)       # noqa: E731
        fake = types.ModuleType("can")
        fake.detect_available_configs = lambda _i: [
            {"channel": 0, "serial": 0, "channel_index": 0, "vector_channel_config": cfg("Virtual Channel 1", 0)},
            {"channel": 0, "serial": 12345, "channel_index": 2, "vector_channel_config": cfg("VN1630A Channel 1", 12345)}]

        def bus(**kw):
            calls.append(kw)
            if kw.get("app_name", "CANalyzer") == "CANalyzer":
                raise OSError("Channel '1' of application 'CANalyzer' is not assigned to any interface")
            if kw.get("serial") is None:
                raise OSError("kein Index")
            return object()
        fake.Bus = bus
        saved = sys.modules.get("can")
        sys.modules["can"] = fake
        try:
            b = cm.PythonCanBackend({"type": "vector", "channel": 0, "bitrate": 500000})
            b.open()
            self.assertEqual(len(calls), 1)
            self.assertEqual((calls[0]["serial"], calls[0]["channel"], calls[0]["app_name"]), (12345, 0, None))
            calls.clear()
            fake.Bus = lambda **kw: calls.append(kw) or (_ for _ in ()).throw(OSError("boom"))
            with self.assertRaises(RuntimeError) as cx:
                cm.PythonCanBackend({"type": "vector", "channel": 1}).open()
            self.assertEqual(len(calls), 3)                                    # Seriennummer, HW-Index, Hardware Config
            self.assertIn("VN1630A", str(cx.exception))
            calls.clear()
            cm.PythonCanBackend({"type": "vector", "channel": 1, "app_name": "MyApp"})
            self.assertEqual(cm.PythonCanBackend({"type": "vector", "app_name": "MyApp"})._vector_attempts(), [{}])
        finally:
            if saved is None:
                sys.modules.pop("can", None)
            else:
                sys.modules["can"] = saved

    def test_detect_and_driver_checks_never_raise(self):
        import platform_info as pi
        self.assertIsInstance(pi.detect_hardware(), list)
        for k in ("vector", "pcan", "socketcan", "simulation"):
            pi.driver_available(k)
        self.assertIsInstance(pi.socketcan_interfaces(), list)

    def test_project_names_portable(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            pm = ProjectManager(tmp)
            for bad in ("CON", "nul", "COM1", "lpt9", "-x", "a/b", ""):
                with self.assertRaises(ValueError, msg=bad):
                    pm.create(bad)
            for good in ("a", "Projekt 1", "demo_2", "ab-c"):
                pm.create(good)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class RawLoggingTests(unittest.TestCase):
    """Rohdaten-Logging; pyarrow wird durch einen Stub ersetzt (Logik, nicht das Dateiformat, wird geprueft)."""

    def setUp(self):
        import types
        self.written = {}
        outer = self

        class Writer:
            def __init__(self, path, schema, compression=None):
                self.path = str(path)
                outer.written[self.path] = []

            def write_table(self, table):
                outer.written[self.path].append(table)

            def close(self):
                pass

        pa = types.ModuleType("pyarrow")
        for n in ("float64", "string", "uint32", "bool_", "uint8", "binary", "int64"):
            setattr(pa, n, lambda n=n: n)
        pa.schema = lambda fields: fields
        pa.Table = types.SimpleNamespace(from_pydict=lambda d, schema=None: d)
        pq = types.ModuleType("pyarrow.parquet")
        pq.ParquetWriter = Writer
        pa.parquet = pq
        self._saved = {k: sys.modules.get(k) for k in ("pyarrow", "pyarrow.parquet")}
        sys.modules["pyarrow"], sys.modules["pyarrow.parquet"] = pa, pq

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def test_raw_logs_all_frames_without_dbc_and_filter(self):
        from parquet_logger import ParquetLogger, parse_id_filter
        self.assertEqual(parse_id_filter("100, 7E0-7EF"), [(0x100, 0x100), (0x7E0, 0x7EF)])
        with self.assertRaises(ValueError):
            parse_id_filter("zz")
        lg = ParquetLogger(DbcDatabase(), Path("x/test.parquet"), {"TX": {}, "RX": {}}, log_raw=True, raw_ids="100, 7E0-7EF")
        lg.start()
        for ident, err in ((0x100, False), (0x200, False), (0x7E8, False), (0x101, True)):
            lg.on_frame(Frame(1.0, ident, bytes([1, 2, 3]), False, False, False, "rx", "ch", err))
        lg.on_frame(Frame(2.0, 0x100, bytes(range(8)), False, False, False, "tx", "ch"))
        lg.stop()
        raw = lg.raw_path
        rows = self.written[str(raw)][0]
        self.assertEqual(rows["can_id"], [0x100, 0x7E8, 0x100])
        self.assertEqual(rows["direction"], ["RX", "RX", "TX"])
        self.assertEqual(rows["data_hex"][0], "01 02 03")
        self.assertEqual(rows["dlc"], [3, 3, 8])
        self.assertEqual(lg.raw_rows_written, 3)

    def test_signals_resolved_with_raw_hex_and_physical(self):
        from parquet_logger import ParquetLogger
        db = DbcDatabase()
        db.messages["Eng"] = {"id": 0x123, "extended": False, "dlc": 8, "sender": "ECU", "multiplexor": None, "signals": {
            "Rpm": {"start": 0, "length": 16, "byte_order": "little", "signed": False, "factor": 0.25, "offset": 0,
                    "unit": "rpm", "float": None, "choices": None, "mux_value": None},
            "Temp": {"start": 16, "length": 8, "byte_order": "little", "signed": True, "factor": 1, "offset": 0,
                     "unit": "degC", "float": None, "choices": None, "mux_value": None},
            "St": {"start": 24, "length": 4, "byte_order": "little", "signed": False, "factor": 1, "offset": 0,
                   "unit": "", "float": None, "choices": {1: "RUN"}, "mux_value": None}}}
        db._by_id[(0x123, False)] = "Eng"
        data = bytes([0x40, 0x1F, 0xF6, 0x01, 0, 0, 0, 0])             # Rpm raw 8000 -> 2000, Temp raw -10, St 1
        lg = ParquetLogger(db, Path("x/s.parquet"), {"RX": {"Eng": None}}, log_raw=True)
        lg.start()
        lg.on_frame(Frame(1.0, 0x123, data, False, False, False, "rx", "ch"))
        lg.on_frame(Frame(1.1, 0x124, data, False, False, False, "rx", "ch"))      # unbekannte ID: nur Rohdatei
        lg.log_external("XCP", "Mod", "EngineSpeed", {"raw": 8000, "raw_hex": "0x1F40", "value": 2000.0, "text": "",
                                                       "unit": "rpm"})
        lg.stop()
        sig = self.written["x/s.parquet"][0]
        i = sig["signal"].index("Rpm")
        self.assertEqual((sig["raw"][i], sig["raw_hex"][i], sig["value"][i], sig["unit"][i], sig["source"][i]),
                         (8000, "0x1F40", 2000.0, "rpm", "CAN"))
        self.assertEqual(sig["message_hex"][i], "40 1F F6 01 00 00 00 00")
        self.assertEqual((sig["raw"][1], sig["raw_hex"][1], sig["value"][1]), (-10, "0xF6", -10))
        self.assertEqual((sig["text"][2], sig["raw_hex"][2]), ("RUN", "0x1"))
        self.assertEqual((sig["source"][3], sig["signal"][3], sig["raw_hex"][3]), ("XCP", "EngineSpeed", "0x1F40"))
        raw = self.written[str(lg.raw_path)][0]
        self.assertIn("Rpm=0x1F40/2000 rpm", raw["signals"][0])
        self.assertIn("St=0x1/1 [RUN]", raw["signals"][0])
        self.assertEqual(raw["signals"][1], "")

    def test_raw_includes_error_frames_when_unfiltered(self):
        from parquet_logger import ParquetLogger
        lg = ParquetLogger(DbcDatabase(), Path("x/e.parquet"), {"TX": {}, "RX": {}}, log_raw=True)
        lg.start()
        lg.on_frame(Frame(1.0, 0, b"", False, False, False, "rx", "ch", True))
        lg.stop()
        self.assertEqual(self.written[str(lg.raw_path)][0]["error"], [True])


class RawSendTests(unittest.TestCase):
    def setUp(self):
        self.mgr = CanManager(DbcDatabase())
        self.got = []
        self.mgr.add_listener(self.got.append)
        self.mgr.connect({"type": "simulation", "sim_network": f"r{time.time_ns()}", "sim_echo": True})

    def tearDown(self):
        self.mgr.disconnect()

    def rx(self, ident):
        return [f for f in self.got if f.arbitration_id == ident and f.direction == "rx"]

    def test_once_default_row(self):
        self.mgr.tx_raw("100", "00 01 02 03 04 05 06 07", 8)
        time.sleep(0.3)
        frames = self.rx(0x100)
        self.assertEqual(len(frames), 1)
        self.assertEqual(bytes(frames[0].data), bytes(range(8)))

    def test_cyclic_start_stop_and_padding(self):
        name = self.mgr.tx_raw("0x100", "AA", 4, cyclic=True, cycle_ms=50)
        time.sleep(0.5)
        self.mgr.tx_raw("0x100", "", cyclic=True, enabled=False)
        time.sleep(0.15)
        n = len(self.rx(0x100))
        self.assertGreaterEqual(n, 4)
        self.assertEqual(bytes(self.rx(0x100)[0].data), b"\xaa\x00\x00\x00")
        time.sleep(0.3)
        self.assertEqual(len(self.rx(0x100)), n)
        self.assertNotIn(name, self.mgr.tx_entries)

    def test_reinit_keeps_tx_list_and_resumes(self):
        self.mgr.tx_raw("0x300", "01", cyclic=True, cycle_ms=50)
        time.sleep(0.3)
        old_rx = self.mgr._rx_thread
        self.mgr.reinit()
        self.assertTrue(self.mgr.connected)
        self.assertFalse(old_rx.is_alive())
        n = len(self.rx(0x300))
        time.sleep(0.4)
        self.assertGreater(len(self.rx(0x300)), n)

    def test_reinit_survives_hanging_close(self):
        backend = self.mgr.backend
        backend.close = lambda: time.sleep(10)
        t0 = time.time()
        self.mgr._close_backend(backend, timeout=0.3)
        self.assertLess(time.time() - t0, 2)
        self.assertTrue(self.mgr.last_error)

    def test_errors_and_automatic_formats(self):
        for args in (("ZZ", "00"), ("100", "0G"), ("100", "00", 9), ("100", "00" * 9, 8), ("20000000", "00")):
            with self.assertRaises(ValueError):
                self.mgr.tx_raw(*args)
        with self.assertRaises(ValueError):
            self.mgr.tx_raw("100", "00", cyclic=True, cycle_ms=0)
        self.mgr.tx_raw("1ABCDE", "01")
        time.sleep(0.2)
        self.assertTrue(self.rx(0x1ABCDE)[0].is_extended)


class ErrorFrameTests(unittest.TestCase):
    def test_error_frames_only_reach_trace_listeners(self):
        mgr = CanManager(DbcDatabase())
        normal, trace = [], []
        mgr.add_listener(normal.append)
        mgr.add_listener(trace.append, errors=True)
        mgr._dispatch(Frame(0.0, 0x100, b"\x01"))
        mgr._dispatch(Frame(0.1, 0, b"", is_error=True))
        self.assertEqual(len(normal), 1)
        self.assertEqual(len(trace), 2)
        mgr.remove_listener(trace.append)
        mgr._dispatch(Frame(0.2, 0, b"", is_error=True))
        self.assertEqual(len(trace), 2)
        self.assertTrue(Frame(0.0, 1, b"", is_error=True).copy_as("tx").is_error)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class BuilderLogicTests(unittest.TestCase):
    def test_elements_and_sanitize(self):
        import builder_logic as bl
        for kind in bl.KINDS:
            el = bl.new_element(kind, 5, 6)
            self.assertEqual((el["kind"], el["x"], el["y"]), (kind, 5, 6))
        with self.assertRaises(ValueError):
            bl.new_element("slider")
        clean = bl.sanitize([{"kind": "gauge", "x": -4, "w": 5, "min": 1}, {"kind": "x"}, 7])
        self.assertEqual(len(clean), 1)
        self.assertEqual((clean[0]["x"], clean[0]["w"], clean[0]["min"], clean[0]["auto"]), (0, 30, 1, True))
        self.assertEqual(bl.sanitize("kaputt"), [])

    def test_led_text_gauge(self):
        import builder_logic as bl
        self.assertFalse(bl.led_state(None))
        self.assertFalse(bl.led_state(0))
        self.assertTrue(bl.led_state(0.1))
        self.assertTrue(bl.led_state(5, 4.5))
        self.assertEqual(bl.format_value(3.14159, 1, "V"), "3.1 V")
        self.assertEqual(bl.format_value(None), "--")
        self.assertEqual(bl.format_value(2, 0), "2")
        self.assertEqual(bl.gauge_angle(bl.gauge_fraction(0, 0, 100)), 180.0)
        self.assertEqual(bl.gauge_angle(bl.gauge_fraction(50, 0, 100)), 90.0)
        self.assertEqual(bl.gauge_angle(bl.gauge_fraction(500, 0, 100)), 0.0)   # begrenzt
        self.assertEqual(bl.gauge_fraction(None, 0, 1), 0.0)
        self.assertEqual(bl.tick_values(0, 100, 5), [0, 25, 50, 75, 100])

    def test_auto_and_manual_scale(self):
        import builder_logic as bl
        a = bl.AutoScale()
        self.assertEqual(a.range(), (0.0, 100.0))
        for v in (10, 20, 87):
            a.update(v)
        a.update(None)
        lo, hi = a.range()
        self.assertTrue(lo <= 10 and hi >= 87)
        self.assertEqual((lo, hi), (0, 100))
        a.update(-3)
        self.assertLessEqual(a.range()[0], -3)
        a.reset()
        self.assertEqual(a.range(), (0.0, 100.0))
        lo, hi = bl.nice_range(-7, -7)
        self.assertTrue(lo <= -7 <= hi)
        self.assertEqual(bl.manual_range("0", "8000"), (0.0, 8000.0))
        for bad in ((5, 5), (9, 1), ("x", 1), (0, float("inf"))):
            with self.assertRaises(ValueError):
                bl.manual_range(*bad)


class BuilderExtensionTests(unittest.TestCase):
    def test_led_digital_and_ranges(self):
        import builder_logic as bl
        led = bl.new_element("led")
        self.assertEqual(bl.led_color(led, 1), led["c1"])
        self.assertEqual(bl.led_color(led, 0), led["c0"])
        self.assertEqual(bl.led_color(led, None), bl.NO_VALUE_COLOR)
        led["mode"] = "range"
        led["ranges"] = [{"on": True, "min": 0, "max": 10, "color": "#111111"},
                         {"on": False, "min": 10, "max": 20, "color": "#222222"},
                         {"on": True, "min": 20, "max": 30, "color": "#333333"}]
        got = [bl.led_color(led, v) for v in (-1, 0, 9.99, 10, 25, 30)]
        self.assertEqual(got, [bl.NO_VALUE_COLOR, "#111111", "#111111", bl.NO_VALUE_COLOR, "#333333", bl.NO_VALUE_COLOR])
        self.assertEqual(bl.compare_class(None, 1), None)
        self.assertEqual(bl.compare_class(5.0 + 1e-12, 5.0), "eq")

    def test_gauge_ranges(self):
        import builder_logic as bl
        self.assertTrue(bl.in_range(0, 0, 10))
        self.assertTrue(bl.in_range(9.99, 0, 10))
        self.assertFalse(bl.in_range(10, 0, 10))                       # min <= x < max
        rng = bl.default_ranges()
        self.assertEqual(len(rng), bl.RANGE_COUNT)
        self.assertEqual(bl.gauge_bands(0, 100, rng), [(0.0, 0.5, "#2ecc40"), (0.5, 0.8, "#f1c40f"), (0.8, 1.0, "#e74c3c")])
        rng[1]["on"] = False
        self.assertEqual(len(bl.gauge_bands(0, 100, rng)), 2)
        cut = [{"on": True, "min": -50, "max": 20, "color": "#111111"}]
        self.assertEqual(bl.gauge_bands(0, 100, cut), [(0.0, 0.2, "#111111")])      # auf die Skala begrenzt
        self.assertEqual(bl.gauge_bands(0, 100, [{"on": True, "min": 200, "max": 300, "color": "#1"}]), [])
        self.assertEqual(bl.gauge_bands(5, 5, rng), [])
        bad = bl.sanitize_ranges([{"min": 5, "max": 5, "color": "#1"}, "x", {"min": 1, "max": 9, "color": "#222222"}])
        self.assertEqual(bad[0], bl.default_ranges()[0])                # ungueltig -> Standard
        self.assertEqual(bl.sanitize_ranges("kaputt"), bl.default_ranges())
        g = bl.new_element("gauge")
        self.assertTrue(g["title_on"])
        self.assertFalse(g["range_on"])
        clean = bl.sanitize([{"kind": "gauge", "range_on": True, "ranges": [{"min": 1, "max": 2, "color": "#abcdef"}]}])[0]
        self.assertEqual(clean["ranges"][0]["color"], "#abcdef")
        self.assertEqual(len(clean["ranges"]), 3)

    def test_new_kinds_and_migration(self):
        import builder_logic as bl
        self.assertIn("background", bl.KINDS)
        self.assertEqual(bl.new_element("image")["path"], "")
        self.assertTrue(bl.new_element("background")["color"].startswith("#"))
        self.assertNotIn("background", bl.FUNCTION_KINDS)
        old = bl.sanitize([{"kind": "led", "threshold": 3, "color": "#ff0000", "x": 7}])[0]
        self.assertEqual((old["mode"], old["x"]), ("range", 7))
        self.assertEqual([bl.led_color(old, v) for v in (2, 3, 3.5)], [bl.LED_COLORS["off"], bl.LED_COLORS["off"], "#ff0000"])
        mid = bl.sanitize([{"kind": "led", "mode": "analog", "xv": 5, "clt": "#1", "ceq": "#2", "cgt": "#3"}])[0]
        self.assertEqual([bl.led_color(mid, v) for v in (4, 5, 6)], ["#1", "#2", "#3"])
        old0 = bl.sanitize([{"kind": "led", "color": "#00ff00"}])[0]
        self.assertEqual((old0["mode"], old0["c1"]), ("digital", "#00ff00"))


class BuilderMinMaxTests(unittest.TestCase):
    def test_minmax(self):
        import builder_logic as bl
        mm = bl.MinMax()
        self.assertEqual((mm.lo, mm.hi), (None, None))
        for v in (5, 2, 9, None, float("nan"), 4):
            mm.update(v)
        self.assertEqual((mm.lo, mm.hi), (2, 9))
        mm.reset()
        self.assertEqual((mm.lo, mm.hi), (None, None))
        g = bl.new_element("gauge")
        self.assertEqual((g["show_min"], g["show_max"]), (False, False))
        self.assertTrue(bl.sanitize([{"kind": "gauge", "show_max": True}])[0]["show_max"])
