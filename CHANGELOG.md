# Changelog

## 2026-07-06

### Fixed
- LCD read thread crash (`IndexError`) when frame byte `0xA5` arrived with an empty RX buffer; frame sync is now reset on every `0x5A` header byte and invalid frame lengths are rejected.
- `READVAR` decoder crash on payloads longer than one word; all words are now decoded.
- Thumbnail rendering crash on images without an alpha channel (RGB/palette PNGs); images are normalized via `convert("RGB")`.
- Interleaved LCD serial frames: payload and `0xFFFFFF` terminator are now written atomically under a lock (writes come from multiple threads).
- `KlippySocket` attribute-initialization race: polling-thread attributes are set before the thread starts.

### Changed
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
