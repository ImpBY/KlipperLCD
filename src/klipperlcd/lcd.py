"""LCD transport and event-handling layer for the Neptune HMI panel.

This module translates raw serial protocol messages from the LCD into
high-level events, and pushes printer state updates back to the display.
"""

import binascii
import os
import atexit
import logging
import math
from array import array
from io import BytesIO
from threading import Thread
from time import sleep

from PIL import Image

from . import lib_col_pic
import serial

logger = logging.getLogger(__name__)


def _log(*args, level=logging.INFO):
    logger.log(level, " ".join(str(arg) for arg in args))


def _lcd_safe_text(value):
    """Prepare text payload for LCD command string."""
    text = str(value)
    return text.replace("\"", "'")


def _hex_preview(data, limit=128):
    """Return a compact hex dump suitable for debug logs."""
    raw = bytes(data)
    hexed = binascii.hexlify(raw).decode()
    if len(raw) > limit:
        return "%s...<%d bytes>" % (hexed[: limit * 2], len(raw))
    return "%s<%d bytes>" % (hexed, len(raw))

# --------------------------- Protocol constants ---------------------------
FHONE = 0x5a
FHTWO = 0xa5
FHLEN = 0x06

MaxFileNumber = 25

RegAddr_W = 0x80
RegAddr_R = 0x81
CMD_WRITEVAR = 0x82
CMD_READVAR  = 0x83
CMD_CONSOLE  = 0x42

ExchangePageBase = 0x5A010000 # Unsigned long
StartSoundSet    = 0x060480A0
FONT_EEPROM      = 0

# variable addr
ExchangepageAddr = 0x0084
SoundAddr        = 0x00A0

RX_STATE_IDLE = 0
RX_STATE_READ_LEN = 1
RX_STATE_READ_CMD = 2
RX_STATE_READ_DAT = 3

PLA   = 0
ABS   = 1
PETG  = 2
TPU   = 3
PROBE = 4


class _printerData:
    """Internal printer state snapshot consumed by LCD update paths."""

    hotend_target   = None
    hotend          = None
    bed_target      = None
    bed             = None

    state           = None

    percent         = None
    duration        = None
    remaining       = None
    feedrate        = None
    flowrate        = 0
    fan             = None
    x_pos           = None
    y_pos           = None
    z_pos           = None
    z_offset        = None
    file_name       = None

    max_velocity           = None
    max_accel              = None
    max_accel_to_decel     = None
    square_corner_velocity = None

class LCDEvents:
    """Event IDs emitted from LCD input handlers to the app layer."""

    HOME           = 1
    MOVE_X         = 2
    MOVE_Y         = 3
    MOVE_Z         = 4
    MOVE_E         = 5
    NOZZLE         = 6
    BED            = 7
    FILES          = 8
    PRINT_START    = 9
    PRINT_STOP     = 10
    PRINT_PAUSE    = 11
    PRINT_RESUME   = 12
    PROBE          = 13
    BED_MESH       = 14
    LIGHT          = 15
    FAN            = 16
    MOTOR_OFF      = 17
    PRINT_STATUS   = 18 ## Not needed?
    PRINT_SPEED    = 19
    FLOW           = 20
    Z_OFFSET       = 21
    PROBE_COMPLETE = 22
    PROBE_BACK     = 23
    ACCEL          = 24
    ACCEL_TO_DECEL = 25
    VELOCITY       = 26
    SQUARE_CORNER_VELOCITY = 27
    THUMBNAIL      = 28
    CONSOLE        = 29


