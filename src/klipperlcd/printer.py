"""Printer transport/state layer for Moonraker + Klippy socket APIs.

This module keeps local printer state mirrored from Klipper/Moonraker,
and exposes high-level control methods used by the LCD UI layer.
"""

import errno
import asyncio
import atexit
import json
import logging
import binascii
import select
import socket
import threading
import time

import requests
from json import JSONDecodeError
from requests.exceptions import ConnectionError, ReadTimeout, RequestException

logger = logging.getLogger(__name__)


def _log(*args, level=logging.INFO):
    logger.log(level, " ".join(str(arg) for arg in args))


def _hex_preview(data, limit=128):
    raw = bytes(data)
    hexed = binascii.hexlify(raw).decode()
    if len(raw) > limit:
        return "%s...<%d bytes>" % (hexed[: limit * 2], len(raw))
    return "%s<%d bytes>" % (hexed, len(raw))


# NOTE: This file intentionally preserves legacy naming and field layout because
# multiple modules rely on these exact attribute names.
class xyze_t:
    x = 0.0
    y = 0.0
    z = 0.0
    e = 0.0
    home_x = False
    home_y = False
    home_z = False
    updated = False

class AxisEnum:
    X_AXIS = 0
    A_AXIS = 0
    Y_AXIS = 1
    B_AXIS = 1
    Z_AXIS = 2
    C_AXIS = 2
    E_AXIS = 3
    X_HEAD = 4
    Y_HEAD = 5
    Z_HEAD = 6
    E0_AXIS = 3
    E1_AXIS = 4
    E2_AXIS = 5
    E3_AXIS = 6
    E4_AXIS = 7
    E5_AXIS = 8
    E6_AXIS = 9
    E7_AXIS = 10
    ALL_AXES = 0xFE
    NO_AXIS = 0xFF

class HMI_value_t:
    E_Temp = 0
    Bed_Temp = 0
    Fan_speed = 0
    print_speed = 100
    Max_Feedspeed = 0.0
    Max_Acceleration = 0.0
    Max_Jerk = 0.0
    Max_Step = 0.0
    Move_X_scale = 0.0
    Move_Y_scale = 0.0
    Move_Z_scale = 0.0
    Move_E_scale = 0.0
    offset_value = 0.0
    show_mode = 0  # -1: Temperature control    0: Printing temperature

class HMI_Flag_t:
    language = 0
    pause_flag = False
    pause_action = False
    print_finish = False
    done_confirm_flag = False
    select_flag = False
    home_flag = False
    heat_flag = False  # 0: heating done  1: during heating
    ETempTooLow_flag = False
    leveling_offset_flag = False
    feedspeed_axis = AxisEnum()
    acc_axis = AxisEnum()
    jerk_axis = AxisEnum()
    step_axis = AxisEnum()

class buzz_t:
    def tone(self, t, n):
        pass

class material_preset_t:
    def __init__(self, name, hotend_temp, bed_temp, fan_speed=100):
        self.name = name
        self.hotend_temp = hotend_temp
        self.bed_temp = bed_temp
        self.fan_speed = fan_speed


