# AFC-compatible status and lane control for the Kiln OS IFS ([kiln_afclite])
#
# Kiln OS loads a plugin's extras/*.py while the plugin is enabled.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import json
import logging
import re
import threading
import urllib.error
import urllib.parse
import urllib.request


LOGGER = logging.getLogger(__name__)

# Kiln's IFS has four slots, feeding the single Klipper "extruder". They
# become AFC lanes E0-E3 in unit "IFS".
SLOTS = (1, 2, 3, 4)
UNIT_NAME = "IFS"
EXTRUDER = "extruder"
# Klipper object of the toolhead filament sensor (Kiln's ad5x.cfg).
HEAD_SENSOR = "adc_filament_sensor toolhead"
# IFS activities (printer.ifs.activity) shown as AFC's current_state, so a
# tool change during a print shows as loading/unloading too.
ACTIVITY_STATES = {"loading": "loading", "unloading": "unloading"}
# printer.ifs.state while a move has faulted (a stall); IFS_RESET clears it.
IFS_STATE_FAULT = 0x7f
# Material names IFS_SET_MATERIAL accepts (Kiln's parse_material).
MATERIAL_RE = re.compile(r"^[A-Za-z0-9+_.-]{1,16}$")
# save_variables key for a lane's weight; keep in sync with SET_WEIGHT.
WEIGHT_VARIABLE_PREFIX = "kiln_afclite_weight_"
# save_variables key for a lane's Spoolman spool ID, set by SET_SPOOL_ID.
SPOOL_VARIABLE_PREFIX = "kiln_afclite_spool_"

# Spoolman is reached through Moonraker's [spoolman] proxy, so its URL is only
# configured once, in Moonraker. NFC tag UIDs live in the spool's "card_uids"
# custom field, the convention shared with the Snapmaker U1 SpoolLink apps.
SPOOLMAN_REMOTE_METHOD = "spoolman_set_active_spool"
# Moonraker can stall for 10-15 s right after boot while it scans files.
SPOOLMAN_TIMEOUT = 30.0
SPOOLMAN_POLL = 2.0
# The IFS reports its slots a few seconds after Klipper starts, so a lane must
# read empty this long before its spool assignment is cleared.
SPOOLMAN_CLEAR_DELAY = 30.0
# After SET_SPOOL_ID writes a spool's color and material into a slot, changes
# to that slot are its own for this long, not a different filament.
SPOOLMAN_APPLY_GRACE = 10.0
# A slot's color or material changed by another plugin is only taken as a
# different filament if no new spool is assigned to the slot within this long
# (FF_NFC sets the material, then SPOOLMAN_SET_SLOT).
SPOOLMAN_CHANGE_GRACE = 5.0
CARD_UIDS_FIELD = "card_uids"
CARD_UIDS_FIELD_DEFINITION = {
    "name": "Card UIDs",
    "field_type": "text",
    "order": 1,
    "default_value": json.dumps(""),
}
# Kiln's own spoolman plugin (SPOOLMAN_SET_SLOT). While it is loaded it sets
# the active spool, and assignments made here are passed on to it.
SPOOLMAN_SLOTS_OBJECT = "spoolman_slots"


def json_request(url, method, query=None, body=None, timeout=5):
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


class AFCState:
    IDLE = "idle"
    LOADING = "loading"
    UNLOADING = "unloading"
    ERROR = "error"


class AFCLaneState:
    EMPTY = "empty"
    LOADED = "loaded"


def color_hex(value):
    """'#RRGGBB' from a 6-digit hex with or without '#', else None."""
    rgb = str(value or "").replace("#", "").strip().upper()[:6]
    try:
        if len(rgb) == 6:
            int(rgb, 16)
            return f"#{rgb}"
    except ValueError:
        pass
    return None


