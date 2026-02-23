from klipperlcd.printer import PrinterData


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

    assert p.print_speed == 120
    assert p.flow_percentage == 95
    assert sent == ["M220 S120", "M221 S95"]


def test_set_led_translates_to_on_and_off_gcode():
    p = _new_printer()
    sent = []
    p.sendGCode = lambda cmd: sent.append(cmd)

    p.set_led(128)
    p.set_led(0)

    assert sent == [
        "SET_LED LED=top_LEDs WHITE=0.5 SYNC=0 TRANSMIT=1",
        "SET_LED LED=top_LEDs WHITE=0 SYNC=0 TRANSMIT=1",
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
