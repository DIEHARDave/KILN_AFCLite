"""Unit tests for extras/kiln_afclite.py with stand-ins for Klipper and
Kiln's IFS. Run from the repository root: python3 -m unittest discover tests
"""
import copy
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location(
    "kiln_afclite", os.path.join(HERE, "..", "extras", "kiln_afclite.py"))
afc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(afc)


class CommandError(Exception):
    pass


class FakeReactor:
    NOW = 0.

    def __init__(self):
        self.now = 100.
        self.callbacks = []
        self.timers = []

    def monotonic(self):
        return self.now

    def register_timer(self, callback, when):
        self.timers.append(callback)
        return callback

    def unregister_timer(self, timer):
        self.timers.remove(timer)

    def register_callback(self, callback):
        self.callbacks.append(callback)


class FakeGcode:
    error = CommandError

    def __init__(self):
        self.commands = {}
        self.scripts = []
        self.fail = None

    def register_command(self, name, func, desc=None):
        self.commands[name] = func

    def run_script_from_command(self, script):
        self.scripts.append(script)
        if self.fail and self.fail in script:
            raise CommandError("IFS: load channel 2 timed out")

    run_script = run_script_from_command


class FakeGcmd:
    def __init__(self, **params):
        self.params = params
        self.messages = []

    def get(self, name, default=None):
        return self.params.get(name, default)

    def respond_info(self, msg):
        self.messages.append(msg)

    def error(self, msg):
        return CommandError(msg)


class FakeWebhooks:
    def __init__(self):
        self._remote_methods = {}
        self.calls = []

    def call_remote_method(self, method, **kwargs):
        self.calls.append((method, kwargs))


class FakeIfs:
    def __init__(self):
        self.status = {
            "connected": True, "busy": False, "channel": 3, "state": 5,
            "activity": "ready", "loaded_channels": [1, 3, 4],
            "tool_map": {"0": 1, "1": 2, "2": 3, "3": 4},
        }
        self.materials = {"available": True, "slots": {
            "1": {"type": "PLA", "color": "#FF7F37", "temp": 208.0},
            "2": {"type": None, "color": "#E53935", "temp": None},
            "3": {"type": "PETG", "color": "", "temp": 250.0},
            "4": {"type": None, "color": "#FF9800", "temp": None},
        }}

    def get_status(self, eventtime):
        return copy.deepcopy(self.status)

    def materials_status(self):
        return copy.deepcopy(self.materials)


class FakeSensor:
    def __init__(self):
        self.detected = True

    def get_status(self, eventtime):
        return {"filament_detected": self.detected, "enabled": True}


class FakeSpoolmanSlots:
    def __init__(self, slots):
        self.slots = dict(slots)

    def get_status(self, eventtime):
        return {"slots": dict(self.slots)}


class FakeSaveVariables:
    def __init__(self):
        self.allVariables = {}


