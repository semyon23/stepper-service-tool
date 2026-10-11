"""Сквозные сценарии с эмулятором платы."""
import sys
import threading

import pytest

import operations as ops
from board_emulator import BoardEmulator, make_test_firmware, serial_factory
from firmware import FLASH_BASE, load_firmware
from serial_link import SerialLink

needs_pty = pytest.mark.skipif(sys.platform == "win32", reason="нужен pty (Linux/macOS)")


@pytest.fixture(autouse=True)
def emulated_ports(monkeypatch):
    monkeypatch.setattr(SerialLink, "serial_class", staticmethod(serial_factory))
    monkeypatch.setattr(ops, "BOOT_DELAY", 0.3)


@pytest.fixture
def firmware(tmp_path):
    path = tmp_path / "firmware.bin"
    path.write_bytes(make_test_firmware(40 * 1024, seed=7))
    return load_firmware(path)


def _ctx(log=None, progress=None, cancel=None):
    return ops.Context(log or (lambda m: None), progress, cancel)


def _settings(emulator, echo=False):
    return ops.LinkSettings(emulator.port, 115200, echo)


@pytest.mark.parametrize("transport", ["fake", pytest.param("pty", marks=needs_pty)])
def test_scan_finds_board(transport):
    with BoardEmulator(device_id=5, transport=transport) as board:
        found = ops.scan_boards(_settings(board), 1, 8, _ctx())
    assert [s.device_id for s in found] == [5]


@pytest.mark.parametrize("transport", ["fake", pytest.param("pty", marks=needs_pty)])
def test_change_id_survives_flash_write_pause(transport):
    with BoardEmulator(device_id=1, id_save_delay=0.8, transport=transport) as board:
        status = ops.change_board_id(_settings(board), 1, 42, _ctx())
        assert status.device_id == 42
        assert board.stored_id == 42


def test_change_id_refused_while_moving():
    with BoardEmulator(device_id=3) as board:
        board.moving = True
        with pytest.raises(ops.OperationError, match="moving"):
            ops.change_board_id(_settings(board), 3, 4, _ctx())
        assert board.stored_id == 3


def test_flash_by_sectors_keeps_id(firmware):
    log = []
    with BoardEmulator(device_id=3) as board:
        opts = ops.FlashOptions(firmware, device_id=3)
        status = ops.flash_board(_settings(board), opts, _ctx(log.append))
        image = firmware.segments[0].data
        assert board.flash[:len(image)] == image
        assert board.erased_sectors == [0, 1, 2]
        assert board.stored_id == 3
        assert board.garbage_bytes == 0
        assert board.host_garbage_bytes == 0
    assert status is not None and status.device_id == 3
    assert any("STM32F411" in line for line in log)


def test_mass_erase_resets_id(firmware):
    with BoardEmulator(device_id=9) as board:
        opts = ops.FlashOptions(firmware, device_id=9, mass_erase=True)
        status = ops.flash_board(_settings(board), opts, _ctx())
        assert board.mass_erases == 1
    assert status.device_id == 1


def test_firmware_overlapping_eeprom_sector_is_refused_before_erase(tmp_path):
    path = tmp_path / "big.bin"
    path.write_bytes(make_test_firmware(130 * 1024))
    with BoardEmulator(device_id=6, flash_kb=256) as board:
        old_image = bytes(board.flash)
        opts = ops.FlashOptions(load_firmware(path), device_id=6)
        with pytest.raises(ops.OperationError, match="last flash sector(.|\n)*program on the board is untouched"):
            ops.flash_board(_settings(board), opts, _ctx())
        assert board.erased_sectors == []
        assert bytes(board.flash) == old_image


def test_flash_with_adapter_echo(firmware):
    with BoardEmulator(device_id=2, echo=True) as board:
        status = ops.flash_board(_settings(board, echo=True), ops.FlashOptions(firmware, device_id=2), _ctx())
    assert status.device_id == 2


def test_flash_when_already_in_bootloader(firmware):
    with BoardEmulator(device_id=4, start_in_bootloader=True) as board:
        opts = ops.FlashOptions(firmware, device_id=4, already_in_bootloader=True)
        status = ops.flash_board(_settings(board), opts, _ctx())
        assert board.jumps == 0
    assert status.device_id == 4


def test_flash_with_key_check_in_firmware(firmware):
    with BoardEmulator(device_id=2, require_key=True, reply_on_jump=True) as board:
        status = ops.flash_board(_settings(board), ops.FlashOptions(firmware, device_id=2), _ctx())
        assert board.jumps == 1
    assert status.device_id == 2


def test_verify_catches_bad_write(firmware):
    with BoardEmulator(device_id=1) as board:
        board.corrupt_write_at = FLASH_BASE + 12345
        with pytest.raises(ops.OperationError, match="0x08003000.*\n\n.*erased"):
            ops.flash_board(_settings(board), ops.FlashOptions(firmware, device_id=1), _ctx())


def test_board_not_found(firmware):
    with BoardEmulator(device_id=1) as board:
        with pytest.raises(ops.OperationError, match="ID 7 does not answer"):
            ops.flash_board(_settings(board), ops.FlashOptions(firmware, device_id=7), _ctx())
        assert board.jumps == 0


def test_cancel_during_write_explains_recovery(firmware):
    cancel = threading.Event()

    def progress(done, total, phase):
        if phase == "Writing" and done > total // 2:
            cancel.set()

    with BoardEmulator(device_id=1) as board:
        with pytest.raises(ops.Cancelled, match="already in bootloader"):
            ops.flash_board(_settings(board), ops.FlashOptions(firmware, device_id=1), _ctx(progress=progress, cancel=cancel))
        # плата осталась в загрузчике: повторная прошивка с галочкой доводит дело до конца
        opts = ops.FlashOptions(firmware, device_id=1, already_in_bootloader=True)
        status = ops.flash_board(_settings(board), opts, _ctx())
    assert status.device_id == 1
