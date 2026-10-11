"""Сверка протокола утилиты с исходниками прошивки (src/CommandManager.*).

Собирает настоящий ProtocolHandler с заглушкой Arduino.h, поэтому тест упадёт,
если формат кадра или StatusData в прошивке изменится.

Путь к проекту прошивки — переменная окружения STEPPERDRIVER_FIRMWARE (папка, где лежит
src/CommandManager.cpp). Без неё тест пропускается: утилите исходники прошивки не нужны.
"""
import os
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from board_protocol import (
    AXIS_GROUP, BOOTLOADER_KEY, CMD_CHANGE_ID_DEVICE, CMD_GET_FEEDBACK, CMD_GET_STATUS,
    CMD_HALT, CMD_JUMP_TO_BOOTLOADER, CMD_MOVE_ABSOLUTE, CMD_MOVE_CONTINUE, CMD_SET_POSITION,
    COMMAND_DATA_SIZE, FEEDBACK_DATA_SIZE, STATUS_DATA_SIZE, AxisCommand, build_frame,
    parse_feedback, parse_frame, parse_status,
)

HERE = Path(__file__).resolve().parent
FIRMWARE_ENV = "STEPPERDRIVER_FIRMWARE"


def _firmware_src() -> Path | None:
    root = os.environ.get(FIRMWARE_ENV)
    if not root:
        return None
    root = Path(root).expanduser()
    for src in (root / "src", root):          # можно указать и сам проект, и его папку src
        if (src / "CommandManager.cpp").is_file():
            return src
    return None


SRC = _firmware_src()
if SRC is None:
    reason = (f"{FIRMWARE_ENV} не задана" if not os.environ.get(FIRMWARE_ENV)
              else f"в {os.environ[FIRMWARE_ENV]} нет src/CommandManager.cpp")
    pytest.skip(f"сверка с прошивкой пропущена: {reason}", allow_module_level=True)


@pytest.fixture(scope="module")
def firmware_parser(tmp_path_factory):
    compiler = shutil.which("g++")
    if compiler is None:
        pytest.skip("нужен g++")
    binary = tmp_path_factory.mktemp("compat") / "compat"
    # заголовки — рядом с .cpp (старая раскладка) или в include/ (PlatformIO)
    includes = [HERE / "compat", SRC] + [d for d in (SRC.parent / "include",) if d.is_dir()]
    build = subprocess.run(
        [compiler, "-std=gnu++17", *(arg for d in includes for arg in ("-I", str(d))),
         str(HERE / "compat" / "compat_main.cpp"), str(SRC / "CommandManager.cpp"), "-o", str(binary)],
        capture_output=True, text=True,
    )
    if build.returncode != 0:
        pytest.fail(f"CommandManager.cpp из {SRC} не собирается с заглушкой Arduino.h:\n{build.stderr}")
    return binary


def _run(binary, data: bytes) -> list[str]:
    result = subprocess.run([str(binary), data.hex()], check=True, capture_output=True, text=True)
    return result.stdout.split("\n")


def test_requests_are_accepted_by_firmware_parser(firmware_parser):
    key = struct.pack("<I", BOOTLOADER_KEY)
    stream = (
        b"\x13\x37"                                     # мусор на линии
        + build_frame(7, CMD_GET_STATUS, b"\x00")
        + build_frame(3, CMD_GET_STATUS, b"\x00")       # чужая плата
        + build_frame(7, CMD_CHANGE_ID_DEVICE, bytes([42]))
        + build_frame(7, CMD_JUMP_TO_BOOTLOADER, key)
    )
    lines = _run(firmware_parser, stream)
    assert lines[:3] == [
        f"RX {CMD_GET_STATUS} 00",
        f"RX {CMD_CHANGE_ID_DEVICE} 2A",
        f"RX {CMD_JUMP_TO_BOOTLOADER} {key.hex().upper()}",
    ]


def test_corrupted_frame_is_rejected_by_firmware(firmware_parser):
    frame = bytearray(build_frame(7, CMD_CHANGE_ID_DEVICE, bytes([42])))
    frame[4] ^= 0xFF
    assert not [line for line in _run(firmware_parser, bytes(frame)) if line.startswith("RX")]


