#!/usr/bin/env python3
"""Сервисная утилита StepperDriver: управление осями, поиск плат, смена ID и прошивка через UART (RS-485)."""
from __future__ import annotations

import sys
import threading
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Qt, QThread, Signal, Slot
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QRadioButton, QSpinBox,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)
from serial.tools import list_ports

import operations as ops
from board_protocol import AXIS_NAMES, MAX_DEVICE_ID, MIN_DEVICE_ID, BoardStatus
from control_tab import ControlTab
from firmware import FLASH_BASE, Firmware, FirmwareError, load_firmware

BAUDRATES = ["9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600"]
BOOTLOADER_BAUDRATES = ["115200", "57600", "38400", "19200", "9600"]
PROJECT_DIR = Path(__file__).resolve().parents[2]
WINDOW_WIDTH = 1000
WINDOW_HEIGHT = 800      # меньше, если не помещается на экран
LOG_MIN_HEIGHT = 120


class Worker(QObject):
    """Выполняет сценарий из operations в отдельном потоке, чтобы окно не зависало."""
    log = Signal(str)
    progress = Signal(int, int, str)
    finished = Signal(object, str, bool)  # результат, текст ошибки, отменено

    def __init__(self, fn, *args):
        super().__init__()
        self._fn = fn
        self._args = args
        self.cancel_event = threading.Event()

    @Slot()
    def run(self) -> None:
        ctx = ops.Context(self.log.emit, self.progress.emit, self.cancel_event)
        try:
            result = self._fn(*self._args, ctx)
        except ops.Cancelled as e:
            self.finished.emit(None, str(e), True)
        except ops.EXPECTED_ERRORS as e:
            self.finished.emit(None, str(e), False)
        except Exception:
            self.finished.emit(None, "Internal error:\n" + traceback.format_exc(), False)
        else:
            self.finished.emit(result, "", False)


def _id_spin(value: int) -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(MIN_DEVICE_ID, MAX_DEVICE_ID)
    spin.setValue(value)
    return spin


