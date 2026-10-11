"""Окно графиков осей: позиция, скорость и рассогласование по записи из GET_FEEDBACK.

Данные пишет ControlTab в TraceBuffer при каждом ответе платы; окно только рисует.
В паузе запись продолжается, а графики стоят: можно приближать мышью и смотреть значения.
"""
from __future__ import annotations

import math
import time

from PySide6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis
from PySide6.QtCore import QMargins, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QKeySequence, QPainter, QPalette, QPen, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QGraphicsLineItem, QHBoxLayout, QLabel,
    QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from axis_trace import TraceBuffer
from board_protocol import AXIS_NAMES

AXIS_COLORS = ("#2f80ed", "#27ae60", "#e67e22")   # X, Y, C — различимы и в светлой, и в тёмной теме
STOP_COLOR = "#c0392b"
MAX_POINTS = 1500       # больше точек на линию не рисуем — прореживаем
REDRAW_MS = 100
WINDOWS = [("10 s", 10.0), ("30 s", 30.0), ("1 min", 60.0), ("5 min", 300.0), ("All", None)]
SAMPLE_PERIODS = [("50 ms", 0.05), ("20 ms", 0.02), ("10 ms", 0.01)]

# график: ключ, подпись оси Y, линии (поле AxisFeedback, это обратная связь)
CHARTS = [
    ("position", "Position, steps", [("position", False), ("feedback_position", True)]),
    ("velocity", "Speed, steps/s", [("velocity", False), ("velocity_feedback", True)]),
    ("lag", "Lag error, steps", [("lag_error", False)]),
]
CHART_TITLES = {"position": "Position", "velocity": "Speed", "lag": "Lag error"}


def nice_step(span: float, ticks: int = 5) -> float:
    """Шаг делений 1, 2, 2.5 или 5 × 10^n — чтобы на оси было около ticks подписей."""
    raw = span / ticks if span > 0 and math.isfinite(span) else 1.0
    magnitude = 10.0 ** math.floor(math.log10(raw))
    for m in (1.0, 2.0, 2.5, 5.0, 10.0):
        if m * magnitude >= raw:
            return m * magnitude
    return 10.0 * magnitude


def _set_range(axis: QValueAxis, low: float, high: float, ticks: int) -> None:
    """Диапазон — ровно по данным, а деления — круглые, с привязкой к нулю.
    Шаг ставим раньше диапазона: иначе на миг старый мелкий шаг на новом широком
    диапазоне дал бы Qt тысячи делений."""
    axis.setTickInterval(nice_step(high - low, ticks))
    axis.setRange(low, high)


def _label_width(axis: QValueAxis) -> int:
    """Ширина самой длинной подписи делений оси Y — те же значения, что нарисует Qt."""
    step = axis.tickInterval()
    if not step > 0:
        return 0
    metrics = QFontMetrics(axis.labelsFont())
    value = math.ceil(axis.min() / step) * step
    width = 0
    for _ in range(100):
        if value > axis.max() + step * 1e-9:
            break
        width = max(width, metrics.horizontalAdvance(f"{0.0 if abs(value) < step * 1e-9 else value:g}"))
        value += step
    return width


