"""Протокол обмена с платой StepperDriver (см. src/CommandManager.hpp).

Кадр: 0xAA | ID | CMD | LEN | DATA[LEN] | CRC8 | 0x55.
CRC-8 (полином 0x31, начальное значение 0) считается по байтам от 0xAA до конца DATA.
На GET_FEEDBACK плата отвечает FeedbackData (81 байт), на остальные команды — StatusData (31 байт).
"""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass

from serial_link import SerialLink

HEADER = 0xAA
STOP = 0x55

CMD_POWER_ENABLE = 1
CMD_MOVE_RELATIVE = 2
CMD_MOVE_ABSOLUTE = 3
CMD_MOVE_CONTINUE = 4
CMD_MOVE_HOME = 5
CMD_HALT = 6
CMD_EMERGENCY_STOP = 7
CMD_SET_POSITION = 8
CMD_GET_STATUS = 10
CMD_GET_FEEDBACK = 20
CMD_RESET_ERROR = 30
CMD_CHANGE_ID_DEVICE = 250
CMD_JUMP_TO_BOOTLOADER = 255

COMMAND_NAMES = {
    CMD_POWER_ENABLE: "POWER_ENABLE",
    CMD_MOVE_RELATIVE: "MOVE_RELATIVE",
    CMD_MOVE_ABSOLUTE: "MOVE_ABSOLUTE",
    CMD_MOVE_CONTINUE: "MOVE_CONTINUE",
    CMD_MOVE_HOME: "MOVE_HOME",
    CMD_HALT: "HALT",
    CMD_EMERGENCY_STOP: "EMERGENCY_STOP",
    CMD_SET_POSITION: "SET_POSITION",
    CMD_RESET_ERROR: "RESET_ERROR",
}

# AxesManager молча отбрасывает эти команды, если transaction_id совпал с предыдущим
TRANSACTION_COMMANDS = frozenset({
    CMD_POWER_ENABLE, CMD_MOVE_RELATIVE, CMD_MOVE_ABSOLUTE, CMD_MOVE_CONTINUE,
    CMD_MOVE_HOME, CMD_HALT, CMD_EMERGENCY_STOP, CMD_RESET_ERROR,
})

AXIS_GROUP = 3   # axis_id для команды на все оси сразу

HOMING_NEGATIVE_LIMIT = -1
HOMING_DIRECT = 0
HOMING_POSITIVE_LIMIT = 1

# Текущая прошивка данные JUMP_TO_BOOTLOADER игнорирует. Ключ нужен, если в прошивке
# включена проверка, как в примере handleJumpToBootloader().
BOOTLOADER_KEY = 0xB007C0DE

# 0 и 0xFF loadID() считает пустой EEPROM и заменяет на DEFAULT_ID
MIN_DEVICE_ID = 1
MAX_DEVICE_ID = 254
DEFAULT_DEVICE_ID = 1

AXIS_NAMES = ("X", "Y", "C")

STATE_DISABLED = 0
STATE_STANDSTILL = 1
STATE_DISCRETE_MOTION = 2
STATE_CONTINUE_MOTION = 3
STATE_STOPPING = 4
STATE_HOMING = 5
STATE_ERROR = 6
AXIS_STATES = {
    STATE_DISABLED: "DISABLED",
    STATE_STANDSTILL: "STANDSTILL",
    STATE_DISCRETE_MOTION: "DISCRETE_MOTION",
    STATE_CONTINUE_MOTION: "CONTINUE_MOTION",
    STATE_STOPPING: "STOPPING",
    STATE_HOMING: "HOMING",
    STATE_ERROR: "ERROR",
}

# ERROR_ID из include/AxisController.hpp текущей прошивки
AXIS_ERRORS = {
    0: "",
    1: "emergency stop",
    2: "drive fault",
    3: "limit −",
    4: "limit +",
    5: "homing timeout",
}
ERROR_EMERGENCY_STOP = 1
ERROR_NEGATIVE_LIMIT = 3
ERROR_POSITIVE_LIMIT = 4
ERROR_HOMING_TIMEOUT = 5

