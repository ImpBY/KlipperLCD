import base64
import logging
import os
import time
from pathlib import Path
from threading import Lock, Thread

from .lcd import LCD, _printerData
from .logging_setup import setup_logging
from .printer import PrinterData

logger = logging.getLogger(__name__)


def _log(*args, level=logging.INFO):
    logger.log(level, " ".join(str(arg) for arg in args))


THUMBNAIL_MAX_B64_LEN = 8 * 1024 * 1024


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
        self._update_thread = None
        self._event_names = self._build_event_names()
        self._thumbnail_lock = Lock()

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

        _log(self.printer.MACHINE_SIZE, level=logging.DEBUG)
        _log(self.printer.SHORT_BUILD_VERSION, level=logging.DEBUG)
        self.lcd.write('information.size.txt="%s"' % self.printer.MACHINE_SIZE)
        self.lcd.write('information.sversion.txt="%s"' % self.printer.SHORT_BUILD_VERSION)
        self.lcd.write("page main")

    def start(self):
        _log("KlipperLCD start")
        self.running = True
        self._update_thread = Thread(target=self.periodic_update, daemon=True)
        self._update_thread.start()

    def stop(self):
        _log("KlipperLCD stop requested")
        self.running = False
        if self._update_thread and self._update_thread.is_alive():
            self._update_thread.join(timeout=2.0)
        self.lcd.stop()
        self.printer.stop()

    def _build_event_names(self):
        event_names = {}
        for attr in dir(self.lcd.evt):
            if attr.startswith("_"):
                continue
            value = getattr(self.lcd.evt, attr)
            if isinstance(value, int):
                event_names[value] = attr
        return event_names

    def _event_name(self, evt):
        return self._event_names.get(evt, "UNKNOWN")

    def _event_rx_log(self, evt, data):
        logger.debug(
            "APP RX event: name=%s id=%s payload=%r",
            self._event_name(evt),
            evt,
            data,
        )

    def _event_tx_log(self, action, payload=None):
        logger.debug("APP TX action: %s payload=%r", action, payload)

    def _build_lcd_snapshot(self):
        p = self.printer
        data = _printerData()
        data.hotend_target = p.thermalManager["temp_hotend"][0]["target"]
        data.hotend = p.thermalManager["temp_hotend"][0]["celsius"]
        data.bed_target = p.thermalManager["temp_bed"]["target"]
        data.bed = p.thermalManager["temp_bed"]["celsius"]
        data.state = p.getState()
        data.percent = p.getPercent()
        data.duration = p.duration()
        data.remaining = p.remain()
        data.feedrate = p.print_speed
        data.flowrate = p.flow_percentage
        data.fan = p.thermalManager["fan_speed"][0]
        data.x_pos = p.current_position.x
        data.y_pos = p.current_position.y
        data.z_pos = p.current_position.z
        data.z_offset = p.BABY_Z_VAR
        data.file_name = p.file_name
        data.max_velocity = p.max_velocity
        data.max_accel = p.max_accel
        data.max_accel_to_decel = p.max_accel_to_decel
        data.square_corner_velocity = p.square_corner_velocity
        return data

    def periodic_update(self):
        # Main state-sync loop: read printer status and push updates to LCD.
        while self.running:
            try:
                if self.wait_probe:
                    _log(
                        "Zpos=%f, Zoff=%f"
                        % (self.printer.current_position.z, self.printer.BABY_Z_VAR),
                        level=logging.DEBUG,
                    )
                    if self.printer.ishomed():
                        self.wait_probe = False
                        _log("IsHomed")
                        self.lcd.probe_mode_start()

                self.printer.update_variable()
                data = self._build_lcd_snapshot()
                logger.debug(
                    "APP periodic snapshot: state=%s percent=%.2f file=%r",
                    data.state,
                    data.percent,
                    data.file_name,
                )
                self.lcd.data_update(data)
                time.sleep(self.update_interval)
            except Exception:
                _log("FATAL: periodic_update crashed", level=logging.ERROR)
                logger.exception("periodic_update crashed")
                # Let systemd restart the service on unrecoverable runtime errors.
                os._exit(1)

    def printer_callback(self, data, data_type):
        logger.debug("APP printer callback: type=%s payload=%r", data_type, data)
        msg = self.lcd.format_console_data(data, data_type)
        if msg:
            self.lcd.write_console(msg)

    def _start_thumbnail_worker(self):
        with self._thumbnail_lock:
            if self.thumbnail_inprogress:
                logger.debug("APP thumbnail worker already running")
                return False
            self.thumbnail_inprogress = True
        Thread(target=self.show_thumbnail, daemon=True).start()
        return True

    def _resolve_thumbnail_file(self):
        file_name = None
        if (
            self.lcd.files
            and isinstance(self.lcd.selected_file, int)
            and 0 <= self.lcd.selected_file < len(self.lcd.files)
        ):
            file_name = self.lcd.files[self.lcd.selected_file]
        elif self.printer.file_name:
            file_name = self.printer.file_name

        if not (self.printer.file_path and file_name):
            return None, None, None

        raw_base_path = self.printer.file_path
        base_path = Path(os.path.expandvars(os.path.expanduser(raw_base_path))).resolve()
        file_path = (base_path / file_name).resolve()
        # Prevent path traversal outside configured gcode directory.
        if os.path.commonpath([str(base_path), str(file_path)]) != str(base_path):
            logger.warning(
                "Thumbnail path rejected: base=%s candidate=%s",
                base_path,
                file_path,
            )
            return None, None, None
        return file_name, base_path, file_path

    def _extract_thumbnail_b64(self, file_path):
        thumbnail_found = False
        parts = []
        total = 0
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if "thumbnail begin" in line:
                    thumbnail_found = True
                    continue
                if "thumbnail end" in line and thumbnail_found:
                    break
                if thumbnail_found:
                    chunk = line.strip(" \t\n\r;")
                    if chunk:
                        total += len(chunk)
                        if total > THUMBNAIL_MAX_B64_LEN:
                            logger.warning(
                                "Thumbnail payload too large (%d), aborting for %s",
                                total,
                                file_path,
                            )
                            return None
                        parts.append(chunk)
        if not parts:
            return None
        return "".join(parts)

    def show_thumbnail(self):
        try:
            file_name, base_path, file_path = self._resolve_thumbnail_file()
            if not file_path:
                _log("File path or name to gcode-files missing", level=logging.WARNING)
                return

            logger.debug(
                "APP thumbnail source: base=%r file=%r resolved=%s",
                str(base_path),
                file_name,
                str(file_path),
            )
            try:
                b64 = self._extract_thumbnail_b64(file_path)
            except OSError as e:
                _log("File could not be opened: %s (%s)" % (file_path, e), level=logging.ERROR)
                return

            if b64:
                try:
                    img = base64.b64decode(b64, validate=False)
                except Exception as e:
                    _log("Thumbnail decode failed: %s" % e, level=logging.ERROR)
                    return
                self.lcd.write_thumbnail(img)
            else:
                self.lcd.clear_thumbnail()
                _log("Aborting thumbnail, no image found", level=logging.WARNING)
        finally:
            with self._thumbnail_lock:
                self.thumbnail_inprogress = False

    def lcd_callback(self, evt, data=None):
        # Route LCD events to printer actions/gcode commands.
        self._event_rx_log(evt, data)
        if evt == self.lcd.evt.HOME:
            self._event_tx_log("printer.home", data)
            self.printer.home(data)
        elif evt == self.lcd.evt.MOVE_X:
            self._event_tx_log("printer.moveRelative", ("X", data, 4000))
            self.printer.moveRelative("X", data, 4000)
        elif evt == self.lcd.evt.MOVE_Y:
            self._event_tx_log("printer.moveRelative", ("Y", data, 4000))
            self.printer.moveRelative("Y", data, 4000)
        elif evt == self.lcd.evt.MOVE_Z:
            self._event_tx_log("printer.moveRelative", ("Z", data, 600))
            self.printer.moveRelative("Z", data, 600)
        elif evt == self.lcd.evt.MOVE_E:
            _log(data, level=logging.DEBUG)
            self._event_tx_log("printer.moveRelative", ("E", data[0], data[1]))
            self.printer.moveRelative("E", data[0], data[1])
        elif evt == self.lcd.evt.Z_OFFSET:
            self._event_tx_log("printer.setZOffset", data)
            self.printer.setZOffset(data)
        elif evt == self.lcd.evt.NOZZLE:
            self._event_tx_log("printer.setExtTemp", data)
            self.printer.setExtTemp(data)
        elif evt == self.lcd.evt.BED:
            self._event_tx_log("printer.setBedTemp", data)
            self.printer.setBedTemp(data)
        elif evt == self.lcd.evt.FILES:
            self._event_tx_log("printer.GetFiles", True)
            files = self.printer.GetFiles(True)
            logger.debug("APP files fetched: count=%d", len(files) if files else 0)
            return files
        elif evt == self.lcd.evt.PRINT_START:
            self._event_tx_log("printer.openAndPrintFile", data)
            self.printer.openAndPrintFile(data)
            self._start_thumbnail_worker()
        elif evt == self.lcd.evt.THUMBNAIL:
            self._start_thumbnail_worker()
        elif evt == self.lcd.evt.PRINT_STATUS:
            pass
        elif evt == self.lcd.evt.PRINT_STOP:
            self._event_tx_log("printer.cancel_job")
            self.printer.cancel_job()
        elif evt == self.lcd.evt.PRINT_PAUSE:
            self._event_tx_log("printer.pause_job")
            self.printer.pause_job()
        elif evt == self.lcd.evt.PRINT_RESUME:
            self._event_tx_log("printer.resume_job")
            self.printer.resume_job()
        elif evt == self.lcd.evt.PRINT_SPEED:
            self._event_tx_log("printer.set_print_speed", data)
            self.printer.set_print_speed(data)
        elif evt == self.lcd.evt.FLOW:
            self._event_tx_log("printer.set_flow", data)
            self.printer.set_flow(data)
        elif evt == self.lcd.evt.PROBE:
            if data is None:
                self._event_tx_log("printer.probe_calibrate")
                self.printer.probe_calibrate()
                self.wait_probe = True
            else:
                self._event_tx_log("printer.probe_adjust", data)
                self.printer.probe_adjust(data)
        elif evt == self.lcd.evt.PROBE_COMPLETE:
            self.wait_probe = False
            _log("Save settings!")
            self._event_tx_log("printer.sendGCode", "ACCEPT")
            self.printer.sendGCode("ACCEPT")
            self._event_tx_log("printer.sendGCode", "G1 F1000 Z15.0")
            self.printer.sendGCode("G1 F1000 Z15.0")
            _log("Calibrate!")
            self._event_tx_log(
                "printer.sendGCode",
                "BED_MESH_CALIBRATE PROFILE=default METHOD=automatic",
            )
            self.printer.sendGCode(
                "BED_MESH_CALIBRATE PROFILE=default METHOD=automatic"
            )
        elif evt == self.lcd.evt.PROBE_BACK:
            _log("BACK!")
            self._event_tx_log("printer.sendGCode", "ACCEPT")
            self.printer.sendGCode("ACCEPT")
            self._event_tx_log("printer.sendGCode", "G1 F1000 Z15.0")
            self.printer.sendGCode("G1 F1000 Z15.0")
            self._event_tx_log("printer.sendGCode", "SAVE_CONFIG")
            self.printer.sendGCode("SAVE_CONFIG")
        elif evt == self.lcd.evt.BED_MESH:
            pass
        elif evt == self.lcd.evt.LIGHT:
            self._event_tx_log("printer.set_led", data)
            self.printer.set_led(data)
        elif evt == self.lcd.evt.FAN:
            self._event_tx_log("printer.set_fan", data)
            self.printer.set_fan(data)
        elif evt == self.lcd.evt.MOTOR_OFF:
            self._event_tx_log("printer.sendGCode", "M18")
            self.printer.sendGCode("M18")
        elif evt == self.lcd.evt.ACCEL:
            self._event_tx_log("printer.sendGCode", "SET_VELOCITY_LIMIT ACCEL=%d" % data)
            self.printer.sendGCode("SET_VELOCITY_LIMIT ACCEL=%d" % data)
        elif evt == self.lcd.evt.ACCEL_TO_DECEL:
            # Modern Klipper uses minimum_cruise_ratio (0..1), UI provides percent.
            self._event_tx_log(
                "printer.sendGCode",
                "SET_VELOCITY_LIMIT MINIMUM_CRUISE_RATIO=%.2f" % (data / 100.0),
            )
            self.printer.sendGCode(
                "SET_VELOCITY_LIMIT MINIMUM_CRUISE_RATIO=%.2f" % (data / 100.0)
            )
        elif evt == self.lcd.evt.VELOCITY:
            self._event_tx_log("printer.sendGCode", "SET_VELOCITY_LIMIT VELOCITY=%d" % data)
            self.printer.sendGCode("SET_VELOCITY_LIMIT VELOCITY=%d" % data)
        elif evt == self.lcd.evt.SQUARE_CORNER_VELOCITY:
            _log(
                "SET_VELOCITY_LIMIT SQUARE_CORNER_VELOCITY=%.1f" % data,
                level=logging.DEBUG,
            )
            self._event_tx_log(
                "printer.sendGCode",
                "SET_VELOCITY_LIMIT SQUARE_CORNER_VELOCITY=%.1f" % data,
            )
            self.printer.sendGCode(
                "SET_VELOCITY_LIMIT SQUARE_CORNER_VELOCITY=%.1f" % data
            )
        elif evt == self.lcd.evt.CONSOLE:
            self._event_tx_log("printer.sendGCode", data)
            self.printer.sendGCode(data)
        else:
            _log("lcd_callback event not recognised %d" % evt, level=logging.WARNING)


def run():
    setup_logging()
    x = KlipperLCD()
    x.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        _log("KeyboardInterrupt received, shutting down")
    finally:
        x.stop()
