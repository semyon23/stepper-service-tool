"""Выключение питания в движении: ось тормозит с торможением профиля и переходит в DISABLED."""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import operations as ops  # noqa: E402
from board_emulator import BoardEmulator, serial_factory  # noqa: E402
from board_protocol import (  # noqa: E402
    AXIS_GROUP, CMD_MOVE_ABSOLUTE, CMD_MOVE_CONTINUE, CMD_POWER_ENABLE, STATE_DISABLED,
    STATE_STANDSTILL, STATE_STOPPING, AxisCommand,
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


def _powered_session(board):
    rec = Recorder()
    session = make_session(board, rec)
    session.start()
    session.submit(CMD_POWER_ENABLE, AxisCommand(AXIS_GROUP, power_enable=1), "power")
    assert wait_until(lambda: all(a.state == STATE_STANDSTILL for a in board.axes))
    return session, rec


def test_power_off_while_jogging_stops_then_disables():
    with BoardEmulator(device_id=1) as board:
        session, rec = _powered_session(board)
        # разгон и торможение при выключении — 4000 шаг/с²: от 4000 шаг/с ось встанет за 1 с
        session.submit(CMD_MOVE_CONTINUE, AxisCommand(0, velocity=4000, acceleration=4000, direction=(1, 0, 0)), "jog")
        assert wait_until(lambda: board.axes[0].position > 500)

        session.submit(CMD_POWER_ENABLE, AxisCommand(0, power_enable=0), "power off")
        assert wait_until(lambda: board.axes[0].state == STATE_STOPPING)
        assert not board.axes[0].enable
        assert wait_until(lambda: rec.statuses[-1].axes[0].is_powered is False)

        # толчок во время торможения прошивка не принимает: питание уже снято
        session.submit(CMD_MOVE_CONTINUE, AxisCommand(0, velocity=4000, acceleration=4000, direction=(1, 0, 0)), "jog again")
        assert wait_until(lambda: board.axes[0].state == STATE_DISABLED, timeout=3.0)
        stopped_at = board.axes[0].position
        time.sleep(0.2)
        assert board.axes[0].position == stopped_at
        assert board.axes[1].state == STATE_STANDSTILL           # остальные оси не тронуты
        session.stop()


def test_power_off_during_move_uses_profile_deceleration():
    with BoardEmulator(device_id=1) as board:
        session, rec = _powered_session(board)
        session.submit(CMD_MOVE_ABSOLUTE, AxisCommand(1, position=(0, 100000, 0), velocity=3000,
                                                      acceleration=30000, deceleration=6000), "move")
        assert wait_until(lambda: board.axes[1].position > 300)
        session.submit(CMD_POWER_ENABLE, AxisCommand(1, power_enable=0), "power off")
        assert wait_until(lambda: board.axes[1].state == STATE_STOPPING)
        assert board.axes[1].deceleration == 6000              # из профиля MOVE_ABSOLUTE
        assert wait_until(lambda: board.axes[1].state == STATE_DISABLED, timeout=3.0)
        assert board.axes[1].position < 100000
        session.stop()


def test_power_on_while_stopping_keeps_axis_enabled():
    with BoardEmulator(device_id=1) as board:
        session, rec = _powered_session(board)
        session.submit(CMD_MOVE_CONTINUE, AxisCommand(2, velocity=4000, acceleration=4000, direction=(0, 0, -1)), "jog")
        assert wait_until(lambda: board.axes[2].position < -500)
        session.submit(CMD_POWER_ENABLE, AxisCommand(2, power_enable=0), "power off")
        assert wait_until(lambda: board.axes[2].state == STATE_STOPPING)
        session.submit(CMD_POWER_ENABLE, AxisCommand(2, power_enable=1), "power on again")
        assert wait_until(lambda: board.axes[2].enable)
        assert wait_until(lambda: board.axes[2].state == STATE_STANDSTILL, timeout=3.0)
        session.stop()


def _pump(app, predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_tab_sends_power_off_while_moving_and_refuses_jog_meanwhile(app):
    import control_tab
    with BoardEmulator(device_id=5) as board:
        tab = control_tab.ControlTab(lambda: ops.LinkSettings(board.port, 115200, False))
        messages = []
        tab.log.connect(messages.append)
        tab.set_device_id(5)
        tab.acceleration.setValue(4000)
        tab.velocity.setValue(4000)
        tab._start_session()
        assert _pump(app, lambda: tab._feedback is not None)
        tab._power(AXIS_GROUP, True)
        assert _pump(app, lambda: tab._feedback.axes[0].state == STATE_STANDSTILL)

        tab._jog_start(0, 1)
        assert _pump(app, lambda: tab._feedback.axes[0].in_motion)
        tab._jogging.clear()                                   # кнопку толчка «не отпускали»

        tab._power(0, False)                                    # раньше утилита отказывалась
        assert _pump(app, lambda: any("power off (X moving: stopping first): accepted" in m for m in messages))
        assert _pump(app, lambda: tab._feedback.axes[0].state == STATE_STOPPING)
        assert _pump(app, lambda: tab._status is not None and not tab._status.axes[0].is_powered)

        sent = len(board.executed)
        tab._jog_start(0, 1)
        assert messages[-1] == "Axis X: jog: not sent, X is stopping to power off"
        assert len(board.executed) == sent

        assert _pump(app, lambda: tab._feedback.axes[0].state == STATE_DISABLED, timeout=4.0)
        tab._jog_start(0, 1)
        assert messages[-1] == "Axis X: jog: not sent, X is off — turn the power on"
        tab.shutdown()