class KlippySocket:
    """Non-blocking Unix-socket client for Klippy's JSON-RPC stream."""

    def __init__(self, uds_filename, callback=None):
        self.connected = False
        self.webhook_socket_create(uds_filename)
        self.lock = threading.Lock()
        self.poll = select.poll()
        self.stop_threads = False
        self.poll.register(self.webhook_socket, select.POLLIN | select.POLLHUP)
        # Wakeup pair lets queue_line interrupt poll() so queued commands are
        # sent immediately instead of waiting for the next poll timeout.
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(0)
        self._wake_w.setblocking(0)
        self.poll.register(self._wake_r, select.POLLIN)
        self.socket_data = ""
        self.callback = callback
        self.lines = []
        self._closed = False
        self._socket_trace = True
        # All attributes used by the polling thread must be set before start().
        self.t = threading.Thread(target=self.polling, daemon=True)
        self.t.start()
        atexit.register(self.klippyExit)

    def klippyExit(self):
        if self._closed:
            return
        self._closed = True
        _log("Shuting down Klippy Socket", level=logging.INFO)
        self.stop_threads = True
        try:
            self.poll.unregister(self.webhook_socket)
        except Exception:
            pass
        try:
            self.webhook_socket.close()
        except Exception:
            pass
        try:
            self.poll.unregister(self._wake_r)
        except Exception:
            pass
        for sock in (getattr(self, "_wake_r", None), getattr(self, "_wake_w", None)):
            try:
                if sock is not None:
                    sock.close()
            except Exception:
                pass
        if self.t.is_alive():
            self.t.join(timeout=2.0)

    def webhook_socket_create(self, uds_filename):
        # Keep retrying until the Klippy socket is ready.
        self.webhook_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.webhook_socket.setblocking(0)
        _log("Waiting for connect to %s" % (uds_filename,), level=logging.INFO)
        wait_logged = False
        while 1:
            try:
                self.webhook_socket.connect(uds_filename)
            except socket.error as e:
                if e.errno in (errno.ECONNREFUSED, errno.ENOENT):
                    if not wait_logged:
                        _log(
                            "Klippy socket not ready (%s). Waiting for %s"
                            % (e, uds_filename),
                            level=logging.WARNING,
                        )
                        wait_logged = True
                    time.sleep(0.1)
                    continue
                if not wait_logged:
                    _log(
                        "Unable to connect socket %s [%d,%s]\n" % (
                            uds_filename, e.errno,
                            errno.errorcode[e.errno]
                        ),
                        level=logging.WARNING,
                    )
                    wait_logged = True
                time.sleep(1)
                continue
            break
        if wait_logged:
            _log("Klippy socket is available again: %s" % uds_filename, level=logging.INFO)
        _log("Connection.", level=logging.INFO)
        self.connected = True

    def process_socket(self):
        if self.stop_threads:
            return
        data = None
        try:
            raw = self.webhook_socket.recv(4096)
            data = raw.decode(errors="replace")
            if self._socket_trace and raw:
                logger.debug("KLIPPY RX raw=%s", _hex_preview(raw))
        except socket.error as e:
            if self.stop_threads:
                return
            # Expected on non-blocking socket when no data is available.
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return
            if e.errno == errno.EBADF:
                # Socket can already be closed during graceful shutdown.
                return
            self.connected = False
            _log("Socket read error: %s" % e, level=logging.ERROR)
            return
        if not data:
            self.connected = False
            # Unregister so poll() does not spin on a dead socket until the
            # main loop notices the disconnect and rebuilds the connection.
            try:
                self.poll.unregister(self.webhook_socket)
            except Exception:
                pass
            _log("Socket closed", level=logging.WARNING)
            return
        parts = data.split('\x03')
        parts[0] = self.socket_data + parts[0]
        self.socket_data = parts.pop()
        for line in parts:
            if self._socket_trace:
                logger.debug("KLIPPY RX line=%r", line)
            if self.callback:
                self.callback(line)

    def queue_line(self, line):
        # Called by other threads; queue is protected by lock.
        with self.lock:
            self.lines.append(line)
            if self._socket_trace:
                logger.debug("KLIPPY TX queue size=%d line=%r", len(self.lines), line)
        try:
            self._wake_w.send(b"\x00")
        except (OSError, socket.error):
            pass

    def send_line(self):
        # Drain the whole queue; stop early on transport failure.
        while self.lines:
            line = self.lines.pop(0).strip()
            if not line or line.startswith('#'):
                continue
            try:
                m = json.loads(line)
            except JSONDecodeError:
                _log("ERROR: Unable to parse line", level=logging.ERROR)
                continue
            cm = json.dumps(m, separators=(',', ':'))
            wdm = '{}\x03'.format(cm)
            try:
                self.webhook_socket.send(wdm.encode())
                if self._socket_trace:
                    logger.debug("KLIPPY TX raw=%s", _hex_preview(wdm.encode()))
            except socket.error as e:
                self.connected = False
                _log("Socket send error: %s" % e, level=logging.ERROR)
                return

    def polling(self):
        wake_fd = self._wake_r.fileno()
        while True:
            if self.stop_threads:
                break
            res = self.poll.poll(1000.)
            if self.stop_threads:
                break
            for fd, event in res:
                if fd == wake_fd:
                    try:
                        self._wake_r.recv(4096)
                    except (OSError, socket.error):
                        pass
                else:
                    self.process_socket()
            with self.lock:
                self.send_line()


