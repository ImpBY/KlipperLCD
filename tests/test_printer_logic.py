from types import SimpleNamespace

import pytest

from klipperlcd.printer import PrinterData, interpolate_mesh


def _new_printer():
    p = PrinterData.__new__(PrinterData)
    p.absolute_moves = True
    p.response_callback = None
    return p


def test_progress_duration_and_remain_active_and_inactive():
    p = _new_printer()
    p.job_Info = {
        "virtual_sdcard": {"is_active": True, "progress": 0.25},
        "print_stats": {"print_duration": 100},
    }

    assert p.getPercent() == 25
    assert p.duration() == 100
    assert p.remain() == 300

    p.job_Info["virtual_sdcard"]["is_active"] = False
    assert p.getPercent() == 0
    assert p.duration() == 0
    assert p.remain() == 0


def test_update_variable_reconnects_only_when_klippy_ready():
    p = _new_printer()
    calls = []
    p.ks = SimpleNamespace(connected=False, klippyExit=lambda: calls.append("exit"))
    p.klippy_start = lambda: calls.append("start")

    # Klippy still starting up: no reconnect yet.
    p.getREST = lambda path: {"result": {"state": "startup"}}
    assert p.update_variable() is False
    assert calls == []

    # Moonraker unreachable: no reconnect either.
    p.getREST = lambda path: None
    assert p.update_variable() is False
    assert calls == []

    # Klippy ready: reconnect and resubscribe.
    p.getREST = lambda path: {"result": {"state": "ready"}}
    assert p.update_variable() is False
    assert calls == ["exit", "start"]


def test_emergency_stop_posts_directly(monkeypatch):
    p = _new_printer()
    p.op = SimpleNamespace(
        base_address="http://printer",
        s=SimpleNamespace(headers={"X-Api-Key": "k"}),
    )
    posts = []
    monkeypatch.setattr(
        "klipperlcd.printer.requests.post",
        lambda url, headers=None, timeout=None: posts.append((url, timeout)),
    )

    p.emergency_stop()

    assert posts == [("http://printer/printer/emergency_stop", 3)]


def test_host_shutdown_uses_machine_endpoint():
    p = _new_printer()
    calls = []
    p.postREST = lambda path, json=None: calls.append((path, json))

    p.host_shutdown()

    assert calls == [("/machine/shutdown", None)]


def test_interpolate_mesh_preserves_corners_and_interpolates():
    result = interpolate_mesh([[0.0, 1.0], [1.0, 2.0]], rows=3, cols=3)

    assert result == [
        [0.0, 0.5, 1.0],
        [0.5, 1.0, 1.5],
        [1.0, 1.5, 2.0],
    ]


def test_interpolate_mesh_same_size_is_identity():
    matrix = [[float(r * 6 + c) for c in range(6)] for r in range(6)]

    assert interpolate_mesh(matrix, rows=6, cols=6) == matrix


def test_get_bed_mesh_returns_probed_matrix_or_none():
    p = _new_printer()
    p.getREST = lambda path: {
        "result": {"status": {"bed_mesh": {"probed_matrix": [[0.1, 0.2], [0.3, 0.4]]}}}
    }
    assert p.get_bed_mesh() == [[0.1, 0.2], [0.3, 0.4]]

    p.getREST = lambda path: {
        "result": {"status": {"bed_mesh": {"probed_matrix": [[]]}}}
    }
    assert p.get_bed_mesh() is None

    p.getREST = lambda path: None
    assert p.get_bed_mesh() is None


def test_build_status_query_includes_only_available_objects():
    p = _new_printer()
    p.filament_sensor_name = "filament_runout_sensor"
    p._status_query = None
    p.getREST = lambda path: {
        "result": {"objects": ["extruder", "toolhead", "led top_LEDs"]}
    }

    query = p._build_status_query()

    assert "led%20top_LEDs" in query
    assert "filament_switch_sensor" not in query
    # Cached: later calls skip the objects/list round-trip.
    p.getREST = lambda path: (_ for _ in ()).throw(AssertionError("not cached"))
    assert p._build_status_query() == query


def test_build_status_query_falls_back_when_object_list_unavailable():
    p = _new_printer()
    p.filament_sensor_name = "filament_runout_sensor"
    p._status_query = None
    p.getREST = lambda path: None

    query = p._build_status_query()

    assert query.endswith("extruder&heater_bed&gcode_move&fan&print_stats&virtual_sdcard&toolhead")
    assert "led%20top_LEDs" not in query
    # Not cached on failure: retried next cycle.
    assert p._status_query is None


def test_update_optional_states_mirrors_led_and_sensor():
    p = _new_printer()
    p.filament_sensor_name = "filament_runout_sensor"
    p.led_brightness = None
    p.filament_sensor_enabled_live = None

    p._update_optional_states({
        "led top_LEDs": {"color_data": [[0.0, 0.0, 0.0, 0.05]]},
        "filament_switch_sensor filament_runout_sensor": {"enabled": False},
    })

    assert p.led_brightness == pytest.approx(5.0)
    assert p.filament_sensor_enabled_live is False

    # Objects absent from the payload leave previous values untouched.
    p._update_optional_states({})
    assert p.led_brightness == pytest.approx(5.0)
    assert p.filament_sensor_enabled_live is False


