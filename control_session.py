"""Постоянное подключение к плате для управления осями.

Поток сеанса владеет портом: опрашивает GET_FEEDBACK с заданным периодом, раз в
несколько опросов — GET_STATUS (питание и «дом» есть только там), а между опросами
сразу выполняет команды из очереди. Результаты отдаются колбэками из потока сеанса.
"""
from __future__ import annotations

import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

import serial

from board_protocol import TRANSACTION_COMMANDS, AxisCommand, BoardClient, BoardFeedback, BoardStatus
from serial_link import LinkError, SerialLink

POLL_INTERVAL = 0.05     # GET_FEEDBACK каждые 50 мс
STATUS_EVERY = 10        # GET_STATUS раз в 10 опросов: питание и «дом» есть только в StatusData
COMMAND_ATTEMPTS = 3     # повтор с тем же transaction_id: плата не выполнит команду дважды
LOST_AFTER = 10          # столько потерянных опросов подряд — «нет связи»


@dataclass
class SessionCallbacks:
    feedback: Callable[[BoardFeedback], None]
    status: Callable[[BoardStatus], None]
    link: Callable[[float, int, bool], None]           # ответов в секунду, потерь подряд, связь есть
    command_done: Callable[[str, bool, str], None]     # подпись команды, принята, пояснение
    stopped: Callable[[str], None]                     # причина остановки, "" — штатно


@dataclass
class _Request:
    cmd: int
    command: AxisCommand
    label: str


class ControlSession:
    def __init__(self, port: str, baudrate: int, echo: bool, device_id: int,
                 callbacks: SessionCallbacks, poll_interval: float = POLL_INTERVAL):
        self.port = port
        self.baudrate = baudrate
        self.echo = echo
        self.device_id = device_id
        self.poll_interval = poll_interval
        self._cb = callbacks
        self._requests: queue.Queue[_Request] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._board_tid: int | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._board_tid = None
        self._thread = threading.Thread(target=self._run, name="board-session", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        """Останавливает сеанс, предварительно отправив всё, что уже стоит в очереди."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def set_device_id(self, device_id: int) -> None:
        self.device_id = device_id
        self._board_tid = None   # у другой платы свой счётчик транзакций

    def submit(self, cmd: int, command: AxisCommand, label: str) -> None:
        self._requests.put(_Request(cmd, command, label))

    # ------------------------------------------------------------ поток сеанса

    def _run(self) -> None:
        reason = ""
        try:
            with SerialLink(self.port, self.baudrate, self.echo) as link:
                self._loop(BoardClient(link))
        except (LinkError, serial.SerialException, OSError) as e:
            reason = str(e)
        except Exception as e:
            reason = f"Session internal error: {e!r}"
        self._cb.stopped(reason)

    def _loop(self, client: BoardClient) -> None:
        next_poll = time.monotonic()
        polls = 0
        lost = 0
        answered: deque[float] = deque()
        while True:
            if self._stop.is_set():
                self._drain(client)
                return
            try:
                request = self._requests.get(timeout=max(0.0, next_poll - time.monotonic()))
            except queue.Empty:
                request = None
            if request is not None:
                self._execute(client, request)
                continue

            now = time.monotonic()
            next_poll = max(next_poll + self.poll_interval, now)   # пропущенные опросы не догоняем
            if self._board_tid is None or polls % STATUS_EVERY == 0:
                self._read_status(client)
            requested = time.monotonic()   # после GET_STATUS: плата снимает показания по этому запросу
            feedback = client.get_feedback(self.device_id)
            polls += 1
            if feedback is not None:
                feedback.timestamp = requested
                lost = 0
                answered.append(now)
                self._cb.feedback(feedback)
            else:
                lost += 1
            while answered and now - answered[0] > 1.0:
                answered.popleft()
            self._cb.link(float(len(answered)), lost, lost < LOST_AFTER)

    def _drain(self, client: BoardClient) -> None:
        while True:
            try:
                self._execute(client, self._requests.get_nowait())
            except queue.Empty:
                return

    def _read_status(self, client: BoardClient) -> BoardStatus | None:
        status = client.get_status(self.device_id)
        if status is not None:
            self._board_tid = status.transaction_id
            self._cb.status(status)
        return status

    def _execute(self, client: BoardClient, request: _Request) -> None:
        if self._board_tid is None and self._read_status(client) is None:
            self._cb.command_done(request.label, False, "board does not answer")
            return
        # следующий номер после последнего, который видела плата; 0 не используем
        tid = self._board_tid % 255 + 1
        for _ in range(COMMAND_ATTEMPTS):
            status = client.command(self.device_id, request.cmd, request.command, tid)
            if status is None:
                continue
            self._board_tid = status.transaction_id
            self._cb.status(status)
            if request.cmd in TRANSACTION_COMMANDS and status.transaction_id != tid:
                self._cb.command_done(request.label, False, "board dropped it as a repeat")
            else:
                self._cb.command_done(request.label, True, "")
            return
        self._cb.command_done(request.label, False, "no answer")
