"""Standalone HMI interaction trace channel.

When ``KLIPPERLCD_HMI_TRACE`` is enabled, every input frame received from
the LCD and every command the service issues while handling it (LCD
writes, dispatched app events, Moonraker REST calls) is appended to a
dedicated trace file, independent of ``KLIPPERLCD_LOG_LEVEL``. The file
stays free of periodic status traffic, so it maps screen elements to the
exact command flow they trigger — intended for reverse-engineering HMI
behavior on the device.
"""

import logging
import os
import threading

TRACE_LOGGER_NAME = "klipperlcd.hmi_trace"
DEFAULT_TRACE_FILE = "~/printer_data/logs/KlipperLCD_hmi_trace.log"

_TRUE_VALUES = ("1", "true", "yes", "on")

logger = logging.getLogger(TRACE_LOGGER_NAME)
# Status messages about the trace channel go to the regular app log.
_status_log = logging.getLogger(__name__ + "_status")

_tls = threading.local()
_enabled = False


def setup():
    """Enable the trace channel from environment. Returns True when active."""
    global _enabled
    raw = os.getenv("KLIPPERLCD_HMI_TRACE", "0").strip().lower()
    _enabled = raw in _TRUE_VALUES
    if not _enabled:
        return False

    path = os.path.expanduser(
        os.getenv("KLIPPERLCD_HMI_TRACE_FILE", DEFAULT_TRACE_FILE)
    )
    logger.setLevel(logging.INFO)
    # Re-running setup (service restart within one process, tests) must not
    # stack duplicate handlers.
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    try:
        handler = logging.FileHandler(path)
    except OSError as e:
        _status_log.warning(
            "HMI trace file %s is not writable (%s); trace goes to main log",
            path,
            e,
        )
        logger.propagate = True
        return True
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    _status_log.info("HMI trace enabled: %s", path)
    logger.info("=== HMI trace started (pid=%d) ===", os.getpid())
    return True


def enabled():
    return _enabled


def current_label():
    """Element label of the HMI interaction being handled in this thread."""
    return getattr(_tls, "label", None)


class _Interaction:
    def __init__(self, label):
        self._label = label

    def __enter__(self):
        _tls.label = self._label
        return self

    def __exit__(self, exc_type, exc, tb):
        _tls.label = None
        return False


def interaction(label):
    """Context manager tagging commands issued while handling one HMI input.

    Pass ``None`` to handle an input without tagging (periodic HMI polls
    whose command flow would only flood the trace).
    """
    return _Interaction(label)


def trace(kind, message):
    """Write one trace line, tagged with the active interaction label."""
    if not _enabled:
        return
    label = current_label()
    if label:
        logger.info("%-5s [%s] %s", kind, label, message)
    else:
        logger.info("%-5s %s", kind, message)
