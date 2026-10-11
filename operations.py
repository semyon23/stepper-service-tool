"""Сценарии работы с платой без привязки к GUI: поиск, смена ID, прошивка."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

import serial

from board_protocol import DEFAULT_DEVICE_ID, MAX_DEVICE_ID, MIN_DEVICE_ID, BoardClient, BoardStatus
from firmware import FLASH_BASE, Firmware, FirmwareError, f4_last_sector, f4_sectors, iter_chunks
from serial_link import LinkError, SerialLink
from stm32_bootloader import CMD_ERASE, CMD_EXTENDED_ERASE, BootloaderError, Stm32Bootloader, describe_device

BOOT_DELAY = 1.2        # setup() прошивки: delay(1000) до регистрации команд
BOOT_TIMEOUT = 5.0
ID_SAVE_TIMEOUT = 4.0   # запись ID стирает сектор flash, плата в это время молчит
F4_FLASH_SIZE_REG = 0x1FFF7A22


class OperationError(Exception):
    pass


class Cancelled(Exception):
    pass


EXPECTED_ERRORS = (OperationError, BootloaderError, LinkError, FirmwareError, serial.SerialException)


@dataclass
class LinkSettings:
    port: str
    baudrate: int = 115200
    echo: bool = False


@dataclass
class FlashOptions:
    firmware: Firmware
    device_id: int
    already_in_bootloader: bool = False
    mass_erase: bool = False
    verify: bool = True
    bootloader_baudrate: int = 115200


class Context:
    def __init__(self, log: Callable[[str], None] = print,
                 progress: Callable[[int, int, str], None] | None = None,
                 cancel_event: threading.Event | None = None):
        self.log = log
        self._progress = progress
        self.cancel_event = cancel_event or threading.Event()

    def progress(self, done: int, total: int, phase: str = "") -> None:
        if self._progress:
            self._progress(done, total, phase)

    def check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled("Cancelled")


def _check_id(device_id: int) -> None:
    if not MIN_DEVICE_ID <= device_id <= MAX_DEVICE_ID:
        raise OperationError(f"ID must be from {MIN_DEVICE_ID} to {MAX_DEVICE_ID}")


def _open(settings: LinkSettings) -> SerialLink:
    if not settings.port:
        raise OperationError("No port selected")
    return SerialLink(settings.port, settings.baudrate, settings.echo)


# ---------------------------------------------------------------- поиск и ID

def scan_boards(settings: LinkSettings, first_id: int, last_id: int, ctx: Context) -> list[BoardStatus]:
    found = []
    ids = range(first_id, last_id + 1)
    with _open(settings) as link:
        client = BoardClient(link)
        for n, device_id in enumerate(ids, 1):
            ctx.check_cancel()
            ctx.progress(n, len(ids), f"Checking ID {device_id}")
            status = client.get_status(device_id)
            if status:
                ctx.log(f"Found board ID {device_id}: {status.describe()}")
                found.append(status)
    ctx.log(f"Search done, boards found: {len(found)}")
    return found


def read_status(settings: LinkSettings, device_id: int, ctx: Context) -> BoardStatus:
    _check_id(device_id)
    with _open(settings) as link:
        status = BoardClient(link).get_status(device_id, attempts=3)
    if status is None:
        raise OperationError(f"Board ID {device_id} does not answer")
    ctx.log(f"Board ID {device_id}: {status.describe()}")
    return status


def change_board_id(settings: LinkSettings, old_id: int, new_id: int, ctx: Context) -> BoardStatus:
    _check_id(old_id)
    _check_id(new_id)
    if old_id == new_id:
        raise OperationError("New ID is the same as the current one")

    with _open(settings) as link:
        client = BoardClient(link)
        status = client.get_status(old_id, attempts=3)
        if status is None:
            raise OperationError(f"Board ID {old_id} does not answer")
        if client.get_status(new_id, attempts=2) is not None:
            raise OperationError(f"ID {new_id} is already used by another board")
        if status.in_motion:
            raise OperationError("Axes are moving — stop them before changing the ID")

        ctx.log(f"Changing ID {old_id} → {new_id}")
        if client.change_id(old_id, new_id) is None:
            ctx.log("Board did not confirm, checking the result…")

        deadline = time.monotonic() + ID_SAVE_TIMEOUT
        while time.monotonic() < deadline:
            ctx.check_cancel()
            status = client.get_status(new_id)
            if status:
                ctx.log(f"Done: board answers as ID {new_id}")
                return status
            time.sleep(0.05)

        if client.get_status(old_id, attempts=2):
            raise OperationError(f"Board kept ID {old_id}: the firmware rejected the change")
        raise OperationError("Board answers neither as the old nor as the new ID")


# ---------------------------------------------------------------- прошивка

_HINT_IN_BOOTLOADER = (
    "The program on the board is untouched, but the board is still in bootloader mode. "
    "Power-cycle the board to get back to work, or flash again "
    "with \"Board is already in bootloader\" checked."
)
_HINT_ERASED = (
    "Flash is already erased, so the board has no working program. It stays in bootloader mode: "
    "do not power it off and flash again with \"Board is already in bootloader\" checked. "
    "If power was lost, set the BOOT0 jumper, power-cycle the board and flash with the same box checked."
)


def flash_board(settings: LinkSettings, opts: FlashOptions, ctx: Context) -> BoardStatus | None:
    fw = opts.firmware
    if not opts.already_in_bootloader:
        _check_id(opts.device_id)
    hint = None
    try:
        with _open(settings) as link:
            if not opts.already_in_bootloader:
                _enter_bootloader(link, opts.device_id, ctx)
            hint = _HINT_IN_BOOTLOADER

            link.configure(opts.bootloader_baudrate, serial.PARITY_EVEN)
            bootloader = Stm32Bootloader(link)
            ctx.log("Connecting to the STM32 bootloader…")
            bootloader.sync()
            version, commands = bootloader.get()
            pid = bootloader.get_id()
            name, family = describe_device(pid)
            ctx.log(f"Bootloader v{version >> 4}.{version & 0xF}, MCU: {name}")

            plan = _plan_erase(bootloader, commands, family, fw, opts, ctx)
            ctx.check_cancel()
            hint = _HINT_ERASED
            _erase(bootloader, plan, ctx)
            _write(bootloader, fw, ctx)
            if opts.verify:
                _verify(bootloader, fw, ctx)

            if fw.start != FLASH_BASE:
                ctx.log(f"Warning: firmware does not start at 0x{FLASH_BASE:08X}")
            ctx.log(f"Starting firmware at 0x{fw.start:08X}")
            bootloader.go(fw.start)
            hint = None

            link.configure(settings.baudrate, serial.PARITY_NONE)
            expected_id = DEFAULT_DEVICE_ID if plan.sectors is None else opts.device_id
            return _wait_for_board(link, expected_id, ctx)
    except Cancelled as e:
        raise Cancelled(f"Flashing cancelled. {hint or ''}".strip()) from e
    except EXPECTED_ERRORS as e:
        if hint is None:
            raise
        raise OperationError(f"{e}\n\n{hint}") from e


def _enter_bootloader(link: SerialLink, device_id: int, ctx: Context) -> None:
    client = BoardClient(link)
    status = client.get_status(device_id, attempts=3)
    if status is None:
        raise OperationError(
            f"Board ID {device_id} does not answer. If the board is already in bootloader mode, "
            "check \"Board is already in bootloader\"."
        )
    if status.in_motion:
        raise OperationError("Axes are moving — stop them before flashing")
    ctx.log(f"Board ID {device_id} found, switching it to bootloader mode")
    if client.jump_to_bootloader(device_id):
        ctx.log("Board confirmed the switch to bootloader")
    time.sleep(0.1)


@dataclass
class ErasePlan:
    extended: bool
    sectors: list[int] | None  # None — стереть всю flash


def _plan_erase(bootloader: Stm32Bootloader, commands: bytes, family: str | None,
                fw: Firmware, opts: FlashOptions, ctx: Context) -> ErasePlan:
    """Все проверки до стирания: если что-то не так, программа на плате останется целой."""
    extended = CMD_EXTENDED_ERASE in commands
    if not extended and CMD_ERASE not in commands:
        raise OperationError("Bootloader does not support erase commands")
    if family != "f4":
        if not opts.mass_erase:
            raise OperationError(
                "Sector erase works only for STM32F2/F4. "
                "Choose \"Erase: whole flash\"."
            )
        return ErasePlan(extended, None)

    sectors = f4_sectors(fw.start, fw.end)
    flash_kb = _read_flash_size(bootloader, ctx)
    if flash_kb:
        if fw.end > FLASH_BASE + flash_kb * 1024:
            raise OperationError(f"Firmware ({fw.size} bytes) does not fit into {flash_kb} KB of flash")
        last = f4_last_sector(flash_kb)
        if last in sectors:
            raise OperationError(
                f"Firmware reaches the last flash sector ({last}). STM32duino keeps the EEPROM "
                "with the board ID there, so the first ID write would break the firmware. "
                "Make the firmware smaller or move the EEPROM."
            )
    return ErasePlan(extended, None if opts.mass_erase else sectors)


def _erase(bootloader: Stm32Bootloader, plan: ErasePlan, ctx: Context) -> None:
    if plan.sectors is None:
        ctx.log("Erasing whole flash (up to a minute)…")
        ctx.progress(0, 1, "Erasing whole flash")
        bootloader.mass_erase(plan.extended)
        ctx.progress(1, 1, "Erasing whole flash")
        return

    ctx.log(f"Erasing sectors: {', '.join(map(str, plan.sectors))}")
    for n, sector in enumerate(plan.sectors):
        ctx.check_cancel()
        ctx.progress(n, len(plan.sectors), f"Erasing sector {sector}")
        bootloader.erase_sectors([sector], plan.extended)
    ctx.progress(len(plan.sectors), len(plan.sectors), "Erasing")


def _read_flash_size(bootloader: Stm32Bootloader, ctx: Context) -> int | None:
    try:
        flash_kb = int.from_bytes(bootloader.read_memory(F4_FLASH_SIZE_REG, 2), "little")
    except BootloaderError:
        ctx.log("Could not read flash size, ID sector check skipped")
        return None
    if not 0 < flash_kb <= 2048:
        return None
    ctx.log(f"Flash size: {flash_kb} KB")
    return flash_kb


def _write(bootloader: Stm32Bootloader, fw: Firmware, ctx: Context) -> None:
    chunks = list(iter_chunks(fw))
    total = sum(len(data) for _, data in chunks)
    done = 0
    ctx.log(f"Writing {fw.size} bytes…")
    for address, data in chunks:
        ctx.check_cancel()
        bootloader.write_memory(address, data)
        done += len(data)
        ctx.progress(done, total, "Writing")


def _verify(bootloader: Stm32Bootloader, fw: Firmware, ctx: Context) -> None:
    chunks = list(iter_chunks(fw))
    total = sum(len(data) for _, data in chunks)
    done = 0
    ctx.log("Verifying…")
    for address, data in chunks:
        ctx.check_cancel()
        if bootloader.read_memory(address, len(data)) != data:
            raise OperationError(f"Verify failed: flash data differs at 0x{address:08X}")
        done += len(data)
        ctx.progress(done, total, "Verifying")
    ctx.log("Verify passed")


def _wait_for_board(link: SerialLink, device_id: int, ctx: Context) -> BoardStatus | None:
    ctx.log("Waiting for the firmware to start…")
    time.sleep(BOOT_DELAY)
    client = BoardClient(link)
    deadline = time.monotonic() + BOOT_TIMEOUT
    while time.monotonic() < deadline:
        status = client.get_status(device_id)
        if status:
            ctx.log(f"Firmware is running, board answers as ID {device_id}")
            return status
    ctx.log(
        f"Board did not answer as ID {device_id}. If the BOOT0 jumper is set, remove it and "
        "power-cycle the board, then find it with Search on the \"Board ID\" tab."
    )
    return None
