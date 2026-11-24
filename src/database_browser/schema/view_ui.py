"""View list and the create/edit form for the Schema tab (UC-09, UC-10, UC-11)."""

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from ..controller import ConnectionController
from ..errors import DatabaseBrowserError
from .view_models import ViewDefinition, ViewSnapshot
from .view_repository import SQLiteViewRepository
from .view_services import ViewManager
from .view_sql import create_view_sql

OP_REFRESH = "schema.view.refresh"
OP_CREATE = "schema.view.create"
OP_ALTER = "schema.view.alter"
OP_DROP = "schema.view.drop"


def describe_view(view: ViewDefinition) -> str:
    lines = [f"View: {view.name}", ""]
    if view.error:
        lines.extend([f"This view cannot be queried now: {view.error}", ""])
    lines.append("Columns: " + (", ".join(view.columns) if view.columns else "(unknown)"))
    lines.extend(["", "Defining query:", view.query])
    if view.used_by:
        lines.extend(["", "Used by views: " + ", ".join(view.used_by)])
    if view.triggers:
        lines.extend(["", "Triggers: " + ", ".join(view.triggers)])
    if not view.editable and view.reason:
        lines.extend(["", view.reason])
    return "\n".join(lines)


class ViewDialog(QDialog):
    """Collect a view name and query without closing before SQLite accepts them."""

    submitted = pyqtSignal(object)

    def __init__(self, parent=None, view: ViewDefinition | None = None):
        super().__init__(parent)
        self._view = view
        self._busy = False
        self.setWindowTitle(f'Edit view "{view.name}"' if view else "Create view")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(780, 620)

        self.name_edit = QLineEdit()
        self.name_edit.setObjectName("view_name")
        self.query_edit = QPlainTextEdit()
        self.query_edit.setObjectName("view_query")
        self.query_edit.setPlaceholderText("SELECT ... FROM ...")
        form = QFormLayout()
        form.addRow("View name:", self.name_edit)
        self.preview_edit = QPlainTextEdit()
        self.preview_edit.setObjectName("view_sql_preview")
        self.preview_edit.setReadOnly(True)
        self.preview_edit.setMaximumHeight(140)
        self.validation_label = QLabel()
        self.validation_label.setObjectName("view_validation")
        self.validation_label.setWordWrap(True)
        self.validation_label.setStyleSheet("color: #b42318")
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self._submit)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(QLabel("Defining SELECT query"))
        layout.addWidget(self.query_edit, 1)
        if view is not None:
            note = QLabel("Saving replaces the view in one step. If SQLite rejects the new definition, the old view stays.")
            note.setWordWrap(True)
            layout.addWidget(note)
        layout.addWidget(QLabel("SQL preview"))
        layout.addWidget(self.preview_edit)
        layout.addWidget(self.validation_label)
        layout.addWidget(self.buttons)

        self.name_edit.textChanged.connect(self._update_preview)
        self.query_edit.textChanged.connect(self._update_preview)
        if view is not None:
            self.name_edit.setText(view.name)
            self.query_edit.setPlainText(view.query)
        self._update_preview()

    def get_definition(self) -> ViewDefinition:
        text = self.name_edit.text()
        # A stored name must stay exact; only a name typed in this form is trimmed.
        original = self._view.name if self._view is not None else None
        name = text if original is not None and text == original else text.strip()
        return ViewDefinition(
            name, self.query_edit.toPlainText().strip(),
            original_name=original, sql=self._view.sql if self._view is not None else None,
        )

    def _update_preview(self, *_):
        definition = self.get_definition()
        if not definition.name or not definition.query:
            self.preview_edit.clear()
            return
        try:
            self.preview_edit.setPlainText(create_view_sql(definition.name, definition.query))
        except (DatabaseBrowserError, ValueError) as error:
            self.preview_edit.setPlainText(str(error))

    def _submit(self):
        if self._busy:
            return
        definition = self.get_definition()
        if not definition.name:
            self.show_error("Enter a view name.")
            self.name_edit.setFocus()
        elif not definition.query:
            self.show_error("Enter the defining SELECT query.")
            self.query_edit.setFocus()
        else:
            self.validation_label.clear()
            # The tab accepts the dialog only after the worker reports success.
            self.submitted.emit(definition)

    def show_error(self, message: str):
        self.validation_label.setText(message)

    def set_busy(self, busy: bool):
        self._busy = busy
        for widget in (self.name_edit, self.query_edit, self.buttons):
            widget.setEnabled(not busy)

    def reject(self):
        # Closing the form while a write is running would hide its outcome.
        if not self._busy:
            super().reject()


