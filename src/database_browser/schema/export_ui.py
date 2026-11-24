"""The Export section of the Schema tab"""

from os.path import lexists
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFileDialog, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget

from ..controller import ConnectionController
from ..models import ActiveConnection
from .export import export_schema


OP_EXPORT = "schema.export.refresh"


class ExportTab(QWidget):
    def __init__(self, controller: ConnectionController, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._active: ActiveConnection | None = None

        self.source_label = QLabel()
        self.source_label.setTextFormat(Qt.TextFormat.PlainText)
        self.source_label.setWordWrap(True)
        description = QLabel("Export tables with their constraints, indexes and views. Table rows are excluded")
        description.setWordWrap(True)
        self.btn_export = QPushButton("Export SQL…")
        self.btn_export.clicked.connect(self._choose_file)
        self.message_label = QLabel()
        self.message_label.setTextFormat(Qt.TextFormat.PlainText)
        self.message_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(self.source_label)
        layout.addWidget(description)
        layout.addWidget(self.btn_export, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.message_label)
        layout.addStretch()

        controller.connection_changed.connect(self._show_connection)
        controller.busy_changed.connect(self._set_busy)
        controller.operation_result.connect(self._show_result)
        controller.operation_failed.connect(self._show_error)
        self._show_connection(controller.active)

    def _show_connection(self, active: ActiveConnection | None):
        if active != self._active:
            self.message_label.clear()
        self._active = active
        self.source_label.setText(
            f"Database: {active.file_path}" if active else "Connect a database to export its schema"
        )
        self._set_busy(self.controller.busy)

    def _set_busy(self, busy: bool):
        self.btn_export.setEnabled(self._active is not None and not busy and not self.controller._closing)

    def _choose_file(self):
        active = self._active
        if active is None or not self.btn_export.isEnabled():
            return
        self.message_label.clear()
        source = Path(active.file_path)
        suggested = source.with_name(f"{source.stem}_schema.sql")
        file_name, _ = QFileDialog.getSaveFileName(
            self, "Export SQLite schema", str(suggested), "SQL files (*.sql)",
            options=QFileDialog.Option.DontConfirmOverwrite,
        )
        if not file_name:
            return
        destination = Path(file_name)
        if not destination.name:
            self.message_label.setText("Choose a SQL file")
            return
        if not destination.suffix:
            destination = destination.with_suffix(".sql")
        if destination.suffix.lower() != ".sql":
            self.message_label.setText("Choose a file ending in .sql")
            return
        overwrite = lexists(destination)
        if overwrite and QMessageBox.question(
            self, "Replace SQL file?", f'Replace "{destination}"?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        if active != self.controller.active:
            self.message_label.setText("The active database changed. Choose the export file again")
            return
        self.message_label.setText("Exporting schema…")
        accepted = self.controller.submit_database_operation(
            OP_EXPORT,
            lambda manager: export_schema(manager.executor.connection, destination, overwrite=overwrite),
        )
        if not accepted:
            self.message_label.setText("Another database operation is running. Try again when it finishes")

    def _show_result(self, name: str, destination: Path):
        if name == OP_EXPORT:
            self.message_label.setText(f"Schema exported to {destination}")

    def _show_error(self, name: str, message: str):
        if name == OP_EXPORT:
            self.message_label.setText(message)
