from klipperlcd.lcd import LCD


def _build_lcd():
    events = []
    writes = []

    def callback(evt, data=None):
        events.append((evt, data))
        return None

    lcd = LCD(callback=callback)
    lcd.write = lambda data, eol=True, lf=False: writes.append(data)
    return lcd, events, writes


def test_main_abort_print_emits_print_stop():
    lcd, events, _ = _build_lcd()

    lcd._MainPage([0x02])

    assert events == [(lcd.evt.PRINT_STOP, None)]


def test_advanced_settings_page_opens_multiset():
    lcd, _, writes = _build_lcd()

    lcd._SettingScreen([0x0D])

    # Resume Printing toggle was removed: no plrbutton write, just the page.
    assert not any("plrbutton" in w for w in writes)
    assert "page multiset" in writes


def test_power_continue_legacy_input_is_noop():
    lcd, _, writes = _build_lcd()

    lcd._PowerContinuePrint([0x01])
    lcd._PowerContinuePrint([0x00])

    # Legacy power-loss recovery input is ignored (no writes emitted).
    assert not any("plrbutton" in w for w in writes)


def test_led2_alt_code_toggles_light_at_5_percent():
    lcd, events, writes = _build_lcd()

    lcd._BedLevelFun([0x07])
    lcd._BedLevelFun([0x07])

    assert events == [(lcd.evt.LIGHT, 5), (lcd.evt.LIGHT, 0)]
    assert "status_led2=1" in writes
    assert "status_led2=0" in writes


def test_settings_fan_toggle_switches_between_40_and_0():
    lcd, events, _ = _build_lcd()
    lcd.printer.fan = None  # no status snapshot yet

    lcd._SettingScreen([0x07])
    lcd._SettingScreen([0x07])

    assert events == [(lcd.evt.FAN, 40), (lcd.evt.FAN, 0)]


def test_adjustment_fan_toggle_uses_40_percent():
    lcd, events, _ = _build_lcd()
    lcd.printer.fan = 0

    lcd._Adjustment([0x03])

    assert events == [(lcd.evt.FAN, 40)]
    assert lcd.printer.fan == 40


def test_filament_check_toggle_emits_sensor_events():
    lcd, events, _ = _build_lcd()

    lcd._FilamentCheck([0x00])
    lcd._FilamentCheck([0x01])
    lcd._FilamentCheck([0x7F])  # unknown code ignored

    assert events == [
        (lcd.evt.FILAMENT_SENSOR, False),
        (lcd.evt.FILAMENT_SENSOR, True),
    ]
    assert lcd.filament_sensor_enabled is True


def test_settings_filament_sensor_button_requests_toggle():
    lcd, events, _ = _build_lcd()

    lcd._SettingScreen([0x08])

    assert events == [(lcd.evt.FILAMENT_SENSOR, None)]


def test_settings_leveling_button_starts_scan_and_blocks_ui():
    lcd, events, writes = _build_lcd()

    lcd._SettingScreen([0x01])

    assert events == [(lcd.evt.BED_MESH, None)]
    assert writes[-1] == "page autohome"
    assert lcd.leveling_active is True


def test_setting_back_saves_when_leveling_active():
    lcd, events, writes = _build_lcd()
    lcd.leveling_active = True

    lcd._SettingBack([0x01])

    assert events == [(lcd.evt.LEVELING_SAVE, None)]
    assert writes[-1] == "page main"
    assert lcd.leveling_active is False


def test_setting_back_keeps_probe_flow_when_not_leveling():
    lcd, events, _ = _build_lcd()
    lcd.probe_mode = True

    lcd._SettingBack([0x01])
    lcd._SettingBack([0x05])  # generic back, handled by the HMI itself

    assert events == [(lcd.evt.PROBE_BACK, None)]
    assert lcd.probe_mode is False


def test_show_leveling_result_writes_grid_and_page():
    lcd, _, writes = _build_lcd()
    matrix = [[0.01 * (r * 6 + c) for c in range(6)] for r in range(6)]

    lcd.show_leveling_result(matrix)

    assert "leveldata_36.x0.val=0" in writes
    assert "leveldata_36.x7.val=7" in writes
    assert "leveldata_36.x35.val=35" in writes
    assert writes[-1] == "page leveldata_36"


def test_show_leveling_result_aborts_on_wrong_size():
    lcd, _, writes = _build_lcd()
    lcd.leveling_active = True

    lcd.show_leveling_result([[0.0, 1.0], [1.0, 2.0]])

    assert writes[-1] == "page main"
    assert lcd.leveling_active is False


