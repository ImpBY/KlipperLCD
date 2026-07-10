# Changelog

## 2026-07-10

### Changed
- Leveling flow reworked (requires `_LCD_BED_LEVELING`/`_LCD_LEVELING_SAVE` macros in the Klipper config): the Leveling button now runs a scan-only `_LCD_BED_LEVELING` (no `SAVE_CONFIG`), keeps the panel on the autohome page during the scan, then shows the probed mesh interpolated to the 6x6 `leveldata_36` grid. Save + Klipper restart happen only when the save element on the result screen is pressed (`_LCD_LEVELING_SAVE`, which also resets fan/filament-sensor/light to defaults first). A 15-minute fallback timeout unblocks the UI if the macro aborts without the completion marker.
- `set_flow` accepts fractional percent (`M221 S101.5`).
- Settings-page open (`0x103E/0x0B`) no longer re-sends `page set`: on the target firmware the notification comes from the page's own load event, so echoing the page switch would loop the page. Element renames: `0x2202/0x0F` is `main.page_open` (sent on every main-page entry, not a hardware test).

### Added
- Klipper-oriented HMI firmware rework spec (`LCD/klipper_ui_spec.md`) and service support for all its new inputs (backwards-compatible — stock firmware never sends them): main-screen E-STOP (`/printer/emergency_stop`, sent directly so it never waits behind a long gcode POST), power-off confirm (`/machine/shutdown`), console button (refreshes gcode history), settings-page Restart Klipper (`FIRMWARE_RESTART`) and Macros buttons, adjustzoffset page-enter indicator sync, flow-trim step selector 1/5/10. Host IP is written to `information.ip.txt` at startup.
- Settings-page toggle indicators now reflect the real printer state: on opening the settings screen the service syncs `status_led2` (light), `set.fanstatue.pic` (fan) and `set.filamentdec.pic` (filament sensor) from Klipper state (`led top_LEDs` and the filament sensor are mirrored via the periodic status query when present); fan/filament toggles update their indicator on each press.
- "E-axis pulse" screen (`motorsetting` -> `motorsetvalue`) repurposed as a flow trim: 1 unit = 0.1% flow (value 15 -> 101.5%), the service renders the value into `motorsetvalue.eaxis.val` and sends fractional `M221`.
- HMI element names for previously unknown inputs: generic back (`0x1040/0x05`), E-axis pulse buttons (`0x1056/0x0B..0x0D`), Resume Printing button (`0x105F`, stateless on this HMI revision, documented — no function attached).
- `make bump` target: bumps the version in `pyproject.toml` (`PART=patch|minor|major`, default `patch`).
- HMI interaction trace mode (`KLIPPERLCD_HMI_TRACE=1`, optional `KLIPPERLCD_HMI_TRACE_FILE`): every touch input from the LCD (`RX`, with the mapped element label; `RX?` for unknown/writevar frames) and every command issued while handling it — LCD writes (`TX`), dispatched app events (`EVENT`), Moonraker REST calls (`REST`), Klippy socket lines (`KLIPPY`) — is appended to a standalone trace file (default `~/printer_data/logs/KlipperLCD_hmi_trace.log`), independent of `KLIPPERLCD_LOG_LEVEL`. Periodic status traffic (app update loop, HMI print-status polls) is excluded, so the trace maps screen elements to their exact command flow.

## 2026-07-08

### Fixed
- Filament runout sensor toggle: on-device testing showed the HMI sends `0x103E` code `0x08` (a stateless button), not `0x105E` state codes. The button now toggles relative to the actual Klipper sensor state (queried via `/printer/objects/query`), with a local fallback when the query fails. The `0x105E` handler is kept for other HMI revisions.
- Klippy subscriptions (toolhead position, gcode output, z-offset/config query) were silently lost when the service connected to the Klippy socket while Klipper was still starting — both after a `SAVE_CONFIG` restart and at cold service start. The socket (re)connect now waits until `/printer/info` reports `state: ready`.