class TraceView(QChartView):
    """График с вертикальной линией-курсором. Сообщает время под мышью."""
    hovered = Signal(float)      # NaN — мышь ушла с графика

    def __init__(self, chart: QChart):
        super().__init__(chart)
        self.setRenderHint(QPainter.Antialiasing)
        self.setMouseTracking(True)
        self.setMinimumHeight(130)
        pen = QPen(QColor(150, 150, 150))
        pen.setStyle(Qt.DashLine)
        self.cursor_line = QGraphicsLineItem()
        self.cursor_line.setPen(pen)
        self.cursor_line.setZValue(100)
        self.cursor_line.hide()
        self.scene().addItem(self.cursor_line)
        self._cursor_t = math.nan
        # область графика меняется при раскладке окна и смене подписей оси — линию пересчитываем
        chart.plotAreaChanged.connect(lambda _: self.show_cursor(self._cursor_t))

    def mouseMoveEvent(self, event) -> None:
        super().mouseMoveEvent(event)
        chart = self.chart()
        point = chart.mapFromScene(self.mapToScene(event.position().toPoint()))
        if chart.plotArea().contains(point) and chart.series():
            self.hovered.emit(chart.mapToValue(point).x())
        else:
            self.hovered.emit(math.nan)

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self.hovered.emit(math.nan)

    def show_cursor(self, t: float) -> None:
        self._cursor_t = t
        chart = self.chart()
        x_axis = chart.axes(Qt.Horizontal)[0]
        if math.isnan(t) or not chart.series() or not x_axis.min() <= t <= x_axis.max():
            self.cursor_line.hide()
            return
        area = chart.plotArea()
        x = chart.mapToPosition(QPointF(t, 0.0)).x()
        top = chart.mapToScene(QPointF(x, area.top()))
        bottom = chart.mapToScene(QPointF(x, area.bottom()))
        self.cursor_line.setLine(top.x(), top.y(), bottom.x(), bottom.y())
        self.cursor_line.show()


class _Chart:
    def __init__(self, key: str, y_title: str, signals: list[tuple[str, bool]], dark: bool):
        self.key = key
        self.chart = QChart()
        self.chart.setTheme(QChart.ChartThemeDark if dark else QChart.ChartThemeLight)
        self.chart.setBackgroundBrush(QApplication.palette().window())
        self.chart.setBackgroundRoundness(0)
        self.chart.setMargins(QMargins(2, 2, 2, 2))
        self.chart.legend().hide()
        self.x_axis = QValueAxis()
        self.x_axis.setLabelFormat("%g")
        self.x_axis.setTitleText("time, s")
        self.y_axis = QValueAxis()
        self.y_axis.setLabelFormat("%g")
        self.y_axis.setTitleText(y_title)
        for axis, ticks in ((self.x_axis, 8), (self.y_axis, 5)):
            # шаг до включения динамических делений: с шагом 0 (по умолчанию) Qt зацикливается
            _set_range(axis, 0.0, 10.0 if axis is self.x_axis else 1.0, ticks)
            axis.setTickAnchor(0.0)
            axis.setTickType(QValueAxis.TicksDynamic)
        self.chart.addAxis(self.x_axis, Qt.AlignBottom)
        self.chart.addAxis(self.y_axis, Qt.AlignLeft)
        self.lines: list[tuple[str, int, bool, QLineSeries]] = []
        for axis in range(len(AXIS_NAMES)):
            for signal, feedback in signals:
                series = QLineSeries()
                self.chart.addSeries(series)       # тема красит линию при добавлении — перо ставим после
                series.attachAxis(self.x_axis)
                series.attachAxis(self.y_axis)
                pen = QPen(QColor(AXIS_COLORS[axis]))
                pen.setWidthF(1.2 if feedback else 1.8)
                if feedback:
                    pen.setStyle(Qt.DashLine)
                series.setPen(pen)
                self.lines.append((signal, axis, feedback, series))
        self.view = TraceView(self.chart)


