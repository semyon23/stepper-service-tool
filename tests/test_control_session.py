"""Сеанс управления осями поверх эмулятора платы."""
import threading
import time

import pytest

import operations as ops
from board_emulator import BoardEmulator, make_test_firmware, serial_factory
from board_protocol import (
    AXIS_GROUP, CMD_HALT, CMD_MOVE_ABSOLUTE, CMD_MOVE_CONTINUE, CMD_MOVE_HOME, CMD_MOVE_RELATIVE,
    CMD_POWER_ENABLE, CMD_RESET_ERROR, CMD_SET_POSITION, HOMING_NEGATIVE_LIMIT, STATE_DISABLED,
    STATE_ERROR, STATE_STANDSTILL, AxisCommand,
)
from control_session import ControlSession, SessionCallbacks
from firmware import load_firmware
from serial_link import SerialLink


@pytest.fixture(autouse=True)
def emulated_ports(monkeypatch):
    monkeypatch.setattr(SerialLink, "serial_class", staticmethod(serial_factory))
    monkeypatch.setattr(ops, "BOOT_DELAY", 0.3)


class Recorder:
    def __init__(self):
        self.feedback = []
        self.feedback_times = []
        self.statuses = []
        self.links = []
        self.commands = []
        self.stopped = threading.Event()
        self.stop_reason = None

    def callbacks(self) -> SessionCallbacks:
        return SessionCallbacks(
            feedback=self._feedback,
            status=self.statuses.append,
            link=lambda rate, lost, ok: self.links.append((rate, lost, ok)),
            command_done=lambda label, ok, why: self.commands.append((label, ok, why)),
            stopped=self._stopped,
        )

    def _feedback(self, fb):
        self.feedback.append(fb)
        self.feedback_times.append(time.monotonic())

    def _stopped(self, reason):
        self.stop_reason = reason
        self.stopped.set()

    def wait_commands(self, count, timeout=3.0):
        deadline = time.monotonic() + timeout
        while len(self.commands) < count and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(self.commands) >= count, self.commands


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def make_session(board, rec, device_id=None):
    return ControlSession(board.port, 115200, False, device_id or board.device_id, rec.callbacks())


MOTION = dict(velocity=20000.0, acceleration=200000.0, deceleration=200000.0)


