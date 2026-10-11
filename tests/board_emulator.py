"""Эмулятор платы для тестов: протокол прошивки + системный загрузчик STM32 (AN3155).

Два транспорта:
- "fake" (по умолчанию): FakeSerial подменяет pyserial и передаёт вместе с байтами
  скорость и чётность на момент отправки. Эмулятор считает байты с чужими настройками
  линии мусором в обе стороны, поэтому тесты ловят ошибки переключения 8N1 ↔ 8E1.
- "pty": настоящий pyserial через псевдотерминал. Чётность на pty ядро Linux
  включить не даёт (EINVAL), поэтому так проверяется только протокол прошивки.
"""
from __future__ import annotations

import os
import queue
import struct
import threading
import time

import serial

from board_protocol import (
    AXIS_GROUP, BOOTLOADER_KEY, CMD_CHANGE_ID_DEVICE, CMD_EMERGENCY_STOP, CMD_GET_FEEDBACK,
    CMD_GET_STATUS, CMD_HALT, CMD_JUMP_TO_BOOTLOADER, CMD_MOVE_ABSOLUTE, CMD_MOVE_CONTINUE, CMD_MOVE_HOME,
    CMD_MOVE_RELATIVE, CMD_POWER_ENABLE, CMD_RESET_ERROR, CMD_SET_POSITION, COMMAND_DATA_SIZE,
    ERROR_EMERGENCY_STOP, ERROR_HOMING_TIMEOUT, ERROR_NEGATIVE_LIMIT, ERROR_POSITIVE_LIMIT,
    HEADER, HOMING_DIRECT, STATE_CONTINUE_MOTION, STATE_DISABLED, STATE_DISCRETE_MOTION,
    STATE_ERROR, STATE_HOMING, STATE_STANDSTILL, STATE_STOPPING, STOP, build_frame, crc8,
)
from firmware import F4_SECTOR_SIZES, FLASH_BASE

ACK, NACK = 0x79, 0x1F
FLASH_SIZE_REG = 0x1FFF7A22
BOOTLOADER_COMMANDS = bytes([0x00, 0x01, 0x02, 0x11, 0x21, 0x31, 0x44, 0x63, 0x73, 0x82, 0x92])
HOMING_TIMEOUT = 10.0
MOTION_STATES = (STATE_DISCRETE_MOTION, STATE_CONTINUE_MOTION, STATE_STOPPING, STATE_HOMING)
DEFAULT_POWER_OFF_DECEL = 1000.0   # как в прошивке: если в профиле торможения нет