class LCD:
    """Serial protocol adapter for the Neptune LCD panel."""

    def __init__(self, port=None, baud=115200, callback=None):
        # Map LCD register addresses to handlers.
        self.addr_func_map = {
            0x1002: self._MainPage,          
            0x1004: self._Adjustment,        
            0x1006: self._PrintSpeed,        
            0x1008: self._StopPrint,         
            0x100A: self._PausePrint,        
            0x100C: self._ResumePrint,       
            0x1026: self._ZOffset,           
            0x1030: self._TempScreen,        
            0x1032: self._CoolScreen,        
            0x1034: self._Heater0TempEnter,  
            0x1038: self._Heater1TempEnter,  
            0x103A: self._HotBedTempEnter,   
            0x103E: self._SettingScreen,     
            0x1040: self._SettingBack,       
            0x1044: self._BedLevelFun,       
            0x1046: self._AxisPageSelect,    
            0x1048: self._Xaxismove,         
            0x104A: self._Yaxismove,         
            0x104C: self._Zaxismove,         
            0x104E: self._SelectExtruder,    
            0x1054: self._Heater0LoadEnter,  
            0x1056: self._FilamentLoad,      
            0x1058: self._Heater1LoadEnter,  
            0x105C: self._SelectLanguage,    
            0x105E: self._FilamentCheck,     
            0x105F: self._PowerContinuePrint,
            0x1090: self._PrintSelectMode,   
            0x1092: self._XhotendOffset,     
            0x1094: self._YhotendOffset,     
            0x1096: self._ZhotendOffset,     
            0x1098: self._StoreMemory,       
            0x2183: self._PrintFileCompat,
            0x2198: self._PrintFile,         
            0x2199: self._SelectFile,        
            0x110E: self._ChangePage,        
            0x2200: self._SetPreNozzleTemp,  
            0x2201: self._SetPreBedTemp,     
            0x2202: self._HardwareTest,      
            0X2203: self._Err_Control,
            0x4201: self._Console
        }

        self.evt = LCDEvents()
        self._callback = callback
        self.callback = self._callback_proxy
        self._event_names = self._build_event_names()
        self._last_ui_context = None
        self._ui_element_map = self._build_ui_element_map()
        self.printer = _printerData()
                              # PLA, ABS, PETG, TPU, PROBE 
        self.preset_temp     = [200, 245,  225, 220, 200]
        self.preset_bed_temp = [ 60, 100,   70,  60,  60]
        self.preset_index    = 0
        # UART communication parameters
        self.ser = serial.Serial()
        self.ser.port = port
        self.ser.baudrate = baud
        self.ser.timeout = None
        self.running = False
        self.rx_buf = bytearray()
        self.rx_data_cnt = 0
        self.rx_state = RX_STATE_IDLE
        self.error_from_lcd = False
        # List of GCode files
        self.files = False
        self.selected_file = False
        self.current_file_page = 1
        self.waiting = None
        # Adjusting temp and move axis params
        self.adjusting = 'Hotend'
        self.temp_unit = 10
        self.move_unit = 1
        self.load_len = 25
        self.feedrate_e = 300
        self.z_offset_unit = None
        self.light = False
        # Adjusting speed
        self.speed_adjusting = None
        self.speed_unit = 10
        self.adjusting_max = False
        self.accel_unit = 100
        # Probe /Level mode
        self.probe_mode = False
        # Thumbnail
        self.is_thumbnail_written = False
        self.askprint = False
        self._read_thread = None
        self._serial_wait_logged = False
        # Make sure the serial port closes when you quit the program.
        atexit.register(self._atexit)

    def _build_event_names(self):
        event_names = {}
        for attr in dir(self.evt):
            if attr.startswith("_"):
                continue
            value = getattr(self.evt, attr)
            if isinstance(value, int):
                event_names[value] = attr
        return event_names

    def _build_ui_element_map(self):
        """Map raw LCD addr/data codes to stable element labels for debug logs."""
        return {
            0x1002: {
                0x01: "main.print_open_files",
                0x02: "main.abort_print_stub",
            },
            0x1004: {
                0x01: "adjustment.filament_tab",
                0x02: "adjustment.back_to_printpause",
                0x03: "adjustment.fan_toggle",
                0x05: "adjustment.temp_tab",
                0x06: "adjustment.speed_tab",
                0x07: "adjustment.zoffset_tab",
                0x08: "adjustment.reset_print_speed",
                0x09: "adjustment.reset_flow",
                0x0A: "adjustment.reset_fan",
            },
            0x1008: {
                0x01: "stop.confirm",
                0xF0: "stop.cancel_dialog",
                0xF1: "stop.force_confirm",
            },
            0x100A: {
                0x01: "pause.open_dialog",
                0xF1: "pause.confirm",
            },
            0x100C: {
                0x01: "resume.confirm",
            },
            0x1030: {
                0x01: "temp.select_hotend",
                0x03: "temp.select_bed",
                0x04: "temp.noop",
                0x05: "temp.set_step_small",
                0x06: "temp.set_step_medium",
                0x07: "temp.set_step_large",
                0x08: "temp.increase",
                0x09: "temp.decrease",
                0x0A: "speed.select_printspeed",
                0x0B: "speed.select_flow",
                0x0C: "speed.select_fan",
                0x0D: "speed.increase",
                0x0E: "speed.decrease",
                0x11: "limits.accel_decrease",
                0x12: "limits.accel_to_decel_decrease",
                0x13: "limits.velocity_decrease",
                0x14: "limits.scv_decrease",
                0x15: "limits.accel_increase",
                0x16: "limits.accel_to_decel_increase",
                0x17: "limits.velocity_increase",
                0x18: "limits.scv_increase",
                0x42: "limits.open_advanced",
                0x43: "limits.close_advanced",
            },
            0x1032: {
                0x01: "cool.nozzle_off",
                0x02: "cool.bed_off",
                0x09: "cool.preheat_pla",
                0x0A: "cool.preheat_abs",
                0x0B: "cool.preheat_petg",
                0x0C: "cool.preheat_tpu",
                0x0D: "cool.edit_pla",
                0x0E: "cool.edit_abs",
                0x0F: "cool.edit_petg",
                0x10: "cool.edit_tpu",
                0x11: "cool.edit_probe",
            },
            0x103E: {
                0x01: "settings.start_probe",
                0x06: "settings.motor_release",
                0x07: "settings.fan_control_stub",
                0x08: "settings.unknown_stub",
                0x09: "settings.preheat_page",
                0x0A: "settings.filament_page",
                0x0B: "settings.main_settings_page",
                0x0C: "settings.read_level_warning_page",
                0x0D: "settings.advanced_page",
            },
            0x1040: {
                0x01: "settings.back_from_probe",
            },
            0x1044: {
                0x02: "zoffset.increase",
                0x03: "zoffset.decrease",
                0x04: "zoffset.step_0_01",
                0x05: "zoffset.step_0_1",
                0x06: "zoffset.step_1_0",
                0x07: "zoffset.led2_stub",
                0x08: "zoffset.light_toggle",
                0x09: "zoffset.mesh_level_start",
                0x0A: "zoffset.print_status_refresh",
                0x0B: "zoffset.temp_refresh",
                0x0C: "zoffset.noop",
                0x16: "zoffset.resume_print_page",
            },
            0x1046: {
                0x04: "axis.home_all",
                0x05: "axis.home_x",
                0x06: "axis.home_y",
                0x07: "axis.home_z",
            },
            0x1048: {
                0x01: "axis.move_x_plus",
                0x02: "axis.move_x_minus",
            },
            0x104A: {
                0x01: "axis.move_y_plus",
                0x02: "axis.move_y_minus",
            },
            0x104C: {
                0x01: "axis.move_z_plus",
                0x02: "axis.move_z_minus",
            },
            0x1056: {
                0x01: "filament.load",
                0x02: "filament.unload",
                0x05: "filament.temp_warning_confirm",
                0x06: "filament.temp_warning_cancel",
                0x0A: "filament.back",
            },
            0x2198: {
                0x01: "file.start_selected",
                0x02: "file.next_page",
                0x03: "file.prev_page",
                0x04: "file.next_page_repeat",
                0x05: "file.prev_page_repeat",
                0x06: "file.next_page_repeat",
                0x07: "file.prev_page_repeat",
                0x08: "file.next_page_repeat",
                0x09: "file.next_page_repeat",
                0x0A: "file.back",
            },
            0x2183: {
                0x01: "file.prev_page_compat",
                0x03: "file.prev_page_compat",
                0x0A: "file.back_compat",
            },
            0x2199: {
                0x01: "file.select_slot_1",
                0x02: "file.select_slot_2",
                0x03: "file.select_slot_3",
                0x04: "file.select_slot_4",
                0x05: "file.select_slot_5",
            },
            0x2200: {
                0x01: "preset.nozzle_plus",
                0x02: "preset.nozzle_minus",
            },
            0x2201: {
                0x01: "preset.bed_plus",
                0x02: "preset.bed_minus",
            },
            0x2202: {
                0x0F: "hardware_test.poll",
            },
            0x4201: {
                0x01: "console.back",
            },
        }

    def _event_name(self, evt):
        return self._event_names.get(evt, "UNKNOWN")

    def _command_name(self, cmd):
        if cmd == CMD_WRITEVAR:
            return "WRITEVAR"
        if cmd == CMD_READVAR:
            return "READVAR"
        if cmd == CMD_CONSOLE:
            return "CONSOLE"
        return "UNKNOWN"

    def _ui_element_name(self, addr, data):
        code = None
        if data:
            code = data[0]
        if code is None:
            return None
        return self._ui_element_map.get(addr, {}).get(code)

    def _callback_proxy(self, evt, data=None):
        ctx = self._last_ui_context or {}
        logger.debug(
            "UI route: element=%s addr=0x%04X code=%s handler=%s -> event=%s(%s) payload=%r",
            ctx.get("element", "unknown"),
            ctx.get("addr", 0),
            (
                "0x%02X" % ctx["code"]
                if isinstance(ctx.get("code"), int)
                else "none"
            ),
            ctx.get("handler", "unknown"),
            self._event_name(evt),
            evt,
            data,
        )
        if self._callback is None:
            logger.debug("UI route dropped: callback is not configured")
            return None
        return self._callback(evt, data)

    def _file_page_count(self):
        if not self.files:
            return 1
        return max(1, math.ceil(len(self.files) / 5))

    def _file_page_next(self):
        if self.current_file_page < self._file_page_count():
            self._show_file_page(self.current_file_page + 1)
        else:
            logger.debug("File page next ignored at last page=%d", self.current_file_page)
            self._show_file_page(self.current_file_page)

    def _file_page_prev(self):
        if self.current_file_page > 1:
            self._show_file_page(self.current_file_page - 1)
        else:
            logger.debug("File page prev ignored at first page=%d", self.current_file_page)
            self._show_file_page(self.current_file_page)

    def _show_file_page(self, page):
        """Render one logical file page (5 items) on the single LCD file page."""
        if not self.files:
            self.current_file_page = 1
            self.write("page file1")
            return
        total_pages = self._file_page_count()
        page = max(1, min(page, total_pages))
        self.current_file_page = page
        start = (page - 1) * 5
        end = min(start + 5, len(self.files))

        for item_num in range(0, 5):
            self.write("file1.t%d.txt=\"\"" % item_num)
        for i in range(start, end):
            item_num = i - start
            safe_file = _lcd_safe_text(self.files[i])
            self.write("file1.t%d.txt=\"%s\"" % (item_num, safe_file))
        logger.debug(
            "File page render: page=%d/%d items=%d..%d",
            self.current_file_page,
            total_pages,
            start + 1,
            end,
        )
        self.write("page file1")

    def _atexit(self):
        # Keep shutdown idempotent for normal exits and fatal-path exits.
        if self.ser.is_open:
            self.ser.close()
        self.running = False

    def start(self, *args, **kwargs):
        """Open serial transport and start background read loop."""
        # Reset runtime UI/session state on every (re)start to avoid stale values
        # after service restart while LCD remains powered.
        self.files = False
        self.selected_file = False
        self.current_file_page = 1
        self.askprint = False
        self.is_thumbnail_written = False
        self.waiting = None
        self.error_from_lcd = False

        self.running = True
        while self.running and not self.ser.is_open:
            try:
                self.ser.open()
                if self._serial_wait_logged:
                    _log(
                        "LCD serial device is available again: %s" % self.ser.port,
                        level=logging.INFO,
                    )
                    self._serial_wait_logged = False
            except (serial.SerialException, OSError) as e:
                if not self._serial_wait_logged:
                    _log(
                        "LCD serial device not ready (%s). Waiting for %s"
                        % (e, self.ser.port),
                        level=logging.WARNING,
                    )
                    self._serial_wait_logged = True
                sleep(1)
                continue
        self._read_thread = Thread(target=self.run, daemon=True)
        self._read_thread.start()

        self.write("page boot")
        self.write("com_star")
        self.write("main.va0.val=1")
        self.write("boot.j0.val=1")
        self.write("boot.t0.txt=\"KlipperLCD.service starting...\"")

    def boot_progress(self, progress):
        self.write("boot.t0.txt=\"Waiting for Klipper...\"")
        self.write("boot.j0.val=%d" % progress)

    def stop(self):
        self.running = False
        if self.ser.is_open:
            self.ser.close()
        if self._read_thread and self._read_thread.is_alive():
            self._read_thread.join(timeout=2.0)

    def about_machine(self, size, fw):
        _log("Machine size: " + self.printer.MACHINE_SIZE, level=logging.INFO)
        _log("Klipper version: " + self.printer.SHORT_BUILD_VERSION, level=logging.INFO)
        self.write("information.size.txt=\"%s\"" % size)
        self.write("information.sversion.txt=\"%s\"" % fw)        

    def write(self, data, eol=True, lf=False):
        """Write a command payload to LCD with optional framing/newline tweaks."""
        dat = bytearray()
        decoded = None
        if isinstance(data, str):
            # Use bytes encoding to avoid crashes on non-ASCII file names.
            dat.extend(data.encode("utf-8", errors="replace"))
            decoded = data
        else:
            dat.extend(data)
            decoded = "<binary>"

        if lf:
            dat.extend(dat[-1:])
            dat.extend(dat[-1:])
            dat[len(dat)-2] = 10 #'\r'
            dat[len(dat)-3] = 13 #'\n'
        framed = bytearray(dat)
        if eol:
            framed.extend(bytearray([0xFF, 0xFF, 0xFF]))
        logger.debug(
            "LCD TX: decoded=%r eol=%s lf=%s raw=%s",
            decoded,
            eol,
            lf,
            _hex_preview(framed),
        )
        try:
            self.ser.write(dat)
            if eol:
                self.ser.write(bytearray([0xFF, 0xFF, 0xFF]))
        except (serial.SerialException, OSError) as e:
            if not self.running:
                return
            _log("FATAL: LCD serial write failed: %s" % e, level=logging.ERROR)
            # Let systemd restart the service on transport failure.
            os._exit(1)

    def clear_thumbnail(self):
        self.write("printpause.cp0.close()")
        self.write("printpause.cp0.aph=0")
        self.write("printpause.va0.txt=\"\"")
        self.write("printpause.va1.txt=\"\"") 

    def write_thumbnail(self, img):
        # Clear screen
        self.clear_thumbnail()

        # Open as image
        im = Image.open(BytesIO(img))
        width, height = im.size
        if width != 160 or height != 160:
            im = im.resize((160, 160))
            width, height = im.size

        pixels = im.load()

        color16 = array('H')
        for i in range(height): #Height
            for j in range(width): #Width
                r, g, b, a = pixels[j, i]
                r = r >> 3
                g = g >> 2
                b = b >> 3
                rgb = (r << 11) | (g << 5) | b
                if rgb == 0x0000:
                    rgb = 0x4AF0
                color16.append(rgb)

        output_data = bytearray(height * width * 10)
        result_int = lib_col_pic.ColPic_EncodeStr(color16, width, height, output_data, width * height * 10, 1024)

        each_max = 512
        j = 0
        k = 0
        result = [bytearray()]
        for i in range(len(output_data)):
            if output_data[i] != 0:
                if j % each_max == 0:
                    result.append(bytearray())
                    k += 1
                result[k].append(output_data[i])
                j += 1

        # Send image to screen
        self.error_from_lcd = True 
        while self.error_from_lcd == True:
            _log("Write thumbnail to LCD", level=logging.DEBUG)
            self.error_from_lcd = False 

            # Clear screen
            self.clear_thumbnail()   

            sleep(0.2)

            for bytes in result:
                self.write("printpause.cp0.aph=0")
                self.write("printpause.va0.txt=\"\"")#

                self.write("printpause.va0.txt=\"", eol = False)
                self.write(bytes, eol = False)
                self.write("\"")

                self.write(("printpause.va1.txt+=printpause.va0.txt"))
                sleep(0.02)

            sleep(0.2)
            self.write("printpause.cp0.aph=127")
            self.write("printpause.cp0.write(printpause.va1.txt)")
            self.is_thumbnail_written = True
            _log("Write thumbnail to LCD done!", level=logging.DEBUG)

        if self.askprint == True:
            self.write("askprint.cp0.aph=127")
            self.write("askprint.cp0.write(printpause.va1.txt)")            

    def clear_console(self):
        self.write("console.buf.txt=\"\"")
        self.write("console.slt0.txt=\"\"")


    def format_console_data(self, msg, data_type):
        data = None
        if data_type == 'command':
            data = "> " + msg
        elif data_type == 'response':
            if 'B:' in msg and 'T0:' in msg:
                pass ## Filter out temperature responses
            else:
                data = msg.replace("// ", "")
                data = data.replace("??????", "?")
                data = data.replace("echo: ", "")
                data = "< " + data
        else:
            _log("format_console_data: type unknown", level=logging.WARNING)

        return data

    def write_console(self, data):
        if "\"" in data:
            data = data.replace("\"", "'")

        if '\n' in data:
            data = data.replace("\n", "\r\n")

        self.write("console.buf.txt=\"%s\"" % data, lf = True)
        self.write("console.buf.txt+=console.slt0.txt")
        self.write("console.slt0.txt=console.buf.txt")

    def write_gcode_store(self, gcode_store):
        self.clear_console()
        for data in gcode_store:
            msg = self.format_console_data(data['message'], data['type'])
            if msg: 
                self.write_console(msg)

    def write_macros(self, macros):
        self.write("macro.cb0.path=\"\"")
        for macro in macros:
            line_feed = True
            if macro == macros[-1]: #Last element, dont print with line feed
                line_feed = False
            self.write("macro.cb0.path+=\"%s\"" % macro, lf = line_feed)


    def data_update(self, data):
        if data.hotend_target != self.printer.hotend_target:
            self.write("pretemp.nozzle.txt=\"%d\"" % data.hotend_target)
        if data.bed_target != self.printer.bed_target:
            self.write("pretemp.bed.txt=\"%d\"" % data.bed_target)
        if data.hotend != self.printer.hotend or data.hotend_target != self.printer.hotend_target:
            self.write("main.nozzletemp.txt=\"%d / %d\"" % (data.hotend, data.hotend_target))
        if data.bed != self.printer.bed or data.bed_target != self.printer.bed_target:
            self.write("main.bedtemp.txt=\"%d / %d\"" % (data.bed, data.bed_target))

        if self.probe_mode and data.z_pos != self.printer.z_pos:
            self.write("leveldata.z_offset.val=%d" % (int)(data.z_pos * 100))

        if self.speed_adjusting == 'PrintSpeed' and data.feedrate != self.printer.feedrate:
            self.write("adjustspeed.targetspeed.val=%d" % data.feedrate)
        elif self.speed_adjusting == 'Flow' and data.flowrate != self.printer.flowrate:
            self.write("adjustspeed.targetspeed.val=%d" % data.flowrate)
        elif self.speed_adjusting == 'Fan' and data.fan != self.printer.fan:
            self.write("adjustspeed.targetspeed.val=%d" % data.fan)

        if self.adjusting_max:
            if data.max_accel != self.printer.max_accel:
                self.write("speed_settings.accel.val=%d" % data.max_accel)
            if data.max_accel_to_decel != self.printer.max_accel_to_decel:
                self.write("speed_settings.accel_to_decel.val=%d" % data.max_accel_to_decel)
            if data.max_velocity != self.printer.max_velocity:
                self.write("speed_settings.velocity.val=%d" % data.max_velocity)
            if data.square_corner_velocity != self.printer.square_corner_velocity:
                self.write("speed_settings.sqr_crnr_vel.val=%d" % int(data.square_corner_velocity*10))

        if data.state != self.printer.state:
                _log("Printer state: %s" % data.state, level=logging.INFO)
                if data.state == "printing":
                    _log("Ongoing print detected", level=logging.INFO)
                    self.write("page printpause")
                    self.write("restFlag1=0")
                    self.write("restFlag2=1")
                    if self.is_thumbnail_written == False:
                        self.callback(self.evt.THUMBNAIL, None)
                elif data.state == "paused" or data.state == "pausing":
                    _log("Ongoing pause detected", level=logging.INFO)
                    self.write("page printpause")
                    self.write("restFlag1=1")
                    if self.is_thumbnail_written == False:
                        self.callback(self.evt.THUMBNAIL, None)
                elif (data.state == "cancelled"):
                    self.write("page main")
                    self.is_thumbnail_written = False
                elif (data.state == "complete"):
                    self.write("page printfinish")
                    self.is_thumbnail_written = False

        if data != self.printer:
            self.printer = data

    def probe_mode_start(self):
        self.probe_mode = True
        self.z_offset_unit = 1
        self.write("leveldata.z_offset.val=%d" % (int)(self.printer.z_pos * 100))
        self.write("page leveldata_36")
        self.write("leveling_36.tm0.en=0")
        self.write("leveling.tm0.en=0")

    def run(self):
        """Read framed serial packets and dispatch handlers until stopped."""
        while self.running:
            try:
                incomingByte = self.ser.read(1)
            except (serial.SerialException, OSError) as e:
                if not self.running:
                    break
                _log("FATAL: LCD serial read failed: %s" % e, level=logging.ERROR)
                # Let systemd restart the service on transport failure.
                os._exit(1)
            except Exception as e:
                # During shutdown pyserial may raise non-OSError exceptions
                # (for example from low-level os.read after fd is closed).
                if not self.running:
                    break
                _log("FATAL: LCD serial read failed: %s" % e, level=logging.ERROR)
                logger.exception("Unexpected LCD serial read exception")
                os._exit(1)
            if not incomingByte:
                continue

            if self.rx_state == RX_STATE_IDLE:
                if incomingByte[0] == FHONE:
                    self.rx_buf.extend(incomingByte)
                elif incomingByte[0] == FHTWO:
                    if self.rx_buf[0] == FHONE:
                        self.rx_buf.extend(incomingByte)
                        self.rx_state = RX_STATE_READ_LEN
                    else:
                        self.rx_buf.clear()
                        _log("Unexpected header received: 0x%02x ()" % incomingByte[0], level=logging.WARNING)
                else:
                    self.rx_buf.clear()
                    self.error_from_lcd = True
                    _log("Unexpected data received: 0x%02x" % incomingByte[0], level=logging.WARNING)

            elif self.rx_state == RX_STATE_READ_LEN:
                # Frame length byte follows header.
                self.rx_buf.extend(incomingByte)
                self.rx_state = RX_STATE_READ_DAT

            elif self.rx_state == RX_STATE_READ_DAT:
                self.rx_buf.extend(incomingByte)
                self.rx_data_cnt += 1
                frame_len = self.rx_buf[2]
                if self.rx_data_cnt >= frame_len:
                    # Full command/frame received from display.
                    cmd = self.rx_buf[3]
                    data = self.rx_buf[-(frame_len-1):]  # remove header + command
                    logger.debug(
                        "LCD RX frame: cmd=%s(0x%02X) raw=%s",
                        self._command_name(cmd),
                        cmd,
                        _hex_preview(self.rx_buf),
                    )
                    try:
                        self._handle_command(cmd, data)
                    except Exception:
                        logger.exception("Unhandled LCD command processing error")
                    self.rx_buf.clear()
                    self.rx_data_cnt = 0
                    self.rx_state = RX_STATE_IDLE

    def _handle_command(self, cmd, dat):
        """Decode command payload and dispatch by protocol opcode."""
        logger.debug(
            "LCD RX payload: cmd=%s(0x%02X) raw=%s",
            self._command_name(cmd),
            cmd,
            _hex_preview(dat),
        )
        if cmd == CMD_WRITEVAR: #0x82
            # Some HMI actions may arrive as WRITEVAR; keep this visible for
            # reverse-engineering unknown UI elements.
            if len(dat) >= 2:
                addr = (dat[0] << 8) | dat[1]
                handler = self.addr_func_map.get(addr)
                logger.warning(
                    "UI writevar: addr=0x%04X handler=%s raw=%s",
                    addr,
                    handler.__name__ if handler else "unknown",
                    binascii.hexlify(dat).decode(),
                )
            else:
                logger.warning(
                    "UI writevar short payload: raw=%s",
                    binascii.hexlify(dat).decode(),
                )
        elif cmd == CMD_READVAR: #0x83
            addr = dat[0]
            addr = (addr << 8) | dat[1]
            bytelen = dat[2]
            data = [32]
            for i in range (0, bytelen, 2):
                idx = int(i / 2)
                data[idx] = dat[3 + i]
                data[idx] = (data[idx] << 8) | dat[4 + i]
            logger.debug(
                "LCD RX READVAR decoded: addr=0x%04X bytelen=%d words=%r",
                addr,
                bytelen,
                data,
            )
            self._handle_readvar(addr, data)
        elif cmd == CMD_CONSOLE: #0x42
            addr = dat[0]
            addr = (addr << 8) | dat[1]
            data = dat[3:] # Remove addr and len
            logger.debug(
                "LCD RX CONSOLE decoded: addr=0x%04X len=%d data=%r",
                addr,
                len(data),
                data,
            )
            self._handle_readvar(addr, data)
        else:
            _log("Command not reqognised: %d" % cmd, level=logging.WARNING)
            _log(binascii.hexlify(dat), level=logging.DEBUG)

    def _handle_readvar(self, addr, data):
        """Dispatch decoded LCD register data to mapped handler."""
        if addr in self.addr_func_map:
            handler = self.addr_func_map[addr]
            code = data[0] if data else None
            element = self._ui_element_name(addr, data) or "unknown"
            if element == "unknown":
                logger.warning(
                    "UI unknown code: addr=0x%04X code=%s handler=%s data=%r",
                    addr,
                    ("0x%02X" % code if isinstance(code, int) else "none"),
                    handler.__name__,
                    data,
                )
            self._last_ui_context = {
                "addr": addr,
                "code": code,
                "element": element,
                "handler": handler.__name__,
            }
            if not (handler.__name__ == "_BedLevelFun" and code == 0x0A):
                logger.debug(
                    "UI input: addr=0x%04X code=%s element=%s handler=%s len=%d data=%r",
                    addr,
                    ("0x%02X" % code if isinstance(code, int) else "none"),
                    element,
                    handler.__name__,
                    len(data),
                    data,
                )
            try:
                handler(data)
            finally:
                self._last_ui_context = None
        else:
            _log(
                "_handle_readvar: addr %x not recognised data=%r" % (addr, data),
                level=logging.WARNING,
            )

    def _Console(self, data):
        if data[0] == 0x01: # Back
            state = self.printer.state
            if state == "printing" or state == "paused" or state == "pausing":
                self.write("page printpause")
            else:
                self.write("page main")
        else:
            _log(data.decode())
            self.callback(self.evt.CONSOLE, data.decode())

    def _MainPage(self, data):
        if data[0] == 1: # Print
            # Request files
            files = self.callback(self.evt.FILES)
            self.files = files
            if (files):
                self.current_file_page = 1
                i = 0
                for file in files:
                    _log(file, level=logging.DEBUG)
                    safe_file = _lcd_safe_text(file)
                    # This HMI appears to expose a flat file label space.
                    # Keep filling labels as file1.t0..file1.tN.
                    self.write("file1.t%d.txt=\"%s\"" % (i, safe_file))
                    i += 1
                self.write("page file1")
            else:
                self.files = False
                self.current_file_page = 1
                self.write("page nosdcard")

        elif data[0] == 2: # Abort print
            _log("Abort print not supported") #TODO: 
        else:
            _log("_MainPage: %d not supported" % data[0])

    def _Adjustment(self, data):
        if data[0] == 0x01: # Filament tab
            self.write("adjusttemp.targettemp.val=%d" % self.printer.hotend_target)
            self.write("adjusttemp.va0.val=1")
            self.write("adjusttemp.va1.val=3") #Setting default to 10
            self.adjusting = 'Hotend'
            self.temp_unit = 10
            self.move_unit = 1
        elif data[0] == 0x02:
            self.write("page printpause")
        elif data[0] == 0x03:
            if self.printer.fan > 0:
                self.printer.fan = 0
                self.callback(self.evt.FAN, 0)
            else:
                self.printer.fan = 100
                self.callback(self.evt.FAN, 100)
        elif data[0] == 0x05:
            _log("Filament tab")
            self.speed_adjusting = None
            self.write("page adjusttemp")
        elif data[0] == 0x06: # Speed tab
            _log("Speed tab")
            self.speed_adjusting = 'PrintSpeed'
            self.write("adjustspeed.targetspeed.val=%d" % self.printer.feedrate)
            self.write("page adjustspeed")
        elif data[0] == 0x07: # Adjust tab
            _log("Adjust tab")
            self.z_offset_unit = 0.1
            self.speed_adjusting = None
            self.write("adjustzoffset.zoffset_value.val=2")
            _log(self.printer.z_offset)
            self.write("adjustzoffset.z_offset.val=%d" % (int) (self.printer.z_offset * 100))
            self.write("page adjustzoffset")
        elif data[0] == 0x08: #
            self.printer.feedrate = 100
            self.write("adjustspeed.targetspeed.val=%d" % 100)
            self.callback(self.evt.PRINT_SPEED, self.printer.feedrate)
        elif data[0] == 0x09:
            self.printer.flowrate = 100
            self.write("adjustspeed.targetspeed.val=%d" % 100)
            self.callback(self.evt.FLOW, self.printer.flowrate)
        elif data[0] == 0x0a:
            self.printer.fan = 100
            self.write("adjustspeed.targetspeed.val=%d" % 100)
            self.callback(self.evt.FAN, self.printer.fan)
        else:
            _log("_Adjustment: %d not supported" % data[0])

    def _PrintSpeed(self, data):        
        _log("_PrintSpeed: %d not supported" % data[0])

    def _StopPrint(self, data):  
        if data[0] == 0x01 or data[0] == 0xf1:
            self.callback(self.evt.PRINT_STOP)
            self.write("resumeconfirm.t1.txt=\"Stopping print. Please wait!\"")
        elif data[0] == 0xF0:
            if self.printer.state == "printing":
                self.write("page printpause")
        else:
            _log("_StopPrint: %d not supported" % data[0])

    def _PausePrint(self, data):        
        if data[0] == 0x01:
            if self.printer.state == "printing":
                self.write("page pauseconfirm")
        elif data[0] == 0xF1:
            self.callback(self.evt.PRINT_PAUSE)
            self.write("page printpause")
        else:
            _log("_PausePrint: %d not supported" % data[0])


    def _ResumePrint(self, data):       
        if data[0] == 0x01:
            if self.printer.state == "paused" or self.printer.state == "pausing":
                self.callback(self.evt.PRINT_RESUME)
            self.write("page printpause")
        else:
            _log("_ResumePrint: %d not supported" % data[0])

    def _ZOffset(self, data):           
        _log("_ZOffset: %d not supported" % data[0])

    def _TempScreen(self, data):
        if data[0] == 0x01: # Hotend
            self.write("adjusttemp.targettemp.val=%d" % self.printer.hotend_target)
            self.adjusting = 'Hotend'
        elif data[0] == 0x03: # Heatbed
            self.write("adjusttemp.targettemp.val=%d" % self.printer.bed_target)
            self.adjusting = 'Heatbed'
        elif data[0] == 0x04: # 
            pass
        elif data[0] == 0x05: # Move 0.1mm / 1C / 1%
            self.temp_unit = 1
            self.speed_unit = 1
            self.move_unit = 0.1
            self.accel_unit = 10
        elif data[0] == 0x06: # Move 1mm / 5C / 5%
            self.temp_unit = 5
            self.speed_unit = 5
            self.move_unit = 1
            self.accel_unit = 50
        elif data[0] == 0x07: # Move 10mm / 10C /10%
            self.temp_unit = 10
            self.speed_unit = 10
            self.move_unit = 10
            self.accel_unit = 100
        elif data[0] == 0x08: # + temp
            if self.adjusting == 'Hotend':
                self.printer.hotend_target += self.temp_unit
                self.write("adjusttemp.targettemp.val=%d" % self.printer.hotend_target)
                self.callback(self.evt.NOZZLE, self.printer.hotend_target)
            elif self.adjusting == 'Heatbed':
                self.printer.bed_target += self.temp_unit
                self.write("adjusttemp.targettemp.val=%d" % self.printer.bed_target)
                self.callback(self.evt.BED, self.printer.bed_target)

        elif data[0] == 0x09: # - temp
            if self.adjusting == 'Hotend':
                self.printer.hotend_target -= self.temp_unit
                self.write("adjusttemp.targettemp.val=%d" % self.printer.hotend_target)
                self.callback(self.evt.NOZZLE, self.printer.hotend_target)
            elif self.adjusting == 'Heatbed':
                self.printer.bed_target -= self.temp_unit
                self.write("adjusttemp.targettemp.val=%d" % self.printer.bed_target)
                self.callback(self.evt.BED, self.printer.bed_target)
        elif data[0] == 0x0a: # Print
            self.write("adjustspeed.targetspeed.val=%d" % self.printer.feedrate)
            self.speed_adjusting = 'PrintSpeed'
        elif data[0] == 0x0b: # Flow
            self.write("adjustspeed.targetspeed.val=%d" % self.printer.flowrate)
            self.speed_adjusting = 'Flow'
        elif data[0] == 0x0c: # Fan
            self.write("adjustspeed.targetspeed.val=%d" % self.printer.fan)
            self.speed_adjusting = 'Fan'
        elif data[0] == 0x0d or data[0] == 0x0e: # Adjust speed
            unit = self.speed_unit 
            if data[0] == 0x0e:
                unit = -self.speed_unit
            if self.speed_adjusting == 'PrintSpeed':
                self.printer.feedrate += unit
                self.write("adjustspeed.targetspeed.val=%d" % self.printer.feedrate)
                self.callback(self.evt.PRINT_SPEED, self.printer.feedrate)
            elif self.speed_adjusting == 'Flow':
                self.printer.flowrate += unit
                self.write("adjustspeed.targetspeed.val=%d" % self.printer.flowrate)
                self.callback(self.evt.FLOW, self.printer.flowrate)
            elif self.speed_adjusting == 'Fan':
                self.printer.fan += unit
                self.write("adjustspeed.targetspeed.val=%d" % self.printer.fan)
                self.callback(self.evt.FAN, self.printer.fan)
            else:
                _log("self.speed_adjusting not recognised %s" % self.speed_adjusting)
        elif data[0] == 0x42: # Accel/Speed advanced
            self.speed_unit = 10
            self.accel_unit = 100
            self.adjusting_max = True
            self.write("speed_settings.t4.font=0")
            self.write("speed_settings.accel.val=%d" % self.printer.max_accel)
            # UI field is reused to display minimum_cruise_ratio in percent (0..100).
            self.write("speed_settings.accel_to_decel.val=%d" % self.printer.max_accel_to_decel)
            self.write("speed_settings.velocity.val=%d" % self.printer.max_velocity)
            self.write("speed_settings.sqr_crnr_vel.val=%d" % int(self.printer.square_corner_velocity*10))
        elif data[0] == 0x43: # Max acceleration set
            self.adjusting_max = False

        elif data[0] == 0x11 or data[0] == 0x15: #Accel decrease / increase
            unit = self.accel_unit
            if data[0] == 0x11:
                unit = -self.accel_unit
            new_accel = self.printer.max_accel + unit
            self.write("speed_settings.accel.val=%d" % new_accel)

            self.callback(self.evt.ACCEL, new_accel)
            self.printer.max_accel = new_accel

        elif data[0] == 0x12 or data[0] == 0x16: #Accel to Decel decrease / increase
            # minimum_cruise_ratio percent is adjusted with speed-like steps.
            unit = self.speed_unit
            if data[0] == 0x12:
                unit = -self.speed_unit
            new_accel = self.printer.max_accel_to_decel + unit
            if new_accel < 0:
                new_accel = 0
            if new_accel > 100:
                new_accel = 100
            self.write("speed_settings.accel_to_decel.val=%d" % new_accel)

            self.callback(self.evt.ACCEL_TO_DECEL, new_accel)
            self.printer.max_accel_to_decel = new_accel

        elif data[0] == 0x13 or data[0] == 0x17: #Velocity decrease / increase
            unit = self.speed_unit
            if data[0] == 0x13:
                unit = -self.speed_unit
            new_velocity = self.printer.max_velocity + unit
            self.write("speed_settings.velocity.val=%d" % new_velocity)

            self.callback(self.evt.VELOCITY, new_velocity)
            self.printer.max_velocity = new_velocity

        elif data[0] == 0x14 or data[0] == 0x18: #Square Corner Velozity decrease / increase
            unit = self.speed_unit/10
            if data[0] == 0x14:
                unit = -self.speed_unit/10
            new_velocity = self.printer.square_corner_velocity + unit
            _log(new_velocity*10)
            self.write("speed_settings.sqr_crnr_vel.val=%d" % int(new_velocity*10))

            self.callback(self.evt.SQUARE_CORNER_VELOCITY, new_velocity)
            self.printer.square_corner_velocity = new_velocity

        else:
            _log("_TempScreen: Not recognised %d" % data[0])

    def _CoolScreen(self, data):
        if data[0] == 0x01: #Turn off nozzle
            if self.printer.state == "printing":
                # Ignore
                self.write("adjusttemp.targettemp.val=%d" % self.printer.hotend_target)
            else:
                self.callback(self.evt.NOZZLE, 0)
        elif data[0] == 0x02: #Turn off bed
            self.callback(self.evt.BED, 0)
        elif data[0] == 0x09: #Preheat PLA
            self.callback(self.evt.NOZZLE, self.preset_temp[PLA])
            self.callback(self.evt.BED, self.preset_bed_temp[PLA])
            self.write("pretemp.nozzle.txt=\"%d\"" % self.preset_temp[PLA])
            self.write("pretemp.bed.txt=\"%d\"" % self.preset_bed_temp[PLA])
        elif data[0] == 0x0a: #Preheat ABS
            self.callback(self.evt.NOZZLE, self.preset_temp[ABS])
            self.callback(self.evt.BED, self.preset_bed_temp[ABS])
            self.write("pretemp.nozzle.txt=\"%d\"" % self.preset_temp[ABS])
            self.write("pretemp.bed.txt=\"%d\"" % self.preset_bed_temp[ABS])
        elif data[0] == 0x0b: #Preheat PETG
            self.callback(self.evt.NOZZLE, self.preset_temp[PETG])
            self.callback(self.evt.BED, self.preset_bed_temp[PETG])
            self.write("pretemp.nozzle.txt=\"%d\"" % self.preset_temp[PETG])
            self.write("pretemp.bed.txt=\"%d\"" % self.preset_bed_temp[PETG])
        elif data[0] == 0x0c: #Preheat TPU
            self.callback(self.evt.NOZZLE, self.preset_temp[TPU])
            self.callback(self.evt.BED, self.preset_bed_temp[TPU])
            self.write("pretemp.nozzle.txt=\"%d\"" % self.preset_temp[TPU])
            self.write("pretemp.bed.txt=\"%d\"" % self.preset_bed_temp[TPU])
        elif data[0] == 0x0d: #Preheat PLA setting
            self.preset_index = PLA
            self.write("tempsetvalue.nozzletemp.val=%d" % self.preset_temp[PLA])
            self.write("tempsetvalue.bedtemp.val=%d" % self.preset_bed_temp[PLA])
            self.write("page tempsetvalue")
        elif data[0] == 0x0e: #Preheat ABS setting
            self.preset_index = ABS
            self.write("tempsetvalue.nozzletemp.val=%d" % self.preset_temp[ABS])
            self.write("tempsetvalue.bedtemp.val=%d" % self.preset_bed_temp[ABS])
            self.write("page tempsetvalue")
        elif data[0] == 0x0f: #Preheat PETG setting
            self.preset_index = PETG
            self.write("tempsetvalue.nozzletemp.val=%d" % self.preset_temp[PETG])
            self.write("tempsetvalue.bedtemp.val=%d" % self.preset_bed_temp[PETG])
            self.write("page tempsetvalue")
        elif data[0] == 0x10: #Preheat TPU setting
            self.preset_index = TPU
            self.write("tempsetvalue.nozzletemp.val=%d" % self.preset_temp[TPU])
            self.write("tempsetvalue.bedtemp.val=%d" % self.preset_bed_temp[TPU])
            self.write("page tempsetvalue")
        elif data[0] == 0x11: # Level
            self.preset_index = PROBE
            self.write("tempsetvalue.nozzletemp.val=%d" % self.preset_temp[PROBE])
            self.write("tempsetvalue.bedtemp.val=%d" % self.preset_bed_temp[PROBE])
            self.write("page tempsetvalue")
        else:
            _log("_CoolScreen: Not recognised %d" % data[0])

    def _Heater0TempEnter(self, data):
        temp = ((data[0] & 0x00FF) << 8) | ((data[0] & 0xFF00) >> 8) 
        _log("Set nozzle temp: %d" % temp)
        self.callback(self.evt.NOZZLE, temp)

    def _Heater1TempEnter(self, data):  
        _log("_Heater1TempEnter: %d not supported" % data[0])

    def _HotBedTempEnter(self, data):   
        temp = ((data[0] & 0x00FF) << 8) | ((data[0] & 0xFF00) >> 8) 
        self.callback(self.evt.BED, temp)

    def _SettingScreen(self, data):
        if data[0] == 0x01:
            self.callback(self.evt.PROBE)
            self.write("page autohome")
            self.write("leveling.va1.val=1")

        elif data[0] == 0x06: # Motor release
            self.callback(self.evt.MOTOR_OFF)
        elif data[0] == 0x07: # Fan Control

            pass
        elif data[0] == 0x08: 
            _log("What is this???")
            pass
        elif data[0] == 0x09: # 
            self.write("page pretemp")
            self.write("pretemp.nozzle.txt=\"%d\"" % self.printer.hotend_target)
            self.write("pretemp.bed.txt=\"%d\"" % self.printer.bed_target)
        elif data[0] == 0x0a:
            self.write("page prefilament")
            self.write("prefilament.filamentlength.txt=\"%d\"" % self.load_len)
            self.write("prefilament.filamentspeed.txt=\"%d\"" % self.feedrate_e)
        elif data[0] == 0x0b:
            self.write("page set")
        elif data[0] == 0x0c:
            self.write("page warn_rdlevel")
        elif data[0] == 0x0d: # Advanced Settings
            self.write("multiset.plrbutton.val=1") #TODO recovery enabled?
            self.write("page multiset")

        else:
            _log("_SettingScreen: Not recognised %d" % data[0])

        return

    def _SettingBack(self, data):
        if data[0] == 0x01:
            if self.probe_mode:
                self.probe_mode = False
                self.callback(self.evt.PROBE_BACK)
        else:
            _log("_SettingBack: Not recognised %d" % data[0])

    def _BedLevelFun(self, data):
        if data[0] == 0x02 or data[0] == 0x03: # z_offset Up / Down
            offset = self.printer.z_offset
            unit = self.z_offset_unit
            if data[0] == 0x03:
                unit = - self.z_offset_unit

            if self.probe_mode:
                z_pos = self.printer.z_pos + unit
                _log("Probe: z_pos %d" % z_pos)
                self.write("leveldata.z_offset.val=%d" % (int)(pos * 100))
                self.callback(self.evt.PROBE, unit)
            else:
                offset += unit
                self.write("adjustzoffset.z_offset.val=%d" % (int)(offset * 100))
                self.callback(self.evt.Z_OFFSET, offset)
                self.printer.z_offset = offset
        elif data[0] == 0x04:
            self.z_offset_unit = 0.01
            self.write("adjustzoffset.zoffset_value.val=1")
        elif data[0] == 0x05:
            self.z_offset_unit = 0.1
            self.write("adjustzoffset.zoffset_value.val=2")
        elif data[0] == 0x06:
            self.z_offset_unit = 1
            self.write("adjustzoffset.zoffset_value.val=3")
        elif data[0] == 0x07: # LED 2 TODO: Where is LED2??
            _log("Toggle led2!!????")
        elif data[0] == 0x08: # Light control
            if self.light == True:
                self.light = False
                self.write("status_led2=0")
                self.callback(self.evt.LIGHT, 0)
            else:
                self.light = True
                self.write("status_led2=1")
                self.callback(self.evt.LIGHT, 128)

        elif data[0] == 0x09: # Bed mesh leveling
            # Wait for heaters?
            self.callback(self.evt.PROBE_COMPLETE)
            self.write("page leveldata_36")
            self.write("leveling_36.tm0.en=0")
            self.write("leveling.tm0.en=0")

        elif data[0] == 0x0a:
            self.write("printpause.printspeed.txt=\"%d\"" % self.printer.feedrate)
            self.write("printpause.fanspeed.txt=\"%d\"" % self.printer.fan)
            self.write("printpause.zvalue.val=%d" % (int)(self.printer.z_pos*10))
            self.write("printpause.printtime.txt=\"%d h %d min\"" % (self.printer.remaining/3600,(self.printer.remaining % 3600)/60))
            self.write("printpause.printprocess.val=%d" % self.printer.percent)
            self.write("printpause.printvalue.txt=\"%d\"" % self.printer.percent)

        elif data[0] == 0x0b:
            pass # Screen requesting nozzle and bed temp
        elif data[0] == 0x0c:
            pass
        elif data[0] == 0x16:
            self.write("main.va0.val=1")
            self.write("printpause.t0.txt=\"%s\"" % self.printer.file_name)
            self.write("printpause.printprocess.val=%d" % self.printer.percent)
            self.write("printpause.printvalue.txt=\"%d\"" % self.printer.percent)
        else:
            _log("_BedLevelFun: Data not recognised %d" % data[0])

    def _AxisPageSelect(self, data):
        if data[0] == 0x04: #Home all
            self.callback(self.evt.HOME, 'X Y Z')
        elif data[0] == 0x05: #Home X
            self.callback(self.evt.HOME, 'X')
        elif data[0] == 0x06: #Home Y
            self.callback(self.evt.HOME, 'Y')
        elif data[0] == 0x07: #Home Z
            self.callback(self.evt.HOME, 'Z')
        else:
            _log("_AxisPageSelect: Data not recognised %d" % data[0])

    def _Xaxismove(self, data):
        if data[0] == 0x01: # X+
            self.callback(self.evt.MOVE_X, self.move_unit)
        elif data[0] == 0x02: # X-
            self.callback(self.evt.MOVE_X, -self.move_unit)
        else:
            _log("_Xaxismove: Data not recognised %d" % data[0])

    def _Yaxismove(self, data):         
        if data[0] == 0x01: # Y+
            self.callback(self.evt.MOVE_Y, self.move_unit)
        elif data[0] == 0x02: # Y-
            self.callback(self.evt.MOVE_Y, -self.move_unit)
        else:
            _log("_Yaxismove: Data not recognised %d" % data[0])

    def _Zaxismove(self, data):
        if data[0] == 0x01: # Z+
            self.callback(self.evt.MOVE_Z, self.move_unit)
        elif data[0] == 0x02: # Z-
            self.callback(self.evt.MOVE_Z, -self.move_unit)
        else:
            _log("_Zaxismove: Data not recognised %d" % data[0])

    def _SelectExtruder(self, data):    
        _log("_SelectExtruder: Not recognised %d" % data[0])

    def _Heater0LoadEnter(self, data):
        load_len = ((data[0] & 0x00FF) << 8) | ((data[0] & 0xFF00) >> 8)
        self.load_len = load_len
        _log(load_len)

    def _Heater1LoadEnter(self, data):  
        feedrate_e = ((data[0] & 0x00FF) << 8) | ((data[0] & 0xFF00) >> 8)
        self.feedrate_e = feedrate_e
        _log(feedrate_e)

    def _FilamentLoad(self, data):
        if data[0] == 0x01 or data[0] == 0x02: # Load / Unload 
            if self.printer.state == 'printing':
                self.write("page warn1_filament")
            else:
                if data[0] == 0x01:
                    self.callback(self.evt.MOVE_E, [-self.load_len, self.feedrate_e])
                else:
                    self.callback(self.evt.MOVE_E, [self.load_len, self.feedrate_e])
        elif data[0] == 0x05: # Temp warning Confirm
            pass
        elif data[0] == 0x06: # Temp warning Cancel
            pass

        elif data[0] == 0x0a: # Back
            self.write("page main")
        else:   
            _log("_FilamentLoad: Not recognised %d" % data[0])

    def _SelectLanguage(self, data):    
        _log("_SelectLanguage: Not recognised %d" % data[0])

    def _FilamentCheck(self, data):     
        _log("_FilamentCheck: Not recognised %d" % data[0])

    def _PowerContinuePrint(self, data):
        _log("_PowerContinuePrint: Not recognised %d" % data[0])

    def _PrintSelectMode(self, data):   
        _log("_PrintSelectMode: Not recognised %d" % data[0])

    def _XhotendOffset(self, data):     
        _log("_XhotendOffset: Not recognised %d" % data[0])

    def _YhotendOffset(self, data):     
        _log("_YhotendOffset: Not recognised %d" % data[0])

    def _ZhotendOffset(self, data):     
        _log("_ZhotendOffset: Not recognised %d" % data[0])

    def _StoreMemory(self, data):       
        _log("_StoreMemory: Not recognised %d" % data[0])

    def _PrintFile(self, data):
        code = data[0]
        if code == 0x01:
            self.write(
                "file%d.t%d.pco=65504"
                % ((self.selected_file // 5) + 1, self.selected_file % 5)
            )
            self.write("printpause.printvalue.txt=\"0\"")
            self.write("printpause.printprocess.val=0")
            self.write("leveldata.z_offset.val=%d" % (int)(self.printer.z_offset * 100))
            self.write("page printpause")
            self.write("restFlag2=1")
            self.callback(self.evt.PRINT_START, self.selected_file)
        elif code == 0x02:  # Next page
            if self.current_file_page < self._file_page_count():
                self.current_file_page += 1
            logger.debug("HMI file-page next: page=%d/%d", self.current_file_page, self._file_page_count())
        elif code in (0x03, 0x07):  # Previous page (+ repeat variant)
            if self.current_file_page > 1:
                self.current_file_page -= 1
            logger.debug("HMI file-page prev: page=%d/%d", self.current_file_page, self._file_page_count())
        elif code in (0x04, 0x06, 0x08, 0x09):  # Repeat/noise variants seen on right-nav
            logger.debug("HMI file-page next repeat/noise code: %d page=%d/%d", code, self.current_file_page, self._file_page_count())
        elif code == 0x05:
            logger.debug("HMI file-page prev repeat/noise code: %d page=%d/%d", code, self.current_file_page, self._file_page_count())

        elif code == 0x0A:
            if self.askprint:
                self.askprint = False
                self.write("page file%d" % self.current_file_page)
            else:
                self.write("page main")

        else:
            _log("_PrintFile: Not recognised %d" % code)

    def _PrintFileCompat(self, data):
        # Some HMI firmware revisions emit 0x2183 for file navigation/back.
        code = data[0] if data else None
        logger.debug("Compat file input: addr=0x2183 code=%r data=%r", code, data)
        if code in (0x01, 0x03):
            if self.current_file_page > 1:
                self.current_file_page -= 1
            logger.debug(
                "HMI compat file-page prev: page=%d/%d",
                self.current_file_page,
                self._file_page_count(),
            )
        elif code == 0x0A:
            # Back to main menu on this HMI compatibility address.
            self.write("page main")
        else:
            _log("_PrintFileCompat: Not recognised %s" % code)

    def _SelectFile(self, data):
        _log(self.files, level=logging.DEBUG)
        if not self.files:
            _log("_SelectFile: file list not available", level=logging.WARNING)
            return

        selected_file = None
        # Primary mode: HMI reports selected slot index (1..5) on current page.
        if data[0] >= 1 and data[0] <= 5:
            page_offset = (self.current_file_page - 1) * 5
            candidate = page_offset + (data[0] - 1)
            if candidate < len(self.files):
                selected_file = candidate
        # Compatibility mode: some firmware variants may report absolute 1-based index.
        elif data[0] >= 1 and data[0] <= len(self.files):
            selected_file = data[0] - 1

        if selected_file is not None:
            self.selected_file = selected_file
            self.current_file_page = (self.selected_file // 5) + 1
            safe_file = _lcd_safe_text(self.files[self.selected_file])
            self.write("askprint.t0.txt=\"%s\"" % safe_file)
            self.write("printpause.t0.txt=\"%s\"" % safe_file)
            self.write("askprint.cp0.close()")
            self.write("askprint.cp0.aph=0")
            self.write("page askprint")
            self.callback(self.evt.THUMBNAIL)
            self.askprint = True
        else:
            _log("_SelectFile: Data not recognised %d" % data[0])


    def _ChangePage(self, data):        
        _log("_ChangePage: Not recognised %d" % data[0])

    def _SetPreNozzleTemp(self, data):
        material = self.preset_index
        if data[0] == 0x01:
            self.preset_temp[material] += self.temp_unit
        elif data[0] == 0x02:
            self.preset_temp[material] -= self.temp_unit
        self.write("tempsetvalue.nozzletemp.val=%d" % self.preset_temp[material])

    def _SetPreBedTemp(self, data):
        material = self.preset_index
        if data[0] == 0x01:
            self.preset_bed_temp[material] += self.temp_unit
        elif data[0] == 0x02:
            self.preset_bed_temp[material] -= self.temp_unit
        material = self.preset_index
        self.write("tempsetvalue.bedtemp.val=%d" % self.preset_bed_temp[material])

    def _HardwareTest(self, data):
        if data[0] == 0x0f: # Hardware test page
            pass #Always requested on main page load, ignore
        else:
            _log("_HardwareTest: Not implemented: 0x%x" % data[0], level=logging.WARNING)

    def _Err_Control(self, data):       
        _log("_Err_Control: Not recognised %d" % data[0])


if __name__ == "__main__":
    lcd = LCD("/dev/ttyUSB0", baud=115200)
    lcd.start()

    lcd.ser.write(b'page boot')
    lcd.ser.write(bytearray([0xFF, 0xFF, 0xFF]))

    lcd.ser.write(b'com_star')
    lcd.ser.write(bytearray([0xFF, 0xFF, 0xFF]))

    lcd.ser.write(b'main.va0.val=1')
    lcd.ser.write(bytearray([0xFF, 0xFF, 0xFF]))


    sleep(1)

    lcd.ser.write(b'page main')
    lcd.ser.write(bytearray([0xFF, 0xFF, 0xFF]))

    lcd.ser.write(b'main.nozzletemp.txt=\"%d / %d\"' % (23, 0))
    lcd.ser.write(bytearray([0xFF, 0xFF, 0xFF]))

    lcd.ser.write(b'main.bedtemp.txt=\"%d / %d\"' % (24, 0))
    lcd.ser.write(bytearray([0xFF, 0xFF, 0xFF]))
