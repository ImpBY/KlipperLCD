from pathlib import Path
from threading import Lock
from types import SimpleNamespace

from klipperlcd.app import KlipperLCD, _env_float, _env_int
from klipperlcd.lcd import LCDEvents


class _DummyThread:
    started = 0
    target = None
    daemon = None

    def __init__(self, target=None, daemon=None):
        self.target = target
        self.daemon = daemon

    def start(self):
        _DummyThread.started += 1


def _new_app():
    app = KlipperLCD.__new__(KlipperLCD)
    app._thumbnail_lock = Lock()
    app.thumbnail_inprogress = False
    app.wait_probe = False
    app._event_rx_log = lambda evt, data: None
    app._event_tx_log = lambda action, payload=None: None
    return app


def test_env_parsers_fallback_and_parse(monkeypatch):
    monkeypatch.setenv("TEST_INT", "42")
    monkeypatch.setenv("TEST_FLOAT", "2.5")
    monkeypatch.setenv("TEST_BAD_INT", "x")
    monkeypatch.setenv("TEST_BAD_FLOAT", "x")

    assert _env_int("TEST_INT", 7) == 42
    assert _env_float("TEST_FLOAT", 1.0) == 2.5
    assert _env_int("TEST_BAD_INT", 7) == 7
    assert _env_float("TEST_BAD_FLOAT", 1.0) == 1.0
    assert _env_int("TEST_MISSING", 9) == 9
    assert _env_float("TEST_MISSING", 3.3) == 3.3


def test_start_thumbnail_worker_prevents_duplicate_threads(monkeypatch):
    app = _new_app()
    app.show_thumbnail = lambda: None
    _DummyThread.started = 0
    monkeypatch.setattr("klipperlcd.app.Thread", _DummyThread)

    first = app._start_thumbnail_worker()
    second = app._start_thumbnail_worker()

    assert first is True
    assert second is False
    assert _DummyThread.started == 1
    assert app.thumbnail_inprogress is True


def test_resolve_thumbnail_file_prefers_selected_file(tmp_path):
    app = _new_app()
    app.lcd = SimpleNamespace(files=["selected.gcode"], selected_file=0)
    app.printer = SimpleNamespace(file_path=str(tmp_path), file_name="fallback.gcode")

    file_name, base_path, file_path = app._resolve_thumbnail_file()

    assert file_name == "selected.gcode"
    assert base_path == Path(tmp_path).resolve()
    assert file_path == (Path(tmp_path) / "selected.gcode").resolve()


def test_resolve_thumbnail_file_rejects_path_traversal(tmp_path):
    app = _new_app()
    app.lcd = SimpleNamespace(files=["../../etc/passwd"], selected_file=0)
    app.printer = SimpleNamespace(file_path=str(tmp_path), file_name=None)

    assert app._resolve_thumbnail_file() == (None, None, None)


def test_extract_thumbnail_b64_returns_payload(tmp_path):
    app = _new_app()
    gcode = tmp_path / "ok.gcode"
    gcode.write_text(
        "; header\n"
        "; thumbnail begin 32x32 123\n"
        "; AAAA\n"
        "; BBBB\n"
        "; thumbnail end\n",
        encoding="utf-8",
    )

    b64 = app._extract_thumbnail_b64(gcode)

    assert b64 == "AAAABBBB"


def test_extract_thumbnail_b64_returns_none_if_too_large(tmp_path, monkeypatch):
    app = _new_app()
    monkeypatch.setattr("klipperlcd.app.THUMBNAIL_MAX_B64_LEN", 4)
    gcode = tmp_path / "large.gcode"
    gcode.write_text(
        "; thumbnail begin 32x32 123\n"
        "; AAAAA\n"
        "; thumbnail end\n",
        encoding="utf-8",
    )

    assert app._extract_thumbnail_b64(gcode) is None


def test_lcd_callback_probe_variants():
    app = _new_app()
    evt = LCDEvents()
    calls = []
    app.lcd = SimpleNamespace(evt=evt)
    app.printer = SimpleNamespace(
        probe_calibrate=lambda: calls.append(("probe_calibrate", None)),
        probe_adjust=lambda x: calls.append(("probe_adjust", x)),
    )

    app.lcd_callback(evt.PROBE, None)
    app.lcd_callback(evt.PROBE, 0.05)

    assert calls == [("probe_calibrate", None), ("probe_adjust", 0.05)]
    assert app.wait_probe is True


def test_lcd_callback_probe_complete_sends_expected_gcode():
    app = _new_app()
    evt = LCDEvents()
    sent = []
    app.lcd = SimpleNamespace(evt=evt)
    app.printer = SimpleNamespace(sendGCode=lambda cmd: sent.append(cmd))
    app.wait_probe = True

    app.lcd_callback(evt.PROBE_COMPLETE)

    assert app.wait_probe is False
    assert sent == [
        "ACCEPT",
        "G1 F1000 Z15.0",
        "BED_MESH_CALIBRATE PROFILE=default METHOD=automatic",
    ]


def test_lcd_callback_accel_to_decel_converts_percent_to_ratio():
    app = _new_app()
    evt = LCDEvents()
    sent = []
    app.lcd = SimpleNamespace(evt=evt)
    app.printer = SimpleNamespace(sendGCode=lambda cmd: sent.append(cmd))

    app.lcd_callback(evt.ACCEL_TO_DECEL, 25)

    assert sent == ["SET_VELOCITY_LIMIT MINIMUM_CRUISE_RATIO=0.25"]