### Changed
- `REST POST failed` log messages now include the request payload; a client-side timeout on long-running gcode scripts (e.g. `BED_LEVELING`) is logged as a warning ("server keeps running") instead of an error.

## 2026-07-07

### Added
- `KLIPPERLCD_MOONRAKER_PORT` env variable (default `80`); the Moonraker port is no longer hard-coded.
- Filament runout sensor toggle on the HMI now works: it sends `SET_FILAMENT_SENSOR SENSOR=<KLIPPERLCD_FILAMENT_SENSOR_NAME> ENABLE=0|1` (new env variable, default `filament_runout_sensor`).
- Model cooling fan toggle on the settings screen now works (was a stub); toggling on sets the fan to 40%. The print-screen fan toggle uses the same 40% instead of 100%.

### Changed
- Print file list is sorted newest-first by Moonraker `modified` timestamp (upload time), so the latest file appears in slot 1 of page 1.
- Light toggle now switches the LED to 5% brightness (was 50%); `set_led` accepts brightness percent and emits `SET_LED ... WHITE=<0.00-1.00>`.
- The settings Leveling button now runs the full `BED_LEVELING` Klipper macro (tap + mesh into profile `default` + `SAVE_CONFIG` with Klipper restart) instead of the interactive `PROBE_CALIBRATE`/TESTZ flow.

### Fixed
- HMI file listing pagination: file names are now written to the per-page label slots (`file1.t0..t4`, `file2.t5..t9`, ... up to 25 slots) instead of flat `file1.tN`, so pages 2+ show file names again. File selection uses the absolute slot index reported by the HMI, and the selected-file highlight targets the correct page component. Stale labels are cleared on every listing refresh.

## 2026-07-06

### Fixed
- LCD read thread crash (`IndexError`) when frame byte `0xA5` arrived with an empty RX buffer; frame sync is now reset on every `0x5A` header byte and invalid frame lengths are rejected.
- `READVAR` decoder crash on payloads longer than one word; all words are now decoded.
- Thumbnail rendering crash on images without an alpha channel (RGB/palette PNGs); images are normalized via `convert("RGB")`.
- Interleaved LCD serial frames: payload and `0xFFFFFF` terminator are now written atomically under a lock (writes come from multiple threads).
- `KlippySocket` attribute-initialization race: polling-thread attributes are set before the thread starts.

### Changed
- Service env file moved from `~/.config/<repo>/service.env` to `~/printer_data/systemd/<repo>.env` to live alongside `klipper.env`/`moonraker.env`; `make config` migrates an existing legacy file automatically.
- Main update loop now issues a single combined Moonraker query per cycle (previously two; unused `motion_report` dropped).
- `KlippySocket` drains the full outgoing queue per poll cycle and uses a wakeup socketpair for immediate command transmission (previously up to 1 line/second); a dead socket is unregistered from poll to avoid busy-spinning until reconnect.
- Thumbnail encoding post-processing replaced a 256 KB per-byte filter loop with slicing by encoded length; pixel access switched to `Image.getdata()`.
- Boot progress bar value is clamped to 100.

## 2026-02-20

### Added
- Project tooling and service management files: `Makefile`, `service.template`, `service.env.example`, packaging metadata.
- Structured Python package layout under `src/klipperlcd`.
- Centralized logging setup (`src/klipperlcd/logging_setup.py`).
- Containerized test environment and unit tests for app, LCD, and printer logic.

### Changed
- Main runtime loop updated with reconnect behavior.
- Motion/config compatibility migrated from `max_accel_to_decel` to `minimum_cruise_ratio`.
- Significant runtime refactoring across app/LCD/printer modules.
- Stability and performance improvements in LCD rendering, printer state handling, and image processing.
- README and developer/test workflow documentation refreshed.

### Removed
- Legacy top-level runtime layout (`printer.py`, old service unit file) in favor of package-based structure and templates.