# StatusData: transaction_id, position[3], stateId[3], isHome[3], isPowered[3],
# isTarget[3], inMotion[3], errorID[3]
_STATUS_DATA = struct.Struct("<B3f18B")
STATUS_DATA_SIZE = _STATUS_DATA.size

# CommandData: transaction_id, axis_id, power_enable, position[3], velocity,
# acceleration, deceleration, direction[3]
_COMMAND_DATA = struct.Struct("<3B3f3f3b")
COMMAND_DATA_SIZE = _COMMAND_DATA.size

# FeedbackData: position[3], feedbackPos[3], lagError[3], velocity[3], velocityFeedback[3],
# inMotion[3], inTarget[3], error[3], error_id[3], stateId[3], negativeLS[3], positiveLS[3]
_FEEDBACK_DATA = struct.Struct("<15f21B")
FEEDBACK_DATA_SIZE = _FEEDBACK_DATA.size


def crc8(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def build_frame(device_id: int, cmd: int, payload: bytes = b"") -> bytes:
    if len(payload) > 255:
        raise ValueError("payload is longer than 255 bytes")
    body = bytes([HEADER, device_id, cmd, len(payload)]) + payload
    return body + bytes([crc8(body), STOP])


def parse_frame(frame: bytes) -> tuple[int, int, bytes]:
    """Проверяет кадр целиком и возвращает (device_id, cmd, payload)."""
    if len(frame) < 6 or frame[0] != HEADER or frame[-1] != STOP:
        raise ValueError("no header or stop byte")
    if frame[3] != len(frame) - 6:
        raise ValueError("length mismatch")
    if crc8(frame[:-2]) != frame[-2]:
        raise ValueError("bad CRC")
    return frame[1], frame[2], frame[4:-2]


@dataclass
class AxisStatus:
    position: float
    state: int
    is_home: bool
    is_powered: bool
    is_target: bool
    in_motion: bool
    error_id: int

    @property
    def state_name(self) -> str:
        return AXIS_STATES.get(self.state, f"state {self.state}")

    def describe(self) -> str:
        text = f"{self.state_name}, pos {self.position:g}"
        if self.in_motion:
            text += ", moving"
        if self.error_id:
            text += f", error {self.error_id}"
        return text


@dataclass
class BoardStatus:
    device_id: int
    transaction_id: int
    axes: list[AxisStatus]

    @property
    def in_motion(self) -> bool:
        return any(axis.in_motion for axis in self.axes)

    def describe(self) -> str:
        return "; ".join(f"{name}: {axis.describe()}" for name, axis in zip(AXIS_NAMES, self.axes))


def parse_status(device_id: int, payload: bytes) -> BoardStatus:
    values = _STATUS_DATA.unpack(payload)
    transaction_id, positions, flags = values[0], values[1:4], values[4:]
    state, home, powered, target, motion, error = (flags[i * 3:(i + 1) * 3] for i in range(6))
    axes = [
        AxisStatus(positions[i], state[i], bool(home[i]), bool(powered[i]),
                   bool(target[i]), bool(motion[i]), error[i])
        for i in range(len(AXIS_NAMES))
    ]
    return BoardStatus(device_id, transaction_id, axes)


@dataclass
class AxisCommand:
    """Данные CommandData. axis — 0..2 или AXIS_GROUP; position и direction — по осям."""
    axis: int
    power_enable: int = 0
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    velocity: float = 0.0
    acceleration: float = 0.0
    deceleration: float = 0.0
    direction: tuple[int, int, int] = (0, 0, 0)

    def pack(self, transaction_id: int) -> bytes:
        return _COMMAND_DATA.pack(transaction_id, self.axis, self.power_enable, *self.position,
                                  self.velocity, self.acceleration, self.deceleration, *self.direction)


@dataclass
class AxisFeedback:
    position: float
    feedback_position: float
    lag_error: float
    velocity: float
    velocity_feedback: float
    in_motion: bool
    in_target: bool
    error: bool
    error_id: int
    state: int
    negative_limit: bool
    positive_limit: bool

    @property
    def state_name(self) -> str:
        return AXIS_STATES.get(self.state, f"state {self.state}")

    @property
    def error_text(self) -> str:
        return AXIS_ERRORS.get(self.error_id, f"error {self.error_id}")


@dataclass
class BoardFeedback:
    axes: list[AxisFeedback]
    timestamp: float = 0.0     # time.monotonic() в момент запроса; ставит ControlSession

    @property
    def in_motion(self) -> bool:
        return any(axis.in_motion for axis in self.axes)


def parse_feedback(payload: bytes) -> BoardFeedback:
    values = _FEEDBACK_DATA.unpack(payload)
    floats, flags = values[:15], values[15:]
    position, feedback, lag, velocity, velocity_fb = (floats[i * 3:(i + 1) * 3] for i in range(5))
    motion, target, error, error_id, state, neg, pos = (flags[i * 3:(i + 1) * 3] for i in range(7))
    axes = [
        AxisFeedback(position[i], feedback[i], lag[i], velocity[i], velocity_fb[i],
                     bool(motion[i]), bool(target[i]), bool(error[i]), error_id[i], state[i],
                     bool(neg[i]), bool(pos[i]))
        for i in range(len(AXIS_NAMES))
    ]
    return BoardFeedback(axes)


class BoardClient:
    def __init__(self, link: SerialLink):
        self.link = link

    def _timeout(self, reply_size: int = STATUS_DATA_SIZE) -> float:
        # запрос + ответ + запас на обработку в loop() платы
        return 0.08 + self.link.transfer_time(COMMAND_DATA_SIZE + 6 + reply_size + 6)

    def get_status(self, device_id: int, attempts: int = 1) -> BoardStatus | None:
        for _ in range(attempts):
            payload = self._transact(device_id, CMD_GET_STATUS, b"\x00")
            if payload is not None:
                return parse_status(device_id, payload)
        return None

    def get_feedback(self, device_id: int) -> BoardFeedback | None:
        payload = self._transact(device_id, CMD_GET_FEEDBACK, b"\x00", reply_size=FEEDBACK_DATA_SIZE)
        return parse_feedback(payload) if payload is not None else None

    def command(self, device_id: int, cmd: int, command: AxisCommand, transaction_id: int) -> BoardStatus | None:
        payload = self._transact(device_id, cmd, command.pack(transaction_id))
        return parse_status(device_id, payload) if payload is not None else None

    def change_id(self, old_id: int, new_id: int) -> BoardStatus | None:
        payload = self._transact(old_id, CMD_CHANGE_ID_DEVICE, bytes([new_id]))
        return parse_status(old_id, payload) if payload is not None else None

    def jump_to_bootloader(self, device_id: int) -> bool:
        """True, если плата подтвердила переход. Текущая прошивка уходит в загрузчик молча."""
        key = struct.pack("<I", BOOTLOADER_KEY)
        return self._transact(device_id, CMD_JUMP_TO_BOOTLOADER, key, timeout=0.3) is not None

    def _transact(self, device_id: int, cmd: int, payload: bytes, timeout: float | None = None,
                  reply_size: int = STATUS_DATA_SIZE) -> bytes | None:
        self.link.flush_input()
        self.link.write(build_frame(device_id, cmd, payload))
        return self._read_reply(device_id, cmd, timeout or self._timeout(reply_size), reply_size)

    def _read_reply(self, device_id: int, cmd: int, timeout: float, reply_size: int) -> bytes | None:
        deadline = time.monotonic() + timeout

        def left() -> float:
            return max(0.0, deadline - time.monotonic())

        while True:
            first = self.link.read_exact(1, left())
            if not first:
                return None
            if first[0] != HEADER:
                continue
            head = self.link.read_exact(3, left())
            if len(head) < 3:
                return None
            tail = self.link.read_exact(head[2] + 2, left())
            if len(tail) < head[2] + 2:
                return None
            try:
                dev, reply_cmd, payload = parse_frame(first + head + tail)
            except ValueError:
                continue
            # эхо собственного запроса отсеивается по длине: запросы короче ответов
            if dev == device_id and reply_cmd == cmd and len(payload) == reply_size:
                return payload
