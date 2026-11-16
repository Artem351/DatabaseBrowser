"""Application window, connection list, and edit form."""

from dataclasses import replace

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from .controller import ConnectionController
from .models import ConnectionProfile


class NotificationManager:
    """Show error messages to the user."""

    @staticmethod
    def show_error(parent, message: str):
        """Open a dialog with the error message."""
        QMessageBox.critical(parent, "Database Browser", message)


class ConnectionDialog(QDialog):
    """Shared form for creating and editing a profile."""

    def __init__(self, parent=None, profile: ConnectionProfile | None = None):
        """Fill the fields from the current profile or leave them empty."""
        super().__init__(parent)
        self._profile = profile
        self.setWindowTitle("Edit saved connection" if profile else "Open SQLite database")
        self.resize(550, 180)
        self.name_edit = QLineEdit(profile.name if profile else "")
        self.name_edit.setObjectName("connection_name")
        self.name_edit.setPlaceholderText("Optional; defaults to the file name")
        self.path_edit = QLineEdit(profile.file_path if profile else "")
        self.path_edit.setObjectName("connection_path")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row = QHBoxLayout()
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        form = QFormLayout()
        form.addRow("Name:", self.name_edit)
        form.addRow("Database file:", path_row)
        self.validation_label = QLabel()
        self.validation_label.setStyleSheet("color: #b42318")
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("Save" if profile else "Connect")
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.validation_label)
        if profile:
            note = QLabel("Changes apply to the saved profile. Reconnect to use a new database path.")
            note.setWordWrap(True)
            layout.addWidget(note)
        layout.addWidget(self.buttons)

    def _browse(self):
        """Use the path selected in the standard file dialog."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose an existing SQLite database", self.path_edit.text(),
            "SQLite databases (*.sqlite *.sqlite3 *.db);;All files (*)",
        )
        if path:
            self.path_edit.setText(path)

    def _accept(self):
        """Prevent submitting the form with an empty path."""
        # The service checks file format and integrity without blocking the UI.
        if not self.path_edit.text().strip():
            self.validation_label.setText("Choose a database file.")
            self.path_edit.setFocus()
            return
        self.accept()

    def get_profile(self) -> ConnectionProfile:
        """Build a profile from the fields, keeping its ID when editing."""
        base = self._profile or ConnectionProfile(None, "", "")
        return replace(base, name=self.name_edit.text().strip(), file_path=self.path_edit.text().strip())


class DatabaseTab(QWidget):
    """Profile list and actions for the three use cases in the Database tab."""

    def __init__(self, controller: ConnectionController, parent=None):
        """Create the tab widgets and connect the controller signals."""
        super().__init__(parent)
        self.controller = controller
        self.connections_list = QListWidget()
        self.connections_list.setObjectName("connections_list")
        self.connections_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.connections_list.currentItemChanged.connect(self._selection_changed)
        self.connections_list.itemDoubleClicked.connect(lambda _: self._connect_saved())
        self.btn_new = QPushButton("Open database…")
        self.btn_connect = QPushButton("Connect")
        self.btn_edit_connection = QPushButton("Edit…")
        self.btn_delete_connection = QPushButton("Delete…")
        self.btn_new.clicked.connect(self._new_connection)
        self.btn_connect.clicked.connect(self._connect_saved)
        self.btn_edit_connection.clicked.connect(self._edit_connection)
        self.btn_delete_connection.clicked.connect(self._delete_connection)
        buttons = QHBoxLayout()
        for button in [self.btn_new, self.btn_connect, self.btn_edit_connection, self.btn_delete_connection]:
            buttons.addWidget(button)
        buttons.addStretch()
        self.path_label = QLabel("No saved connection selected.")
        self.path_label.setWordWrap(True)
        self.active_label = QLabel("Not connected")
        self.active_label.setObjectName("active_connection")
        self.active_label.setWordWrap(True)
        self.active_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.message_label = QLabel("Open an existing SQLite database to get started.")
        self.message_label.setWordWrap(True)
        self.busy_label = QLabel()
        layout = QVBoxLayout(self)
        title = QLabel("Saved connections")
        title.setStyleSheet("font-size: 18px; font-weight: 600")
        layout.addWidget(title)
        layout.addWidget(self.connections_list, 1)
        layout.addLayout(buttons)
        layout.addWidget(self.path_label)
        layout.addSpacing(12)
        layout.addWidget(self.active_label)
        layout.addWidget(self.message_label)
        layout.addWidget(self.busy_label)
        controller.profiles_changed.connect(self._show_profiles)
        controller.connection_changed.connect(self._show_connection)
        controller.busy_changed.connect(self._set_busy)
        controller.operation_failed.connect(self._operation_error)
        controller.operation_finished.connect(self._operation_finished)
        self._show_profiles(controller.profiles)
        self._set_busy(controller.busy)

    def _selected_profile(self) -> ConnectionProfile | None:
        """Find the selected profile using the ID stored in the list item."""
        item = self.connections_list.currentItem()
        if item is None:
            return None
        identifier = item.data(Qt.ItemDataRole.UserRole)
        return next((profile for profile in self.controller.profiles if profile.id == identifier), None)

    def _show_profiles(self, profiles: list[ConnectionProfile]):
        """Refresh the list and keep the previous selection when possible."""
        selected = self._selected_profile()
        selected_id = selected.id if selected else None
        self.connections_list.clear()
        for profile in profiles:
            item = QListWidgetItem(profile.name)
            # Names can repeat, so actions use the ID rather than the displayed text.
            item.setData(Qt.ItemDataRole.UserRole, profile.id)
            item.setToolTip(profile.file_path)
            self.connections_list.addItem(item)
            if profile.id == selected_id:
                self.connections_list.setCurrentItem(item)
        if self.connections_list.currentItem() is None and profiles:
            self.connections_list.setCurrentRow(0)
        self._selection_changed()

    def _selection_changed(self, *_):
        """Show the selected profile path and update the buttons."""
        selected = self._selected_profile()
        self.path_label.setText(selected.file_path if selected else "No saved connection selected.")
        self._set_busy(self.controller.busy)

    def _set_busy(self, busy: bool):
        """Disable actions while a background task is running."""
        self.btn_new.setEnabled(not busy)
        self.connections_list.setEnabled(not busy)
        selected = self._selected_profile() is not None
        for button in [self.btn_connect, self.btn_edit_connection, self.btn_delete_connection]:
            button.setEnabled(not busy and selected)
        self.busy_label.setText("Working…" if busy else "")

    def _show_connection(self, active):
        """Show the active file and a reconnect reminder after a path change."""
        if active is None:
            self.active_label.setText("Not connected")
            return
        mode = "read-only" if active.read_only else "read/write"
        text = f"Connected ({mode}): {active.file_path}"
        # The saved profile may have changed while the session still uses the old file.
        saved = next((p for p in self.controller.profiles if p.id == active.profile.id), None)
        if saved and saved.file_path != active.file_path:
            text += "\nThe saved path has changed. Reconnect to use it."
        self.active_label.setText(text)

    def _show_error(self, message: str):
        """Keep the error on the tab and show a separate message."""
        self.message_label.setText(message)
        NotificationManager.show_error(self, message)

    def _operation_error(self, name: str, message: str):
        # Schema errors belong to their own tab and should only be shown once.
        if not name.startswith("schema."):
            self._show_error(message)

    def _operation_finished(self, name: str, success: bool):
        """Show the result message after a successful operation."""
        if not success:
            return
        messages = {
            "connect": "Database connected and profile available in the list.",
            "edit": "Connection profile saved. Reconnect to apply a changed path.",
            "delete": "Saved connection deleted. The database file was not deleted.",
        }
        if name in messages:
            self.message_label.setText(messages[name])

    def _new_connection(self):
        """Open the new connection form; submit nothing if it is cancelled."""
        dialog = ConnectionDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            profile = dialog.get_profile()
            self.controller.create(profile.name, profile.file_path)

    def _connect_saved(self):
        """Connect using the selected profile."""
        selected = self._selected_profile()
        if selected:
            self.controller.connect_saved(selected.id)

    def _edit_connection(self):
        """Open the filled form and submit the confirmed changes."""
        selected = self._selected_profile()
        if not selected:
            return
        dialog = ConnectionDialog(self, selected)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.controller.edit(dialog.get_profile())

    def _delete_connection(self):
        """Ask for confirmation before deleting the record."""
        selected = self._selected_profile()
        if not selected:
            return
        answer = QMessageBox.question(
            self, "Delete saved connection?",
            f'Delete "{selected.name}"?\n\nThe database file will NOT be deleted. '
            "If this profile has an open connection, it will be closed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.delete(selected.id)


class MainWindow(QMainWindow):
    """Main window with the project tabs."""

    def __init__(self, controller: ConnectionController):
        """Add the connection tab and placeholders for the other modules."""
        super().__init__()
        self.controller = controller
        self._ready_to_close = False
        self.setWindowTitle("Database Browser")
        self.resize(850, 560)
        self.tab_widget = QTabWidget()
        self.database_tab = DatabaseTab(controller)
        self.tab_widget.addTab(self.database_tab, "Database")
        from .schema.ui import SchemaTab

        self.schema_tab = SchemaTab(controller)
        self.tab_widget.addTab(self.schema_tab, "Schema")
        # The team will add the other use cases as separate modules.
        for name in ["SQL Editor", "Query Plan"]:
            index = self.tab_widget.addTab(QWidget(), name)
            self.tab_widget.setTabEnabled(index, False)
        self.setCentralWidget(self.tab_widget)
        controller.shutdown_complete.connect(self._finish_close)

    def closeEvent(self, event):
        """Delay closing the window until the connection is released."""
        if self._ready_to_close:
            event.accept()
        else:
            # Wait for the worker before closing the window so the connection is released.
            event.ignore()
            self.controller.request_shutdown()

    def _finish_close(self):
        """Close the window after the worker shutdown signal."""
        self._ready_to_close = True
        self.close()
