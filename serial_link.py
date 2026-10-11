"""Последовательный порт с точными тайм-аутами и поддержкой эха RS-485 адаптера."""
from __future__ import annotations

import time

import serial


class LinkError(Exception):
    pass


class SerialLink:
    # timeout pyserial не меняем на ходу: на Linux каждое изменение — это лишний tcsetattr
    _POLL = 0.01
    serial_class = serial.Serial  # тесты подменяют на эмулятор платы

    def __init__(self, port: str, baudrate: int, echo: bool = False):
        self.echo = echo
        try:
            self._ser = self.serial_class(
                port=port,
                baudrate=baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self._POLL,
                write_timeout=2.0,
            )
        except (serial.SerialException, ValueError) as e:
            raise LinkError(f"Cannot open port {port}: {e}") from e

    def __enter__(self) -> SerialLink:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def baudrate(self) -> int:
        return self._ser.baudrate

    def configure(self, baudrate: int, parity: str = serial.PARITY_NONE) -> None:
        self._ser.baudrate = baudrate
        self._ser.parity = parity
        time.sleep(0.02)
        self._ser.reset_input_buffer()

    def close(self) -> None:
        self._ser.close()

    def transfer_time(self, size: int) -> float:
        return size * 11 / self._ser.baudrate

    def flush_input(self) -> None:
        self._ser.reset_input_buffer()

    def write(self, data: bytes) -> None:
        self._ser.write(data)
        self._ser.flush()
        if self.echo:
            echoed = self.read_exact(len(data), 0.2 + self.transfer_time(len(data)))
            if echoed != data:
                raise LinkError(
                    "Adapter echo does not match the sent data. "
                    "If your adapter does not echo, uncheck \"Adapter echo\"."
                )

    def read_exact(self, size: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        buf = bytearray()
        while len(buf) < size and time.monotonic() < deadline:
            buf += self._ser.read(size - len(buf))
        return bytes(buf)