class MoonrakerSocket:
    """HTTP session wrapper for Moonraker REST calls."""

    def __init__(self, address, port, api_key):
        self.s = requests.Session()
        self.s.headers.update({
            'X-Api-Key': api_key,
            'Content-Type': 'application/json'
        })
        self.base_address = 'http://' + address + ':' + str(port)
        self.timeout = 8


class PrinterData:
    """State cache + control facade used by the LCD controller."""

    event_loop = None
    HAS_HOTEND = True
    HOTENDS = 1
    HAS_HEATED_BED = True
    HAS_FAN = False
    HAS_ZOFFSET_ITEM = True
    HAS_ONESTEP_LEVELING = False
    HAS_PREHEAT = True
    HAS_BED_PROBE = False
    PREVENT_COLD_EXTRUSION = True
    EXTRUDE_MINTEMP = 170
    EXTRUDE_MAXLENGTH = 200

    HEATER_0_MAXTEMP = 275
    HEATER_0_MINTEMP = 5
    HOTEND_OVERSHOOT = 15

    MAX_E_TEMP = (HEATER_0_MAXTEMP - (HOTEND_OVERSHOOT))
    MIN_E_TEMP = HEATER_0_MINTEMP

    BED_OVERSHOOT = 10
    BED_MAXTEMP = 150
    BED_MINTEMP = 5

    BED_MAX_TARGET = (BED_MAXTEMP - (BED_OVERSHOOT))
    MIN_BED_TEMP = BED_MINTEMP

    X_MIN_POS = 0.0
    Y_MIN_POS = 0.0
    Z_MIN_POS = 0.0
    Z_MAX_POS = 200

    Z_PROBE_OFFSET_RANGE_MIN = -20
    Z_PROBE_OFFSET_RANGE_MAX = 20

    buzzer = buzz_t()

    material_preset = [
        material_preset_t('PLA', 200, 60),
        material_preset_t('ABS', 210, 100)
    ]
    files = None
    MACHINE_SIZE = "220x220x250"
    SHORT_BUILD_VERSION = "1.00"
    CORP_WEBSITE_E = "https://www.klipper3d.org/"

    def __init__(self, API_Key, URL='127.0.0.1', port=80, klippy_sock='/home/pi/printer_data/comms/klippy.sock', callback=None):
        # Runtime comms + state mirrors.
        self.response_callback = callback
        self.klippy_sock      = klippy_sock
        self.BABY_Z_VAR       = 0
        self.print_speed      = 100
        self.flow_percentage  = 100
        self.led_percentage   = 0
        self.temphot          = 0
        self.tempbed          = 0
        self.HMI_ValueStruct  = HMI_value_t()
        self.HMI_flag         = HMI_Flag_t()
        self.current_position = xyze_t()
        self.gcm              = None
        self.z_offset         = 0
        self.thermalManager   = {
            'temp_bed': {'celsius': 20, 'target': 120},
            'temp_hotend': [{'celsius': 20, 'target': 120}],
            'fan_speed': [100]
        }
        self.job_Info               = None
        self.file_path              = None
        self.file_name              = None
        self.status                 = None
        self.max_velocity           = None
        self.max_accel              = None
        # Stored as percent (0..100) mapped from toolhead.minimum_cruise_ratio (0.0..1.0)
        self.max_accel_to_decel     = None
        self.square_corner_velocity = None
        
        self.op = MoonrakerSocket(URL, port, API_Key)
        _log("Moonraker address: %s" % self.op.base_address, level=logging.INFO)

        # Do not subscribe while Klippy is still starting: subscriptions sent
        # before the ready state are rejected and silently lost.
        wait_logged = False
        while not self.klippy_ready():
            if not wait_logged:
                _log("Waiting for Klippy to become ready", level=logging.INFO)
                wait_logged = True
            time.sleep(1)
        if wait_logged:
            _log("Klippy is ready", level=logging.INFO)
        self.klippy_start()

        self.event_loop = asyncio.new_event_loop()
        threading.Thread(target=self.event_loop.run_forever, daemon=True).start()

    def stop(self):
        if hasattr(self, "ks") and self.ks:
            self.ks.klippyExit()
        if self.event_loop and self.event_loop.is_running():
            self.event_loop.call_soon_threadsafe(self.event_loop.stop)

    # ------------- Klippy socket integration ----------
    def klippy_start(self):
        self.ks = KlippySocket(self.klippy_sock, callback=self.klippy_callback)
        subscribe = {
            "id": 4001,
            "method": "objects/subscribe",
            "params": {
                "objects": {
                    "toolhead": [
                        "position"
                    ]
                },
                "response_template": {}
            }
        }
        self.klippy_z_offset = '{"id": 4002, "method": "objects/query", "params": {"objects": {"configfile": ["config"]}}}'
        self.klippy_home = '{"id": 4003, "method": "objects/query", "params": {"objects": {"toolhead": ["homed_axes"]}}}'
        self.gcode = '{"id": 4004, "method": "gcode/subscribe_output", "params": {"response_template":{}}}'

        self.ks.queue_line(json.dumps(subscribe))
        self.ks.queue_line(self.klippy_z_offset)
        self.ks.queue_line(self.klippy_home)
        self.ks.queue_line(self.gcode)

    def klippy_callback(self, line):
        # Parse async subscription payloads and update local cache.
        try:
            klippyData = json.loads(line)
        except JSONDecodeError:
            _log("klippy_callback: failed to decode JSON line", level=logging.WARNING)
            logger.debug("klippy_callback raw line=%r", line)
            return
        status = None
        if 'result' in klippyData:
            if 'status' in klippyData['result']:
                status = klippyData['result']['status']
        if 'params' in klippyData:
            if 'status' in klippyData['params']:
                status = klippyData['params']['status']
            if 'response' in klippyData['params']:
                if self.response_callback:
                    resp = klippyData['params']['response']
                    if 'B:' in resp and 'T0:' in resp:
                        pass ## Filter out temperature responses
                    else:
                        self.response_callback(resp, 'response')

        if status:
            if 'toolhead' in status:
                if 'position' in status['toolhead']:
                    if self.current_position.x != status['toolhead']['position'][0]:
                        self.current_position.x = status['toolhead']['position'][0]
                        self.current_position.updated = True
                    if self.current_position.y != status['toolhead']['position'][1]:
                        self.current_position.y = status['toolhead']['position'][1]
                        self.current_position.updated = True
                    if self.current_position.z != status['toolhead']['position'][2]:
                        self.current_position.z = status['toolhead']['position'][2]
                        self.current_position.updated = True
                    if self.current_position.e != status['toolhead']['position'][3]:
                        self.current_position.e = status['toolhead']['position'][3]
                        self.current_position.updated = True
                    
                if 'homed_axes' in status['toolhead']:
                    if 'x' in status['toolhead']['homed_axes']:
                        self.current_position.home_x = True
                    else:
                        self.current_position.home_x = False
                    if 'y' in status['toolhead']['homed_axes']:
                        self.current_position.home_y = True
                    else:
                        self.current_position.home_y = False
                    if 'z' in status['toolhead']['homed_axes']:
                        self.current_position.home_z = True
                    else:
                        self.current_position.home_z = False
                
                if 'max_velocity' in status['toolhead']:
                    if self.max_velocity != status['toolhead']['max_velocity']:
                        self.max_velocity = status['toolhead']['max_velocity']
                if 'max_accel' in status['toolhead']:
                    if self.max_accel != status['toolhead']['max_accel']:
                        self.max_accel = status['toolhead']['max_accel']
                if 'minimum_cruise_ratio' in status['toolhead']:
                    min_cruise_ratio = int(status['toolhead']['minimum_cruise_ratio'] * 100 + 0.5)
                    if self.max_accel_to_decel != min_cruise_ratio:
                        self.max_accel_to_decel = min_cruise_ratio
                if 'square_corner_velocity' in status['toolhead']:
                    if self.square_corner_velocity != status['toolhead']['square_corner_velocity']:
                        self.square_corner_velocity = status['toolhead']['square_corner_velocity']

            if 'configfile' in status:
                if 'config' in status['configfile']:
                    if 'bltouch' in status['configfile']['config']:
                        if 'z_offset' in status['configfile']['config']['bltouch']:
                            if status['configfile']['config']['bltouch']['z_offset']:
                                self.BABY_Z_VAR = float(status['configfile']['config']['bltouch']['z_offset'])
                    if 'virtual_sdcard' in status['configfile']['config']:
                        if 'path' in status['configfile']['config']['virtual_sdcard']:
                            self.file_path = status['configfile']['config']['virtual_sdcard']['path']

    def ishomed(self):
        if self.current_position.home_x and self.current_position.home_y and self.current_position.home_z:
            return True
        else:
            self.ks.queue_line(self.klippy_home)
            return False

    def offset_z(self, new_offset):
        self.BABY_Z_VAR = new_offset
        self.sendGCode('ACCEPT')

    def add_mm(self, axs, new_offset):
        gc = 'TESTZ Z={}'.format(new_offset)
        _log(axs, gc, level=logging.DEBUG)
        self.sendGCode(gc)

    def probe_adjust(self, change):
        gc = 'TESTZ Z={}'.format(change)
        _log(gc, level=logging.DEBUG)
        self.sendGCode(gc)

    def probe_calibrate(self):
        if self.ishomed() == False:
            self.sendGCode('G28')
        self.sendGCode('PROBE_CALIBRATE')
        self.sendGCode('G1 Z0.0')

    # ------------- Moonraker REST integration ----------

    def getREST(self, path):
        # Thin helper: keep error handling centralized in this module.
        url = self.op.base_address + path
        try:
            r = self.op.s.get(url, timeout=self.op.timeout)
            r.raise_for_status()
        except RequestException as e:
            _log("REST GET failed: %s (%s)" % (url, e), level=logging.ERROR)
            return None
        d = r.content.decode('utf-8', errors='replace')
        logger.debug("REST GET ok: path=%s bytes=%d", path, len(d))
        try:
            return json.loads(d)
        except JSONDecodeError:
            _log('Decoding JSON has failed', level=logging.ERROR)
            logger.debug("REST GET non-JSON payload path=%s payload=%r", path, d[:256])
        return None

    async def _postREST(self, path, json):
        url = self.op.base_address + path
        try:
            r = self.op.s.post(url, json=json, timeout=self.op.timeout)
            r.raise_for_status()
            logger.debug("REST POST ok: path=%s status=%s", path, r.status_code)
        except ReadTimeout:
            # gcode/script POSTs block until the script completes; a client
            # timeout on a long-running macro is expected, Moonraker keeps
            # executing the script server-side.
            _log(
                "REST POST client timeout (server keeps running): %s payload=%r"
                % (url, json),
                level=logging.WARNING,
            )
        except RequestException as e:
            _log(
                "REST POST failed: %s payload=%r (%s)" % (url, json, e),
                level=logging.ERROR,
            )

    def postREST(self, path, json):
        logger.debug("Sending REST command: path=%s payload=%r", path, json)
        self.event_loop.call_soon_threadsafe(asyncio.create_task,self._postREST(path,json))

    def init_Webservices(self):
        # Initialize runtime metadata and printer limits.
        try:
            self.op.s.get(self.op.base_address, timeout=self.op.timeout).raise_for_status()
        except (ConnectionError, RequestException):
            _log('Web site does not exist', level=logging.ERROR)
            return
        else:
            _log('Web site exists', level=logging.INFO)
        api_printer = self.getREST('/api/printer')
        if api_printer is None:
            return
        self.update_variable()

        update_status = self.getREST('/machine/update/status?refresh=false')
        if update_status and 'result' in update_status:
            self.SHORT_BUILD_VERSION = update_status['result']['version_info']['klipper']['version']
        else:
            info = self.getREST('/printer/info')
            if info and 'result' in info and 'software_version' in info['result']:
                full_version = info['result']['software_version']
                self.SHORT_BUILD_VERSION = '-'.join(full_version.split('-', 2)[:2])

        data_resp = self.getREST('/printer/objects/query?toolhead')
        if not data_resp or 'result' not in data_resp:
            return
        data = data_resp['result']['status']
        toolhead = data['toolhead']
        volume = toolhead['axis_maximum'] #[x,y,z,w]
        self.MACHINE_SIZE = "{}x{}x{}".format(
            int(volume[0]),
            int(volume[1]),
            int(volume[2])
        )
        self.X_MAX_POS = int(volume[0])
        self.Y_MAX_POS = int(volume[1])
        self.max_velocity           = toolhead['max_velocity']
        self.max_accel              = toolhead['max_accel']
        self.max_accel_to_decel     = int(toolhead['minimum_cruise_ratio'] * 100 + 0.5)
        self.square_corner_velocity = toolhead['square_corner_velocity']

    def get_gcode_store(self, count=100):
        gcode_store = None
        try:
            resp = self.getREST('/server/gcode_store?count=%d' % count)
            if resp and 'result' in resp:
                gcode_store = resp['result']['gcode_store']
        except Exception:
            _log("GCode store read failed!", level=logging.ERROR)
        
        return gcode_store
    
    def get_macros(self, filter_internal = True):
        macros = []
        objects = []
        try:
            resp = self.getREST('/printer/objects/list')
            if resp and 'result' in resp:
                objects = resp['result']['objects']
        except Exception:
            _log("Could not read macro objects!", level=logging.ERROR)
        
        for obj in objects:
            if 'gcode_macro' in obj:
                macro = obj.split(' ')[1]
                if filter_internal:
                    if macro[0] != '_':
                        macros.append(macro)
                else:
                    macros.append(macro)
        return macros    

    def get_filament_sensor_enabled(self, sensor_name):
        """Return Klipper filament sensor 'enabled' state, or None if unknown."""
        obj = 'filament_switch_sensor %s' % sensor_name
        resp = self.getREST('/printer/objects/query?' + obj.replace(' ', '%20'))
        try:
            return bool(resp['result']['status'][obj]['enabled'])
        except (TypeError, KeyError):
            _log(
                "Filament sensor state query failed for %s" % sensor_name,
                level=logging.WARNING,
            )
            return None

    def GetFiles(self, refresh=False):
        if not self.files or refresh:
            try:
                resp = self.getREST('/server/files/list')
                if resp and "result" in resp:
                    files = resp["result"]
                    # Newest first: "modified" is the upload time for gcode
                    # files, so the latest file lands on page 1, slot 1.
                    files.sort(key=lambda fl: fl.get("modified", 0), reverse=True)
                    self.files = files
            except Exception:
                _log("Exception 418", level=logging.ERROR)
        if not self.files:
            return []
        names = []
        for fl in self.files:
            names.append(fl["path"])
        return names

    def klippy_ready(self):
        """Check via Moonraker whether Klippy reached the ready state."""
        info = self.getREST('/printer/info')
        try:
            return info['result']['state'] == 'ready'
        except (TypeError, KeyError):
            return False

    def update_variable(self):
        # Periodic full-state refresh used by the main update loop.
        if self.ks.connected == False:
            # Reconnect only once Klippy is ready: subscriptions sent while
            # Klippy is still starting (e.g. right after SAVE_CONFIG restart)
            # are rejected and would be silently lost.
            if not self.klippy_ready():
                logger.debug("Klippy not ready yet, delaying socket reconnect")
                return False
            _log("Klippy ready, reconnecting socket and resubscribing", level=logging.INFO)
            self.ks.klippyExit()
            self.klippy_start()
            return False
        # Single combined query: virtual_sdcard/print_stats are fetched together
        # with the rest of the state to avoid a second HTTP round-trip per cycle.
        query = '/printer/objects/query?extruder&heater_bed&gcode_move&fan&print_stats&virtual_sdcard&toolhead'
        try:
            resp = self.getREST(query)
            if not resp or 'result' not in resp:
                return False
            data = resp['result']['status']
            self.gcm = data['gcode_move']
            self.z_offset = self.gcm['homing_origin'][2] #z offset
            self.flow_percentage = self.gcm['extrude_factor'] * 100 #flow rate percent
            self.absolute_moves = self.gcm['absolute_coordinates'] #absolute or relative
            self.absolute_extrude = self.gcm['absolute_extrude'] #absolute or relative
            self.speed = self.gcm['speed'] #current speed in mm/s
            self.print_speed = self.gcm['speed_factor'] * 100 #print speed percent
            self.bed = data['heater_bed'] #temperature, target
            self.extruder = data['extruder'] #temperature, target
            self.fan = data['fan']
            self.toolhead = data['toolhead']
        except Exception:
            _log("Exception 431", level=logging.ERROR)
            return False
        Update = False
        try:
            if self.thermalManager['temp_bed']['celsius'] != int(self.bed['temperature']):
                self.thermalManager['temp_bed']['celsius'] = int(self.bed['temperature'])
                Update = True
            if self.thermalManager['temp_bed']['target'] != int(self.bed['target']):
                self.thermalManager['temp_bed']['target'] = int(self.bed['target'])
                Update = True
            if self.thermalManager['temp_hotend'][0]['celsius'] != int(self.extruder['temperature']):
                self.thermalManager['temp_hotend'][0]['celsius'] = int(self.extruder['temperature'])
                Update = True
            if self.thermalManager['temp_hotend'][0]['target'] != int(self.extruder['target']):
                self.thermalManager['temp_hotend'][0]['target'] = int(self.extruder['target'])
                Update = True
            if self.thermalManager['fan_speed'][0] != int((self.fan['speed'] * 100) + 0.5):
                self.thermalManager['fan_speed'][0] = int((self.fan['speed'] * 100) + 0.5)
                Update = True
            if self.BABY_Z_VAR != self.z_offset:
                self.BABY_Z_VAR = self.z_offset
                self.HMI_ValueStruct.offset_value = self.z_offset * 100
                Update = True
            
            if self.max_velocity != self.toolhead['max_velocity']:
                self.max_velocity = self.toolhead['max_velocity']
                Update = True
            if self.max_accel != self.toolhead['max_accel']:
                self.max_accel = self.toolhead['max_accel']
                Update = True
            min_cruise_ratio = int(self.toolhead['minimum_cruise_ratio'] * 100 + 0.5)
            if self.max_accel_to_decel != min_cruise_ratio:
                self.max_accel_to_decel = min_cruise_ratio
                Update = True
            if self.square_corner_velocity != self.toolhead['square_corner_velocity']:
                self.square_corner_velocity = self.toolhead['square_corner_velocity']
                Update = True
        except:
            # Missing keys can happen transiently while Klipper is starting up.
            pass
        if 'virtual_sdcard' in data and 'print_stats' in data:
            self.job_Info = {
                'virtual_sdcard': data['virtual_sdcard'],
                'print_stats': data['print_stats'],
            }
        else:
            _log("Job info missing in status payload", level=logging.WARNING)
            return False

        if self.job_Info:
            self.file_name = self.job_Info['print_stats']['filename']
            self.status = self.job_Info['print_stats']['state']
            self.HMI_flag.print_finish = self.getPercent() == 100.0
        return Update

    def getState(self):
        if self.job_Info:
            return self.job_Info['print_stats']['state']
        else:
            return None

    def printingIsPaused(self):
        if self.job_Info:
            return self.job_Info['print_stats']['state'] == "paused" or self.job_Info['print_stats']['state'] == "pausing"
        else:
            return None

    def getPercent(self):
        if self.job_Info:
            if self.job_Info['virtual_sdcard']['is_active']:
                return self.job_Info['virtual_sdcard']['progress'] * 100
        return 0

    def duration(self):
        if self.job_Info:
            if self.job_Info['virtual_sdcard']['is_active']:
                return self.job_Info['print_stats']['print_duration']
        return 0

    def remain(self):
        percent = self.getPercent()
        duration = self.duration()
        if percent:
            total = duration / (percent / 100)
            return total - duration
        return 0

    def openAndPrintFile(self, filenum):
        self.file_name = self.files[filenum]['path']
        self.postREST('/printer/print/start', json={'filename': self.file_name})

    def cancel_job(self): #fixed
        _log('Canceling job:', level=logging.INFO)
        self.postREST('/printer/print/cancel', json=None)

    def pause_job(self): #fixed
        _log('Pausing job:', level=logging.INFO)
        self.postREST('/printer/print/pause', json=None)

    def resume_job(self): #fixed
        _log('Resuming job:', level=logging.INFO)
        self.postREST('/printer/print/resume', json=None)

    def set_print_speed(self, fr):
        self.print_speed = fr
        self.sendGCode('M220 S%d' % fr)

    def set_flow(self, fl):
        self.flow_percentage = fl
        self.sendGCode('M221 S%d' % fl)

    def set_led(self, led):
        # led is brightness in percent (0..100).
        self.led_percentage = led
        brightness = max(0.0, min(led / 100.0, 1.0))
        self.sendGCode('SET_LED LED=top_LEDs WHITE=%.2f SYNC=0 TRANSMIT=1' % brightness)

    def set_fan(self, fan):
        self.fan_percentage = fan
        self.sendGCode('M106 S%s' % (int)(fan*255/100))

    def home(self, axis): #fixed using gcode
        GCode = 'G28 '
        if axis == 'X' or axis == 'Y' or axis == 'Z' or axis == 'X Y Z':
            GCode += axis
        else:
            _log("home: parameter not recognised " + axis, level=logging.WARNING)
            return

        self.sendGCode(GCode)

    def moveRelative(self, axis, distance, speed):
        # Temporarily switch to relative mode for this move, then restore.
        self.sendGCode('%s \n%s %s%s F%s%s' % ('G91', 'G1', axis, distance, speed,
            '\nG90' if self.absolute_moves else ''))

    def moveAbsolute(self, axis, position, speed):
        # Temporarily switch to absolute mode for this move, then restore.
        self.sendGCode('%s \n%s %s%s F%s%s' % ('G90', 'G1', axis, position, speed,
            '\nG91' if not self.absolute_moves else ''))

    def sendGCode(self, gcode):
        logger.debug("Sending GCode: %s", gcode)
        self.postREST('/printer/gcode/script', json={'script': gcode})
        if self.response_callback:
            self.response_callback(gcode, 'command')

    def disable_all_heaters(self):
        self.setExtTemp(0)
        self.setBedTemp(0)

    def zero_fan_speeds(self):
        pass

    def preheat(self, profile):
        if profile == "PLA":
            self.preHeat(self.material_preset[0].bed_temp, self.material_preset[0].hotend_temp)
        elif profile == "ABS":
            self.preHeat(self.material_preset[1].bed_temp, self.material_preset[1].hotend_temp)

    def save_settings(self):
        _log('saving settings', level=logging.INFO)
        return True

    def setExtTemp(self, target, toolnum=0):
        self.sendGCode('M104 T%s S%s' % (toolnum, target))

    def setBedTemp(self, target):
        self.sendGCode('M140 S%s' % target)

    def preHeat(self, bedtemp, exttemp, toolnum=0):
        self.setBedTemp(bedtemp)
        self.setExtTemp(exttemp)

    def setZOffset(self, offset):
        self.sendGCode('SET_GCODE_OFFSET Z=%s MOVE=1' % offset)