def test_feedback_polled_every_50_ms():
    with BoardEmulator(device_id=3) as board:
        rec = Recorder()
        session = make_session(board, rec)
        session.start()
        time.sleep(2.0)
        session.stop()
    intervals = sorted(b - a for a, b in zip(rec.feedback_times, rec.feedback_times[1:]))
    assert len(rec.feedback) >= 38
    assert 0.045 <= intervals[len(intervals) // 2] <= 0.055      # медиана — 50 мс
    assert rec.links[-1][0] >= 18 and rec.links[-1][2]
    assert rec.stopped.is_set() and rec.stop_reason == ""


def test_transaction_id_continues_from_board():
    # плата помнит transaction_id = 37 с прошлого сеанса; номер 1 она бы приняла,
    # а вот совпадение с 37 молча отбросила бы
    with BoardEmulator(device_id=2, transaction_id=37) as board:
        rec = Recorder()
        session = make_session(board, rec)
        session.start()
        session.submit(CMD_POWER_ENABLE, AxisCommand(AXIS_GROUP, power_enable=1), "питание")
        rec.wait_commands(1)
        session.stop()
        assert rec.commands == [("питание", True, "")]
        assert board.executed == [(CMD_POWER_ENABLE, 38)]
        assert all(axis.state == STATE_STANDSTILL for axis in board.axes)


def test_moves_jog_halt_set_position():
    with BoardEmulator(device_id=1) as board:
        rec = Recorder()
        session = make_session(board, rec)
        session.start()
        session.submit(CMD_POWER_ENABLE, AxisCommand(AXIS_GROUP, power_enable=1), "питание")
        session.submit(CMD_MOVE_ABSOLUTE, AxisCommand(0, position=(5000, 0, 0), **MOTION), "X абс")
        assert wait_until(lambda: board.axes[0].position == 5000 and board.axes[0].state == STATE_STANDSTILL)

        session.submit(CMD_MOVE_RELATIVE, AxisCommand(1, position=(0, -1500, 0), **MOTION), "Y отн")
        assert wait_until(lambda: board.axes[1].position == -1500)

        session.submit(CMD_MOVE_CONTINUE, AxisCommand(2, velocity=8000, acceleration=100000, direction=(0, 0, 1)), "C толчок")
        assert wait_until(lambda: board.axes[2].position > 1000)
        session.submit(CMD_HALT, AxisCommand(2, deceleration=200000), "C стоп")
        assert wait_until(lambda: board.axes[2].state == STATE_STANDSTILL)

        session.submit(CMD_SET_POSITION, AxisCommand(2, position=(0, 0, 0)), "C ноль")
        assert wait_until(lambda: board.axes[2].position == 0)

        session.submit(CMD_MOVE_ABSOLUTE, AxisCommand(AXIS_GROUP, position=(0, 0, 3000), **MOTION), "группа")
        assert wait_until(lambda: [a.position for a in board.axes] == [0, 0, 3000])
        session.stop()
    assert all(ok for _, ok, _ in rec.commands), rec.commands
    fb = rec.feedback[-1]
    assert [round(a.position) for a in fb.axes] == [0, 0, 3000]


def test_limit_error_and_reset_and_homing():
    with BoardEmulator(device_id=1) as board:
        board.axes[0].negative_switch = -500
        rec = Recorder()
        session = make_session(board, rec)
        session.start()
        session.submit(CMD_POWER_ENABLE, AxisCommand(AXIS_GROUP, power_enable=1), "питание")
        session.submit(CMD_MOVE_ABSOLUTE, AxisCommand(0, position=(-10000, 0, 0), **MOTION), "X на концевик")
        assert wait_until(lambda: board.axes[0].state == STATE_ERROR)
        assert wait_until(lambda: rec.feedback and rec.feedback[-1].axes[0].error_text == "limit −")

        session.submit(CMD_RESET_ERROR, AxisCommand(AXIS_GROUP), "сброс")
        assert wait_until(lambda: board.axes[0].state == STATE_STANDSTILL)

        session.submit(CMD_MOVE_HOME, AxisCommand(0, position=(10.0, 0, 0), velocity=5000, acceleration=50000,
                                                  deceleration=50000, direction=(HOMING_NEGATIVE_LIMIT, 0, 0)), "X хоминг")
        assert wait_until(lambda: board.axes[0].home and board.axes[0].state == STATE_STANDSTILL)
        assert wait_until(lambda: rec.statuses[-1].axes[0].is_home)
        session.stop()


def test_lost_reply_is_retried_with_same_transaction():
    with BoardEmulator(device_id=1) as board:
        rec = Recorder()
        session = make_session(board, rec)
        session.start()
        assert wait_until(lambda: rec.feedback)
        board.drop_command_replies = 2
        session.submit(CMD_POWER_ENABLE, AxisCommand(AXIS_GROUP, power_enable=1), "питание")
        rec.wait_commands(1)
        session.stop()
    assert rec.commands == [("питание", True, "")]
    assert [cmd for cmd, _ in board.executed] == [CMD_POWER_ENABLE]   # выполнена один раз, хотя отправлена трижды


def test_queued_command_is_sent_before_stop():
    with BoardEmulator(device_id=1) as board:
        rec = Recorder()
        session = make_session(board, rec)
        session.start()
        session.submit(CMD_POWER_ENABLE, AxisCommand(AXIS_GROUP, power_enable=1), "питание")
        session.submit(CMD_MOVE_CONTINUE, AxisCommand(0, velocity=5000, acceleration=50000, direction=(1, 0, 0)), "X толчок")
        assert wait_until(lambda: board.axes[0].in_motion)
        session.submit(CMD_HALT, AxisCommand(AXIS_GROUP, deceleration=100000), "стоп всех")
        session.stop()
        assert (CMD_HALT, board.transaction_id) == board.executed[-1]
        assert wait_until(lambda: not board.axes[0].in_motion)


def test_session_survives_id_change_and_firmware_update(tmp_path):
    path = tmp_path / "firmware.bin"
    path.write_bytes(make_test_firmware(40 * 1024, seed=5))
    with BoardEmulator(device_id=4) as board:
        rec = Recorder()
        session = make_session(board, rec)
        session.start()
        session.submit(CMD_POWER_ENABLE, AxisCommand(AXIS_GROUP, power_enable=1), "питание")
        rec.wait_commands(1)

        # смена ID: сеанс на паузе, порт свободен для операции
        session.stop()
        settings = ops.LinkSettings(board.port, 115200, False)
        ops.change_board_id(settings, 4, 9, ops.Context(lambda m: None))
        session.set_device_id(9)
        session.start()
        before = len(rec.feedback)
        assert wait_until(lambda: len(rec.feedback) > before + 5)
        session.submit(CMD_MOVE_ABSOLUTE, AxisCommand(0, position=(777, 0, 0), **MOTION), "X абс по новому ID")
        assert wait_until(lambda: board.axes[0].position == 777)

        # прошивка: после перезапуска оси выключены, счётчик транзакций платы с нуля
        session.stop()
        status = ops.flash_board(settings, ops.FlashOptions(load_firmware(path), device_id=9), ops.Context(lambda m: None))
        session.set_device_id(status.device_id)
        session.start()
        assert wait_until(lambda: rec.feedback and rec.feedback[-1].axes[0].state == STATE_DISABLED)
        session.submit(CMD_POWER_ENABLE, AxisCommand(AXIS_GROUP, power_enable=1), "питание после прошивки")
        assert wait_until(lambda: board.axes[0].state == STATE_STANDSTILL)
        session.stop()
    assert all(ok for _, ok, _ in rec.commands), rec.commands


def test_port_error_is_reported():
    rec = Recorder()
    session = ControlSession("emulator://нет-такого", 115200, False, 1, rec.callbacks())
    session.start()
    assert rec.stopped.wait(2)
    assert "Cannot open port" in rec.stop_reason