def test_settings_page_open_syncs_toggle_indicators():
    lcd, _, writes = _build_lcd()
    lcd.printer.led = 5
    lcd.printer.fan = 0
    lcd.printer.filament_sensor = True

    lcd._SettingScreen([0x0B])

    assert "status_led2=1" in writes
    assert "set.fanstatue.pic=76" in writes
    assert "set.filamentdec.pic=77" in writes
    # The page switch is local on the HMI; the host must not re-send it.
    assert "page set" not in writes
    assert lcd.light is True
    assert lcd.filament_sensor_enabled is True


def test_settings_page_open_falls_back_to_local_sensor_state():
    lcd, _, writes = _build_lcd()
    lcd.printer.led = None
    lcd.printer.fan = None
    lcd.printer.filament_sensor = None
    lcd.filament_sensor_enabled = False

    lcd._SettingScreen([0x0B])

    assert "status_led2=0" in writes
    assert "set.fanstatue.pic=76" in writes
    assert "set.filamentdec.pic=76" in writes


def test_settings_fan_toggle_updates_indicator():
    lcd, _, writes = _build_lcd()
    lcd.printer.fan = 0

    lcd._SettingScreen([0x07])
    assert "set.fanstatue.pic=77" in writes

    lcd._SettingScreen([0x07])
    assert "set.fanstatue.pic=76" in writes


def test_settings_filament_toggle_uses_callback_result():
    events = []
    writes = []

    def callback(evt, data=None):
        events.append((evt, data))
        return False  # app reports the new sensor state

    lcd = LCD(callback=callback)
    lcd.write = lambda data, eol=True, lf=False: writes.append(data)

    lcd._SettingScreen([0x08])

    assert events == [(lcd.evt.FILAMENT_SENSOR, None)]
    assert lcd.filament_sensor_enabled is False
    assert "set.filamentdec.pic=76" in writes


def test_main_page_new_klipper_buttons_emit_events():
    lcd, events, _ = _build_lcd()

    lcd._MainPage([0x03])
    lcd._MainPage([0x04])
    lcd._MainPage([0x05])

    assert events == [
        (lcd.evt.CONSOLE_OPEN, None),
        (lcd.evt.EMERGENCY_STOP, None),
        (lcd.evt.POWER_OFF, None),
    ]


def test_settings_restart_and_macros_buttons_emit_events():
    lcd, events, _ = _build_lcd()

    lcd._SettingScreen([0x0E])
    lcd._SettingScreen([0x0F])

    assert events == [
        (lcd.evt.KLIPPER_RESTART, None),
        (lcd.evt.MACROS_OPEN, None),
    ]


def test_zoffset_page_open_syncs_indicators():
    lcd, _, writes = _build_lcd()
    lcd.printer.led = 0
    lcd.printer.filament_sensor = True

    lcd._BedLevelFun([0x20])

    assert "status_led2=0" in writes
    assert "adjustzoffset.led2.pic=76" in writes
    assert "adjustzoffset.filamentdec.pic=77" in writes


def test_eaxis_step_select_updates_step_and_indicator():
    lcd, events, writes = _build_lcd()
    lcd.printer.flowrate = 100

    lcd._FilamentLoad([0x0F])  # step 5
    assert lcd.eaxis_step == 5
    assert "motorsetvalue.p1.pic=63" in writes

    lcd._FilamentLoad([0x0D])  # plus by 5
    assert lcd.eaxis_trim == 5
    assert events == [(lcd.evt.FLOW, 100.5)]

    lcd._FilamentLoad([0x10])  # step 10
    assert lcd.eaxis_step == 10
    assert "motorsetvalue.p1.pic=64" in writes
    lcd._FilamentLoad([0x0E])  # step 1
    assert lcd.eaxis_step == 1
    assert "motorsetvalue.p1.pic=62" in writes


def test_eaxis_pulse_trims_flow_in_tenths_of_percent():
    lcd, events, writes = _build_lcd()
    lcd.printer.flowrate = 101.5

    lcd._FilamentLoad([0x0B])  # screen open renders current trim
    assert lcd.eaxis_trim == 15
    assert "motorsetvalue.eaxis.val=15" in writes
    assert events == []

    lcd._FilamentLoad([0x0D])  # plus
    assert lcd.eaxis_trim == 16
    assert "motorsetvalue.eaxis.val=16" in writes
    assert events == [(lcd.evt.FLOW, 100 + 16 / 10.0)]

    lcd._FilamentLoad([0x0C])  # minus
    assert lcd.eaxis_trim == 15
    assert events[-1] == (lcd.evt.FLOW, 101.5)


