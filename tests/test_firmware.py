import pytest

from board_emulator import make_test_firmware
from firmware import FLASH_BASE, FirmwareError, f4_last_sector, f4_sectors, iter_chunks, load_firmware


def _hex_record(address: int, record_type: int, data: bytes) -> str:
    raw = bytes([len(data)]) + address.to_bytes(2, "big") + bytes([record_type]) + data
    return ":" + (raw + bytes([-sum(raw) & 0xFF])).hex().upper()


def _to_hex(base: int, data: bytes) -> str:
    lines = [_hex_record(0, 0x04, (base >> 16).to_bytes(2, "big"))]
    for offset in range(0, len(data), 16):
        lines.append(_hex_record((base & 0xFFFF) + offset, 0x00, data[offset:offset + 16]))
    lines.append(_hex_record(0, 0x05, (base + 0x199).to_bytes(4, "big")))
    lines.append(_hex_record(0, 0x01, b""))
    return "\n".join(lines) + "\n"


def test_bin_and_hex_give_same_image(tmp_path):
    image = make_test_firmware(5000)
    (tmp_path / "fw.bin").write_bytes(image)
    (tmp_path / "fw.hex").write_text(_to_hex(FLASH_BASE, image))

    from_bin = load_firmware(tmp_path / "fw.bin")
    from_hex = load_firmware(tmp_path / "fw.hex")
    assert from_bin.segments[0].data == from_hex.segments[0].data == image
    assert from_hex.start == FLASH_BASE and from_hex.end == FLASH_BASE + 5000
    assert from_bin.warnings() == []


def test_hex_checksum_error(tmp_path):
    text = _to_hex(FLASH_BASE, make_test_firmware(64)).replace(":10", ":11", 1)
    (tmp_path / "bad.hex").write_text(text)
    with pytest.raises(FirmwareError, match="line 2"):
        load_firmware(tmp_path / "bad.hex")


def test_wrong_file_is_flagged(tmp_path):
    (tmp_path / "text.bin").write_bytes(b"hello world, definitely not firmware")
    warnings = load_firmware(tmp_path / "text.bin").warnings()
    assert len(warnings) == 2


def test_chunks_are_aligned_and_padded(tmp_path):
    (tmp_path / "odd.bin").write_bytes(make_test_firmware(1001))
    chunks = list(iter_chunks(load_firmware(tmp_path / "odd.bin")))
    assert all(addr % 4 == 0 and len(data) % 4 == 0 and len(data) <= 256 for addr, data in chunks)
    assert sum(len(data) for _, data in chunks) == 1004
    assert chunks[-1][1].endswith(b"\xff\xff\xff")


def test_f4_sector_map():
    assert f4_sectors(FLASH_BASE, FLASH_BASE + 40 * 1024) == [0, 1, 2]
    assert f4_sectors(FLASH_BASE, FLASH_BASE + 64 * 1024) == [0, 1, 2, 3]
    assert f4_sectors(FLASH_BASE, FLASH_BASE + 64 * 1024 + 1) == [0, 1, 2, 3, 4]
    assert f4_sectors(FLASH_BASE + 0x20000, FLASH_BASE + 0x20010) == [5]
    assert f4_last_sector(128) == 4
    assert f4_last_sector(256) == 5
    assert f4_last_sector(512) == 7
    assert f4_last_sector(1024) == 11
