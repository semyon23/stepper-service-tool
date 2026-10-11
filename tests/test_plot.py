"""Запись показаний для графика и окно графиков (без экрана, QT_QPA_PLATFORM=offscreen)."""
import csv
import math
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from axis_trace import TraceBuffer  # noqa: E402
from board_protocol import AxisFeedback, BoardFeedback  # noqa: E402


def feedback(position: float, velocity: float = 0.0, lag: float = 0.0) -> BoardFeedback:
    axes = [AxisFeedback(position * (i + 1), round(position * (i + 1)), lag, velocity, round(velocity),
                         velocity != 0, False, False, 0, 1, False, False) for i in range(3)]
    return BoardFeedback(axes)


def test_time_starts_at_zero_and_only_grows():
    trace = TraceBuffer()
    trace.add(1000.0, feedback(1))
    trace.add(1000.05, feedback(2))
    trace.add(1000.02, feedback(3))       # пришло «из прошлого» — пропускаем
    assert list(trace.times) == pytest.approx([0.0, 0.05])
    assert list(trace.values[("position", 2)]) == [3.0, 6.0]
    assert list(trace.values[("feedback_position", 1)]) == [2.0, 4.0]


def test_trim_keeps_last_samples():
    trace = TraceBuffer(max_samples=100)
    for i in range(1000):
        trace.add(i * 0.01, feedback(i))
    assert 100 <= len(trace) <= 125
    assert trace.values[("position", 0)][-1] == 999.0
    assert trace.times[-1] == pytest.approx(9.99)


def test_index_range_and_nearest():
    trace = TraceBuffer()
    for i in range(101):
        trace.add(i * 0.1, feedback(i))
    i0, i1 = trace.index_range(2.0, 3.0)
    # один отсчёт за каждым краем, чтобы линия доходила до краёв графика
    assert trace.times[i0] == pytest.approx(1.9) and trace.times[i1 - 1] == pytest.approx(3.1)
    assert trace.index_range(-5, -1) == (0, 1)
    assert trace.nearest(2.04) == 20 and trace.nearest(2.06) == 21
    assert trace.nearest(-1) == 0 and trace.nearest(99) == 100
    assert TraceBuffer().nearest(1.0) is None


def test_save_csv(tmp_path):
    trace = TraceBuffer()
    trace.add(5.0, feedback(10, velocity=500, lag=2))
    trace.add(5.05, feedback(11, velocity=520, lag=1))
    path = tmp_path / "axes.csv"
    assert trace.save_csv(path) == 2
    rows = list(csv.reader(path.open()))
    assert rows[0][:3] == ["time_s", "X_position", "X_feedback_position"]
    assert len(rows[0]) == 1 + 3 * 5
    assert rows[2][0] == "0.0500" and float(rows[2][rows[0].index("Y_position")]) == 22.0
    assert float(rows[1][rows[0].index("C_velocity")]) == 500.0


