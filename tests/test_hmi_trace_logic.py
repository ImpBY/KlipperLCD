import logging

import pytest

from klipperlcd import hmi_trace
from klipperlcd.lcd import LCD


class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@pytest.fixture
def capture_trace(monkeypatch):
    handler = _CaptureHandler()
    monkeypatch.setattr(hmi_trace, "_enabled", True)
    hmi_trace.logger.setLevel(logging.INFO)
    hmi_trace.logger.addHandler(handler)
    hmi_trace.logger.propagate = False
    yield handler.lines
    hmi_trace.logger.removeHandler(handler)


def test_setup_disabled_by_default(monkeypatch):
    monkeypatch.delenv("KLIPPERLCD_HMI_TRACE", raising=False)
    assert hmi_trace.setup() is False
    assert hmi_trace.enabled() is False


def test_setup_enabled_writes_to_file(monkeypatch, tmp_path):
    trace_file = tmp_path / "hmi_trace.log"
    monkeypatch.setenv("KLIPPERLCD_HMI_TRACE", "1")
    monkeypatch.setenv("KLIPPERLCD_HMI_TRACE_FILE", str(trace_file))

    assert hmi_trace.setup() is True
    assert hmi_trace.enabled() is True
    hmi_trace.trace("RX", "test line")

    content = trace_file.read_text()
    assert "HMI trace started" in content
    assert "RX    test line" in content

    # Teardown: detach the file handler and restore disabled state.
    monkeypatch.setenv("KLIPPERLCD_HMI_TRACE", "0")
    for handler in list(hmi_trace.logger.handlers):
        hmi_trace.logger.removeHandler(handler)
        handler.close()
    hmi_trace.setup()


def test_trace_tags_lines_with_interaction_label(capture_trace):
    hmi_trace.trace("RX", "untagged")
    with hmi_trace.interaction("settings.fan_toggle"):
        hmi_trace.trace("TX", "'page main'")
    hmi_trace.trace("TX", "after")

    assert capture_trace == [
        "RX    untagged",
        "TX    [settings.fan_toggle] 'page main'",
        "TX    after",
    ]
    assert hmi_trace.current_label() is None


def test_handle_readvar_traces_input_and_commands(capture_trace):
    events = []
    lcd = LCD(callback=lambda evt, data=None: events.append((evt, data)))

    lcd._handle_readvar(0x103E, [0x08])

    tagged = [l for l in capture_trace if "[settings.filament_sensor_toggle]" in l]
    assert any(l.startswith("RX ") for l in capture_trace)
    assert "element=settings.filament_sensor_toggle" in capture_trace[0]
    # The dispatched event is traced within the interaction context.
    assert any(l.startswith("EVENT") for l in tagged)
    assert events == [(lcd.evt.FILAMENT_SENSOR, None)]


def test_handle_readvar_quiet_poll_not_traced(capture_trace):
    lcd = LCD(callback=lambda evt, data=None: None)
    seen_labels = []
    lcd.addr_func_map[0x1044] = lambda data: seen_labels.append(
        hmi_trace.current_label()
    )

    lcd._handle_readvar(0x1044, [0x0A])

    # The poll is handled, but without a trace label: neither the input
    # nor any command issued while handling it lands in the trace.
    assert seen_labels == [None]
    assert capture_trace == []


def test_handle_readvar_unknown_addr_traced(capture_trace):
    lcd = LCD(callback=lambda evt, data=None: None)

    lcd._handle_readvar(0x9999, [0x01])

    assert capture_trace == ["RX?   addr=0x9999 not mapped data=[1]"]