class KilnState:
    """One snapshot of Kiln's IFS per Klipper status poll, shared by the AFC,
    unit and lane objects so the IFS objects are read once per poll."""

    def __init__(self, printer, head_sensor):
        self.printer = printer
        self.head_sensor_name = head_sensor
        self.ifs = None
        self.head_sensor = None
        self.save_variables = None
        self.spoolman_slots = None
        self.default_weight = 0.
        self._eventtime = None
        self._snapshot = None

    def handle_connect(self):
        self.ifs = self.printer.lookup_object("ifs", None)
        if self.ifs is None or not callable(getattr(self.ifs, "materials_status", None)):
            raise self.printer.config_error(
                "Kiln AFC Lite needs Kiln OS's [ifs] (printer.ifs and "
                "printer.ifs_materials)"
            )
        self.head_sensor = self.printer.lookup_object(self.head_sensor_name, None)
        # Lane weights and spool IDs are kept in save_variables.
        self.save_variables = self.printer.lookup_object("save_variables", None)
        # With Kiln's spoolman plugin loaded, its slot spools are the lane
        # spools, so assignments made by other plugins (FF_NFC) show too.
        self.spoolman_slots = self.printer.lookup_object(SPOOLMAN_SLOTS_OBJECT, None)

    def get(self, eventtime):
        if self.ifs is None:
            # Moonraker queries status while Klipper is still starting, before
            # klippy:connect has run.
            return self._empty_snapshot()
        if eventtime is None:
            eventtime = self.printer.get_reactor().monotonic()
        if self._snapshot is None or eventtime != self._eventtime:
            self._snapshot = self._read(eventtime)
            self._eventtime = eventtime
        return self._snapshot

    @staticmethod
    def _empty_snapshot():
        """Placeholder until the IFS is found; not cached."""
        return {
            "connected": False,
            "busy": False,
            "activity": None,
            "state": None,
            "current_slot": 0,
            "extruder_sensor": False,
            "presence_known": False,
            "present": [],
            "slots": {},
            "mapping": {},
            "variables": {},
            "slot_spools": None,
        }

    def _read(self, eventtime):
        # Both are cached values: neither touches the IFS serial link.
        ifs = self.ifs.get_status(eventtime)
        materials = self.ifs.materials_status()
        connected = bool(ifs.get("connected"))
        # loaded_channels is the filament each lane holds; it is only known
        # once the IFS has answered.
        present = list(ifs.get("loaded_channels") or [])
        presence_known = connected and ifs.get("state") is not None
        # channel is Kiln's record of the slot loaded into the head (0 when
        # none); the head sensor tells whether filament is in it.
        current_slot = int(ifs.get("channel") or 0)
        if self.head_sensor is not None:
            extruder_sensor = bool(
                self.head_sensor.get_status(eventtime).get("filament_detected"))
        else:
            extruder_sensor = current_slot > 0
        slots = {}
        for slot, material in (materials.get("slots") or {}).items():
            try:
                slots[int(slot)] = material or {}
            except (TypeError, ValueError):
                continue
        # tool_map aims slicer tools at slots for the print ({"0": 1, ...}).
        mapping = {}
        for tool, slot in sorted((ifs.get("tool_map") or {}).items(),
                                 key=lambda item: int(item[0])):
            try:
                mapping.setdefault(int(slot), f"T{int(tool)}")
            except (TypeError, ValueError):
                continue
        slot_spools = None
        if self.spoolman_slots is not None:
            slot_spools = {}
            raw = (self.spoolman_slots.get_status(eventtime) or {}).get("slots") or {}
            for slot, spool_id in raw.items():
                try:
                    slot_spools[int(slot)] = int(spool_id) if spool_id else None
                except (TypeError, ValueError):
                    continue
        return {
            "connected": connected,
            "busy": bool(ifs.get("busy")),
            "activity": ifs.get("activity"),
            "state": ifs.get("state"),
            "current_slot": current_slot,
            "extruder_sensor": extruder_sensor,
            "presence_known": presence_known,
            "present": present,
            "slots": slots,
            "mapping": mapping,
            "variables": dict(getattr(self.save_variables, "allVariables", None) or {}),
            "slot_spools": slot_spools,
        }