def test_status_frame_from_firmware_is_parsed(firmware_parser):
    tx_line = next(line for line in _run(firmware_parser, b"") if line.startswith("TX "))
    device_id, cmd, payload = parse_frame(bytes.fromhex(tx_line[3:]))
    assert (device_id, cmd, len(payload)) == (7, CMD_GET_STATUS, STATUS_DATA_SIZE)

    status = parse_status(device_id, payload)
    assert status.transaction_id == 42
    assert [a.position for a in status.axes] == [1.5, -2.25, 1000.0]
    assert [a.state_name for a in status.axes] == ["STANDSTILL", "DISCRETE_MOTION", "ERROR"]
    assert [a.is_home for a in status.axes] == [True, False, False]
    assert [a.is_powered for a in status.axes] == [False, True, False]
    assert [a.is_target for a in status.axes] == [False, False, True]
    assert status.in_motion and status.axes[1].in_motion
    assert [a.error_id for a in status.axes] == [0, 0, 3]


def test_command_data_is_decoded_by_firmware(firmware_parser):
    move = AxisCommand(axis=AXIS_GROUP, position=(1000.5, -250.25, 3.0),
                       velocity=5000.0, acceleration=50000.0, deceleration=40000.0)
    jog = AxisCommand(axis=1, velocity=1200.0, acceleration=8000.0, direction=(0, -1, 0))
    halt = AxisCommand(axis=2, deceleration=30000.0)
    assert len(move.pack(1)) == COMMAND_DATA_SIZE
    stream = (build_frame(7, CMD_MOVE_ABSOLUTE, move.pack(17))
              + build_frame(7, CMD_MOVE_CONTINUE, jog.pack(18))
              + build_frame(7, CMD_HALT, halt.pack(19))
              + build_frame(7, CMD_SET_POSITION, AxisCommand(axis=0, position=(-12.5, 0, 0)).pack(20)))
    lines = [line for line in _run(firmware_parser, stream) if line.startswith("CMD")]
    assert lines == [
        f"CMD {CMD_MOVE_ABSOLUTE} tid=17 axis=3 power=0 pos=1000.500,-250.250,3.000 v=5000.000 a=50000.000 d=40000.000 dir=0,0,0",
        f"CMD {CMD_MOVE_CONTINUE} tid=18 axis=1 power=0 pos=0.000,0.000,0.000 v=1200.000 a=8000.000 d=0.000 dir=0,-1,0",
        f"CMD {CMD_HALT} tid=19 axis=2 power=0 pos=0.000,0.000,0.000 v=0.000 a=0.000 d=30000.000 dir=0,0,0",
        f"CMD {CMD_SET_POSITION} tid=20 axis=0 power=0 pos=-12.500,0.000,0.000 v=0.000 a=0.000 d=0.000 dir=0,0,0",
    ]


def test_feedback_frame_from_firmware_is_parsed(firmware_parser):
    tx_line = next(line for line in _run(firmware_parser, b"") if line.startswith("TXF "))
    device_id, cmd, payload = parse_frame(bytes.fromhex(tx_line[4:]))
    assert (device_id, cmd, len(payload)) == (7, CMD_GET_FEEDBACK, FEEDBACK_DATA_SIZE)

    fb = parse_feedback(payload)
    assert [a.position for a in fb.axes] == [100.5, 201.0, 301.5]
    assert [a.feedback_position for a in fb.axes] == [100.0, 200.0, 300.0]
    assert [a.lag_error for a in fb.axes] == [0.0, -1.0, -2.0]
    assert [a.velocity for a in fb.axes] == [2500.25, 5000.5, 7500.75]
    assert [a.velocity_feedback for a in fb.axes] == [2500.0, 5000.0, 7500.0]
    assert [a.in_motion for a in fb.axes] == [False, True, False]
    assert [a.in_target for a in fb.axes] == [True, False, False]
    assert [a.error for a in fb.axes] == [False, False, True]
    assert [a.error_id for a in fb.axes] == [0, 0, 3]
    assert [a.state_name for a in fb.axes] == ["STANDSTILL", "CONTINUE_MOTION", "ERROR"]
    assert [a.negative_limit for a in fb.axes] == [False, False, True]
    assert [a.positive_limit for a in fb.axes] == [True, False, False]
    assert fb.axes[2].error_text == "limit −"
