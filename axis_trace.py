"""Запись показаний осей из GET_FEEDBACK для графика: время и пять величин на каждую ось."""
from __future__ import annotations

import csv
from array import array
from bisect import bisect_left, bisect_right
from pathlib import Path

from board_protocol import AXIS_NAMES, BoardFeedback

# поле AxisFeedback и подпись для CSV
SIGNALS = {
    "position": "position",
    "feedback_position": "feedback_position",
    "velocity": "velocity",
    "velocity_feedback": "velocity_feedback",
    "lag_error": "lag_error",
}
MAX_SAMPLES = 60000      # 10 мин при опросе раз в 10 мс, 50 мин — раз в 50 мс


class TraceBuffer:
    """Кольцевая запись в array('d'): ~8 байт на значение. Время — секунды от первого отсчёта."""

    def __init__(self, max_samples: int = MAX_SAMPLES):
        self.max_samples = max_samples
        self.version = 0          # растёт при каждом изменении: графику есть что перерисовать
        self._t0: float | None = None
        self.times = array("d")
        self.values = {(signal, axis): array("d") for signal in SIGNALS for axis in range(len(AXIS_NAMES))}

    def __len__(self) -> int:
        return len(self.times)

    @property
    def first_time(self) -> float:
        return self.times[0] if self.times else 0.0

    @property
    def last_time(self) -> float:
        return self.times[-1] if self.times else 0.0

    def clear(self) -> None:
        self._t0 = None
        del self.times[:]
        for values in self.values.values():
            del values[:]
        self.version += 1

    def add(self, timestamp: float, feedback: BoardFeedback) -> None:
        if self._t0 is None:
            self._t0 = timestamp
        t = timestamp - self._t0
        if self.times and t < self.times[-1]:
            return                 # время только растёт, иначе поиск по времени сломается
        self.times.append(t)
        for (signal, axis), values in self.values.items():
            values.append(float(getattr(feedback.axes[axis], signal)))
        # обрезаем пачкой, а не по одному: удаление из начала array стоит O(n)
        if len(self.times) > self.max_samples + self.max_samples // 4:
            extra = len(self.times) - self.max_samples
            del self.times[:extra]
            for values in self.values.values():
                del values[:extra]
        self.version += 1

    def index_range(self, t_from: float, t_to: float) -> tuple[int, int]:
        """Индексы отсчётов [i0, i1) в окне времени, с одним отсчётом за краями —
        чтобы линия доходила до краёв графика."""
        i0 = max(0, bisect_left(self.times, t_from) - 1)
        i1 = min(len(self.times), bisect_right(self.times, t_to) + 1)
        return i0, max(i0, i1)

    def nearest(self, t: float) -> int | None:
        if not self.times:
            return None
        i = bisect_left(self.times, t)
        if i == 0:
            return 0
        if i == len(self.times):
            return i - 1
        return i if self.times[i] - t < t - self.times[i - 1] else i - 1

    def save_csv(self, path: str | Path) -> int:
        """Сохраняет всю запись. Возвращает число строк."""
        columns = [(signal, axis) for axis in range(len(AXIS_NAMES)) for signal in SIGNALS]
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["time_s"] + [f"{AXIS_NAMES[axis]}_{SIGNALS[signal]}" for signal, axis in columns])
            for i, t in enumerate(self.times):
                writer.writerow([f"{t:.4f}"] + [f"{self.values[key][i]:.6g}" for key in columns])
        return len(self.times)