def resolve_material(material):
    """IFS material type for a Spoolman material, or None. Kiln accepts any
    name of up to 16 letters, digits and +_.- (spaces become -)."""
    material = "-".join(str(material or "").upper().split())
    return material if MATERIAL_RE.match(material) else None


def spoolman_color(filament):
    """First 6-digit RGB color of a Spoolman filament, or None."""
    colors = [filament.get("color_hex") or ""]
    colors += str(filament.get("multi_color_hexes") or "").split(",")
    for color in colors:
        color = color_hex(color)
        if color:
            return color[1:]
    return None


def decode_card_uids(spool):
    """Card UIDs of a Spoolman spool. Spoolman stores custom text fields JSON
    encoded, so the value is usually '"AABBCCDD,11223344"'."""
    raw = (spool.get("extra") or {}).get(CARD_UIDS_FIELD) or ""
    try:
        decoded = json.loads(raw)
        if isinstance(decoded, str):
            raw = decoded
    except ValueError:
        pass
    return [uid.strip().upper() for uid in str(raw).split(",") if uid.strip()]


def normalize_card_uid(uid):
    uid = "".join(ch for ch in str(uid) if ch not in ": -").upper()
    try:
        int(uid, 16)
    except ValueError:
        return None
    return uid


class SpoolmanLink:
    _UNSET = object()

    """Lane spool assignments backed by Spoolman.

    SET_SPOOL_ID looks a spool up by ID or NFC card UID through Moonraker's
    [spoolman] proxy, saves its ID for the lane, and writes the spool's
    material, color and temperature into the IFS slot. Mainsail and Fluidd
    then show the spool's details from Spoolman. The spool in the loaded slot
    is reported to Moonraker as the active spool, so Spoolman tracks the
    filament used."""

    def __init__(self, printer, state, lanes, moonraker_url, clear_on_empty,
                 set_temperature):
        self.printer = printer
        self.reactor = printer.get_reactor()
        self.state = state
        self.lanes = lanes
        self.url = moonraker_url.rstrip("/") + "/server/spoolman/proxy"
        self.clear_on_empty = clear_on_empty
        self.set_temperature = set_temperature
        self.gcode = printer.lookup_object("gcode")
        self.webhooks = printer.lookup_object("webhooks")
        self.spoolman_slots = None
        # Spool last reported to Moonraker; _UNSET until the first report.
        self._active = self._UNSET
        self._ready_time = None
        self._loaded = set()
        self._empty_since = {}
        # Last seen (color, material) per slot, and until when changes to a
        # slot come from this plugin's own SET_SPOOL_ID.
        self._filament = {}
        self._applying = {}
        # Slots whose filament changed: (when to clear, spool at the change).
        self._changed = {}
        self._spools = {}
        self._timer = None
        self.gcode.register_command(
            "SET_SPOOL_ID", self.cmd_SET_SPOOL_ID,
            desc="Assign a Spoolman spool to an AFC lane by ID or NFC card UID",
        )
        printer.register_event_handler("klippy:ready", self._handle_ready)
        printer.register_event_handler("klippy:disconnect", self._handle_disconnect)

    def available(self):
        """True while Moonraker has a [spoolman] section and is connected."""
        methods = getattr(self.webhooks, "_remote_methods", None) or {}
        return SPOOLMAN_REMOTE_METHOD in methods

    def _handle_ready(self):
        self.spoolman_slots = self.printer.lookup_object(SPOOLMAN_SLOTS_OBJECT, None)
        self._ready_time = self.reactor.monotonic()
        self._timer = self.reactor.register_timer(self._poll, self.reactor.NOW)

    def _handle_disconnect(self):
        if self._timer is not None:
            self.reactor.unregister_timer(self._timer)
            self._timer = None

    # Spoolman requests -----------------------------------------------------

    def _proxy(self, method, path, query=None, body=None):
        """Send a request through Moonraker's Spoolman proxy without blocking
        the reactor, and return Spoolman's response."""
        payload = {"request_method": method, "path": path, "use_v2_response": True}
        if query:
            payload["query"] = urllib.parse.urlencode(query)
        if body is not None:
            payload["body"] = body
        completion = self.reactor.completion()

        def run():
            try:
                result = (json_request(
                    self.url, "POST", body=payload, timeout=SPOOLMAN_TIMEOUT
                ), None)
            except Exception as exc:
                result = (None, exc)
            self.reactor.async_complete(completion, result)

        thread = threading.Thread(target=run, name="kiln_afclite spoolman")
        thread.daemon = True
        thread.start()
        reply, exc = completion.wait(
            self.reactor.monotonic() + SPOOLMAN_TIMEOUT + 1.,
            (None, TimeoutError("no reply from Moonraker")),
        )
        if exc is not None:
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
                exc = "Moonraker has no [spoolman] section"
            elif isinstance(exc, (TimeoutError, OSError)) and "timed out" in str(exc):
                exc = "Moonraker did not answer in time (busy?), try again"
            raise self.printer.command_error(f"Spoolman request failed: {exc}")
        result = reply.get("result") or {}
        error = result.get("error")
        if error:
            message = error.get("message") if isinstance(error, dict) else error
            raise self.printer.command_error(f"Spoolman: {message}")
        return result.get("response")

    def _get_spool(self, spool_id):
        spool = self._proxy("GET", f"/v1/spool/{spool_id}")
        if not isinstance(spool, dict) or "id" not in spool:
            raise self.printer.command_error(f"Spoolman has no spool {spool_id}")
        return spool

    def _all_spools(self):
        spools = self._proxy("GET", "/v1/spool", {"allow_archived": "true"})
        return spools if isinstance(spools, list) else []

    def _find_by_card(self, card_uid):
        for spool in self._all_spools():
            if card_uid in decode_card_uids(spool) and not spool.get("archived"):
                return spool
        raise self.printer.command_error(
            f"No Spoolman spool has card UID {card_uid}. Pair it once with "
            f"SET_SPOOL_ID LANE=<lane> SPOOL_ID=<id> CARD_UID={card_uid}"
        )

    def _set_card_uids(self, spool, uids):
        self._proxy("PATCH", f"/v1/spool/{spool['id']}", body={
            "extra": {CARD_UIDS_FIELD: json.dumps(",".join(uids))},
        })

    def _bind_card(self, spool, card_uid):
        """Add card_uid to the spool and remove it from every other spool, so a
        later scan of the tag resolves to this spool."""
        fields = self._proxy("GET", "/v1/field/spool") or []
        if not any(field.get("key") == CARD_UIDS_FIELD for field in fields):
            self._proxy(
                "POST", f"/v1/field/spool/{CARD_UIDS_FIELD}",
                body=CARD_UIDS_FIELD_DEFINITION,
            )
        uids = decode_card_uids(spool)
        if card_uid not in uids:
            self._set_card_uids(spool, uids + [card_uid])
        for other in self._all_spools():
            other_uids = decode_card_uids(other)
            if other["id"] != spool["id"] and card_uid in other_uids:
                self._set_card_uids(other, [u for u in other_uids if u != card_uid])

    # Lane assignment -------------------------------------------------------

    def _save(self, lane, spool_id):
        if self.state.save_variables is None:
            raise self.printer.command_error(
                "No [save_variables] configured, so the lane spool cannot be saved."
            )
        self.gcode.run_script_from_command(
            f"SAVE_VARIABLE VARIABLE={SPOOL_VARIABLE_PREFIX}{lane.name.lower()} "
            f"VALUE={spool_id}"
        )
        if self.spoolman_slots is not None:
            self.gcode.run_script_from_command(
                f"SPOOLMAN_SET_SLOT SLOT={lane.slot} SPOOL={spool_id or ''}"
            )

    def _apply(self, gcmd, lane, spool):
        """Write the spool's material, color and temperature into the slot."""
        filament = spool.get("filament") or {}
        args = []
        material = resolve_material(filament.get("material"))
        if material is None:
            gcmd.respond_info(
                f"Spoolman material {filament.get('material')!r} is not a name "
                f"the IFS accepts; {lane.name} keeps its material."
            )
        else:
            args.append(f"TYPE={material}")
        color = spoolman_color(filament)
        if color is not None:
            args.append(f"COLOR={color}")
        temp = filament.get("settings_extruder_temp")
        if self.set_temperature and isinstance(temp, (int, float)) and 150 <= temp <= 350:
            args.append(f"TEMP={temp:g}")
        if not args:
            return
        self._applying[lane.slot] = self.reactor.monotonic() + SPOOLMAN_APPLY_GRACE
        self.gcode.run_script_from_command(
            f"IFS_SET_MATERIAL SLOT={lane.slot} {' '.join(args)}"
        )

    def cmd_SET_SPOOL_ID(self, gcmd):
        lane = self.lanes.get(gcmd.get("LANE", "").strip().upper())
        if lane is None:
            raise gcmd.error("LANE must be an AFC lane such as E0.")
        spool_id = gcmd.get("SPOOL_ID", "").strip()
        try:
            spool_id = int(spool_id or 0)
        except ValueError:
            raise gcmd.error("SPOOL_ID must be a Spoolman spool ID.")
        card_uid = gcmd.get("CARD_UID", "").strip()
        if card_uid:
            card_uid = normalize_card_uid(card_uid)
            if card_uid is None:
                raise gcmd.error("CARD_UID must be the tag UID in hex.")

        if spool_id <= 0 and not card_uid:
            self._save(lane, 0)
            gcmd.respond_info(f"{lane.name}: Spoolman spool cleared.")
            return
        if not self.available():
            raise gcmd.error(
                "Spoolman is not available. Add a [spoolman] section to "
                "moonraker.conf and restart Moonraker."
            )

        if spool_id > 0:
            spool = self._get_spool(spool_id)
            if card_uid:
                self._bind_card(spool, card_uid)
        else:
            spool = self._find_by_card(card_uid)
        self._save(lane, spool["id"])
        self._apply(gcmd, lane, spool)
        filament = spool.get("filament") or {}
        vendor = (filament.get("vendor") or {}).get("name")
        name = " ".join(str(part) for part in (vendor, filament.get("name")) if part)
        gcmd.respond_info(f"{lane.name}: Spoolman spool {spool['id']} {name}".rstrip())

    # Active spool ----------------------------------------------------------

    def _poll(self, eventtime):
        if self.state.ifs is None:
            return eventtime + SPOOLMAN_POLL
        try:
            snapshot = self.state.get(eventtime)
            # Assignments are checked even while Spoolman is unreachable, so
            # a filament change then is not missed.
            self._check_assignments(snapshot, eventtime)
            # Kiln's spoolman plugin sets the active spool itself.
            if self.available() and self.spoolman_slots is None:
                self._update(snapshot, eventtime)
        except Exception:
            LOGGER.exception("Kiln AFC Lite: Spoolman active spool update failed")
        return eventtime + SPOOLMAN_POLL

    def _check_assignments(self, snapshot, eventtime):
        self._clear_changed(snapshot, eventtime)
        if self.clear_on_empty and snapshot["presence_known"]:
            self._clear_removed(snapshot, eventtime)

    def _update(self, snapshot, eventtime):
        """Report the spool feeding the extruder as Moonraker's active spool,
        which Spoolman's usage tracking and the Spoolman panel follow."""
        if self._ready_time is not None and eventtime - self._ready_time < SPOOLMAN_CLEAR_DELAY:
            # The head sensor and the IFS status settle after startup.
            return
        current = snapshot["current_slot"]
        loaded = current in SLOTS
        if snapshot["extruder_sensor"] and not loaded:
            # Filament is in the head but Kiln has no record of its slot:
            # keep the last.
            return
        active = None
        if snapshot["extruder_sensor"]:
            lane = self.lanes.get(f"E{current - 1}")
            active = lane.spool_id(snapshot) if lane is not None else None
        if self._active is self._UNSET and active is None and not any(
                lane.spool_id(snapshot) for lane in self.lanes.values()):
            # Nobody uses lane spools: leave a spool set outside this plugin.
            self._active = None
            return
        if active == self._active:
            return
        try:
            self.webhooks.call_remote_method(SPOOLMAN_REMOTE_METHOD, spool_id=active)
        except self.printer.command_error as exc:
            LOGGER.info("Kiln AFC Lite: unable to set active spool: %s", exc)
            return
        self._active = active

    def _clear_spool(self, lane, reason):
        message = f"{lane.name}: {reason}, Spoolman spool cleared."
        LOGGER.info("Kiln AFC Lite: %s", message)
        script = f"SAVE_VARIABLE VARIABLE={SPOOL_VARIABLE_PREFIX}{lane.name.lower()} VALUE=0\n"
        if self.spoolman_slots is not None:
            script += f"SPOOLMAN_SET_SLOT SLOT={lane.slot} SPOOL=\n"
        script += f"RESPOND MSG=\"{message}\""
        self.reactor.register_callback(lambda e: self.gcode.run_script(script))

    def _clear_changed(self, snapshot, eventtime):
        """Forget a lane's spool once its color or material is changed outside
        this plugin (the touch screen, IFS_SET_MATERIAL, an NFC tag): that is
        a different filament, and Mainsail and Fluidd would otherwise keep
        showing the spool's name and material from Spoolman. A spool assigned
        along with the change (an NFC scan) is kept."""
        for slot, material in snapshot["slots"].items():
            lane = self.lanes.get(f"E{slot - 1}")
            spool_id = lane.spool_id(snapshot) if lane is not None else None
            current = (
                str(material.get("color") or "").upper(),
                str(material.get("type") or "").upper(),
            )
            previous = self._filament.get(slot)
            self._filament[slot] = current
            previous_spool = self._spools.get(slot)
            self._spools[slot] = spool_id
            pending = self._changed.get(slot)
            if pending is not None:
                if spool_id != pending[1]:
                    # A spool came with the filament (an NFC scan).
                    self._changed.pop(slot)
                elif eventtime >= pending[0]:
                    self._changed.pop(slot)
                    if spool_id:
                        self._clear_spool(lane, "filament changed outside the AFC panel")
            if eventtime < self._applying.get(slot, 0.):
                continue
            self._applying.pop(slot, None)
            if previous is None or previous == current or lane is None:
                continue
            if spool_id and spool_id == previous_spool and slot not in self._changed:
                self._changed[slot] = (eventtime + SPOOLMAN_CHANGE_GRACE, spool_id)

    def _clear_removed(self, snapshot, eventtime):
        """Forget a lane's spool once its filament has been taken out of the
        IFS, as AFC does on eject; the next spool there needs its own
        SET_SPOOL_ID."""
        for slot in SLOTS:
            if slot in snapshot["present"]:
                self._loaded.add(slot)
                self._empty_since.pop(slot, None)
                continue
            if slot not in self._loaded:
                continue
            since = self._empty_since.setdefault(slot, eventtime)
            if eventtime - since < SPOOLMAN_CLEAR_DELAY:
                continue
            self._loaded.discard(slot)
            self._empty_since.pop(slot, None)
            lane = self.lanes.get(f"E{slot - 1}")
            if lane is not None and lane.spool_id(snapshot):
                self._clear_spool(lane, "filament removed")