class PlotWindow(QWidget):
    closed = Signal()
    sample_period_changed = Signal(float)
    emergency_stop = Signal()
    log = Signal(str)

    def __init__(self, trace: TraceBuffer, parent=None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle("Axis plots — StepperDriver Service Tool")
        self.resize(1000, 760)
        self.trace = trace
        self._rendered_version = -1
        self._rendering = False
        self._shown_range = (0.0, 10.0)          # что сейчас по оси времени
        self._paused_range: tuple[float, float] | None = None   # куда возвращает Reset zoom
        self._cursor_t = math.nan

        layout = QVBoxLayout(self)
        layout.addLayout(self._build_select_row())
        layout.addLayout(self._build_view_row())

        dark = QApplication.palette().color(QPalette.Window).lightness() < 128
        self.charts = [_Chart(key, title, signals, dark) for key, title, signals in CHARTS]
        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)
        for chart in self.charts:
            chart.view.hovered.connect(self._on_hover)
            chart.x_axis.rangeChanged.connect(self._on_zoom)
            splitter.addWidget(chart.view)
        layout.addWidget(splitter, 1)

        bottom = QHBoxLayout()
        self.readout = QLabel("")
        self.readout.setFont(QFont("monospace"))
        self.readout.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.hint = QLabel("")
        self.hint.setStyleSheet("color: gray")
        bottom.addWidget(self.readout, 1)
        bottom.addWidget(self.hint)
        layout.addLayout(bottom)

        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=self.emergency_stop.emit)
        self._timer = QTimer(self)
        self._timer.setInterval(REDRAW_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._apply_visibility()
        self._update_hint()

    # ------------------------------------------------------------ интерфейс

    def _build_select_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel("Axes:"))
        self.axis_checks = []
        for name, color in zip(AXIS_NAMES, AXIS_COLORS):
            check = QCheckBox(name)
            check.setChecked(True)
            check.setStyleSheet(f"color: {color}; font-weight: bold")
            check.toggled.connect(self._redraw)
            self.axis_checks.append(check)
            row.addWidget(check)
        row.addSpacing(20)
        row.addWidget(QLabel("Show:"))
        self.chart_checks = {}
        for key, _, _ in CHARTS:
            check = QCheckBox(CHART_TITLES[key])
            check.setChecked(True)
            check.toggled.connect(self._apply_visibility)
            self.chart_checks[key] = check
            row.addWidget(check)
        self.feedback_check = QCheckBox("Feedback (dashed)")
        self.feedback_check.setChecked(True)
        self.feedback_check.setToolTip("Dashed lines: feedback position and speed reported by the board")
        self.feedback_check.toggled.connect(self._redraw)
        row.addSpacing(20)
        row.addWidget(self.feedback_check)
        row.addStretch(1)
        stop = QPushButton("E-STOP  (Esc)")
        stop.setToolTip("Emergency stop of all axes: the drivers are switched off")
        stop.setStyleSheet(f"background: {STOP_COLOR}; color: white; font-weight: bold; padding: 4px 12px")
        stop.clicked.connect(self.emergency_stop.emit)
        row.addWidget(stop)
        return row

    def _build_view_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel("Window:"))
        self.window_combo = QComboBox()
        for label, seconds in WINDOWS:
            self.window_combo.addItem(label, seconds)
        self.window_combo.setCurrentIndex(1)
        self.window_combo.currentIndexChanged.connect(self._window_changed)
        row.addWidget(self.window_combo)
        row.addSpacing(12)
        row.addWidget(QLabel("Sample every:"))
        self.period_combo = QComboBox()
        for label, seconds in SAMPLE_PERIODS:
            self.period_combo.addItem(label, seconds)
        self.period_combo.setToolTip(
            "How often the board is polled while this window is open.\n"
            "At 115200 baud one feedback reply takes about 8 ms, so 10 ms is the limit.\n"
            "Closing the window returns polling to 50 ms.")
        self.period_combo.currentIndexChanged.connect(
            lambda _: self.sample_period_changed.emit(self.sample_period()))
        row.addWidget(self.period_combo)
        row.addStretch(1)
        self.pause_button = QPushButton("Pause")
        self.pause_button.setCheckable(True)
        self.pause_button.setToolTip("Freeze the plots to zoom and read values. Recording continues.")
        self.pause_button.toggled.connect(self._pause_toggled)
        self.reset_zoom_button = QPushButton("Reset zoom")
        self.reset_zoom_button.setEnabled(False)
        self.reset_zoom_button.clicked.connect(self._reset_zoom)
        clear = QPushButton("Clear")
        clear.setToolTip("Delete the recorded data")
        clear.clicked.connect(self._clear)
        save = QPushButton("Save CSV…")
        save.setToolTip("Save all recorded data to a CSV file")
        save.clicked.connect(self._save_csv)
        for button in (self.pause_button, self.reset_zoom_button, clear, save):
            row.addWidget(button)
        return row

    # ------------------------------------------------------------ для ControlTab

    def sample_period(self) -> float:
        return self.period_combo.currentData()

    def load_settings(self, settings) -> None:
        geometry = settings.value("plot/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        self.window_combo.setCurrentIndex(settings.value("plot/window", 1, type=int))
        for i, check in enumerate(self.axis_checks):
            check.setChecked(settings.value(f"plot/axis{i}", True, type=bool))
        for key, check in self.chart_checks.items():
            check.setChecked(settings.value(f"plot/show_{key}", True, type=bool))
        self.feedback_check.setChecked(settings.value("plot/feedback", True, type=bool))
        # период опроса не запоминаем: при каждом запуске — обычные 50 мс

    def save_settings(self, settings) -> None:
        settings.setValue("plot/geometry", self.saveGeometry())
        settings.setValue("plot/window", self.window_combo.currentIndex())
        for i, check in enumerate(self.axis_checks):
            settings.setValue(f"plot/axis{i}", check.isChecked())
        for key, check in self.chart_checks.items():
            settings.setValue(f"plot/show_{key}", check.isChecked())
        settings.setValue("plot/feedback", self.feedback_check.isChecked())

    def closeEvent(self, event) -> None:
        event.accept()
        self.closed.emit()

    # ------------------------------------------------------------ отрисовка

    @property
    def paused(self) -> bool:
        return self.pause_button.isChecked()

    def _tick(self) -> None:
        if self.isVisible() and not self.paused and self.trace.version != self._rendered_version:
            self._render()
        if self.isVisible():
            self._update_hint()

    def _redraw(self) -> None:
        self._render()

    def _apply_visibility(self) -> None:
        for chart in self.charts:
            chart.view.setVisible(self.chart_checks[chart.key].isChecked())
        self._render()

    def _live_range(self) -> tuple[float, float]:
        window = self.window_combo.currentData()
        if window is None:
            first = self.trace.first_time
            return first, max(self.trace.last_time, first + 1.0)
        # первые секунды — ось от нуля, дальше окно едет за последним отсчётом
        end = max(self.trace.last_time, window)
        return end - window, end

    def _render(self) -> None:
        t_from, t_to = self._shown_range if self.paused else self._live_range()
        i0, i1 = self.trace.index_range(t_from, t_to)
        step = max(1, -(-(i1 - i0) // MAX_POINTS))
        times = self.trace.times[i0:i1:step]
        add_last = i1 > i0 and (i1 - 1 - i0) % step != 0      # последний отсчёт рисуем всегда
        if add_last:
            times.append(self.trace.times[i1 - 1])
        show_feedback = self.feedback_check.isChecked()

        self._rendering = True
        try:
            for chart in self.charts:
                if not chart.view.isVisibleTo(self):
                    continue
                low, high = math.inf, -math.inf
                for signal, axis, feedback, series in chart.lines:
                    visible = self.axis_checks[axis].isChecked() and (show_feedback or not feedback)
                    series.setVisible(visible)
                    if not visible:
                        continue
                    data = self.trace.values[(signal, axis)]
                    values = data[i0:i1:step]
                    if add_last:
                        values.append(data[i1 - 1])
                    series.replace([QPointF(t, v) for t, v in zip(times, values)])
                    if values:
                        low = min(low, min(values))
                        high = max(high, max(values))
                _set_range(chart.x_axis, t_from, t_to, 8)
                if low > high or not (math.isfinite(low) and math.isfinite(high)):
                    low, high = -1.0, 1.0           # нет данных или плата прислала NaN/inf
                elif high - low < 1e-6:
                    low, high = low - 1.0, high + 1.0
                else:
                    pad = (high - low) * 0.06
                    low, high = low - pad, high + pad
                _set_range(chart.y_axis, low, high, 5)
        finally:
            self._rendering = False
        self._align_time_axes()
        self._shown_range = (t_from, t_to)
        self._rendered_version = self.trace.version
        self._show_cursor()

    def _align_time_axes(self) -> None:
        """Подписи по Y у графиков разной ширины («20000» и «4»), и шкалы времени
        съезжают. Добавляем слева поле, чтобы все шкалы начинались на одной вертикали."""
        visible = [chart for chart in self.charts if chart.view.isVisibleTo(self)]
        widths = {id(chart): _label_width(chart.y_axis) for chart in visible}
        widest = max(widths.values(), default=0)
        for chart in visible:
            margins = QMargins(2 + widest - widths[id(chart)], 2, 2, 2)
            if chart.chart.margins() != margins:
                chart.chart.setMargins(margins)

    # ------------------------------------------------------------ мышь, пауза, приближение

    def _on_hover(self, t: float) -> None:
        self._cursor_t = t
        self._show_cursor()

    def _show_cursor(self) -> None:
        for chart in self.charts:
            chart.view.show_cursor(self._cursor_t)
        index = None if math.isnan(self._cursor_t) else self.trace.nearest(self._cursor_t)
        self.readout.setText("" if index is None else self._describe(index))

    def _describe(self, i: int) -> str:
        v = self.trace.values
        show = {key: check.isChecked() for key, check in self.chart_checks.items()}
        fb = self.feedback_check.isChecked()
        parts = [f"t {self.trace.times[i]:8.3f} s"]
        for axis, check in enumerate(self.axis_checks):
            if not check.isChecked():
                continue
            items = []
            if show["position"]:
                items.append(f"pos {v[('position', axis)][i]:,.1f}")
                if fb:
                    items.append(f"fb {v[('feedback_position', axis)][i]:,.0f}")
            if show["velocity"]:
                items.append(f"v {v[('velocity', axis)][i]:,.1f}")
                if fb:
                    items.append(f"fb {v[('velocity_feedback', axis)][i]:,.0f}")
            if show["lag"]:
                items.append(f"lag {v[('lag_error', axis)][i]:,.0f}")
            parts.append(f"{AXIS_NAMES[axis]}: " + " ".join(items))
        return "   ".join(parts)

    def _pause_toggled(self, paused: bool) -> None:
        for chart in self.charts:
            chart.view.setRubberBand(QChartView.HorizontalRubberBand if paused else QChartView.NoRubberBand)
        self.reset_zoom_button.setEnabled(paused)
        if paused:
            self._paused_range = self._shown_range
        else:
            self._paused_range = None
            self._render()
        self._update_hint()

    def _on_zoom(self, low: float, high: float) -> None:
        """Пользователь выделил участок (или правой кнопкой отдалил) на одном из графиков:
        тот же участок показываем на всех, масштаб по Y — по видимым данным."""
        if self._rendering or not self.paused:
            return
        self._shown_range = (low, high)
        self._render()

    def _reset_zoom(self) -> None:
        if self._paused_range is not None:
            self._shown_range = self._paused_range
            self._render()

    def _window_changed(self) -> None:
        if self.paused:
            window = self.window_combo.currentData()
            end = self._shown_range[1]
            start = self.trace.first_time if window is None else end - window
            self._paused_range = self._shown_range = (start, end)
        self._render()

    def _clear(self) -> None:
        self.pause_button.setChecked(False)
        self.trace.clear()
        self._cursor_t = math.nan
        self._render()

    def _save_csv(self) -> None:
        if not len(self.trace):
            self.log.emit("Plot: nothing to save yet")
            return
        name = time.strftime("axes_%Y%m%d_%H%M%S.csv")
        path, _ = QFileDialog.getSaveFileName(self, "Save plot data", name, "CSV files (*.csv)")
        if not path:
            return
        try:
            count = self.trace.save_csv(path)
        except OSError as e:
            self.log.emit(f"Plot: cannot save {path}: {e}")
            return
        self.log.emit(f"Plot: saved {count} samples to {path}")

    def _update_hint(self) -> None:
        if self.paused:
            text = "Paused: drag on a plot to zoom in, right-click to zoom out"
        else:
            text = f"{len(self.trace)} samples"
            if len(self.trace) > 10:
                times = self.trace.times
                span = times[-1] - times[-11]
                if span > 0:
                    text += f", {10 / span:.0f} per second"
        if self.hint.text() != text:
            self.hint.setText(text)