def test_session_stamps_feedback_with_request_time(monkeypatch):
    import operations as ops
    from board_emulator import BoardEmulator, serial_factory
    from control_session import ControlSession, SessionCallbacks
    from serial_link import SerialLink
    import threading
    import time

    monkeypatch.setattr(SerialLink, "serial_class", staticmethod(serial_factory))
    monkeypatch.setattr(ops, "BOOT_DELAY", 0.3)
    got, done = [], threading.Event()
    callbacks = SessionCallbacks(
        feedback=lambda fb: got.append((fb.timestamp, time.monotonic())),
        status=lambda st: None, link=lambda *a: None, command_done=lambda *a: None,
        stopped=lambda reason: done.set())
    with BoardEmulator(device_id=4) as board:
        session = ControlSession(board.port, 115200, False, 4, callbacks, poll_interval=0.02)
        session.start()
        time.sleep(0.6)
        session.stop()
    assert len(got) >= 20                                  # 20 мс — опрос чаще прежних 50 мс
    assert all(0 < arrived - stamp < 0.05 for stamp, arrived in got)
    stamps = [stamp for stamp, _ in got]
    assert all(b > a for a, b in zip(stamps, stamps[1:]))


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_plot_window_draws_zooms_and_reads_values(app, tmp_path, monkeypatch):
    from PySide6.QtCharts import QChartView
    from PySide6.QtCore import QPointF
    import plot_window as pw

    trace = TraceBuffer()
    window = pw.PlotWindow(trace)
    window.window_combo.setCurrentIndex(0)                 # окно 10 с
    for i in range(400):                                   # 20 с по 50 мс: разгон, ход, торможение
        t = i * 0.05
        v = min(t, 5.0, max(0.0, 20.0 - t)) * 1000
        trace.add(100.0 + t, feedback(t * 1000, velocity=v, lag=i % 3))
    window.show()
    app.processEvents()
    window._render()

    position, velocity, lag = window.charts
    assert position.x_axis.min() == pytest.approx(9.95) and position.x_axis.max() == pytest.approx(19.95)
    x_pos = next(s for sig, axis, fb, s in position.lines if sig == "position" and axis == 0)
    points = x_pos.points()
    assert len(points) == 202                              # 10 с по 50 мс + отсчёт за каждым краем
    assert points[-1].y() == pytest.approx(19950.0)
    assert velocity.y_axis.max() >= 5000.0                 # масштаб по Y — по видимым данным

    # оси и линии обратной связи скрываются
    window.axis_checks[1].setChecked(False)
    window.feedback_check.setChecked(False)
    hidden = [s for sig, axis, fb, s in position.lines if axis == 1 or fb]
    assert hidden and not any(s.isVisible() for s in hidden)

    # пауза: запись идёт, графики стоят; приближение на одном графике — на всех
    window.pause_button.setChecked(True)
    assert position.view.rubberBand() == QChartView.HorizontalRubberBand
    trace.add(100.0 + 20.0, feedback(20000))
    window._tick()
    assert position.x_axis.max() == pytest.approx(19.95)
    velocity.x_axis.setRange(12.0, 13.0)                   # так делает выделение мышью
    assert position.x_axis.min() == pytest.approx(12.0) and lag.x_axis.max() == pytest.approx(13.0)
    # по Y — только видимое: X от 11 950 до C 39 150 (у C позиция втрое больше), Y скрыта
    assert 10000.0 < position.y_axis.min() < 11950.0 and 39150.0 < position.y_axis.max() < 41000.0
    window._reset_zoom()
    assert position.x_axis.min() == pytest.approx(9.95)

    # курсор: значения ближайшего отсчёта
    window._on_hover(12.52)
    text = window.readout.text()
    assert "t   12.500 s" in text and "X: pos 12,500.0" in text and "Y:" not in text
    assert position.view.cursor_line.isVisible()
    # после изменения размера окна линия курсора остаётся на том же времени
    window.resize(1300, 900)
    app.processEvents()
    expected = position.chart.mapToScene(position.chart.mapToPosition(QPointF(12.52, 0.0))).x()
    assert position.view.cursor_line.line().x1() == pytest.approx(expected, abs=0.5)
    window._on_hover(math.nan)
    assert window.readout.text() == "" and not position.view.cursor_line.isVisible()

    # снятие паузы — снова живое окно
    window.pause_button.setChecked(False)
    assert position.x_axis.max() == pytest.approx(20.0)

    # CSV
    path = tmp_path / "plot.csv"
    monkeypatch.setattr(pw.QFileDialog, "getSaveFileName", lambda *a, **k: (str(path), ""))
    messages = []
    window.log.connect(messages.append)
    window._save_csv()
    assert path.exists() and "saved 401 samples" in messages[-1]
    window.close()


def test_control_tab_plot_checkbox_and_poll_interval(app):
    import control_tab
    from control_session import POLL_INTERVAL

    class FakeSession:
        poll_interval = POLL_INTERVAL

    tab = control_tab.ControlTab(lambda: None)
    tab._session = FakeSession()
    tab.plot_check.setChecked(True)
    assert tab.plot.isVisible()
    assert tab._session.poll_interval == 0.05
    tab.plot.period_combo.setCurrentIndex(2)               # 10 мс
    assert tab._session.poll_interval == 0.01
    tab.plot.close()                                       # крестик окна снимает галочку
    assert not tab.plot_check.isChecked() and not tab.plot.isVisible()
    assert tab._session.poll_interval == POLL_INTERVAL     # без графика — снова 50 мс
    tab._session = None

    # запись идёт и без открытого окна; смена платы начинает запись заново
    fb = feedback(1.0)
    fb.timestamp = 50.0
    tab._record(fb)
    fb2 = feedback(2.0)
    fb2.timestamp = 50.05
    tab._record(fb2)
    assert len(tab.trace) == 2
    tab.id_spin.setValue(7)
    tab._record(fb2)
    assert len(tab.trace) == 1
    tab.shutdown()


def test_nice_tick_step():
    from plot_window import nice_step
    assert nice_step(30.0, 8) == 5.0
    assert nice_step(10.0, 8) == 2.0
    assert nice_step(30464.0, 5) == 10000.0
    assert nice_step(9.0, 5) == 2.0
    assert nice_step(0.3, 5) == 0.1


def test_nan_from_board_does_not_break_plot(app):
    import plot_window as pw
    trace = TraceBuffer()
    window = pw.PlotWindow(trace)
    trace.add(0.0, feedback(1.0))
    nan, inf = float("nan"), float("inf")
    bad = [AxisFeedback(nan, nan, nan, inf, -inf, True, False, False, 0, 2, False, False) for _ in range(3)]
    trace.add(0.05, BoardFeedback(bad))
    window.show()
    window._render()
    assert math.isfinite(window.charts[0].y_axis.min()) and math.isfinite(window.charts[1].y_axis.max())
    window.close()


def test_time_axes_line_up(app):
    import plot_window as pw
    trace = TraceBuffer()
    window = pw.PlotWindow(trace)
    for i in range(200):
        trace.add(i * 0.05, feedback(i * 100.0, velocity=1000.0, lag=i % 2))   # «9900» и «1» по Y
    window.show()
    window._render()
    app.processEvents()
    lefts = [chart.chart.plotArea().left() for chart in window.charts]
    assert max(lefts) - min(lefts) < 3, lefts
    window.close()
