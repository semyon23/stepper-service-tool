"""Загрузка файла прошивки (.bin / Intel HEX) и раскладка flash STM32F2/F4."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

FLASH_BASE = 0x08000000

# Сектора STM32F2/F4 (первые 1 МБ): 4×16 КБ, 64 КБ, 7×128 КБ
F4_SECTOR_SIZES = [16 * 1024] * 4 + [64 * 1024] + [128 * 1024] * 7

_SRAM_START, _SRAM_END = 0x10000000, 0x20100000  # CCM + SRAM


class FirmwareError(Exception):
    pass


@dataclass
class Segment:
    address: int
    data: bytes

    @property
    def end(self) -> int:
        return self.address + len(self.data)


@dataclass
class Firmware:
    path: str
    segments: list[Segment]

    @property
    def start(self) -> int:
        return self.segments[0].address

    @property
    def end(self) -> int:
        return self.segments[-1].end

    @property
    def size(self) -> int:
        return sum(len(s.data) for s in self.segments)

    def warnings(self) -> list[str]:
        """Проверка таблицы векторов: защита от прошивки не того файла."""
        first = self.segments[0]
        if len(first.data) < 8:
            return ["file is too short for a vector table"]
        sp, reset = struct.unpack_from("<II", first.data)
        result = []
        if not _SRAM_START <= sp <= _SRAM_END:
            result.append(f"initial SP 0x{sp:08X} does not point to RAM — maybe this is not STM32 firmware")
        if not reset & 1 or not self.start <= reset < self.end:
            result.append(f"Reset vector 0x{reset:08X} is outside the firmware — maybe a wrong load address")
        return result


def load_firmware(path: str | Path, bin_address: int = FLASH_BASE) -> Firmware:
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise FirmwareError(f"Cannot read {path}: {e.strerror}") from e
    if path.suffix.lower() in (".hex", ".ihex"):
        segments = _parse_hex(raw.decode("ascii", errors="replace"))
    else:
        segments = [Segment(bin_address, raw)]
    segments = [s for s in segments if s.data]
    if not segments:
        raise FirmwareError("Firmware file is empty")
    return Firmware(str(path), segments)


def _parse_hex(text: str) -> list[Segment]:
    records = []
    base = 0
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            if not line.startswith(":"):
                raise ValueError("line must start with ':'")
            raw = bytes.fromhex(line[1:])
            if len(raw) < 5 or len(raw) != raw[0] + 5:
                raise ValueError("wrong record length")
            if sum(raw) & 0xFF:
                raise ValueError("wrong checksum")
        except ValueError as e:
            raise FirmwareError(f"HEX line {lineno}: {e}") from e
        address, record_type, data = int.from_bytes(raw[1:3], "big"), raw[3], raw[4:-1]
        if record_type == 0x00:
            records.append((base + address, data))
        elif record_type == 0x01:
            break
        elif record_type == 0x02:
            base = int.from_bytes(data, "big") << 4
        elif record_type == 0x04:
            base = int.from_bytes(data, "big") << 16
        elif record_type not in (0x03, 0x05):  # адрес старта не нужен
            raise FirmwareError(f"HEX line {lineno}: unknown record type {record_type:02X}")

    segments: list[Segment] = []
    for address, data in sorted(records):
        if segments and address < segments[-1].end:
            raise FirmwareError(f"HEX: records overlap at 0x{address:08X}")
        if segments and address == segments[-1].end:
            segments[-1] = Segment(segments[-1].address, segments[-1].data + data)
        else:
            segments.append(Segment(address, bytes(data)))
    return segments


def iter_chunks(firmware: Firmware, size: int = 256):
    """Блоки для Write Memory: адрес и длина кратны 4, хвосты дополнены 0xFF."""
    for segment in firmware.segments:
        start = segment.address & ~3
        data = b"\xff" * (segment.address - start) + segment.data
        data += b"\xff" * (-len(data) % 4)
        for offset in range(0, len(data), size):
            yield start + offset, data[offset:offset + size]


def f4_sectors(start: int, end: int) -> list[int]:
    if start < FLASH_BASE:
        raise FirmwareError(f"Address 0x{start:08X} is below flash start 0x{FLASH_BASE:08X}")
    sectors = []
    address = FLASH_BASE
    for index, size in enumerate(F4_SECTOR_SIZES):
        if address < end and address + size > start:
            sectors.append(index)
        address += size
    if end > address:
        raise FirmwareError("Firmware goes beyond the first megabyte of flash")
    return sectors


def f4_last_sector(flash_kb: int) -> int | None:
    """Последний сектор — там STM32duino хранит эмуляцию EEPROM (а в ней ID платы)."""
    total = 0
    for index, size in enumerate(F4_SECTOR_SIZES):
        total += size
        if total >= flash_kb * 1024:
            return index
    return None
