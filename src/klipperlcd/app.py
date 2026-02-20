import base64
import os
import time
import traceback
from threading import Thread

from .lcd import LCD, _printerData
from .printer import PrinterData


def _env_int(name, default):
    """Read integer env var and fallback to default on invalid values."""
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_float(name, default):
    """Read float env var and fallback to default on invalid values."""
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


class KlipperLCD:
    def __init__(self):
        # Runtime settings are provided through systemd EnvironmentFile.
        self.lcd_port = os.getenv("KLIPPERLCD_LCD_PORT", "/dev/ttyAMA0")
        self.lcd_baud = _env_int("KLIPPERLCD_LCD_BAUDRATE", 115200)
        self.api_key = os.getenv("KLIPPERLCD_API_KEY", "XXXXXX")
        self.moonraker_url = os.getenv("KLIPPERLCD_MOONRAKER_URL", "127.0.0.1")
        self.klippy_sock = os.getenv(
            "KLIPPERLCD_KLIPPY_SOCK", "/home/pi/printer_data/comms/klippy.sock"
        )
        self.update_interval = _env_float("KLIPPERLCD_UPDATE_INTERVAL", 2.0)

        # Initialize transport endpoints.
        self.lcd = LCD(self.lcd_port, baud=self.lcd_baud, callback=self.lcd_callback)
        self.lcd.start()
        self.printer = PrinterData(
            self.api_key,
            URL=self.moonraker_url,
            klippy_sock=self.klippy_sock,
            callback=self.printer_callback,
        )

        self.running = False
        self.wait_probe = False
        self.thumbnail_inprogress = False

        # Wait until klippy status is available while showing boot progress.
        progress_bar = 1
        while self.printer.update_variable() is False:
            progress_bar += 5
            self.lcd.boot_progress(progress_bar)
            time.sleep(1)

        # Initial data load for LCD UI.
        self.printer.init_Webservices()
        gcode_store = self.printer.get_gcode_store()
        self.lcd.write_gcode_store(gcode_store)

        macros = self.printer.get_macros()
        self.lcd.write_macros(macros)

        print(self.printer.MACHINE_SIZE)
        print(self.printer.SHORT_BUILD_VERSION)
        self.lcd.write('information.size.txt="%s"' % self.printer.MACHINE_SIZE)
        self.lcd.write('information.sversion.txt="%s"' % self.printer.SHORT_BUILD_VERSION)
        self.lcd.write("page main")

    def start(self):
        print("KlipperLCD start")
        self.running = True
        Thread(target=self.periodic_update).start()

    def periodic_update(self):
        # Main state-sync loop: read printer status and push updates to LCD.
        while self.running:
            try:
                if self.wait_probe:
                    print(
                        "Zpos=%f, Zoff=%f"
                        % (self.printer.current_position.z, self.printer.BABY_Z_VAR)
                    )
                    if self.printer.ishomed():
                        self.wait_probe = False
                        print("IsHomed")
                        self.lcd.probe_mode_start()

                self.printer.update_variable()
                data = _printerData()
                data.hotend_target = self.printer.thermalManager["temp_hotend"][0]["target"]
                data.hotend = self.printer.thermalManager["temp_hotend"][0]["celsius"]
                data.bed_target = self.printer.thermalManager["temp_bed"]["target"]
                data.bed = self.printer.thermalManager["temp_bed"]["celsius"]
                data.state = self.printer.getState()
                data.percent = self.printer.getPercent()
                data.duration = self.printer.duration()
                data.remaining = self.printer.remain()
                data.feedrate = self.printer.print_speed
                data.flowrate = self.printer.flow_percentage
                data.fan = self.printer.thermalManager["fan_speed"][0]
                data.x_pos = self.printer.current_position.x
                data.y_pos = self.printer.current_position.y
                data.z_pos = self.printer.current_position.z
                data.z_offset = self.printer.BABY_Z_VAR
                data.file_name = self.printer.file_name
                data.max_velocity = self.printer.max_velocity
                data.max_accel = self.printer.max_accel
                data.max_accel_to_decel = self.printer.max_accel_to_decel
                data.square_corner_velocity = self.printer.square_corner_velocity

                self.lcd.data_update(data)
                time.sleep(self.update_interval)
            except Exception:
                print("FATAL: periodic_update crashed")
                traceback.print_exc()
                # Let systemd restart the service on unrecoverable runtime errors.
                os._exit(1)

    def printer_callback(self, data, data_type):
        msg = self.lcd.format_console_data(data, data_type)
        if msg:
            self.lcd.write_console(msg)

    def show_thumbnail(self):
        if self.printer.file_path and (
            self.printer.file_name or self.lcd.files[self.lcd.selected_file]
        ):
            file_name = ""
            if self.lcd.files:
                file_name = self.lcd.files[self.lcd.selected_file]
            elif self.printer.file_name:
                file_name = self.printer.file_name
            else:
                print("ERROR: gcode file not known")

            file = self.printer.file_path + "/" + file_name

            # Read gcode file and parse embedded base64 thumbnail block.
            print(file)
            f = open(file, "r")
            if not f:
                f.close()
                print("File could not be opened: %s" % file)
                return
            buf = f.readlines()
            if not f:
                f.close()
                print("File could not be read")
                return

            f.close()
            thumbnail_found = False
            b64 = ""

            for line in buf:
                if "thumbnail begin" in line:
                    thumbnail_found = True
                elif "thumbnail end" in line:
                    thumbnail_found = False
                    break
                elif thumbnail_found:
                    b64 += line.strip(" \t\n\r;")

            if len(b64):
                # Decode Base64 image payload and forward to LCD transport.
                img = base64.b64decode(b64)
                self.lcd.write_thumbnail(img)
            else:
                self.lcd.clear_thumbnail()
                print("Aborting thumbnail, no image found")
        else:
            print("File path or name to gcode-files missing")

        self.thumbnail_inprogress = False

    def lcd_callback(self, evt, data=None):
        # Route LCD events to printer actions/gcode commands.
        if evt == self.lcd.evt.HOME:
            self.printer.home(data)
        elif evt == self.lcd.evt.MOVE_X:
            self.printer.moveRelative("X", data, 4000)
        elif evt == self.lcd.evt.MOVE_Y:
            self.printer.moveRelative("Y", data, 4000)
        elif evt == self.lcd.evt.MOVE_Z:
            self.printer.moveRelative("Z", data, 600)
        elif evt == self.lcd.evt.MOVE_E:
            print(data)
            self.printer.moveRelative("E", data[0], data[1])
        elif evt == self.lcd.evt.Z_OFFSET:
            self.printer.setZOffset(data)
        elif evt == self.lcd.evt.NOZZLE:
            self.printer.setExtTemp(data)
        elif evt == self.lcd.evt.BED:
            self.printer.setBedTemp(data)
        elif evt == self.lcd.evt.FILES:
            files = self.printer.GetFiles(True)
            return files
        elif evt == self.lcd.evt.PRINT_START:
            self.printer.openAndPrintFile(data)
            if self.thumbnail_inprogress is False:
                self.thumbnail_inprogress = True
        elif evt == self.lcd.evt.THUMBNAIL:
            if self.thumbnail_inprogress is False:
                self.thumbnail_inprogress = True
                Thread(target=self.show_thumbnail).start()
        elif evt == self.lcd.evt.PRINT_STATUS:
            pass
        elif evt == self.lcd.evt.PRINT_STOP:
            self.printer.cancel_job()
        elif evt == self.lcd.evt.PRINT_PAUSE:
            self.printer.pause_job()
        elif evt == self.lcd.evt.PRINT_RESUME:
            self.printer.resume_job()
        elif evt == self.lcd.evt.PRINT_SPEED:
            self.printer.set_print_speed(data)
        elif evt == self.lcd.evt.FLOW:
            self.printer.set_flow(data)
        elif evt == self.lcd.evt.PROBE:
            if data is None:
                self.printer.probe_calibrate()
                self.wait_probe = True
            else:
                self.printer.probe_adjust(data)
        elif evt == self.lcd.evt.PROBE_COMPLETE:
            self.wait_probe = False
            print("Save settings!")
            self.printer.sendGCode("ACCEPT")
            self.printer.sendGCode("G1 F1000 Z15.0")
            print("Calibrate!")
            self.printer.sendGCode(
                "BED_MESH_CALIBRATE PROFILE=default METHOD=automatic"
            )
        elif evt == self.lcd.evt.PROBE_BACK:
            print("BACK!")
            self.printer.sendGCode("ACCEPT")
            self.printer.sendGCode("G1 F1000 Z15.0")
            self.printer.sendGCode("SAVE_CONFIG")
        elif evt == self.lcd.evt.BED_MESH:
            pass
        elif evt == self.lcd.evt.LIGHT:
            self.printer.set_led(data)
        elif evt == self.lcd.evt.FAN:
            self.printer.set_fan(data)
        elif evt == self.lcd.evt.MOTOR_OFF:
            self.printer.sendGCode("M18")
        elif evt == self.lcd.evt.ACCEL:
            self.printer.sendGCode("SET_VELOCITY_LIMIT ACCEL=%d" % data)
        elif evt == self.lcd.evt.ACCEL_TO_DECEL:
            # Modern Klipper uses minimum_cruise_ratio (0..1), UI provides percent.
            self.printer.sendGCode(
                "SET_VELOCITY_LIMIT MINIMUM_CRUISE_RATIO=%.2f" % (data / 100.0)
            )
        elif evt == self.lcd.evt.VELOCITY:
            self.printer.sendGCode("SET_VELOCITY_LIMIT VELOCITY=%d" % data)
        elif evt == self.lcd.evt.SQUARE_CORNER_VELOCITY:
            print("SET_VELOCITY_LIMIT SQUARE_CORNER_VELOCITY=%.1f" % data)
            self.printer.sendGCode(
                "SET_VELOCITY_LIMIT SQUARE_CORNER_VELOCITY=%.1f" % data
            )
        elif evt == self.lcd.evt.CONSOLE:
            self.printer.sendGCode(data)
        else:
            print("lcd_callback event not recognised %d" % evt)


def run():
    x = KlipperLCD()
    x.start()