class ViewsTab(QWidget):
    """List views and submit create, edit, and delete operations to the connection's worker."""

    def __init__(self, controller: ConnectionController, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.snapshot = ViewSnapshot(())
        self.view_dialog: ViewDialog | None = None
        self._connection_key = None
        self._needs_refresh = False
        self._refresh_error: str | None = None

        self.active_label = QLabel("Connect a database to view its views.")
        self.active_label.setWordWrap(True)
        self.btn_create = QPushButton("Create view…")
        self.btn_edit = QPushButton("Edit view…")
        self.btn_drop = QPushButton("Delete view…")
        self.btn_refresh = QPushButton("Refresh")
        self.btn_create.clicked.connect(self._new_view)
        self.btn_edit.clicked.connect(self._edit_view)
        self.btn_drop.clicked.connect(self._drop_view)
        self.btn_refresh.clicked.connect(self.refresh)
        buttons = QHBoxLayout()
        for button in (self.btn_create, self.btn_edit, self.btn_drop, self.btn_refresh):
            buttons.addWidget(button)
        buttons.addStretch()
        self.views_list = QListWidget()
        self.views_list.setObjectName("views_list")
        self.views_list.currentItemChanged.connect(self._selection_changed)
        self.details_edit = QPlainTextEdit()
        self.details_edit.setObjectName("view_details")
        self.details_edit.setReadOnly(True)
        split = QSplitter()
        split.addWidget(self.views_list)
        split.addWidget(self.details_edit)
        split.setStretchFactor(1, 1)
        self.message_label = QLabel()
        self.message_label.setObjectName("views_message")
        self.message_label.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(self.active_label)
        layout.addLayout(buttons)
        layout.addWidget(split, 1)
        layout.addWidget(self.message_label)

        controller.connection_changed.connect(self._connection_changed)
        controller.busy_changed.connect(self._set_busy)
        controller.operation_result.connect(self._operation_result)
        controller.operation_failed.connect(self._operation_failed)
        controller.operation_finished.connect(self._operation_finished)
        self._connection_changed(controller.active)
        self._set_busy(controller.busy)
        if self._needs_refresh and not controller.busy:
            self.refresh()

    # ----- connection state -----

    def schema_outdated(self):
        """Tables changed, so view definitions and their health may have changed too."""
        self._needs_refresh = self.controller.active is not None

    def _connection_changed(self, active):
        key = (active.profile.id, active.file_path, active.read_only) if active else None
        if key != self._connection_key:
            self._connection_key = key
            self._needs_refresh = active is not None
            self._show_snapshot(ViewSnapshot(()))
            self.message_label.clear()
            self._refresh_error = None
            if self.view_dialog is not None:
                self.view_dialog.set_busy(False)
                self.view_dialog.reject()
        if active:
            mode = "read-only" if active.read_only else "read/write"
            self.active_label.setText(f"Views in {active.file_path} ({mode})")
        else:
            self.active_label.setText("Connect a database to view its views.")

    def _set_busy(self, busy: bool):
        active = self.controller.active
        available = active is not None and not busy and not self.controller._closing
        writable = available and not active.read_only
        selected = self.selected_view()
        self.btn_refresh.setEnabled(available)
        self.btn_create.setEnabled(writable)
        self.btn_edit.setEnabled(writable and selected is not None and selected.editable)
        self.btn_edit.setToolTip(selected.reason if selected is not None and selected.reason else "")
        self.btn_drop.setEnabled(writable and selected is not None)
        self.views_list.setEnabled(not busy)
        if self.view_dialog is not None:
            self.view_dialog.set_busy(busy)

    def refresh(self):
        if self.controller.active is None or self.controller.busy or self.controller._closing:
            return False
        accepted = self.controller.submit_database_operation(
            OP_REFRESH,
            lambda manager: ViewManager(SQLiteViewRepository(manager.executor.connection)).get_views(),
        )
        if accepted:
            self._needs_refresh = False
        return accepted

    # ----- results -----

    def _operation_finished(self, name: str, success: bool):
        if name == "connect" and success:
            self._needs_refresh = self.controller.active is not None
        if name in {OP_ALTER, OP_DROP} and not success:
            # Another application may have changed the schema since our last read.
            self._needs_refresh = self.controller.active is not None
        if name != "shutdown" and self._needs_refresh:
            self.refresh()

    def _operation_result(self, name: str, result):
        if name not in {OP_REFRESH, OP_CREATE, OP_ALTER, OP_DROP}:
            return
        selected_name = None
        if name in {OP_CREATE, OP_ALTER} and self.view_dialog is not None:
            selected_name = self.view_dialog.get_definition().name
        self._show_snapshot(result, selected_name)
        if name != OP_REFRESH:
            self._refresh_error = None
            verb = {OP_CREATE: "created", OP_ALTER: "updated", OP_DROP: "deleted"}[name]
            message = f"View {verb}."
            if result.in_transaction:
                message = f"View {verb} in the current transaction. Commit it to save the change."
            self.message_label.setText(message)
            if name in {OP_CREATE, OP_ALTER} and self.view_dialog is not None:
                self.view_dialog.accept()
        elif self._refresh_error:
            self.message_label.setText(self._refresh_error)
            self._refresh_error = None
        elif result.views:
            broken = sum(1 for view in result.views if view.error)
            text = f"Views: {len(result.views)}"
            self.message_label.setText(text + (f" ({broken} cannot be queried)" if broken else ""))
        else:
            self.message_label.setText("No views in this database.")

    def _operation_failed(self, name: str, message: str):
        if not name.startswith("schema.view."):
            return
        self.message_label.setText(message)
        if name in {OP_ALTER, OP_DROP}:
            self._refresh_error = message
        elif name == OP_REFRESH:
            self._refresh_error = None
        if name in {OP_CREATE, OP_ALTER} and self.view_dialog is not None:
            self.view_dialog.show_error(message)

    # ----- list -----

    def _show_snapshot(self, snapshot: ViewSnapshot, selected_name: str | None = None):
        previous = self.selected_view()
        if selected_name is None and previous is not None:
            selected_name = previous.name
        self.snapshot = snapshot
        self.views_list.clear()
        for view in snapshot.views:
            suffix = (" · broken" if view.error else "") + ("" if view.editable else " · view only")
            item = QListWidgetItem(f"{view.name}{suffix}")
            item.setData(Qt.ItemDataRole.UserRole, view.name)
            self.views_list.addItem(item)
            if view.name == selected_name:
                self.views_list.setCurrentItem(item)
        if self.views_list.currentItem() is None and snapshot.views:
            self.views_list.setCurrentRow(0)
        self._selection_changed()
        self._set_busy(self.controller.busy)

    def selected_view(self) -> ViewDefinition | None:
        item = self.views_list.currentItem()
        if item is None:
            return None
        name = item.data(Qt.ItemDataRole.UserRole)
        return next((view for view in self.snapshot.views if view.name == name), None)

    def _selection_changed(self, *_):
        self._set_busy(self.controller.busy)
        view = self.selected_view()
        if view is None:
            self.details_edit.clear()
        else:
            self.details_edit.setPlainText(describe_view(view))

    # ----- actions -----

    def _new_view(self):
        if not self.btn_create.isEnabled():
            return
        self._open_dialog()

    def _edit_view(self):
        view = self.selected_view()
        if view is None or not self.btn_edit.isEnabled():
            return
        self._open_dialog(view)

    def _open_dialog(self, view: ViewDefinition | None = None):
        if self.view_dialog is not None:
            self.view_dialog.raise_()
            self.view_dialog.activateWindow()
            return
        dialog = ViewDialog(self, view=view)
        self.view_dialog = dialog
        dialog.submitted.connect(self._alter_view if view is not None else self._create_view)
        dialog.finished.connect(lambda _: self._dialog_closed(dialog))
        dialog.open()

    def _dialog_closed(self, dialog: ViewDialog):
        if self.view_dialog is dialog:
            self.view_dialog = None
        dialog.deleteLater()

    def _submit_form(self, name: str, action):
        accepted = self.controller.submit_database_operation(
            name, lambda manager: action(ViewManager(SQLiteViewRepository(manager.executor.connection))),
        )
        if not accepted and self.view_dialog is not None:
            self.view_dialog.show_error("Another database operation is running. Try again when it finishes.")

    def _create_view(self, definition: ViewDefinition):
        self._submit_form(OP_CREATE, lambda manager: manager.create_view(definition))

    def _alter_view(self, definition: ViewDefinition):
        self._submit_form(OP_ALTER, lambda manager: manager.alter_view(definition))

    def _drop_view(self):
        view = self.selected_view()
        if view is None or not self.btn_drop.isEnabled():
            return
        text = f'Delete view "{view.name}"?\n\nThe tables it reads are not changed.'
        if view.used_by:
            text += "\n\nViews that use it will stop working: " + ", ".join(view.used_by) + "."
        if view.triggers:
            text += "\n\nIts triggers are deleted with it: " + ", ".join(view.triggers) + "."
        answer = QMessageBox.question(
            self, "Delete view?", text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.submit_database_operation(
                OP_DROP,
                lambda manager: ViewManager(SQLiteViewRepository(manager.executor.connection)).drop_view(view.name),
            )