def test_get_filament_sensor_enabled_parses_state():
    p = _new_printer()
    p.getREST = lambda path: {
        "result": {
            "status": {
                "filament_switch_sensor filament_runout_sensor": {
                    "filament_detected": True,
                    "enabled": False,
                }
            }
        }
    }

    assert p.get_filament_sensor_enabled("filament_runout_sensor") is False

    p.getREST = lambda path: None
    assert p.get_filament_sensor_enabled("filament_runout_sensor") is None


def test_get_files_sorts_newest_first():
    p = _new_printer()
    p.files = None
    p.getREST = lambda path: {
        "result": [
            {"path": "old.gcode", "modified": 100.0},
            {"path": "newest.gcode", "modified": 300.0},
            {"path": "mid.gcode", "modified": 200.0},
            {"path": "no_ts.gcode"},
        ]
    }

    names = p.GetFiles(True)

    assert names == ["newest.gcode", "mid.gcode", "old.gcode", "no_ts.gcode"]
    # self.files stays aligned with the returned names for index-based selection.
    assert [fl["path"] for fl in p.files] == names


def test_open_and_print_file_uses_selected_path():
    p = _new_printer()
    calls = []
    p.files = [{"path": "job.gcode"}]
    p.postREST = lambda path, json=None: calls.append((path, json))

    p.openAndPrintFile(0)

    assert p.file_name == "job.gcode"
    assert calls == [("/printer/print/start", {"filename": "job.gcode"})]


def test_cancel_pause_resume_post_expected_endpoints():
    p = _new_printer()
    calls = []
    p.postREST = lambda path, json=None: calls.append((path, json))

    p.cancel_job()
    p.pause_job()
    p.resume_job()

    assert calls == [
        ("/printer/print/cancel", None),
        ("/printer/print/pause", None),
        ("/printer/print/resume", None),
    ]


def test_set_print_speed_and_flow_emit_expected_gcode():
    p = _new_printer()
    sent = []
    p.sendGCode = lambda cmd: sent.append(cmd)

    p.set_print_speed(120)
    p.set_flow(95)
    p.set_flow(101.5)

    assert p.print_speed == 120
    assert p.flow_percentage == 101.5
    assert sent == ["M220 S120", "M221 S95", "M221 S101.5"]


def test_set_led_translates_percent_to_brightness_gcode():
    p = _new_printer()
    sent = []
    p.sendGCode = lambda cmd: sent.append(cmd)

    p.set_led(5)
    p.set_led(0)
    p.set_led(150)  # clamped to 100%

    assert sent == [
        "SET_LED LED=top_LEDs WHITE=0.05 SYNC=0 TRANSMIT=1",
        "SET_LED LED=top_LEDs WHITE=0.00 SYNC=0 TRANSMIT=1",
        "SET_LED LED=top_LEDs WHITE=1.00 SYNC=0 TRANSMIT=1",
    ]


def test_set_fan_scales_percent_to_m106_range():
    p = _new_printer()
    sent = []
    p.sendGCode = lambda cmd: sent.append(cmd)

    p.set_fan(50)

    assert p.fan_percentage == 50
    assert sent == ["M106 S127"]


def test_home_valid_and_invalid_axis():
    p = _new_printer()
    sent = []
    p.sendGCode = lambda cmd: sent.append(cmd)

    p.home("X")
    p.home("X Y Z")
    p.home("A")

    assert sent == ["G28 X", "G28 X Y Z"]


def test_move_relative_and_absolute_format_respects_mode():
    p = _new_printer()
    sent = []
    p.sendGCode = lambda cmd: sent.append(cmd)

    p.absolute_moves = True
    p.moveRelative("X", 10, 3000)
    p.absolute_moves = False
    p.moveRelative("X", 10, 3000)

    p.absolute_moves = False
    p.moveAbsolute("Y", 5, 1200)
    p.absolute_moves = True
    p.moveAbsolute("Y", 5, 1200)

    assert sent == [
        "G91 \nG1 X10 F3000\nG90",
        "G91 \nG1 X10 F3000",
        "G90 \nG1 Y5 F1200\nG91",
        "G90 \nG1 Y5 F1200",
    ]


def test_send_gcode_posts_script_and_notifies_callback():
    p = _new_printer()
    posts = []
    callbacks = []
    p.postREST = lambda path, json=None: posts.append((path, json))
    p.response_callback = lambda msg, kind: callbacks.append((msg, kind))

    p.sendGCode("M105")

    assert posts == [("/printer/gcode/script", {"script": "M105"})]
    assert callbacks == [("M105", "command")]
