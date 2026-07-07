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


def test_advanced_settings_page_reflects_recovery_state():
    lcd, _, writes = _build_lcd()
    lcd.power_loss_recovery_enabled = False

    lcd._SettingScreen([0x0D])

    assert "multiset.plrbutton.val=0" in writes
    assert "page multiset" in writes


def test_power_continue_updates_recovery_flag():
    lcd, _, writes = _build_lcd()
    lcd.power_loss_recovery_enabled = False

    lcd._PowerContinuePrint([0x01])
    lcd._PowerContinuePrint([0x00])

    assert lcd.power_loss_recovery_enabled is False
    assert "multiset.plrbutton.val=1" in writes
    assert "multiset.plrbutton.val=0" in writes


def test_led2_alt_code_toggles_light():
    lcd, events, writes = _build_lcd()

    lcd._BedLevelFun([0x07])
    lcd._BedLevelFun([0x07])

    assert events == [(lcd.evt.LIGHT, 128), (lcd.evt.LIGHT, 0)]
    assert "status_led2=1" in writes
    assert "status_led2=0" in writes


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