class AFC:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.state = KilnState(
            self.printer, config.get("head_sensor", HEAD_SENSOR))
        # The Mainsail and Fluidd filament dialogs keep "Set" disabled until the
        # lane has a weight, so report one by default; 0 hides weights again.
        self.state.default_weight = config.getfloat("default_weight", 1000., minval=0.)
        self.lanes = {}

        for name in ["AFC", f"AFC_unit {UNIT_NAME}"] + [
                f"AFC_lane E{slot - 1}" for slot in SLOTS]:
            if self.printer.lookup_object(name, None) is not None:
                raise config.error(
                    f"Cannot enable Kiln AFC Lite: Klipper object {name!r} "
                    "already exists. Disable the conflicting AFC integration."
                )
        self.printer.add_object("AFC", self)
        for slot in SLOTS:
            lane = AFCLane(self.state, f"E{slot - 1}", slot)
            self.lanes[lane.name] = lane
            self.printer.add_object(f"AFC_lane {lane.name}", lane)
        self.printer.add_object(f"AFC_unit {UNIT_NAME}", AFCUnit(self.lanes))

        self.gcode = self.printer.lookup_object("gcode")
        # A lane load or unload in progress, and the error of the last one that
        # failed. Clients such as HelixScreen follow an operation through
        # current_state and show a failure from error_state and message, as
        # with AFC.
        self.operation = None
        self.error_message = ""
        self.gcode.register_command(
            "_AFC_LANE_OP", self.cmd_AFC_LANE_OP,
            desc="Load, unload or eject a lane with the IFS and report it as AFC",
        )
        for name in ("AFC_RESET", "RESET_FAILURE", "AFC_CLEAR_MESSAGE"):
            self.gcode.register_command(
                name, self.cmd_AFC_CLEAR_ERROR,
                desc="Clear the last AFC lane error",
            )

        self.spoolman = SpoolmanLink(
            self.printer, self.state, self.lanes,
            config.get("moonraker_url", "http://127.0.0.1:7125"),
            clear_on_empty=config.getboolean("spoolman_clear_on_empty", True),
            set_temperature=config.getboolean("spoolman_set_temperature", True),
        )
        self.printer.register_event_handler("klippy:connect", self.state.handle_connect)

    def cmd_AFC_LANE_OP(self, gcmd):
        action = gcmd.get("ACTION", "load").lower()
        if action not in ("load", "unload", "eject"):
            raise gcmd.error("ACTION must be load, unload or eject.")
        lane_name = gcmd.get("LANE", "").strip().upper()
        if not lane_name and action == "unload":
            # TOOL_UNLOAD without LANE (HelixScreen's error screen and its
            # unload with no lane picked) means the lane in the toolhead.
            current = self.state.get(None)["current_slot"]
            if current in SLOTS:
                lane_name = f"E{current - 1}"
        if lane_name not in self.lanes:
            if lane_name:
                raise gcmd.error(f"Unknown lane {lane_name!r}.")
            raise gcmd.error(
                "LANE is required: no lane is loaded." if action == "unload"
                else "LANE is required."
            )
        self.error_message = ""
        self.operation = AFCState.LOADING if action == "load" else AFCState.UNLOADING
        try:
            self.gcode.run_script_from_command(
                f"_AFC_KILN_LANE LANE={lane_name} ACTION={action}"
            )
        except self.gcode.error as exc:
            self.error_message = f"{lane_name}: {exc}"
            raise
        finally:
            self.operation = None

    def cmd_AFC_CLEAR_ERROR(self, gcmd):
        # There are no AFC lane motors or failure latches to reset. An IFS left
        # faulted by a stall is returned to idle (IFS_RESET only stops it).
        state = self.state.get(None)
        if state["state"] == IFS_STATE_FAULT:
            self.gcode.run_script_from_command("IFS_RESET")
        if self.error_message:
            gcmd.respond_info(f"Cleared: {self.error_message}")
        self.error_message = ""

    def get_status(self, eventtime=None):
        state = self.state.get(eventtime)
        current_lane = None
        if state["current_slot"] in SLOTS:
            current_lane = f"E{state['current_slot'] - 1}"
        current_state = self.operation or AFCState.IDLE
        if state["busy"] and not self.operation:
            current_state = ACTIVITY_STATES.get(state["activity"], current_state)
        if self.error_message:
            current_state = AFCState.ERROR

        return {
            "current_load": None,
            "current_lane": current_lane,
            "next_lane": None,
            "current_state": current_state,
            "current_toolchange": 0,
            "number_of_toolchanges": 0,
            "spoolman": self.spoolman.available(),
            "td1_present": False,
            # Kiln's kiln_lanes Moonraker component keeps lane_data itself.
            "lane_data_enabled": False,
            "error_state": bool(self.error_message),
            "bypass_state": False,
            "quiet_mode": False,
            "position_saved": False,
            # Mainsail/Fluidd expect "<type> <name>" and look the unit up as
            # the "AFC_<type> <name>" object, i.e. "AFC_unit IFS".
            "units": [f"unit {UNIT_NAME}"],
            "lanes": list(self.lanes),
            # No AFC_extruder object exists, so list none (as AFC-Lite does)
            # instead of making the UI draw an empty toolhead card.
            "extruders": [],
            "hubs": [],
            "buffers": [],
            # AFC's message shape; HelixScreen only reads it as an object and
            # treats an empty text as the error being cleared.
            "message": {
                "message": self.error_message,
                "type": "error" if self.error_message else "",
            },
            "led_state": "",
        }