class EmulatedAxis:
    """Упрощённая модель AxisControl: те же состояния и правила приёма команд,
    движение с постоянной скоростью (торможение — только в STOPPING)."""

    def __init__(self):
        self.state = STATE_DISABLED
        self.enable = False
        self.position = 0.0
        self.velocity = 0.0
        self.target = 0.0
        self.speed = 0.0
        self.deceleration = 0.0
        self.profile_deceleration = 0.0   # чем тормозить при «power off» в движении (powerOffDeceleration)
        self.direction = 1
        self.home = False
        self.move_done = False
        self.error_id = 0
        self.homing_mode = HOMING_DIRECT
        self.homing_offset = 0.0
        self.homing_time = 0.0
        self.negative_switch: float | None = None   # координата срабатывания концевика
        self.positive_switch: float | None = None

    @property
    def negative_limit(self) -> bool:
        return self.negative_switch is not None and self.position <= self.negative_switch

    @property
    def positive_limit(self) -> bool:
        return self.positive_switch is not None and self.position >= self.positive_switch

    @property
    def in_motion(self) -> bool:
        return self.state in MOTION_STATES

    def enable_driver(self) -> None:
        if self.state == STATE_STOPPING:          # передумали во время торможения
            self.enable = True
        elif self.state == STATE_DISABLED:
            self.enable = True
            self.state = STATE_STANDSTILL

    def disable_driver(self) -> None:
        """Новая прошивка: в движении — торможение с торможением профиля, затем DISABLED."""
        self.enable = False
        if self.state == STATE_STANDSTILL:
            self.state = STATE_DISABLED
        elif self.state in (STATE_DISCRETE_MOTION, STATE_CONTINUE_MOTION, STATE_HOMING):
            dec = self.profile_deceleration
            self.deceleration = dec if dec > 0 else DEFAULT_POWER_OFF_DECEL
            self.state = STATE_STOPPING

    def move_to(self, target: float, speed: float, deceleration: float = 0.0) -> None:
        if not self.enable or self.state not in (STATE_STANDSTILL, STATE_DISCRETE_MOTION):
            return
        self.profile_deceleration = deceleration
        self.move_done = False
        self.target = target
        self.speed = speed
        self.state = STATE_DISCRETE_MOTION

    def move_velocity(self, speed: float, direction: int, acceleration: float = 0.0) -> None:
        if not self.enable or self.state not in (STATE_STANDSTILL, STATE_CONTINUE_MOTION, STATE_STOPPING):
            return
        self.profile_deceleration = acceleration      # moveVelocity() торможение не задаёт
        self.move_done = False
        self.speed = speed
        self.direction = direction
        self.state = STATE_CONTINUE_MOTION

    def move_home(self, offset: float, speed: float, mode: int, deceleration: float = 0.0) -> None:
        if not self.enable or self.state != STATE_STANDSTILL:
            return
        self.profile_deceleration = deceleration
        self.home = False
        self.homing_offset = offset
        self.speed = speed
        self.homing_mode = mode
        self.homing_time = 0.0
        if mode == HOMING_DIRECT:
            self.position = self.target = offset
            self.home = True
            return
        self.state = STATE_HOMING

    def stopping(self, deceleration: float) -> None:
        if self.state in (STATE_CONTINUE_MOTION, STATE_DISCRETE_MOTION, STATE_HOMING):
            self.deceleration = deceleration
            self.state = STATE_STOPPING

    def emergency_stop(self) -> None:
        """AxisControl::emergencyStop(): из любого состояния — стоп сразу, драйвер выключен, ERROR."""
        self.enable = False
        self._fail(ERROR_EMERGENCY_STOP)

    def recover(self) -> None:
        if self.state == STATE_ERROR:
            self.error_id = 0
            self.state = STATE_STANDSTILL if self.enable else STATE_DISABLED

    def reset_position(self, position: float) -> None:
        self.position = position
        if not self.in_motion:
            self.target = position

    def advance(self, dt: float) -> None:
        if self.state == STATE_DISCRETE_MOTION:
            remaining = self.target - self.position
            direction = 1 if remaining > 0 else -1
            if self._limit_hit(direction):
                return
            step = self.speed * dt
            if abs(remaining) <= step:
                self.position = self.target
                self.velocity = 0.0
                self.move_done = True
                self.state = STATE_STANDSTILL
            else:
                self.velocity = direction * self.speed
                self.position += direction * step
        elif self.state == STATE_CONTINUE_MOTION:
            if self._limit_hit(self.direction):
                return
            self.velocity = self.direction * self.speed
            self.position += self.velocity * dt
        elif self.state == STATE_STOPPING:
            dv = self.deceleration * dt
            if abs(self.velocity) <= dv:
                self.velocity = 0.0
                self.target = self.position
                self.move_done = True
                self.state = STATE_STANDSTILL if self.enable else STATE_DISABLED
            else:
                self.velocity -= dv if self.velocity > 0 else -dv
                self.position += self.velocity * dt
        elif self.state == STATE_HOMING:
            self.homing_time += dt
            reached = self.negative_limit if self.homing_mode < 0 else self.positive_limit
            if reached:
                self.position = self.target = self.homing_offset
                self.velocity = 0.0
                self.home = True
                self.state = STATE_STANDSTILL
            elif self.homing_time > HOMING_TIMEOUT:
                self._fail(ERROR_HOMING_TIMEOUT)
            else:
                self.velocity = self.homing_mode * self.speed
                self.position += self.velocity * dt

    def _limit_hit(self, direction: int) -> bool:
        if direction < 0 and self.negative_limit:
            self._fail(ERROR_NEGATIVE_LIMIT)
            return True
        if direction > 0 and self.positive_limit:
            self._fail(ERROR_POSITIVE_LIMIT)
            return True
        return False

    def _fail(self, error_id: int) -> None:
        self.error_id = error_id
        self.velocity = 0.0
        self.target = self.position
        self.state = STATE_ERROR