def _find_default_firmware() -> str:
    builds = sorted((PROJECT_DIR / ".pio" / "build").glob("*/firmware.bin"),
                    key=lambda p: p.stat().st_mtime, reverse=True)
    return str(builds[0]) if builds else ""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("StepperDriver Service Tool")
        self.settings = QSettings("StepperDriver", "BoardTool")
        self._thread: QThread | None = None
        self._worker: Worker | None = None
        self._on_done = None
        self._firmware: Firmware | None = None
        self._resume_control = False
        self._resume_on_error = True

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.addWidget(self._build_connection_box())

        self.control = ControlTab(self._link_settings)
        self.control.log.connect(self._log)
        self.control.connection_changed.connect(lambda _: self._update_port_widgets())
        self.tabs = QTabWidget()
        self.tabs.addTab(self.control, "Control")
        self.tabs.addTab(self._build_id_tab(), "Board ID")
        self.tabs.addTab(self._build_flash_tab(), "Firmware")
        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=self.control.emergency_stop)
        layout.addWidget(self.tabs)

        progress_row = QHBoxLayout()
        self.progress_label = QLabel("")
        self.progress_label.setMinimumWidth(170)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel)
        progress_row.addWidget(self.progress_label)
        progress_row.addWidget(self.progress, 1)
        progress_row.addWidget(self.cancel_button)
        layout.addLayout(progress_row)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setFont(QFont("monospace"))
        self.log_view.setMinimumHeight(LOG_MIN_HEIGHT)
        layout.addWidget(self.log_view, 1)

        self._port_widgets = [self.port_combo, self.refresh_button, self.baud_combo, self.echo_check]
        self._action_buttons = [self.scan_button, self.check_button, self.change_button, self.flash_button]
        self._refresh_ports()
        self._load_settings()
        self.fix_window_size(QApplication.primaryScreen().availableGeometry())

    def fix_window_size(self, available) -> None:
        """Окно неизменяемого размера. Вкладки — ровно по самой высокой (Control),
        всё остальное по высоте отдаётся журналу."""
        self.tabs.setFixedHeight(self.tabs.minimumSizeHint().height())
        minimum = self.minimumSizeHint()
        width = max(minimum.width(), min(WINDOW_WIDTH, available.width() - 20))
        # 40 px — на заголовок окна, которого нет в размерах клиентской области
        height = max(minimum.height(), min(WINDOW_HEIGHT, available.height() - 40))
        self.setFixedSize(width, height)

    # ------------------------------------------------------------ интерфейс

    def _build_connection_box(self) -> QWidget:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        self.port_combo = QComboBox()
        self.port_combo.setEditable(True)
        self.port_combo.setMinimumWidth(260)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self._refresh_ports)
        self.baud_combo = QComboBox()
        self.baud_combo.setEditable(True)
        self.baud_combo.addItems(BAUDRATES)
        self.echo_check = QCheckBox("Adapter echo")
        self.echo_check.setToolTip("Check this if your USB-RS485 adapter sends back everything it transmits")
        row.addWidget(QLabel("Port:"))
        row.addWidget(self.port_combo, 1)
        row.addWidget(self.refresh_button)
        row.addWidget(QLabel("Baud:"))
        row.addWidget(self.baud_combo)
        row.addWidget(self.echo_check)
        return box

    def _build_id_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        scan_box = QGroupBox("Find boards on the bus")
        scan_layout = QVBoxLayout(scan_box)
        row = QHBoxLayout()
        self.scan_from = _id_spin(1)
        self.scan_to = _id_spin(16)
        self.scan_button = QPushButton("Search")
        self.scan_button.clicked.connect(self._scan)
        row.addWidget(QLabel("ID from"))
        row.addWidget(self.scan_from)
        row.addWidget(QLabel("to"))
        row.addWidget(self.scan_to)
        row.addStretch(1)
        row.addWidget(self.scan_button)
        scan_layout.addLayout(row)

        self.boards_table = QTableWidget(0, 1 + len(AXIS_NAMES))
        self.boards_table.setHorizontalHeaderLabels(["ID"] + [f"Axis {name}" for name in AXIS_NAMES])
        self.boards_table.verticalHeader().setVisible(False)
        self.boards_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.boards_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.boards_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        header = self.boards_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.boards_table.itemSelectionChanged.connect(self._board_selected)
        scan_layout.addWidget(self.boards_table)
        layout.addWidget(scan_box, 1)

        change_box = QGroupBox("Change ID")
        grid = QGridLayout(change_box)
        self.current_id = _id_spin(1)
        self.new_id = _id_spin(2)
        self.check_button = QPushButton("Check")
        self.check_button.clicked.connect(self._check_board)
        self.change_button = QPushButton("Change ID")
        self.change_button.clicked.connect(self._change_id)
        grid.addWidget(QLabel("Current ID:"), 0, 0)
        grid.addWidget(self.current_id, 0, 1)
        grid.addWidget(self.check_button, 0, 2)
        grid.addWidget(QLabel("New ID:"), 1, 0)
        grid.addWidget(self.new_id, 1, 1)
        grid.addWidget(self.change_button, 1, 2)
        grid.setColumnStretch(3, 1)
        hint = QLabel("Two boards with the same ID on one bus disturb each other. "
                      "Connect and set up new boards (ID 1) one at a time.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray")
        grid.addWidget(hint, 2, 0, 1, 4)
        layout.addWidget(change_box)
        return tab

    def _build_flash_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        file_box = QGroupBox("Firmware file")
        grid = QGridLayout(file_box)
        self.fw_path = QLineEdit()
        self.fw_path.setPlaceholderText(".pio/build/<env>/firmware.bin or .hex")
        self.fw_path.editingFinished.connect(self._update_firmware_info)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_firmware)
        self.bin_address = QLineEdit(f"0x{FLASH_BASE:08X}")
        self.bin_address.setMaximumWidth(120)
        self.bin_address.editingFinished.connect(self._update_firmware_info)
        self.fw_info = QLabel("No file selected")
        self.fw_info.setWordWrap(True)
        grid.addWidget(self.fw_path, 0, 0, 1, 3)
        grid.addWidget(browse, 0, 3)
        grid.addWidget(QLabel(".bin address:"), 1, 0)
        grid.addWidget(self.bin_address, 1, 1)
        grid.setColumnStretch(2, 1)
        grid.addWidget(self.fw_info, 2, 0, 1, 4)
        layout.addWidget(file_box)

        options_box = QGroupBox("Options")
        form = QFormLayout(options_box)
        form.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)
        self.flash_id = _id_spin(1)
        self.in_bootloader = QCheckBox("Board is already in bootloader (BOOT0 jumper or interrupted flashing)")
        self.in_bootloader.setToolTip("No switch-to-bootloader command is sent. "
                                      "The ID is only used to check the board after flashing.")
        self.erase_sectors = QRadioButton("Firmware sectors only — board ID is kept")
        self.erase_all = QRadioButton("Whole flash — board ID resets to 1")
        self.erase_sectors.setChecked(True)
        erase_widget = QWidget()
        erase_layout = QVBoxLayout(erase_widget)
        erase_layout.setContentsMargins(0, 0, 0, 0)
        erase_layout.addWidget(self.erase_sectors)
        erase_layout.addWidget(self.erase_all)
        self.verify_check = QCheckBox("Verify after writing")
        self.verify_check.setChecked(True)
        self.boot_baud = QComboBox()
        self.boot_baud.addItems(BOOTLOADER_BAUDRATES)
        form.addRow("Board ID:", self.flash_id)
        form.addRow("", self.in_bootloader)
        form.addRow("Erase:", erase_widget)
        form.addRow("", self.verify_check)
        form.addRow("Bootloader baud:", self.boot_baud)
        layout.addWidget(options_box)

        self.flash_button = QPushButton("Flash")
        self.flash_button.setMinimumHeight(36)
        font = self.flash_button.font()
        font.setBold(True)
        self.flash_button.setFont(font)
        self.flash_button.clicked.connect(self._flash)
        layout.addWidget(self.flash_button)
        layout.addStretch(1)
        return tab

    # ------------------------------------------------------------ настройки

    def _load_settings(self) -> None:
        port = self.settings.value("port", "")
        if port:
            index = self.port_combo.findData(port)
            if index >= 0:
                self.port_combo.setCurrentIndex(index)
            else:
                self.port_combo.setEditText(port)
        self.baud_combo.setCurrentText(self.settings.value("baudrate", "115200"))
        self.echo_check.setChecked(self.settings.value("echo", False, type=bool))
        self.fw_path.setText(self.settings.value("firmware", "") or _find_default_firmware())
        self.bin_address.setText(self.settings.value("bin_address", f"0x{FLASH_BASE:08X}"))
        self.flash_id.setValue(self.settings.value("flash_id", 1, type=int))
        self.current_id.setValue(self.flash_id.value())
        self.boot_baud.setCurrentText(self.settings.value("boot_baudrate", "115200"))
        self.control.load_settings(self.settings)
        if self.fw_path.text():
            self._update_firmware_info()

    def _save_settings(self) -> None:
        self.settings.setValue("port", self._port())
        self.settings.setValue("baudrate", self.baud_combo.currentText())
        self.settings.setValue("echo", self.echo_check.isChecked())
        self.settings.setValue("firmware", self.fw_path.text())
        self.settings.setValue("bin_address", self.bin_address.text())
        self.settings.setValue("flash_id", self.flash_id.value())
        self.settings.setValue("boot_baudrate", self.boot_baud.currentText())
        self.control.save_settings(self.settings)

    def closeEvent(self, event) -> None:
        if self._thread is not None:
            QMessageBox.warning(self, "Operation running", "Wait until the operation finishes or press Cancel.")
            event.ignore()
            return
        self.control.shutdown()
        self._save_settings()
        event.accept()

    # ------------------------------------------------------------ служебное

    def _log(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"{stamp}  {message}")

    def _refresh_ports(self) -> None:
        current = self._port()
        self.port_combo.clear()
        for info in sorted(list_ports.comports(), key=lambda p: p.device):
            label = info.device if info.description in ("", "n/a") else f"{info.device} — {info.description}"
            self.port_combo.addItem(label, info.device)
        index = self.port_combo.findData(current)
        if index >= 0:
            self.port_combo.setCurrentIndex(index)
        elif current:
            self.port_combo.setEditText(current)

    def _port(self) -> str:
        text = self.port_combo.currentText().strip()
        index = self.port_combo.currentIndex()
        if index >= 0 and self.port_combo.itemText(index) == text:
            return self.port_combo.itemData(index) or text
        return text

    def _link_settings(self) -> ops.LinkSettings | None:
        try:
            baudrate = int(self.baud_combo.currentText())
        except ValueError:
            QMessageBox.warning(self, "Baud", "Baud rate must be a number")
            return None
        port = self._port()
        if not port:
            QMessageBox.warning(self, "Port", "Select a serial port")
            return None
        return ops.LinkSettings(port, baudrate, self.echo_check.isChecked())

    def _update_port_widgets(self) -> None:
        # порт держит либо операция, либо сеанс управления — настройки на это время не меняем
        free = self._thread is None and not self.control.connected
        for widget in self._port_widgets:
            widget.setEnabled(free)

    def _start(self, title: str, on_done, fn, *args, resume_on_error: bool = True) -> None:
        if self._thread is not None:
            return
        self._save_settings()
        self._resume_control = self.control.pause()
        self._resume_on_error = resume_on_error
        self.control.set_busy(True)
        if self._resume_control:
            self._log("Axis polling paused for this operation")
        for widget in self._action_buttons:
            widget.setEnabled(False)
        self._update_port_widgets()
        self.cancel_button.setEnabled(True)
        self.progress_label.setText(title)
        self.progress.setRange(0, 0)
        self._on_done = on_done

        self._worker = Worker(fn, *args)
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.log.connect(self._log)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._thread.start()

    @Slot(int, int, str)
    def _on_progress(self, done: int, total: int, phase: str) -> None:
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(done)
        if phase:
            self.progress_label.setText(phase)

    @Slot(object, str, bool)
    def _on_finished(self, result, error: str, cancelled: bool) -> None:
        self._thread.quit()
        self._thread.wait()
        self._thread.deleteLater()
        self._worker.deleteLater()
        self._thread = None
        self._worker = None

        for widget in self._action_buttons:
            widget.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(0 if error else 1)
        self.control.set_busy(False)

        if cancelled:
            self.progress_label.setText("Cancelled")
            self._log(error)
            self._release_control(failed=True)
            QMessageBox.information(self, "Cancelled", error)
        elif error:
            self.progress_label.setText("Error")
            self._log("ERROR: " + error)
            self._release_control(failed=True)
            QMessageBox.warning(self, "Error", error)
        else:
            self.progress_label.setText("Done")
            if self._on_done:
                self._on_done(result)       # может сменить ID для управления и сам возобновить опрос
            self._release_control(failed=False)
        self._update_port_widgets()

    def _release_control(self, failed: bool) -> None:
        """Возобновляет опрос осей после операции (с уже обновлённым ID). Повторный вызов ничего не делает."""
        if not self._resume_control:
            return
        self._resume_control = False
        if failed and not self._resume_on_error:
            self.control.cancel_resume("after a failed flash the board may still be in bootloader mode. "
                                       "Connect manually when the board answers again")
        else:
            self.control.resume()
        self._update_port_widgets()

    def _cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel_event.set()
            self._log("Cancelling…")

    # ------------------------------------------------------------ вкладка ID

    def _scan(self) -> None:
        settings = self._link_settings()
        if settings is None:
            return
        first, last = sorted((self.scan_from.value(), self.scan_to.value()))
        self.boards_table.setRowCount(0)
        self._log(f"Searching boards ID {first}–{last} on {settings.port}")
        self._start("Search", self._show_boards, ops.scan_boards, settings, first, last)

    def _show_boards(self, boards: list[BoardStatus]) -> None:
        self.boards_table.setRowCount(len(boards))
        for row, status in enumerate(boards):
            self.boards_table.setItem(row, 0, QTableWidgetItem(str(status.device_id)))
            for column, axis in enumerate(status.axes, 1):
                self.boards_table.setItem(row, column, QTableWidgetItem(axis.describe()))
        if boards:
            self.boards_table.selectRow(0)

    def _board_selected(self) -> None:
        rows = self.boards_table.selectionModel().selectedRows()
        if not rows:
            return
        device_id = int(self.boards_table.item(rows[0].row(), 0).text())
        self.current_id.setValue(device_id)
        self.flash_id.setValue(device_id)
        self.control.set_device_id(device_id)

    def _check_board(self) -> None:
        settings = self._link_settings()
        if settings is not None:
            self._start("Check", None, ops.read_status, settings, self.current_id.value())

    def _change_id(self) -> None:
        settings = self._link_settings()
        if settings is None:
            return
        old_id, new_id = self.current_id.value(), self.new_id.value()
        self._start("Change ID", lambda status: self._id_changed(old_id, status),
                    ops.change_board_id, settings, old_id, new_id)

    def _id_changed(self, old_id: int, status: BoardStatus) -> None:
        for row in range(self.boards_table.rowCount()):
            item = self.boards_table.item(row, 0)
            if item.text() == str(old_id):
                item.setText(str(status.device_id))
        self.current_id.setValue(status.device_id)
        self.flash_id.setValue(status.device_id)
        if self.control.device_id() == old_id:
            self.control.set_device_id(status.device_id)
        self._release_control(failed=False)
        QMessageBox.information(self, "Change ID", f"Board now answers as ID {status.device_id}")

    # ------------------------------------------------------------ вкладка прошивки

    def _browse_firmware(self) -> None:
        start = self.fw_path.text() or str(PROJECT_DIR)
        path, _ = QFileDialog.getOpenFileName(self, "Firmware file", start,
                                              "Firmware (*.bin *.hex);;All files (*)")
        if path:
            self.fw_path.setText(path)
            self._update_firmware_info()

    def _update_firmware_info(self) -> None:
        path = self.fw_path.text().strip()
        is_bin = not path.lower().endswith((".hex", ".ihex"))
        self.bin_address.setEnabled(is_bin)
        self._firmware = None
        if not path:
            self.fw_info.setText("No file selected")
            self.fw_info.setStyleSheet("")
            return
        try:
            address = int(self.bin_address.text(), 0) if is_bin else FLASH_BASE
            fw = load_firmware(path, address)
        except ValueError:
            self.fw_info.setText(".bin address must be a number, like 0x08000000")
            self.fw_info.setStyleSheet("color: #c0392b")
            return
        except FirmwareError as e:
            self.fw_info.setText(str(e))
            self.fw_info.setStyleSheet("color: #c0392b")
            return
        self._firmware = fw
        modified = time.strftime("%Y-%m-%d %H:%M", time.localtime(Path(path).stat().st_mtime))
        text = f"{fw.size} bytes, 0x{fw.start:08X}–0x{fw.end - 1:08X}, built {modified}"
        warnings = fw.warnings()
        if warnings:
            text += "\nWarning: " + "; ".join(warnings)
        self.fw_info.setText(text)
        self.fw_info.setStyleSheet("color: #b9770e" if warnings else "")

    def _flash(self) -> None:
        settings = self._link_settings()
        if settings is None:
            return
        self._update_firmware_info()  # файл мог быть пересобран
        fw = self._firmware
        if fw is None:
            QMessageBox.warning(self, "Flash", "Select a valid firmware file")
            return
        opts = ops.FlashOptions(
            firmware=fw,
            device_id=self.flash_id.value(),
            already_in_bootloader=self.in_bootloader.isChecked(),
            mass_erase=self.erase_all.isChecked(),
            verify=self.verify_check.isChecked(),
            bootloader_baudrate=int(self.boot_baud.currentText()),
        )
        board = f"ID {opts.device_id}" + (" (already in bootloader)" if opts.already_in_bootloader else "")
        erase = "whole flash, board ID resets to 1" if opts.mass_erase else "firmware sectors only"
        text = (f"File: {Path(fw.path).name} ({fw.size} bytes)\n"
                f"Board: {board}\nErase: {erase}\n")
        if fw.warnings():
            text += "\nWarnings:\n• " + "\n• ".join(fw.warnings()) + "\n"
        text += "\nDo not unplug power or cable while flashing. Flash now?"
        if QMessageBox.question(self, "Flash", text) != QMessageBox.Yes:
            return
        self._log(f"Flashing {fw.path} → board {board}")
        self._start("Flash", self._flash_done, ops.flash_board, settings, opts, resume_on_error=False)

    def _flash_done(self, status: BoardStatus | None) -> None:
        if status is None:
            self._release_control(failed=True)
            QMessageBox.warning(self, "Flash", "Firmware was written, but the board did not answer after start. "
                                               "See the log.")
            return
        self.current_id.setValue(status.device_id)
        self.flash_id.setValue(status.device_id)
        self.control.set_device_id(status.device_id)   # после стирания всей flash ID мог стать 1
        self._release_control(failed=False)
        QMessageBox.information(self, "Flash", f"Done, board answers as ID {status.device_id}")


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("StepperDriver Board Tool")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
