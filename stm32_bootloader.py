"""Системный загрузчик STM32 по USART (ST AN3155).

Формат линии: 8 бит, контроль чётности EVEN, 1 стоп-бит; скорость загрузчик
определяет сам по первому байту 0x7F.
"""
from __future__ import annotations

from serial_link import SerialLink

ACK = 0x79
NACK = 0x1F

CMD_GET = 0x00
CMD_GET_ID = 0x02
CMD_READ_MEMORY = 0x11
CMD_GO = 0x21
CMD_WRITE_MEMORY = 0x31
CMD_ERASE = 0x43
CMD_EXTENDED_ERASE = 0x44

MAX_BLOCK = 256

# PID из AN2606. "f4" — сектора 16/16/16/16/64/128… КБ
STM32_DEVICES = {
    0x411: ("STM32F2xx", "f4"),
    0x413: ("STM32F405/407/415/417", "f4"),
    0x419: ("STM32F42x/43x", "f4"),
    0x421: ("STM32F446", "f4"),
    0x423: ("STM32F401xB/C", "f4"),
    0x431: ("STM32F411xC/E", "f4"),
    0x433: ("STM32F401xD/E", "f4"),
    0x434: ("STM32F469/479", "f4"),
    0x441: ("STM32F412", "f4"),
    0x458: ("STM32F410", "f4"),
    0x463: ("STM32F413/423", "f4"),
}


class BootloaderError(Exception):
    pass


def describe_device(pid: int) -> tuple[str, str | None]:
    return STM32_DEVICES.get(pid, (f"unknown MCU (PID 0x{pid:03X})", None))


def _xor(data: bytes) -> int:
    result = 0
    for byte in data:
        result ^= byte
    return result


class Stm32Bootloader:
    def __init__(self, link: SerialLink):
        self.link = link

    def sync(self, attempts: int = 30) -> None:
        for _ in range(attempts):
            self.link.flush_input()
            self.link.write(b"\x7f")
            reply = self.link.read_exact(1, 0.1)
            # NACK приходит, если загрузчик уже инициализирован и принял 0x7F за команду
            if reply and reply[0] in (ACK, NACK):
                return
        raise BootloaderError("STM32 bootloader does not answer 0x7F")

    def get(self) -> tuple[int, bytes]:
        """Версия загрузчика и список поддерживаемых команд."""
        self._command(CMD_GET, "Get")
        count = self._read(1, "Get")[0]
        data = self._read(count + 1, "Get")
        self._ack("Get")
        return data[0], data[1:]

    def get_id(self) -> int:
        self._command(CMD_GET_ID, "Get ID")
        count = self._read(1, "Get ID")[0]
        pid = int.from_bytes(self._read(count + 1, "Get ID"), "big")
        self._ack("Get ID")
        return pid

    def read_memory(self, address: int, size: int) -> bytes:
        if not 1 <= size <= MAX_BLOCK:
            raise ValueError("size must be 1..256")
        self._command(CMD_READ_MEMORY, "Read Memory")
        self._send_address(address, "Read Memory")
        self.link.write(bytes([size - 1, (size - 1) ^ 0xFF]))
        self._ack("Read Memory: length")
        return self._read(size, "Read Memory: data")

    def write_memory(self, address: int, data: bytes) -> None:
        if address % 4 or len(data) % 4 or not 4 <= len(data) <= MAX_BLOCK:
            raise ValueError("address and length must be multiples of 4, length 4..256")
        self._command(CMD_WRITE_MEMORY, "Write Memory")
        self._send_address(address, "Write Memory")
        n = len(data) - 1
        self.link.write(bytes([n]) + data + bytes([n ^ _xor(data)]))
        self._ack(f"Write Memory 0x{address:08X}", timeout=2.0)

    def erase_sectors(self, sectors: list[int], extended: bool) -> None:
        if extended:
            self._command(CMD_EXTENDED_ERASE, "Extended Erase")
            payload = (len(sectors) - 1).to_bytes(2, "big") + b"".join(s.to_bytes(2, "big") for s in sectors)
        else:
            self._command(CMD_ERASE, "Erase")
            payload = bytes([len(sectors) - 1, *sectors])
        self.link.write(payload + bytes([_xor(payload)]))
        # сектор 128 КБ на F4 стирается до ~2–4 с
        self._ack(f"erase sectors {sectors}", timeout=10.0 * len(sectors))

    def mass_erase(self, extended: bool) -> None:
        if extended:
            self._command(CMD_EXTENDED_ERASE, "Extended Erase")
            self.link.write(b"\xff\xff\x00")
        else:
            self._command(CMD_ERASE, "Erase")
            self.link.write(b"\xff\x00")
        self._ack("erase whole flash", timeout=60.0)

    def go(self, address: int) -> None:
        self._command(CMD_GO, "Go")
        self._send_address(address, "Go")

    def _command(self, cmd: int, what: str) -> None:
        self.link.flush_input()
        self.link.write(bytes([cmd, cmd ^ 0xFF]))
        self._ack(what)

    def _send_address(self, address: int, what: str) -> None:
        raw = address.to_bytes(4, "big")
        self.link.write(raw + bytes([_xor(raw)]))
        self._ack(f"{what}: address 0x{address:08X}")

    def _read(self, size: int, what: str) -> bytes:
        data = self.link.read_exact(size, 1.0 + self.link.transfer_time(size))
        if len(data) != size:
            raise BootloaderError(f"{what}: got {len(data)} of {size} bytes")
        return data

    def _ack(self, what: str, timeout: float = 1.0) -> None:
        reply = self.link.read_exact(1, timeout)
        if not reply:
            raise BootloaderError(f"{what}: no answer from bootloader")
        if reply[0] == NACK:
            raise BootloaderError(f"{what}: bootloader answered NACK")
        if reply[0] != ACK:
            raise BootloaderError(f"{what}: unexpected answer 0x{reply[0]:02X}")
