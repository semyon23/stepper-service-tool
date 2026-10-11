"""E-STOP: команда EMERGENCY_STOP (7) и запасной HALT для прошивки без неё."""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import operations as ops  # noqa: E402
from board_emulator import BoardEmulator, serial_factory  # noqa: E402
from board_protocol import (  # noqa: E402
    AXIS_GROUP, CMD_EMERGENCY_STOP, CMD_HALT, CMD_MOVE_CONTINUE, CMD_POWER_ENABLE, CMD_RESET_ERROR,
    STATE_DISABLED, STATE_ERROR, STATE_STANDSTILL, AxisCommand,
)
from serial_link import SerialLink  # noqa: E402
from test_control_session import Recorder, make_session, wait_until  # noqa: E402


@pytest.fixture(autouse=True)
def emulated_ports(monkeypatch):
    monkeypatch.setattr(SerialLink, "serial_class", staticmethod(serial_factory))
    monkeypatch.setattr(ops, "BOOT_DELAY", 0.3)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_emergency_stop_switches_drives_off_and_needs_reset():
    with BoardEmulator(device_id=1) as board:
        rec = Recorder()
        session = make_session(board, rec)
        session.start()
        session.submit(CMD_POWER_ENABLE, AxisCommand(AXIS_GROUP, power_enable=1), "power")
        session.submit(CMD_MOVE_CONTINUE, AxisCommand(0, velocity=5000, acceleration=50000, direction=(1, 0, 0)), "jog")
        assert wait_until(lambda: board.axes[0].in_motion)

        session.submit(CMD_EMERGENCY_STOP, AxisCommand(AXIS_GROUP), "E-STOP")
        rec.wait_commands(3)
        assert rec.commands[-1] == ("E-STOP", True, "")
        assert (CMD_EMERGENCY_STOP, board.transaction_id) == board.executed[-1]
        assert all(a.state == STATE_ERROR and not a.enable and not a.in_motion for a in board.axes)
        assert wait_until(lambda: rec.feedback[-1].axes[0].error_text == "emergency stop")
        stopped_at = board.axes[0].position
        time.sleep(0.2)
        assert board.axes[0].position == stopped_at

        # как в прошивке: после сброса ошибки оси выключены, питание включают заново
        session.submit(CMD_RESET_ERROR, AxisCommand(AXIS_GROUP), "reset")
        assert wait_until(lambda: all(a.state == STATE_DISABLED for a in board.axes))
        session.submit(CMD_POWER_ENABLE, AxisCommand(AXIS_GROUP, power_enable=1), "power again")
        assert wait_until(lambda: all(a.state == STATE_STANDSTILL for a in board.axes))
        session.stop()


def _pump(app, predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _connected_tab(app, board):
    import control_tab
    tab = control_tab.ControlTab(lambda: ops.LinkSettings(board.port, 115200, False))
    messages = []
    tab.log.connect(messages.append)
    tab.set_device_id(board.device_id)
    tab._start_session()
    assert _pump(app, lambda: tab._feedback is not None)
    return tab, messages


@pytest.mark.parametrize("from_plot", [False, True])
def test_estop_button_sends_emergency_stop(app, from_plot):
    with BoardEmulator(device_id=2) as board:
        tab, messages = _connected_tab(app, board)
        assert tab.estop_button.text() == "E-STOP  (Esc)"
        tab._send(CMD_POWER_ENABLE, AxisCommand(AXIS_GROUP, power_enable=1), "power")
        tab._send(CMD_MOVE_CONTINUE, AxisCommand(1, velocity=4000, acceleration=40000, direction=(0, -1, 0)), "jog")
        assert _pump(app, lambda: board.axes[1].in_motion)

        if from_plot:
            tab.plot.emergency_stop.emit()                 # кнопка и Esc в окне графиков
        else:
            tab.estop_button.click()
        assert _pump(app, lambda: any("drivers are off" in m for m in messages))
        assert board.executed[-1][0] == CMD_EMERGENCY_STOP
        assert all(a.state == STATE_ERROR and not a.enable for a in board.axes)
        assert not any("fallback" in m for m in messages)
        tab.shutdown()


def test_old_firmware_without_emergency_stop_gets_halt(app):
    with BoardEmulator(device_id=3, emergency_stop=False) as board:
        tab, messages = _connected_tab(app, board)
        tab._send(CMD_POWER_ENABLE, AxisCommand(AXIS_GROUP, power_enable=1), "power")
        tab._send(CMD_MOVE_CONTINUE, AxisCommand(0, velocity=4000, acceleration=40000, direction=(1, 0, 0)), "jog")
        assert _pump(app, lambda: board.axes[0].in_motion)

        tab.emergency_stop()
        assert _pump(app, lambda: board.executed[-1][0] == CMD_HALT, timeout=8.0)
        assert any("E-STOP: NOT accepted" in m for m in messages)
        assert any("not confirmed by the board: sending HALT" in m for m in messages)
        assert _pump(app, lambda: not board.axes[0].in_motion)
        assert board.axes[0].state == STATE_STANDSTILL        # HALT — плавная остановка, питание не трогает
        tab.shutdown()


def test_estop_when_not_connected_only_logs(app):
    import control_tab
    tab = control_tab.ControlTab(lambda: None)
    messages = []
    tab.log.connect(messages.append)
    tab.emergency_stop()
    assert messages == ["E-STOP: not connected"]
    tab.shutdown()
