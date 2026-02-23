# Changelog

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
