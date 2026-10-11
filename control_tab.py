"""Вкладка Control: состояние осей по GET_FEEDBACK и команды по протоколу платы."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton,
    QSpinBox, QVBoxLayout, QWidget,
)

import operations as ops
from axis_trace import TraceBuffer
from board_protocol import (
    AXIS_GROUP, AXIS_NAMES, CMD_EMERGENCY_STOP, CMD_HALT, CMD_MOVE_ABSOLUTE, CMD_MOVE_CONTINUE, CMD_MOVE_HOME,
    CMD_MOVE_RELATIVE, CMD_POWER_ENABLE, CMD_RESET_ERROR, CMD_SET_POSITION, HOMING_DIRECT,
    HOMING_NEGATIVE_LIMIT, HOMING_POSITIVE_LIMIT, MAX_DEVICE_ID, MIN_DEVICE_ID, STATE_CONTINUE_MOTION,
    STATE_DISABLED, STATE_DISCRETE_MOTION, STATE_ERROR, STATE_HOMING, STATE_STANDSTILL,
    STATE_STOPPING, AxisCommand, BoardFeedback, BoardStatus,
)
from control_session import POLL_INTERVAL, ControlSession, SessionCallbacks
from plot_window import PlotWindow

MAX_STEP_RATE = 50000.0   # PulseDriver выдаёт не больше одного шага за тик 50 кГц
FIELD_WIDTH = 100         # у каждой оси три колонки: поле и две кнопки — все строки ложатся в одну сетку;
BUTTON_WIDTH = 56         # это минимальные ширины, свободное место колонки делят 3 : 2 : 2
GREEN, BLUE, ORANGE, RED, GRAY = "#2e9e44", "#1f6fd1", "#d68910", "#c0392b", "#888888"
STATE_COLORS = {
    STATE_DISABLED: GRAY, STATE_STANDSTILL: GREEN, STATE_DISCRETE_MOTION: BLUE,
    STATE_CONTINUE_MOTION: BLUE, STATE_HOMING: BLUE, STATE_STOPPING: ORANGE, STATE_ERROR: RED,
}
ESTOP_LABEL = "E-STOP"
ESTOP_TOOLTIP = ("Emergency stop (EMERGENCY_STOP): all axes stop at once and the drivers are switched off.\n"
                 "Position may be lost. To continue: Reset errors, then Power on.")
HOMING_MODES = [("to limit −", HOMING_NEGATIVE_LIMIT), ("to limit +", HOMING_POSITIVE_LIMIT),
                ("set here", HOMING_DIRECT)]


class SessionBridge(QObject):
    """Переносит колбэки потока сеанса в поток окна. Первый аргумент — номер сеанса:
    сигналы уже остановленного сеанса могут прийти позже и должны игнорироваться."""
    feedback = Signal(int, object)
    status = Signal(int, object)
    link = Signal(int, float, int, bool)
    command_done = Signal(int, str, bool, str)
    stopped = Signal(int, str)

    def callbacks(self, token: int) -> SessionCallbacks:
        return SessionCallbacks(
            feedback=lambda fb: self.feedback.emit(token, fb),
            status=lambda st: self.status.emit(token, st),
            link=lambda rate, lost, ok: self.link.emit(token, rate, lost, ok),
            command_done=lambda label, ok, why: self.command_done.emit(token, label, ok, why),
            stopped=lambda reason: self.stopped.emit(token, reason),
        )


class Lamp(QLabel):
    """Индикатор «● подпись»; перерисовывается только при изменении."""

    def __init__(self, text: str, color: str = GREEN):
        super().__init__()
        self._text = text
        self._color = color
        self._on: bool | None = object()
        self.set(None)

    def set(self, on: bool | None) -> None:
        if on == self._on:
            return
        self._on = on
        self.setText(("● " if on else "○ ") + self._text)
        self.setStyleSheet(f"color: {self._color}" if on else f"color: {GRAY}")


def _number(minimum: float, maximum: float, value: float, decimals: int = 2, suffix: str = "",
            width: int = 95) -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setRange(minimum, maximum)
    spin.setDecimals(decimals)
    spin.setValue(value)
    spin.setSuffix(suffix)
    spin.setKeyboardTracking(False)
    spin.setMinimumWidth(width)
    return spin


def _button(text: str, width: int = BUTTON_WIDTH) -> QPushButton:
    button = QPushButton(text)
    button.setMinimumWidth(width)
    return button


def _field(minimum: float, maximum: float) -> QDoubleSpinBox:
    spin = _number(minimum, maximum, 0.0)
    spin.setMinimumWidth(FIELD_WIDTH)
    return spin


def _row(*widgets, center: bool = False) -> QWidget:
    holder = QWidget()
    line = QHBoxLayout(holder)
    line.setContentsMargins(0, 0, 0, 0)
    line.setSpacing(4)
    if center:
        line.addStretch(1)
    for widget in widgets:
        line.addWidget(widget)
    line.addStretch(1)
    return holder


def _fmt(value: float, decimals: int) -> str:
    return f"{value:,.{decimals}f}"


class AxisWidgets:
    """Показания и кнопки одной оси."""

    def __init__(self, index: int):
        self.index = index
        self.name = AXIS_NAMES[index]
        self.state = QLabel("—")
        font = self.state.font()
        font.setBold(True)
        self.state.setFont(font)
        self.error = QLabel("")
        self.error.setStyleSheet(f"color: {RED}")
        self.position = QLabel("—")
        self.feedback = QLabel("")
        self.feedback.setStyleSheet(f"color: {GRAY}")
        self.feedback.setToolTip("Feedback position")
        self.velocity = QLabel("—")
        self.lag = QLabel("")
        self.lag.setStyleSheet(f"color: {GRAY}")
        self.lag.setToolTip("Lag error, steps")
        self.powered = Lamp("power")
        self.home = Lamp("homed")
        self.in_motion = Lamp("moving", BLUE)
        self.in_target = Lamp("at target")
        self.limit_neg = Lamp("limit −", RED)
        self.limit_pos = Lamp("limit +", RED)

        self.power_on = _button("On")
        self.power_off = _button("Off")
        self.jog_neg = _button("◀ −")
        self.jog_pos = _button("+ ▶")
        for button in (self.jog_neg, self.jog_pos):
            button.setToolTip("Moves while the button is held")
        self.halt = _button("Stop", FIELD_WIDTH)
        self.target = _field(-1e9, 1e9)
        self.move_abs = _button("Abs")
        self.move_abs.setToolTip("Move to this position")
        self.move_rel = _button("Rel")
        self.move_rel.setToolTip("Move by this distance")
        self.new_position = _field(-1e9, 1e9)
        self.set_position = _button("Set")
        self.set_position.setToolTip("Tell the board that the axis is at this position now (no motion)")
        self.homing_mode = QComboBox()
        self.homing_mode.setMinimumWidth(FIELD_WIDTH)
        for label, mode in HOMING_MODES:
            self.homing_mode.addItem(label, mode)
        self.homing_offset = _field(-1e9, 1e9)
        self.homing_offset.setToolTip("Position value to set at home")
        self.home_start = _button("Start")

    def buttons(self) -> list[QWidget]:
        return [self.power_on, self.power_off, self.jog_neg, self.jog_pos, self.move_abs, self.move_rel,
                self.halt, self.set_position, self.home_start]

    def show_feedback(self, fb) -> None:
        self.state.setText(fb.state_name)
        self.state.setStyleSheet(f"color: {STATE_COLORS.get(fb.state, GRAY)}")
        self.error.setText(fb.error_text)
        self.position.setText(_fmt(fb.position, 2))
        self.feedback.setText(f"fb {_fmt(fb.feedback_position, 0)}")
        self.velocity.setText(f"{_fmt(fb.velocity, 1)} /s")
        self.lag.setText(f"lag {fb.lag_error:.0f}")
        self.in_motion.set(fb.in_motion)
        self.in_target.set(fb.in_target)
        self.limit_neg.set(fb.negative_limit)
        self.limit_pos.set(fb.positive_limit)

    def show_status(self, st) -> None:
        self.powered.set(st.is_powered)
        self.home.set(st.is_home)

    def clear(self) -> None:
        for label in (self.state, self.position, self.velocity):
            label.setText("—")
        for label in (self.error, self.feedback, self.lag):
            label.setText("")
        self.state.setStyleSheet("")
        for lamp in (self.powered, self.home, self.in_motion, self.in_target, self.limit_neg, self.limit_pos):
            lamp.set(None)


class ControlTab(QWidget):
    log = Signal(str)
    connection_changed = Signal(bool)

    def __init__(self, link_settings: Callable[[], ops.LinkSettings | None], parent=None):
        super().__init__(parent)
        self._link_settings = link_settings
        self._session: ControlSession | None = None
        self._token = 0
        self._bridge = SessionBridge(self)
        self._bridge.feedback.connect(self._on_feedback)
        self._bridge.status.connect(self._on_status)
        self._bridge.link.connect(self._on_link)
        self._bridge.command_done.connect(self._on_command_done)
        self._bridge.stopped.connect(self._on_stopped)
        self._feedback: BoardFeedback | None = None
        self._status: BoardStatus | None = None       # питание есть только в StatusData
        self._paused = False
        self._busy = False
        self._jogging: set[int] = set()
        # запись для графика ведётся всегда, пока идёт опрос: открыл окно — история уже есть
        self.trace = TraceBuffer()
        self._trace_board: int | None = None
        self.plot = PlotWindow(self.trace)
        self.plot.closed.connect(lambda: self.plot_check.setChecked(False))
        self.plot.sample_period_changed.connect(lambda _: self._apply_poll_interval())
        self.plot.emergency_stop.connect(self.emergency_stop)
        self.plot.log.connect(self.log)

        self.axes = [AxisWidgets(i) for i in range(len(AXIS_NAMES))]
        layout = QVBoxLayout(self)
        layout.addLayout(self._build_top_row())
        layout.addLayout(self._build_params())
        layout.addWidget(self._build_axes())
        layout.addWidget(self._build_group())
        layout.addStretch(1)
        self._update_enabled()

    # ------------------------------------------------------------ интерфейс

    def _build_top_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.id_spin = QSpinBox()
        self.id_spin.setRange(MIN_DEVICE_ID, MAX_DEVICE_ID)
        self.id_spin.valueChanged.connect(self._id_spin_changed)
        self.connect_button = QPushButton("Connect")
        self.connect_button.setCheckable(True)
        self.connect_button.setMinimumWidth(95)
        self.connect_button.clicked.connect(self._toggle_connection)
        self.link_label = QLabel("not connected")
        self.link_label.setStyleSheet(f"color: {GRAY}")
        self.estop_button = QPushButton("E-STOP  (Esc)")
        self.estop_button.setToolTip(ESTOP_TOOLTIP)
        self.estop_button.setStyleSheet(f"background: {RED}; color: white; font-weight: bold; padding: 5px 14px")
        self.estop_button.clicked.connect(self.emergency_stop)
        row.addWidget(QLabel("Board ID:"))
        row.addWidget(self.id_spin)
        row.addWidget(self.connect_button)
        row.addWidget(self.link_label)
        row.addStretch(1)
        self.plot_check = QCheckBox("Plot")
        self.plot_check.setToolTip("Open the plot window: position, speed and lag error of the axes")
        self.plot_check.toggled.connect(self._toggle_plot)
        row.addWidget(self.plot_check)
        row.addSpacing(12)
        row.addWidget(self.estop_button)
        return row

    def _build_params(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.velocity = _number(0.01, MAX_STEP_RATE, 5000.0, suffix=" steps/s", width=130)
        self.acceleration = _number(0.01, 1e7, 50000.0, suffix=" steps/s²", width=140)
        self.deceleration = _number(0.01, 1e7, 50000.0, suffix=" steps/s²", width=140)
        self.deceleration.setToolTip("Also used by Stop and jog release")
        for label, widget in (("Speed:", self.velocity), ("Accel:", self.acceleration),
                              ("Decel:", self.deceleration)):
            row.addWidget(QLabel(label))
            row.addWidget(widget, 1)
        return row

    def _build_axes(self) -> QGroupBox:
        box = QGroupBox("Axes")
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(3)

        # У каждой оси 4 колонки сетки: поле, кнопка, кнопка, промежуток до следующей оси.
        # Строка — список (виджет, подколонка, сколько подколонок занять).
        rows = [
            ("State", lambda a: [(_row(a.state, a.error, center=True), 0, 3)]),
            ("Position", lambda a: [(_row(a.position, a.feedback, center=True), 0, 3)]),
            ("Speed", lambda a: [(_row(a.velocity, a.lag, center=True), 0, 3)]),
            ("Motion", lambda a: [(_row(a.in_motion, a.in_target, center=True), 0, 3)]),
            ("Limits", lambda a: [(_row(a.limit_neg, a.limit_pos, center=True), 0, 3)]),
            None,
            ("Power", lambda a: [(a.powered, 0, 1), (a.power_on, 1, 1), (a.power_off, 2, 1)]),
            ("Stop / jog", lambda a: [(a.halt, 0, 1), (a.jog_neg, 1, 1), (a.jog_pos, 2, 1)]),
            ("Move", lambda a: [(a.target, 0, 1), (a.move_abs, 1, 1), (a.move_rel, 2, 1)]),
            ("Set position", lambda a: [(a.new_position, 0, 1), (a.set_position, 1, 1)]),
            ("Home", lambda a: [(a.homing_mode, 0, 1), (a.home_start, 1, 1)]),
            ("Home position", lambda a: [(a.homing_offset, 0, 1), (a.home, 1, 2)]),
        ]

        def first_column(axis: AxisWidgets) -> int:
            return 1 + axis.index * 4

        for axis in self.axes:
            title = QLabel(f"Axis {axis.name}")
            font = title.font()
            font.setBold(True)
            title.setFont(font)
            grid.addWidget(title, 0, first_column(axis), 1, 3, Qt.AlignHCenter)
            for sub, stretch in ((0, 3), (1, 2), (2, 2)):
                grid.setColumnStretch(first_column(axis) + sub, stretch)
            if axis.index < len(self.axes) - 1:
                grid.setColumnMinimumWidth(first_column(axis) + 3, 18)   # промежуток между осями
        for row, entry in enumerate(rows, 1):
            if entry is None:
                grid.setRowMinimumHeight(row, 8)
                continue
            label, cells_of = entry
            grid.addWidget(QLabel(label), row, 0)
            for axis in self.axes:
                for widget, sub, span in cells_of(axis):
                    grid.addWidget(widget, row, first_column(axis) + sub, 1, span)

        for axis in self.axes:
            i = axis.index
            axis.power_on.clicked.connect(lambda _=False, i=i: self._power(i, True))
            axis.power_off.clicked.connect(lambda _=False, i=i: self._power(i, False))
            axis.jog_neg.pressed.connect(lambda i=i: self._jog_start(i, -1))
            axis.jog_pos.pressed.connect(lambda i=i: self._jog_start(i, 1))
            axis.jog_neg.released.connect(lambda i=i: self._jog_stop(i))
            axis.jog_pos.released.connect(lambda i=i: self._jog_stop(i))
            axis.move_abs.clicked.connect(lambda _=False, i=i: self._move(i, absolute=True))
            axis.move_rel.clicked.connect(lambda _=False, i=i: self._move(i, absolute=False))
            axis.halt.clicked.connect(lambda _=False, i=i: self._halt(i))
            axis.set_position.clicked.connect(lambda _=False, i=i: self._set_position(i))
            axis.home_start.clicked.connect(lambda _=False, i=i: self._home(i))
        return box

    def _build_group(self) -> QGroupBox:
        box = QGroupBox("All axes")
        row = QHBoxLayout(box)
        self.group_targets = [_number(-1e9, 1e9, 0.0, width=90) for _ in AXIS_NAMES]
        for name, spin in zip(AXIS_NAMES, self.group_targets):
            row.addWidget(QLabel(f"{name}:"))
            row.addWidget(spin, 1)
        self.group_abs = QPushButton("Abs all")
        self.group_rel = QPushButton("Rel all")
        for button in (self.group_abs, self.group_rel):
            button.setToolTip("Straight-line move of all axes (the board scales the speed of each axis). "
                              "Sent only when all axes are stopped.")
        self.group_abs.clicked.connect(lambda: self._group_move(absolute=True))
        self.group_rel.clicked.connect(lambda: self._group_move(absolute=False))
        self.group_power_on = QPushButton("Power on all")
        self.group_power_off = QPushButton("Power off all")
        self.reset_errors = QPushButton("Reset errors")
        self.group_power_on.clicked.connect(lambda: self._power(AXIS_GROUP, True))
        self.group_power_off.clicked.connect(lambda: self._power(AXIS_GROUP, False))
        self.reset_errors.clicked.connect(self._reset_errors)
        for button in (self.group_abs, self.group_rel):
            row.addWidget(button, 1)
        row.addSpacing(12)
        for button in (self.group_power_on, self.group_power_off, self.reset_errors):
            row.addWidget(button, 1)
        return box

    # ------------------------------------------------------------ для главного окна

    @property
    def connected(self) -> bool:
        return self._session is not None

    def device_id(self) -> int:
        return self.id_spin.value()

    def set_device_id(self, device_id: int) -> None:
        self.id_spin.setValue(device_id)

    def pause(self) -> bool:
        """Освобождает порт на время операции другой вкладки. True — сеанс был."""
        if not self.connected:
            return False
        self._stop_session()
        self._paused = True
        self.link_label.setText("paused: another operation is running")
        self.link_label.setStyleSheet(f"color: {ORANGE}")
        return True

    def resume(self) -> None:
        if self._paused:
            self._paused = False
            self._start_session()

    def cancel_resume(self, reason: str) -> None:
        if self._paused:
            self._paused = False
            self._set_disconnected_ui()
            self.log.emit(f"Control not resumed: {reason}")

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_enabled()

    def shutdown(self) -> None:
        if self.connected:
            self._disconnect()
        self.plot.close()       # иначе приложение не завершится: окно графиков осталось бы последним

    def emergency_stop(self) -> None:
        if not self.connected:
            self.log.emit(f"{ESTOP_LABEL}: not connected")
            return
        self._jogging.clear()
        # прошивка выполняет EMERGENCY_STOP для всех осей, axis_id не важен
        self._send(CMD_EMERGENCY_STOP, AxisCommand(AXIS_GROUP), ESTOP_LABEL)

    def _halt_all(self, label: str) -> None:
        self._jogging.clear()
        self._send(CMD_HALT, AxisCommand(AXIS_GROUP, deceleration=self.deceleration.value()), label)

    def load_settings(self, settings) -> None:
        self.id_spin.setValue(settings.value("control/id", 1, type=int))
        self.velocity.setValue(settings.value("control/velocity", 5000.0, type=float))
        self.acceleration.setValue(settings.value("control/acceleration", 50000.0, type=float))
        self.deceleration.setValue(settings.value("control/deceleration", 50000.0, type=float))
        self.plot.load_settings(settings)

    def save_settings(self, settings) -> None:
        settings.setValue("control/id", self.id_spin.value())
        settings.setValue("control/velocity", self.velocity.value())
        settings.setValue("control/acceleration", self.acceleration.value())
        settings.setValue("control/deceleration", self.deceleration.value())
        self.plot.save_settings(settings)

    # ------------------------------------------------------------ сеанс

    def _toggle_connection(self) -> None:
        if self.connected:
            self._disconnect()
        else:
            self._start_session()

    def _start_session(self) -> None:
        settings = self._link_settings()
        if settings is None:
            self._set_disconnected_ui()
            return
        self._token += 1
        self._feedback = None
        self._status = None
        self._session = ControlSession(settings.port, settings.baudrate, settings.echo,
                                       self.id_spin.value(), self._bridge.callbacks(self._token),
                                       poll_interval=self._poll_interval())
        self._session.start()
        self.connect_button.setChecked(True)
        self.connect_button.setText("Disconnect")
        self.link_label.setText("connecting…")
        self.link_label.setStyleSheet(f"color: {GRAY}")
        self.log.emit(f"Control: polling board ID {self.id_spin.value()} on {settings.port}")
        self._update_enabled()
        self.connection_changed.emit(True)

    def _stop_session(self) -> None:
        if self._session is not None:
            self._session.stop()
            self._session = None
            self._token += 1          # всё, что ещё придёт от старого сеанса, — в мусор
            self._jogging.clear()
        self.connection_changed.emit(False)

    def _disconnect(self) -> None:
        moving = self._feedback is not None and any(
            a.state in (STATE_CONTINUE_MOTION, STATE_HOMING) for a in self._feedback.axes)
        if self._jogging or moving:
            # без опроса и без оператора бесконечное движение никто не остановит
            # плавная остановка, а не E-STOP: драйверы остаются включёнными, позиция не теряется
            self._halt_all("Stop all axes before disconnecting")
        self._stop_session()
        self._set_disconnected_ui()
        self.log.emit("Control disconnected")

    def _set_disconnected_ui(self) -> None:
        self.connect_button.setChecked(False)
        self.connect_button.setText("Connect")
        self.link_label.setText("not connected")
        self.link_label.setStyleSheet(f"color: {GRAY}")
        self._feedback = None
        self._status = None
        for axis in self.axes:
            axis.clear()
        self._update_enabled()

    def _id_spin_changed(self, value: int) -> None:
        if self._session is not None:
            self._session.set_device_id(value)
            self._feedback = None
            self._status = None
            for axis in self.axes:
                axis.clear()
            self.log.emit(f"Control: now polling board ID {value}")

    def _update_enabled(self) -> None:
        active = self.connected and not self._busy
        for axis in self.axes:
            for widget in axis.buttons():
                widget.setEnabled(active)
        for widget in (self.group_abs, self.group_rel, self.group_power_on, self.group_power_off,
                       self.reset_errors, self.estop_button):
            widget.setEnabled(active)
        self.connect_button.setEnabled(not self._busy)

    @Slot(int, object)
    def _on_feedback(self, token: int, fb: BoardFeedback) -> None:
        if token != self._token:
            return
        self._feedback = fb
        for axis, data in zip(self.axes, fb.axes):
            axis.show_feedback(data)
        self._record(fb)

    # ------------------------------------------------------------ график

    def _record(self, fb: BoardFeedback) -> None:
        board = self.id_spin.value()
        if self._trace_board != board:
            self.trace.clear()          # другая плата — своя запись
            self._trace_board = board
        self.trace.add(fb.timestamp, fb)

    def _toggle_plot(self, on: bool) -> None:
        if on:
            self.plot.show()
            self.plot.raise_()
            self.plot.activateWindow()
        else:
            self.plot.hide()
        self._apply_poll_interval()

    def _poll_interval(self) -> float:
        # чаще 50 мс опрашиваем только пока открыт график: это заметная нагрузка на шину
        return self.plot.sample_period() if self.plot_check.isChecked() else POLL_INTERVAL

    def _apply_poll_interval(self) -> None:
        if self._session is not None:
            self._session.poll_interval = self._poll_interval()

    @Slot(int, object)
    def _on_status(self, token: int, st: BoardStatus) -> None:
        if token != self._token:
            return
        self._status = st
        for axis, data in zip(self.axes, st.axes):
            axis.show_status(data)

    @Slot(int, float, int, bool)
    def _on_link(self, token: int, rate: float, lost: int, ok: bool) -> None:
        if token != self._token:
            return
        if ok:
            self.link_label.setText(f"● link: {rate:.0f} replies/s")
            self.link_label.setStyleSheet(f"color: {GREEN if rate >= 15 else ORANGE}")
        else:
            self.link_label.setText(f"● no reply from board ID {self.id_spin.value()}")
            self.link_label.setStyleSheet(f"color: {RED}")

    @Slot(int, str, bool, str)
    def _on_command_done(self, token: int, label: str, ok: bool, why: str) -> None:
        if token != self._token:
            return
        self.log.emit(f"{label}: {'accepted' if ok else 'NOT accepted — ' + why}")
        if label != ESTOP_LABEL:
            return
        if ok:
            self.log.emit(f"{ESTOP_LABEL}: drivers are off. To continue: Reset errors, then Power on")
        else:
            # прошивка без EMERGENCY_STOP не отвечает на неё совсем — оси остановит хотя бы HALT
            self.log.emit(f"{ESTOP_LABEL} not confirmed by the board: sending HALT to all axes")
            self._halt_all("HALT all axes (E-STOP fallback)")

    @Slot(int, str)
    def _on_stopped(self, token: int, reason: str) -> None:
        if token != self._token or not reason:
            return
        self._session = None
        self._token += 1
        self._set_disconnected_ui()
        self.connection_changed.emit(False)
        self.log.emit(f"Control stopped: {reason}")

    # ------------------------------------------------------------ команды

    def _send(self, cmd: int, command: AxisCommand, label: str) -> None:
        if self._session is None:
            self.log.emit(f"{label}: not connected")
            return
        self._session.submit(cmd, command, label)

    def _check(self, indices, allowed: set[int], action: str, needs_power: bool = False) -> bool:
        """Прошивка молча игнорирует команду в неподходящем состоянии — проверяем заранее.
        needs_power: команда движения. Без питания прошивка её не примет, в том числе пока
        ось тормозит после «power off» (состояние STOPPING, питание уже 0)."""
        if self._feedback is None:
            self.log.emit(f"{action}: no data from the board yet")
            return False
        problems = []
        for i in indices:
            state = self._feedback.axes[i].state
            powered = self._status is None or self._status.axes[i].is_powered
            if needs_power and not powered and state not in (STATE_DISABLED, STATE_ERROR):
                problems.append(f"{AXIS_NAMES[i]} is stopping to power off")
                continue
            if state in allowed:
                continue
            if state == STATE_DISABLED:
                problems.append(f"{AXIS_NAMES[i]} is off — turn the power on")
            elif state == STATE_ERROR:
                problems.append(f"{AXIS_NAMES[i]} has an error — reset errors first")
            else:
                problems.append(f"{AXIS_NAMES[i]} is in {self._feedback.axes[i].state_name}")
        if problems:
            self.log.emit(f"{action}: not sent, " + "; ".join(problems))
            return False
        return True

    def _motion(self) -> dict:
        return dict(velocity=self.velocity.value(), acceleration=self.acceleration.value(),
                    deceleration=self.deceleration.value())

    @staticmethod
    def _per_axis(index: int, value, empty):
        values = [empty, empty, empty]
        values[index] = value
        return tuple(values)

    def _power(self, index: int, on: bool) -> None:
        indices = range(len(AXIS_NAMES)) if index == AXIS_GROUP else [index]
        name = "All axes" if index == AXIS_GROUP else f"Axis {AXIS_NAMES[index]}"
        label = f"{name}: power {'on' if on else 'off'}"
        if not on and self._feedback is not None:
            moving = [AXIS_NAMES[i] for i in indices if self._feedback.axes[i].in_motion]
            if moving:
                # прошивка тормозит с торможением текущего профиля (или 1000 шаг/с²) и потом выключает
                label += f" ({', '.join(moving)} moving: stopping first)"
        self._send(CMD_POWER_ENABLE, AxisCommand(index, power_enable=int(on)), label)

    def _jog_start(self, index: int, direction: int) -> None:
        if not self._check([index], {STATE_STANDSTILL, STATE_CONTINUE_MOTION, STATE_STOPPING},
                           f"Axis {AXIS_NAMES[index]}: jog", needs_power=True):
            return
        self._jogging.add(index)
        self._send(CMD_MOVE_CONTINUE,
                   AxisCommand(index, velocity=self.velocity.value(), acceleration=self.acceleration.value(),
                               direction=self._per_axis(index, direction, 0)),
                   f"Axis {AXIS_NAMES[index]}: jog {'+' if direction > 0 else '−'}")

    def _jog_stop(self, index: int) -> None:
        if index not in self._jogging:
            return
        self._jogging.discard(index)
        self._send(CMD_HALT, AxisCommand(index, deceleration=self.deceleration.value()),
                   f"Axis {AXIS_NAMES[index]}: jog released")

    def _move(self, index: int, absolute: bool) -> None:
        value = self.axes[index].target.value()
        action = f"Axis {AXIS_NAMES[index]}: {'move to' if absolute else 'move by'} {value:g}"
        if not self._check([index], {STATE_STANDSTILL, STATE_DISCRETE_MOTION}, action, needs_power=True):
            return
        self._send(CMD_MOVE_ABSOLUTE if absolute else CMD_MOVE_RELATIVE,
                   AxisCommand(index, position=self._per_axis(index, value, 0.0), **self._motion()), action)

    def _halt(self, index: int) -> None:
        self._jogging.discard(index)
        self._send(CMD_HALT, AxisCommand(index, deceleration=self.deceleration.value()),
                   f"Axis {AXIS_NAMES[index]}: stop")

    def _set_position(self, index: int) -> None:
        value = self.axes[index].new_position.value()
        action = f"Axis {AXIS_NAMES[index]}: set position {value:g}"
        # в движении прошивка пересчитала бы путь к старой цели уже в новых координатах
        if not self._check([index], {STATE_STANDSTILL, STATE_DISABLED, STATE_ERROR}, action):
            return
        self._send(CMD_SET_POSITION, AxisCommand(index, position=self._per_axis(index, value, 0.0)), action)

    def _home(self, index: int) -> None:
        axis = self.axes[index]
        action = f"Axis {AXIS_NAMES[index]}: home ({axis.homing_mode.currentText()})"
        if not self._check([index], {STATE_STANDSTILL}, action, needs_power=True):
            return
        self._send(CMD_MOVE_HOME,
                   AxisCommand(index, position=self._per_axis(index, axis.homing_offset.value(), 0.0),
                               direction=self._per_axis(index, axis.homing_mode.currentData(), 0),
                               **self._motion()),
                   action)

    def _group_move(self, absolute: bool) -> None:
        values = tuple(spin.value() for spin in self.group_targets)
        action = f"All axes: {'move to' if absolute else 'move by'} {', '.join(f'{v:g}' for v in values)}"
        # из движения интерполяция в прошивке не работает: скорости и торможения считаются от покоя
        if not self._check(range(len(AXIS_NAMES)), {STATE_STANDSTILL}, action, needs_power=True):
            return
        self._send(CMD_MOVE_ABSOLUTE if absolute else CMD_MOVE_RELATIVE,
                   AxisCommand(AXIS_GROUP, position=values, **self._motion()), action)

    def _reset_errors(self) -> None:
        self._send(CMD_RESET_ERROR, AxisCommand(AXIS_GROUP), "Reset errors")