def test_file_slots_rendered_across_hmi_pages():
    lcd, _, writes = _build_lcd()
    lcd.files = ['a.gcode', 'b".gcode', "c.gcode", "d.gcode", "e.gcode", "f.gcode"]

    assert lcd._file_page_count() == 2
    lcd._render_file_slots()

    # Page 1 holds t0..t4, page 2 holds t5..t9 (absolute component ids).
    assert "file1.t0.txt=\"a.gcode\"" in writes
    assert "file1.t1.txt=\"b'.gcode\"" in writes
    assert "file2.t5.txt=\"f.gcode\"" in writes
    # Unused slots are cleared, including on later pages.
    assert "file2.t6.txt=\"\"" in writes
    assert "file5.t24.txt=\"\"" in writes
    assert 'file1.t1.txt="b".gcode"' not in writes


def test_main_page_renders_file_list_and_opens_page_one():
    lcd, _, writes = _build_lcd()
    files = [f"f{i}.gcode" for i in range(1, 8)]
    lcd._callback = lambda evt, data=None: files if evt == lcd.evt.FILES else None
    lcd.current_file_page = 3

    lcd._MainPage([0x01])

    assert lcd.files == files
    assert lcd.current_file_page == 1
    assert "file1.t0.txt=\"f1.gcode\"" in writes
    assert "file2.t6.txt=\"f7.gcode\"" in writes
    assert writes[-1] == "page file1"


def test_select_file_uses_absolute_index_and_triggers_thumbnail():
    lcd, events, writes = _build_lcd()
    lcd.files = ["f1.gcode", "f2.gcode", "f3.gcode", "f4.gcode", "f5.gcode", 'f6".gcode', "f7.gcode"]

    lcd._SelectFile([0x07])

    assert lcd.selected_file == 6
    assert lcd.current_file_page == 2
    assert lcd.askprint is True
    assert 'askprint.t0.txt="f7.gcode"' in writes
    assert 'printpause.t0.txt="f7.gcode"' in writes
    assert (lcd.evt.THUMBNAIL, None) in events


def test_select_file_rejects_out_of_range_index():
    lcd, events, _ = _build_lcd()
    lcd.files = ["a.gcode", "b.gcode"]

    lcd._SelectFile([0x03])

    assert lcd.selected_file is False
    assert events == []


def test_print_file_navigation_and_back_behavior():
    lcd, _, writes = _build_lcd()
    lcd.files = [f"f{i}.gcode" for i in range(1, 12)]
    lcd.current_file_page = 1
    lcd.askprint = True

    lcd._PrintFile([0x03])
    assert lcd.current_file_page == 1

    lcd._PrintFile([0x02])
    lcd._PrintFile([0x02])
    lcd._PrintFile([0x02])
    assert lcd.current_file_page == 3

    lcd._PrintFile([0x0A])
    assert lcd.askprint is False
    assert "page file3" in writes

    lcd._PrintFile([0x0A])
    assert writes[-1] == "page main"


def test_print_file_start_emits_event_and_updates_ui():
    lcd, events, writes = _build_lcd()
    lcd.selected_file = 4
    lcd.printer.z_offset = 0.12

    lcd._PrintFile([0x01])

    assert (lcd.evt.PRINT_START, 4) in events
    assert "printpause.printprocess.val=0" in writes
    assert "leveldata.z_offset.val=12" in writes
    assert "page printpause" in writes


def test_print_file_start_highlights_absolute_slot_on_second_page():
    lcd, events, writes = _build_lcd()
    lcd.selected_file = 6
    lcd.printer.z_offset = 0.0

    lcd._PrintFile([0x01])

    assert (lcd.evt.PRINT_START, 6) in events
    assert "file2.t6.pco=65504" in writes


def test_print_file_compat_prev_and_back():
    lcd, _, writes = _build_lcd()
    lcd.files = ["a.gcode", "b.gcode"]
    lcd.current_file_page = 2

    lcd._PrintFileCompat([0x01])
    assert lcd.current_file_page == 1

    lcd._PrintFileCompat([0x0A])
    assert writes[-1] == "page main"