class AFCLane:
    def __init__(self, state, name, slot):
        self.state = state
        self.name = name
        self.slot = slot

    def get_status(self, eventtime=None):
        state = self.state.get(eventtime)
        material = state["slots"].get(self.slot, {})
        # Without presence data (IFS not answering), a slot is shown loaded
        # rather than empty so its color and material stay visible.
        loaded = self.slot in state["present"] or (
            not state["presence_known"] and bool(material.get("type")))
        tool_loaded = state["current_slot"] == self.slot and state["extruder_sensor"]
        mtype = str(material.get("type") or "").upper()

        status = {
            "name": self.name,
            "unit": UNIT_NAME,
            "lane": self.slot - 1,
            "ifs_slot": self.slot,
            "extruder": EXTRUDER,
            "map": state["mapping"].get(self.slot, "NONE"),
            "load": loaded,
            "prep": loaded,
            "tool_loaded": tool_loaded,
            "loaded_to_hub": False,
            "material": mtype,
            "spool_id": self.spool_id(state),
            "color": color_hex(material.get("color")) or "#FFFFFF",
            "runout_lane": "NONE",
            "filament_status": "unknown",
            "filament_status_led": "gray",
            "status": AFCLaneState.LOADED if loaded else AFCLaneState.EMPTY,
        }
        if mtype:
            status["filament_name"] = mtype
        if material.get("temp"):
            status["extruder_temp"] = round(float(material["temp"]))
        weight = self.weight(state)
        if weight > 0:
            status["weight"] = weight
        return status

    def weight(self, state):
        """Weight saved by SET_WEIGHT, else the configured default. The IFS
        does not measure filament, so this is only what the user entered."""
        saved = state["variables"].get(f"{WEIGHT_VARIABLE_PREFIX}{self.name.lower()}")
        try:
            return max(0, round(float(saved)))
        except (TypeError, ValueError):
            return round(self.state.default_weight)

    def spool_id(self, state):
        """The slot's spool in Kiln's spoolman plugin when it is loaded, else
        the Spoolman spool ID saved by SET_SPOOL_ID, or None."""
        if state.get("slot_spools") is not None:
            return state["slot_spools"].get(self.slot)
        saved = state["variables"].get(f"{SPOOL_VARIABLE_PREFIX}{self.name.lower()}")
        try:
            spool_id = int(saved)
        except (TypeError, ValueError):
            return None
        return spool_id if spool_id > 0 else None


class AFCUnit:
    def __init__(self, lanes):
        self.lanes = lanes

    def get_status(self, eventtime=None):
        return {
            "lanes": list(self.lanes),
            "extruders": [],
            "hubs": [],
            "buffers": [],
        }


def load_config(config):
    return AFC(config)
