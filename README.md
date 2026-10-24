# Kiln AFC Lite

An AFC-compatible status and control adapter for the four-slot IFS of the
Flashforge AD5X on [Kiln OS](https://github.com/djvalic/kiln-os).

It is the Kiln OS port of [zmod_afclite](https://github.com/DIEHARDave/zmod_afclite),
which does the same on Zmod. The status shape follows the U1
[AFC-Lite stub](https://snapmakeru1-extended-firmware.pages.dev/afc-lite).

IFS slots 1-4 become AFC lanes E0-E3 in unit `IFS`. All lanes feed the single
Klipper `extruder`; the current lane is the slot Kiln has loaded into the
head (`printer.ifs.channel`).

## What it does

- The **AFC panels in Mainsail and Fluidd** show the four lanes: filament
  present (`printer.ifs.loaded_channels`), material, color, nozzle
  temperature, the lane in the toolhead, and each lane's tool from Kiln's
  per-print tool map.
- **Load, Unload and Eject** use Kiln's own IFS commands. The printer is homed
  first if it is not.

  | AFC command | Kiln command | |
  |---|---|---|
  | `CHANGE_TOOL LANE=E1` | `IFS_LOAD SLOT=2` | load into the toolhead (the loaded lane is cut and unloaded first) |
  | `TOOL_UNLOAD [LANE=E1]` | `IFS_UNLOAD SLOT=2` | cut and unload the toolhead; without `LANE`, the lane in the toolhead |
  | `LANE_UNLOAD LANE=E1` | `IFS_EJECT SLOT=2` | pull the lane's filament out of the IFS |

- Load and unload progress and errors are reported as AFC does:
  `current_state` is `loading` or `unloading` while the IFS works (also during
  tool changes in a print), and a failure such as an IFS stall sets
  `error_state` and `message`, which Mainsail, Fluidd and HelixScreen show.
  `AFC_RESET`, `RESET_FAILURE` or `AFC_CLEAR_MESSAGE` clears it, and returns
  an IFS left faulted by the stall to idle (`IFS_RESET`).
- **Color and material** from the filament dialog go to `IFS_SET_MATERIAL`, the
  same storage as the touch screen. Kiln accepts any material name of up to
  16 letters, digits and `+_.-`; names in Kiln's temperature list also set
  the slot's handling temperature. An empty color or material (HelixScreen's
  lane **Clear**) leaves the slot as it is.
- **`SET_MAP LANE=E1 MAP=T0`** aims a slicer tool at a lane through Kiln's
  per-print tool map (`IFS_MAP_TOOL`); the slot must hold filament.
  `IFS_MAP_TOOL RESET=1` puts T0-T3 back on slots 1-4.
- **Weights**: the IFS does not measure filament. Mainsail's and Fluidd's
  filament dialogs only apply a color or material to a lane with a weight, so
  every lane reports `default_weight` (1000 g) until you enter one. The
  weight you enter (`SET_WEIGHT`) is saved per lane in `save_variables`;
  `WEIGHT=0` goes back to the default.
- **Spoolman** spools per lane (below).

OrcaSlicer's filament sync needs nothing from this plugin: Kiln's own
`kiln_lanes` Moonraker component keeps the `lane_data` namespace up to date
from the same slot data.

## HelixScreen

HelixScreen picks its AFC backend whenever Klipper has an `AFC` object, ahead
of its native Kiln IFS backend, and it has no setting to choose. So **while
this plugin is enabled, HelixScreen shows the IFS through its AFC screens**
from its next start (Klipper restarts alone leave the running screen as it
is). Load, unload, eject, color, material, weight and Spoolman spools work
there through the commands above. Features that only the native IFS screens
offer, such as auto-refill and swap tuning, are still available as G-code
(`IFS_AUTO_REFILL`, `IFS_TUNE`) but not on the touch screen. Disable the
plugin to go back.

## Spoolman

With [Spoolman](https://github.com/Donkie/Spoolman) set up in Moonraker
(a `[spoolman]` section in `moonraker.conf`), each lane can be tied to a
spool. Mainsail and Fluidd then show that spool's details from Spoolman, and
clicking a lane's spool opens the Spoolman spool picker. Assigning a spool
also writes its material, color and nozzle temperature into the IFS slot.

Other tools (NFC readers, phone apps, scripts) assign spools with the same
command, through Moonraker's `/printer/gcode/script`:

```gcode
SET_SPOOL_ID LANE=E1 SPOOL_ID=5                  ; by Spoolman spool ID
SET_SPOOL_ID LANE=E1 CARD_UID=04A1B2C3D4E5F6     ; by NFC tag UID
SET_SPOOL_ID LANE=E1 SPOOL_ID=5 CARD_UID=04A1B2C3D4E5F6  ; pair tag with spool
SET_SPOOL_ID LANE=E1 SPOOL_ID=0                  ; clear
```

- Tag UIDs are kept in the spool's `card_uids` custom field (uppercase hex,
  comma separated), the convention of the Snapmaker U1 SpoolLink apps.
  Pairing creates the field if it is missing and moves the UID off any other
  spool.
- The assignment is saved per lane in `save_variables` as
  `kiln_afclite_spool_<lane>`.
- The spool in the loaded slot is reported to Moonraker as the active spool,
  so Spoolman deducts the filament used while printing. If Kiln's own
  `spoolman` plugin is enabled, it sets the active spool instead, and
  assignments made here are passed on to it (`SPOOLMAN_SET_SLOT`).
- When a lane's filament is pulled out of the IFS, its spool is forgotten, as
  AFC does on eject (`spoolman_clear_on_empty: False` keeps it). When a
  lane's color or material is changed elsewhere (the touch screen, an NFC
  tag), its spool is forgotten too: that is a different filament.

## Install

On the printer, as root:

```sh
git clone https://github.com/DIEHARDave/KILN_AFCLite.git /data/ad5x/plugins/afclite
chown -R klipper:klipper /data/ad5x/plugins/afclite
```

Then in the console:

```gcode
ENABLE_PLUGIN NAME=afclite
```

Klipper restarts, and Moonraker restarts once to add the plugin's update
entry (Software Updates → `kiln-afclite`). There is no root part: the plugin
is a Klipper module and macros only.

Options go in a `[kiln_afclite]` section of your own in `local.cfg`; the
defaults are listed at the top of `plugin.cfg`.

To remove it: `DISABLE_PLUGIN NAME=afclite`, then delete
`/data/ad5x/plugins/afclite`. Saved weights and spools stay in
`variables.cfg` (`kiln_afclite_*`) and are harmless.

## Not supported

- AFC hubs, buffers, runout lanes and LEDs.
- Material subtypes and vendors (the IFS does not store them).
- Other Kiln OS printers than the AD5X, and more than one IFS.

## Tests

```sh
python3 -m unittest discover tests
```

The tests run against stand-ins for Klipper and Kiln's `ifs` object.