class FakePrinter:
    command_error = CommandError

    def __init__(self):
        self.reactor = FakeReactor()
        self.gcode = FakeGcode()
        self.webhooks = FakeWebhooks()
        self.ifs = FakeIfs()
        self.sensor = FakeSensor()
        self.save_variables = FakeSaveVariables()
        self.objects = {
            "gcode": self.gcode, "webhooks": self.webhooks, "ifs": self.ifs,
            "adc_filament_sensor toolhead": self.sensor,
            "save_variables": self.save_variables,
        }
        self.handlers = {}

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)

    def add_object(self, name, obj):
        assert name not in self.objects, name
        self.objects[name] = obj

    def register_event_handler(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def send(self, event):
        for handler in self.handlers.get(event, []):
            handler()

    def config_error(self, msg):
        return CommandError(msg)


class FakeConfig:
    def __init__(self, printer, **options):
        self.printer = printer
        self.options = options

    def get_printer(self):
        return self.printer

    def get(self, name, default=None):
        return self.options.get(name, default)

    def getfloat(self, name, default=None, minval=None):
        return float(self.options.get(name, default))

    def getboolean(self, name, default=None):
        return self.options.get(name, default)

    def error(self, msg):
        return CommandError(msg)


def make(**options):
    printer = FakePrinter()
    obj = afc.load_config(FakeConfig(printer, **options))
    printer.send("klippy:connect")
    printer.send("klippy:ready")
    return printer, obj


def tick(printer):
    """Advance time so the next status read is a new snapshot."""
    printer.reactor.now += 1.
    return printer.reactor.now


class StatusTest(unittest.TestCase):
    def test_objects(self):
        printer, obj = make()
        for name in ("AFC", "AFC_unit IFS", "AFC_lane E0", "AFC_lane E3"):
            self.assertIn(name, printer.objects)
        self.assertEqual(printer.objects["AFC_unit IFS"].get_status()["lanes"],
                         ["E0", "E1", "E2", "E3"])

    def test_conflict(self):
        printer = FakePrinter()
        printer.objects["AFC"] = object()
        with self.assertRaises(CommandError):
            afc.load_config(FakeConfig(printer))

    def test_afc_status(self):
        printer, obj = make()
        st = obj.get_status(tick(printer))
        self.assertEqual(st["current_lane"], "E2")
        self.assertEqual(st["current_state"], "idle")
        self.assertEqual(st["units"], ["unit IFS"])
        self.assertFalse(st["error_state"])

    def test_lanes(self):
        printer, obj = make()
        e0 = printer.objects["AFC_lane E0"].get_status(tick(printer))
        self.assertTrue(e0["load"])
        self.assertFalse(e0["tool_loaded"])
        self.assertEqual(e0["material"], "PLA")
        self.assertEqual(e0["color"], "#FF7F37")
        self.assertEqual(e0["extruder_temp"], 208)
        self.assertEqual(e0["map"], "T0")
        self.assertEqual(e0["weight"], 1000)
        e1 = printer.objects["AFC_lane E1"].get_status(tick(printer))
        self.assertFalse(e1["load"])
        self.assertEqual(e1["status"], "empty")
        self.assertEqual(e1["material"], "")
        self.assertNotIn("filament_name", e1)
        e2 = printer.objects["AFC_lane E2"].get_status(tick(printer))
        self.assertTrue(e2["tool_loaded"])
        self.assertEqual(e2["color"], "#FFFFFF")

    def test_head_empty(self):
        printer, obj = make()
        printer.sensor.detected = False
        e2 = printer.objects["AFC_lane E2"].get_status(tick(printer))
        self.assertFalse(e2["tool_loaded"])

    def test_tool_map(self):
        printer, obj = make()
        printer.ifs.status["tool_map"] = {"0": 3, "1": 3, "2": 3, "3": 4}
        now = tick(printer)
        self.assertEqual(printer.objects["AFC_lane E2"].get_status(now)["map"], "T0")
        self.assertEqual(printer.objects["AFC_lane E0"].get_status(now)["map"], "NONE")

    def test_ifs_not_connected_shows_typed_slots(self):
        printer, obj = make()
        printer.ifs.status.update(connected=False, loaded_channels=[], state=None)
        now = tick(printer)
        self.assertTrue(printer.objects["AFC_lane E0"].get_status(now)["load"])
        self.assertFalse(printer.objects["AFC_lane E1"].get_status(now)["load"])

    def test_activity(self):
        printer, obj = make()
        printer.ifs.status.update(busy=True, activity="unloading")
        self.assertEqual(obj.get_status(tick(printer))["current_state"], "unloading")

    def test_weight_saved(self):
        printer, obj = make(default_weight=0.)
        printer.save_variables.allVariables["kiln_afclite_weight_e1"] = 750
        now = tick(printer)
        self.assertEqual(printer.objects["AFC_lane E1"].get_status(now)["weight"], 750)
        self.assertNotIn("weight", printer.objects["AFC_lane E0"].get_status(now))

    def test_status_before_connect(self):
        printer = FakePrinter()
        obj = afc.load_config(FakeConfig(printer))
        self.assertIsNone(obj.get_status(1.)["current_lane"])
        self.assertFalse(printer.objects["AFC_lane E0"].get_status(1.)["load"])


class LaneOpTest(unittest.TestCase):
    def test_load(self):
        printer, obj = make()
        printer.gcode.commands["_AFC_LANE_OP"](FakeGcmd(ACTION="load", LANE="e1"))
        self.assertEqual(printer.gcode.scripts, ["_AFC_KILN_LANE LANE=E1 ACTION=load"])
        self.assertIsNone(obj.operation)

    def test_unload_without_lane_takes_loaded(self):
        printer, obj = make()
        printer.gcode.commands["_AFC_LANE_OP"](FakeGcmd(ACTION="unload"))
        self.assertEqual(printer.gcode.scripts, ["_AFC_KILN_LANE LANE=E2 ACTION=unload"])

    def test_unload_nothing_loaded(self):
        printer, obj = make()
        printer.ifs.status["channel"] = 0
        tick(printer)
        with self.assertRaises(CommandError):
            printer.gcode.commands["_AFC_LANE_OP"](FakeGcmd(ACTION="unload"))

    def test_eject_needs_lane(self):
        printer, obj = make()
        with self.assertRaises(CommandError):
            printer.gcode.commands["_AFC_LANE_OP"](FakeGcmd(ACTION="eject"))

    def test_error_reported_and_cleared(self):
        printer, obj = make()
        printer.gcode.fail = "_AFC_KILN_LANE"
        with self.assertRaises(CommandError):
            printer.gcode.commands["_AFC_LANE_OP"](FakeGcmd(ACTION="load", LANE="E1"))
        st = obj.get_status(tick(printer))
        self.assertEqual(st["current_state"], "error")
        self.assertTrue(st["error_state"])
        self.assertIn("timed out", st["message"]["message"])
        printer.ifs.status["state"] = afc.IFS_STATE_FAULT
        tick(printer)
        printer.gcode.fail = None
        gcmd = FakeGcmd()
        printer.gcode.commands["AFC_RESET"](gcmd)
        self.assertIn("IFS_RESET", printer.gcode.scripts)
        self.assertEqual(obj.get_status(tick(printer))["current_state"], "idle")


class HelpersTest(unittest.TestCase):
    def test_resolve_material(self):
        self.assertEqual(afc.resolve_material("pla"), "PLA")
        self.assertEqual(afc.resolve_material("PLA Silk"), "PLA-SILK")
        self.assertEqual(afc.resolve_material("PETG-CF"), "PETG-CF")
        self.assertIsNone(afc.resolve_material(""))
        self.assertIsNone(afc.resolve_material("A" * 17))
        self.assertIsNone(afc.resolve_material("PLA/PHA"))

    def test_spoolman_color(self):
        self.assertEqual(afc.spoolman_color({"color_hex": "ff0000"}), "FF0000")
        self.assertEqual(afc.spoolman_color(
            {"color_hex": None, "multi_color_hexes": "zz,00ff00"}), "00FF00")
        self.assertIsNone(afc.spoolman_color({}))

    def test_card_uids(self):
        spool = {"extra": {"card_uids": '"aabb,CCDD"'}}
        self.assertEqual(afc.decode_card_uids(spool), ["AABB", "CCDD"])
        self.assertEqual(afc.normalize_card_uid("04:a1-b2"), "04A1B2")
        self.assertIsNone(afc.normalize_card_uid("xyz"))


class SpoolmanTest(unittest.TestCase):
    def setUp(self):
        self.printer, self.obj = make()
        self.link = self.obj.spoolman
        self.printer.webhooks._remote_methods[afc.SPOOLMAN_REMOTE_METHOD] = True
        self.spool = {"id": 7, "filament": {
            "name": "Matte Black", "material": "PLA", "color_hex": "111111",
            "settings_extruder_temp": 215, "vendor": {"name": "Acme"}}}
        self.link._proxy = lambda method, path, query=None, body=None: self.spool

    def test_set_spool(self):
        gcmd = FakeGcmd(LANE="E1", SPOOL_ID="7")
        self.printer.gcode.commands["SET_SPOOL_ID"](gcmd)
        self.assertEqual(self.printer.gcode.scripts, [
            "SAVE_VARIABLE VARIABLE=kiln_afclite_spool_e1 VALUE=7",
            "IFS_SET_MATERIAL SLOT=2 TYPE=PLA COLOR=111111 TEMP=215",
        ])
        self.assertIn("Acme Matte Black", gcmd.messages[-1])

    def test_set_spool_passes_on_to_kiln_spoolman(self):
        self.printer.objects["spoolman_slots"] = object()
        self.link._handle_ready()
        self.printer.gcode.commands["SET_SPOOL_ID"](FakeGcmd(LANE="E1", SPOOL_ID="7"))
        self.assertIn("SPOOLMAN_SET_SLOT SLOT=2 SPOOL=7", self.printer.gcode.scripts)

    def test_clear(self):
        self.printer.gcode.commands["SET_SPOOL_ID"](FakeGcmd(LANE="E0", SPOOL_ID="0"))
        self.assertEqual(self.printer.gcode.scripts,
                         ["SAVE_VARIABLE VARIABLE=kiln_afclite_spool_e0 VALUE=0"])

    def test_unavailable(self):
        self.printer.webhooks._remote_methods.clear()
        with self.assertRaises(CommandError):
            self.printer.gcode.commands["SET_SPOOL_ID"](FakeGcmd(LANE="E0", SPOOL_ID="7"))

    def test_spool_id_in_lane_status(self):
        self.printer.save_variables.allVariables["kiln_afclite_spool_e0"] = 7
        st = self.printer.objects["AFC_lane E0"].get_status(tick(self.printer))
        self.assertEqual(st["spool_id"], 7)

    def test_active_spool(self):
        self.printer.save_variables.allVariables["kiln_afclite_spool_e2"] = 9
        now = self.printer.reactor.now + afc.SPOOLMAN_CLEAR_DELAY + 1
        self.printer.reactor.now = now
        self.link._poll(now)
        self.assertEqual(self.printer.webhooks.calls,
                         [(afc.SPOOLMAN_REMOTE_METHOD, {"spool_id": 9})])
        self.printer.sensor.detected = False
        self.link._poll(tick(self.printer))
        self.assertEqual(self.printer.webhooks.calls[-1][1], {"spool_id": None})

    def test_no_lane_spools_leaves_active_spool(self):
        now = self.printer.reactor.now + afc.SPOOLMAN_CLEAR_DELAY + 1
        self.link._poll(now)
        self.assertEqual(self.printer.webhooks.calls, [])

    def test_clear_on_change(self):
        self.printer.save_variables.allVariables["kiln_afclite_spool_e0"] = 7
        self.link._poll(tick(self.printer))
        self.printer.ifs.materials["slots"]["1"]["color"] = "#000000"
        self.link._poll(tick(self.printer))
        self.assertEqual(self.printer.reactor.callbacks, [])
        self.printer.reactor.now += afc.SPOOLMAN_CHANGE_GRACE
        self.link._poll(self.printer.reactor.now)
        self.assertEqual(len(self.printer.reactor.callbacks), 1)
        self.printer.reactor.callbacks[0](0)
        self.assertIn("kiln_afclite_spool_e0 VALUE=0", self.printer.gcode.scripts[-1])

    def test_own_change_keeps_spool(self):
        self.printer.save_variables.allVariables["kiln_afclite_spool_e1"] = 7
        self.link._poll(tick(self.printer))
        self.printer.gcode.commands["SET_SPOOL_ID"](FakeGcmd(LANE="E1", SPOOL_ID="7"))
        self.printer.ifs.materials["slots"]["2"].update(type="PLA", color="#111111")
        self.link._poll(tick(self.printer))
        self.assertEqual(self.printer.reactor.callbacks, [])

    def use_kiln_spoolman(self, slots):
        kiln = FakeSpoolmanSlots(slots)
        self.printer.objects["spoolman_slots"] = kiln
        self.printer.objects["AFC"].state.handle_connect()
        self.link._handle_ready()
        return kiln

    def test_kiln_spoolman_slot_in_lane_status(self):
        self.use_kiln_spoolman({"1": None, "2": 20, "3": None, "4": None})
        st = self.printer.objects["AFC_lane E1"].get_status(tick(self.printer))
        self.assertEqual(st["spool_id"], 20)
        st = self.printer.objects["AFC_lane E0"].get_status(tick(self.printer))
        self.assertIsNone(st["spool_id"])

    def test_nfc_scan_keeps_new_spool(self):
        kiln = self.use_kiln_spoolman({"2": 5})
        self.link._poll(tick(self.printer))
        # KILN_NFC: IFS_SET_MATERIAL, then SPOOLMAN_SET_SLOT, between polls.
        self.printer.ifs.materials["slots"]["2"]["color"] = "#FFC0CB"
        kiln.slots["2"] = 20
        self.link._poll(tick(self.printer))
        self.printer.reactor.now += afc.SPOOLMAN_CHANGE_GRACE
        self.link._poll(self.printer.reactor.now)
        self.assertEqual(self.printer.reactor.callbacks, [])

    def test_nfc_scan_spool_after_material_poll(self):
        kiln = self.use_kiln_spoolman({"2": 5})
        self.link._poll(tick(self.printer))
        self.printer.ifs.materials["slots"]["2"]["color"] = "#FFC0CB"
        self.link._poll(tick(self.printer))
        kiln.slots["2"] = 20
        self.link._poll(tick(self.printer))
        self.printer.reactor.now += afc.SPOOLMAN_CHANGE_GRACE
        self.link._poll(self.printer.reactor.now)
        self.assertEqual(self.printer.reactor.callbacks, [])

    def test_kiln_spoolman_cleared_on_change(self):
        self.use_kiln_spoolman({"2": 5})
        self.link._poll(tick(self.printer))
        self.printer.ifs.materials["slots"]["2"]["color"] = "#000000"
        self.link._poll(tick(self.printer))
        self.printer.reactor.now += afc.SPOOLMAN_CHANGE_GRACE
        self.link._poll(self.printer.reactor.now)
        self.assertEqual(len(self.printer.reactor.callbacks), 1)
        self.printer.reactor.callbacks[0](0)
        self.assertIn("SPOOLMAN_SET_SLOT SLOT=2 SPOOL=", self.printer.gcode.scripts[-1])

    def test_clear_on_removed(self):
        self.printer.save_variables.allVariables["kiln_afclite_spool_e0"] = 7
        self.link._poll(tick(self.printer))
        self.printer.ifs.status["loaded_channels"] = [3, 4]
        self.link._poll(tick(self.printer))
        self.assertEqual(self.printer.reactor.callbacks, [])
        self.printer.reactor.now += afc.SPOOLMAN_CLEAR_DELAY
        self.link._poll(self.printer.reactor.now)
        self.assertEqual(len(self.printer.reactor.callbacks), 1)


if __name__ == "__main__":
    unittest.main()