class FakeSerial:
    """Подмена serial.Serial: порт с именем emulator://… подключён к эмулятору."""
    registry: dict[str, BoardEmulator] = {}

    def __init__(self, port, baudrate=9600, parity=serial.PARITY_NONE, timeout=None, **_):
        if port not in self.registry:
            raise serial.SerialException(f"нет порта {port}")
        self.board = self.registry[port]
        self.baudrate = baudrate
        self.parity = parity
        self.timeout = timeout
        self._rx = bytearray()
        self._cond = threading.Condition()
        self.board.attach(self)

    def _line(self):
        return self.baudrate, self.parity

    def write(self, data) -> int:
        data = bytes(data)
        if self.board.echo:
            self.deliver(data, self._line())
        self.board.receive(data, self._line())
        return len(data)

    def flush(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        with self._cond:
            self._rx.clear()

    def deliver(self, data: bytes, line) -> None:
        if line != self._line():
            self.board.host_garbage_bytes += len(data)
            data = b"\x00" * len(data)
        with self._cond:
            self._rx += data
            self._cond.notify_all()

    def read(self, size: int = 1) -> bytes:
        deadline = time.monotonic() + (self.timeout or 0)
        with self._cond:
            while len(self._rx) < size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            data = bytes(self._rx[:size])
            del self._rx[:size]
        return data

    def close(self) -> None:
        self.board.detach(self)


def serial_factory(port, **kwargs):
    """Для SerialLink.serial_class: эмулятор по имени emulator://…, иначе настоящий порт."""
    if port in FakeSerial.registry:
        return FakeSerial(port, **kwargs)
    return serial.Serial(port, **kwargs)


def _xor(data) -> int:
    result = 0
    for byte in data:
        result ^= byte
    return result


def make_test_firmware(size: int, seed: int = 1) -> bytes:
    """Похожий на настоящий образ: SP в ОЗУ, Reset внутри прошивки, дальше псевдослучайные байты."""
    body = bytearray(size)
    state = seed
    for i in range(8, size):
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        body[i] = state >> 16 & 0xFF
    struct.pack_into("<II", body, 0, 0x20020000, FLASH_BASE + 0x199)
    return bytes(body)


class BoardEmulator:
    def __init__(self, device_id: int = 1, flash_kb: int = 512, pid: int = 0x431, *,
                 transport: str = "fake", baudrate: int = 115200, echo: bool = False,
                 require_key: bool = False, reply_on_jump: bool = False,
                 start_in_bootloader: bool = False, id_save_delay: float = 0.3,
                 boot_delay: float = 0.2, transaction_id: int = 0, emergency_stop: bool = True):
        self.flash_kb = flash_kb
        self.pid = pid
        self.baudrate = baudrate
        self.echo = echo
        self.require_key = require_key
        self.reply_on_jump = reply_on_jump
        self.id_save_delay = id_save_delay
        self.boot_delay = boot_delay
        self.emergency_stop = emergency_stop    # False — старая прошивка: EMERGENCY_STOP не зарегистрирована

        self.moving = False
        self.corrupt_write_at: int | None = None
        self.jumps = 0
        self.erased_sectors: list[int] = []
        self.mass_erases = 0
        self.garbage_bytes = 0       # пришло платой с чужими настройками линии
        self.host_garbage_bytes = 0  # пришло хостом с чужими настройками линии

        self.axes = [EmulatedAxis() for _ in range(3)]
        self.transaction_id = transaction_id   # AxesManager::current_transaction_id
        self.executed: list[tuple[int, int]] = []   # (команда, transaction_id), реально выполненные
        self.feedback_requests = 0
        self.drop_command_replies = 0           # столько ответов на команды «потерять»
        self._last_advance = time.monotonic()

        self.flash = bytearray(b"\xff" * flash_kb * 1024)
        self.sector_starts = self._sector_starts()
        # ID хранится в последнем секторе, как EEPROM-эмуляция STM32duino
        self.id_offset = self.sector_starts[-1]
        old = make_test_firmware(20 * 1024, seed=99)
        self.flash[:len(old)] = old
        self.flash[self.id_offset] = device_id
        self.device_id = device_id

        self._rx = bytearray()
        self._bl = None
        self._boot_at = 0.0
        self.mode = "app"
        if start_in_bootloader:
            self._enter_bootloader()

        self._inbox: queue.Queue = queue.Queue()
        self._host: FakeSerial | None = None
        self._stop = threading.Event()
        self.transport = transport
        self._threads = [threading.Thread(target=self._run, daemon=True)]
        if transport == "pty":
            self._open_pty()
            self._threads.append(threading.Thread(target=self._pty_reader, daemon=True))
        else:
            self.port = f"emulator://{id(self)}"
            FakeSerial.registry[self.port] = self
        for thread in self._threads:
            thread.start()

    def close(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2)
        if self.transport == "pty":
            os.close(self._master)
            os.close(self._slave)
        else:
            FakeSerial.registry.pop(self.port, None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def stored_id(self) -> int:
        return self.flash[self.id_offset]

    def _sector_starts(self) -> list[int]:
        starts, offset = [], 0
        for size in F4_SECTOR_SIZES:
            if offset >= len(self.flash):
                break
            starts.append(offset)
            offset += size
        return starts

    # ------------------------------------------------------------ транспорт

    def attach(self, host: FakeSerial) -> None:
        self._host = host

    def detach(self, host: FakeSerial) -> None:
        if self._host is host:
            self._host = None

    def receive(self, data: bytes, line) -> None:
        self._inbox.put((data, line))

    def _line(self):
        """Прошивка работает в 8N1, системный загрузчик STM32 — в 8E1."""
        parity = serial.PARITY_EVEN if self.mode == "bootloader" else serial.PARITY_NONE
        return self.baudrate, parity

    def _open_pty(self) -> None:
        import pty
        import tty
        self._master, self._slave = pty.openpty()
        tty.setraw(self._slave)
        self.port = os.ttyname(self._slave)

    def _pty_reader(self) -> None:
        import select
        import termios
        speeds = {getattr(termios, f"B{b}"): b for b in (9600, 19200, 38400, 57600, 115200)}
        while not self._stop.is_set():
            if not select.select([self._master], [], [], 0.01)[0]:
                continue
            try:
                data = os.read(self._master, 4096)
            except OSError:
                continue
            if self.echo:
                os.write(self._master, data)
            attrs = termios.tcgetattr(self._slave)
            parity = serial.PARITY_EVEN if attrs[2] & termios.PARENB else serial.PARITY_NONE
            self.receive(data, (speeds.get(attrs[4]), parity))

    def _run(self) -> None:
        while not self._stop.is_set():
            if self.mode == "booting" and time.monotonic() >= self._boot_at:
                self._boot()
            now = time.monotonic()
            if self.mode == "app":
                for axis in self.axes:
                    axis.advance(now - self._last_advance)
            self._last_advance = now
            try:
                data, line = self._inbox.get(timeout=0.01)
            except queue.Empty:
                continue
            if self.mode not in ("app", "bootloader"):
                continue
            if line != self._line():
                self.garbage_bytes += len(data)
                continue
            for byte in data:
                if self.mode == "app":
                    self._app_byte(byte)
                elif self.mode == "bootloader":
                    self._bl.send(byte)

    def _out(self, data) -> None:
        data = bytes(data)
        if self.transport == "pty":
            os.write(self._master, data)
        elif self._host is not None:
            self._host.deliver(data, self._line())

    # ------------------------------------------------------------ прошивка (app)

    def _app_byte(self, byte: int) -> None:
        rx = self._rx
        if not rx and byte != HEADER:
            return
        if len(rx) == 1 and byte != self.device_id:
            rx.clear()
            return
        rx.append(byte)
        if len(rx) > 3 and len(rx) == 6 + rx[3]:
            frame = bytes(rx)
            rx.clear()
            if frame[-1] == STOP and crc8(frame[:-2]) == frame[-2]:
                self._app_command(frame[2], frame[4:-2])

    def _status_payload(self) -> bytes:
        a = self.axes
        return struct.pack(
            "<B3f18B", self.transaction_id, *(x.position for x in a),
            *(x.state for x in a), *(x.home for x in a), *(x.enable for x in a),
            *(x.move_done for x in a), *((x.in_motion or self.moving) for x in a), *(x.error_id for x in a),
        )

    def _feedback_payload(self) -> bytes:
        a = self.axes
        return struct.pack(
            "<15f21B", *(x.position for x in a), *(float(int(x.position)) for x in a), *(0.0 for _ in a),
            *(x.velocity for x in a), *(x.velocity for x in a),
            *(x.in_motion for x in a), *(abs(int(x.target) - int(x.position)) <= 1 for x in a),
            *(x.state == STATE_ERROR for x in a), *(x.error_id for x in a), *(x.state for x in a),
            *(x.negative_limit for x in a), *(x.positive_limit for x in a),
        )

    def _send_status(self, cmd: int, device_id: int | None = None) -> None:
        self._out(build_frame(self.device_id if device_id is None else device_id, cmd, self._status_payload()))

    def _axes_command(self, cmd: int, data: bytes) -> None:
        """AxesManager + лямбды registerCommands(): ответ отправляется всегда, даже если команда отброшена."""
        if len(data) < COMMAND_DATA_SIZE:
            return
        values = struct.unpack_from("<3B3f3f3b", data)
        tid, axis_id = values[0], values[1]
        power, positions, (velocity, acceleration, deceleration), direction = (
            values[2], values[3:6], values[6:9], values[9:12])
        targets = [a for i, a in enumerate(self.axes) if axis_id == AXIS_GROUP or axis_id == i]

        if cmd == CMD_SET_POSITION:                     # setCurrentPositions: без проверки транзакции
            for i, axis in enumerate(self.axes):
                if axis in targets:
                    axis.reset_position(positions[i])
            self.executed.append((cmd, tid))
        elif axis_id <= AXIS_GROUP and tid != self.transaction_id:
            self.transaction_id = tid
            self.executed.append((cmd, tid))
            if cmd == CMD_POWER_ENABLE:
                for axis in targets:
                    axis.enable_driver() if power else axis.disable_driver()
            elif cmd in (CMD_MOVE_ABSOLUTE, CMD_MOVE_RELATIVE):
                ends = [positions[i] if cmd == CMD_MOVE_ABSOLUTE else a.position + positions[i]
                        for i, a in enumerate(self.axes)]
                distances = [abs(ends[i] - a.position) for i, a in enumerate(self.axes)]
                longest = max(distances[i] for i, a in enumerate(self.axes) if a in targets)
                for i, axis in enumerate(self.axes):
                    if axis in targets:
                        speed = velocity * distances[i] / longest if axis_id == AXIS_GROUP and longest else velocity
                        dec = deceleration * distances[i] / longest if axis_id == AXIS_GROUP and longest else deceleration
                        axis.move_to(ends[i], speed, dec)
            elif cmd == CMD_MOVE_CONTINUE:
                for i, axis in enumerate(self.axes):
                    if axis in targets:
                        axis.move_velocity(velocity, direction[i], acceleration)
            elif cmd == CMD_MOVE_HOME:
                for i, axis in enumerate(self.axes):
                    if axis in targets:
                        axis.move_home(positions[i], velocity, direction[i], acceleration)   # AxesManager передаёт acceleration как dec
            elif cmd == CMD_HALT:
                for axis in targets:
                    axis.stopping(deceleration)
            elif cmd == CMD_EMERGENCY_STOP:          # AxesManager::hardStop: все оси, axis_id не важен
                for axis in self.axes:
                    axis.emergency_stop()
            elif cmd == CMD_RESET_ERROR:
                for axis in self.axes:
                    axis.recover()

        if self.drop_command_replies > 0:
            self.drop_command_replies -= 1
            return
        self._send_status(cmd)

    def _app_command(self, cmd: int, data: bytes) -> None:
        if cmd in (CMD_POWER_ENABLE, CMD_MOVE_RELATIVE, CMD_MOVE_ABSOLUTE, CMD_MOVE_CONTINUE,
                   CMD_MOVE_HOME, CMD_HALT, CMD_SET_POSITION, CMD_RESET_ERROR) or (
                cmd == CMD_EMERGENCY_STOP and self.emergency_stop):
            self._axes_command(cmd, data)
        elif cmd == CMD_GET_FEEDBACK and len(data) >= 1:
            self.feedback_requests += 1
            self._out(build_frame(self.device_id, cmd, self._feedback_payload()))
        elif cmd == CMD_GET_STATUS and len(data) >= 1:
            self._send_status(cmd)
        elif cmd == CMD_CHANGE_ID_DEVICE and len(data) == 1:
            self._send_status(cmd)
            if data[0] != self.device_id:
                self.device_id = data[0]
                self._stall(self.id_save_delay)
                self.flash[self.id_offset] = self.device_id
        elif cmd == CMD_JUMP_TO_BOOTLOADER:
            if self.require_key and (len(data) != 4 or struct.unpack("<I", data)[0] != BOOTLOADER_KEY):
                return
            if self.require_key and self.moving:
                return
            if self.reply_on_jump:
                self._send_status(cmd)
            self.jumps += 1
            self._enter_bootloader()

    def _stall(self, seconds: float) -> None:
        """Стирание flash: код не выполняется, входящие байты теряются."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                self._inbox.get(timeout=0.01)
            except queue.Empty:
                pass
        self._rx.clear()

    def _boot(self) -> None:
        sp, reset = struct.unpack_from("<II", self.flash)
        if not (0x20000000 <= sp <= 0x20100000 and reset & 1):
            self.mode = "dead"
            return
        stored = self.flash[self.id_offset]
        if stored in (0x00, 0xFF):
            stored = 1
            self.flash[self.id_offset] = stored
        self.device_id = stored
        self._rx.clear()
        self.axes = [EmulatedAxis() for _ in range(3)]   # после перезапуска оси выключены
        self.transaction_id = 0
        self.mode = "app"

    # ------------------------------------------------------------ загрузчик

    def _enter_bootloader(self) -> None:
        self.mode = "bootloader"
        self._bl = self._bootloader()
        next(self._bl)

    def _bootloader(self):
        while (yield) != 0x7F:
            pass
        self._out([ACK])
        handlers = {0x00: self._bl_get, 0x02: self._bl_get_id, 0x11: self._bl_read,
                    0x31: self._bl_write, 0x44: self._bl_extended_erase, 0x21: self._bl_go}
        while True:
            cmd = yield
            complement = yield
            handler = handlers.get(cmd)
            if cmd ^ complement != 0xFF or handler is None:
                self._out([NACK])
                continue
            self._out([ACK])
            yield from handler()
            if self.mode != "bootloader":
                while True:
                    yield

    def _read_bytes(self, count: int):
        data = bytearray()
        for _ in range(count):
            data.append((yield))
        return bytes(data)

    def _read_address(self):
        raw = yield from self._read_bytes(5)
        if _xor(raw[:4]) != raw[4]:
            self._out([NACK])
            return None
        return int.from_bytes(raw[:4], "big")

    def _flash_offset(self, address: int, size: int) -> int | None:
        offset = address - FLASH_BASE
        return offset if 0 <= offset and offset + size <= len(self.flash) else None

    def _bl_get(self):
        self._out([len(BOOTLOADER_COMMANDS), 0x31, *BOOTLOADER_COMMANDS, ACK])
        return
        yield

    def _bl_get_id(self):
        self._out([1, self.pid >> 8, self.pid & 0xFF, ACK])
        return
        yield

    def _bl_read(self):
        address = yield from self._read_address()
        if address is None:
            return
        if address != FLASH_SIZE_REG and self._flash_offset(address, 1) is None:
            self._out([NACK])
            return
        self._out([ACK])
        n = yield
        complement = yield
        if n ^ complement != 0xFF:
            self._out([NACK])
            return
        self._out([ACK])
        if address == FLASH_SIZE_REG:
            self._out(struct.pack("<H", self.flash_kb)[:n + 1])
        else:
            offset = self._flash_offset(address, n + 1)
            self._out(self.flash[offset:offset + n + 1])

    def _bl_write(self):
        address = yield from self._read_address()
        if address is None:
            return
        if address % 4 or self._flash_offset(address, 4) is None:
            self._out([NACK])
            return
        self._out([ACK])
        n = yield
        data = yield from self._read_bytes(n + 1)
        checksum = yield
        offset = self._flash_offset(address, n + 1)
        if checksum != n ^ _xor(data) or offset is None:
            self._out([NACK])
            return
        # программировать можно только стёртые ячейки
        if any(b != 0xFF for b in self.flash[offset:offset + len(data)]):
            self._out([NACK])
            return
        data = bytearray(data)
        if self.corrupt_write_at is not None and offset <= self.corrupt_write_at - FLASH_BASE < offset + len(data):
            data[self.corrupt_write_at - FLASH_BASE - offset] ^= 0x01
        self.flash[offset:offset + len(data)] = data
        self._out([ACK])

    def _bl_extended_erase(self):
        head = yield from self._read_bytes(2)
        count = int.from_bytes(head, "big")
        if count == 0xFFFF:
            checksum = yield
            if checksum != 0x00:
                self._out([NACK])
                return
            self.flash[:] = b"\xff" * len(self.flash)
            self.mass_erases += 1
            self._out([ACK])
            return
        raw = yield from self._read_bytes((count + 1) * 2)
        checksum = yield
        if checksum != _xor(head + raw):
            self._out([NACK])
            return
        sectors = [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw), 2)]
        if any(s >= len(self.sector_starts) for s in sectors):
            self._out([NACK])
            return
        for sector in sectors:
            start = self.sector_starts[sector]
            self.flash[start:start + F4_SECTOR_SIZES[sector]] = b"\xff" * F4_SECTOR_SIZES[sector]
            self.erased_sectors.append(sector)
        self._out([ACK])

    def _bl_go(self):
        address = yield from self._read_address()
        if address is None:
            return
        self._out([ACK])
        self.mode = "booting"
        self._boot_at = time.monotonic() + self.boot_delay
