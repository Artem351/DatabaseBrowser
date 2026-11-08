"""Index list and editor for the Schema tab."""

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..controller import ConnectionController
from ..errors import DatabaseBrowserError
from .models import Index, IndexColumn, SchemaSnapshot, TableInfo
from .repositories import SQLiteIndexRepository
from .services import SchemaManager
from .sql import create_index_sql


class IndexDialog(QDialog):
    """Collect an index definition without closing before SQLite accepts it."""

    submitted = pyqtSignal(object)

    def __init__(self, tables: tuple[TableInfo, ...], parent=None):
        super().__init__(parent)
        self._tables = tables
        self._busy = False
        self.setWindowTitle("Create index")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(720, 620)

        self.table_combo = QComboBox()
        self.table_combo.setObjectName("index_table")
        self.table_combo.addItems([table.name for table in tables])
        self.name_edit = QLineEdit()
        self.name_edit.setObjectName("index_name")
        self.unique_check = QCheckBox("UNIQUE")
        self.unique_check.setToolTip("Reject duplicate combinations of the selected values.")
        form = QFormLayout()
        form.addRow("Table:", self.table_combo)
        form.addRow("Index name:", self.name_edit)
        form.addRow("", self.unique_check)

        self.columns_table = QTableWidget(0, 2)
        self.columns_table.setObjectName("index_columns")
        self.columns_table.setHorizontalHeaderLabels(["Column", "Direction"])
        self.columns_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.columns_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.columns_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.columns_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.columns_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.btn_up = QPushButton("Move up")
        self.btn_down = QPushButton("Move down")
        self.btn_up.clicked.connect(lambda: self._move_column(-1))
        self.btn_down.clicked.connect(lambda: self._move_column(1))
        order_buttons = QHBoxLayout()
        order_buttons.addWidget(self.btn_up)
        order_buttons.addWidget(self.btn_down)
        order_buttons.addStretch()

        self.preview_edit = QPlainTextEdit()
        self.preview_edit.setObjectName("index_sql_preview")
        self.preview_edit.setReadOnly(True)
        self.preview_edit.setMaximumHeight(130)
        self.validation_label = QLabel()
        self.validation_label.setObjectName("index_validation")
        self.validation_label.setWordWrap(True)
        self.validation_label.setStyleSheet("color: #b42318")
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
        )
        self.buttons.accepted.connect(self._submit)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(QLabel("Check columns to include. Their order follows the list."))
        layout.addWidget(self.columns_table, 1)
        layout.addLayout(order_buttons)
        layout.addWidget(QLabel("SQL preview"))
        layout.addWidget(self.preview_edit)
        layout.addWidget(self.validation_label)
        layout.addWidget(self.buttons)

        self.table_combo.currentIndexChanged.connect(self._show_columns)
        self.name_edit.textChanged.connect(self._update_preview)
        self.unique_check.toggled.connect(self._update_preview)
        self.columns_table.itemChanged.connect(self._update_preview)
        self.columns_table.currentCellChanged.connect(self._update_move_buttons)
        self._show_columns()

    def _show_columns(self, *_):
        name = self.table_combo.currentText()
        table = next((table for table in self._tables if table.name == name), None)
        rows = [(column, False, False) for column in table.columns] if table else []
        self._fill_columns(rows)

    def _fill_columns(self, rows: list[tuple[str, bool, bool]], selected_row: int = -1):
        self.columns_table.blockSignals(True)
        self.columns_table.setRowCount(0)
        self.columns_table.setRowCount(len(rows))
        for row, (name, checked, descending) in enumerate(rows):
            item = QTableWidgetItem(name)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            self.columns_table.setItem(row, 0, item)
            direction = QComboBox()
            direction.addItems(["ASC", "DESC"])
            direction.setCurrentIndex(int(descending))
            direction.currentIndexChanged.connect(self._update_preview)
            self.columns_table.setCellWidget(row, 1, direction)
        self.columns_table.blockSignals(False)
        if selected_row >= 0:
            self.columns_table.setCurrentCell(selected_row, 0)
        self._update_preview()
        self._update_move_buttons()

    def _column_rows(self) -> list[tuple[str, bool, bool]]:
        rows = []
        for row in range(self.columns_table.rowCount()):
            item = self.columns_table.item(row, 0)
            direction = self.columns_table.cellWidget(row, 1)
            rows.append((item.text(), item.checkState() == Qt.CheckState.Checked, direction.currentIndex() == 1))
        return rows

    def _move_column(self, offset: int):
        row = self.columns_table.currentRow()
        target = row + offset
        rows = self._column_rows()
        if self._busy or row < 0 or not 0 <= target < len(rows):
            return
        rows[row], rows[target] = rows[target], rows[row]
        self._fill_columns(rows, target)

    def _update_move_buttons(self, *_):
        row = self.columns_table.currentRow()
        self.btn_up.setEnabled(not self._busy and row > 0)
        self.btn_down.setEnabled(not self._busy and 0 <= row < self.columns_table.rowCount() - 1)

    def get_index(self) -> Index:
        columns = tuple(IndexColumn(name, descending) for name, checked, descending in self._column_rows() if checked)
        return Index(
            name=self.name_edit.text().strip(), table_name=self.table_combo.currentText(),
            columns=columns, unique=self.unique_check.isChecked(),
        )

    def _update_preview(self, *_):
        index = self.get_index()
        if not index.name or not index.table_name or not index.columns:
            self.preview_edit.clear()
            return
        try:
            self.preview_edit.setPlainText(create_index_sql(index))
        except (DatabaseBrowserError, ValueError) as error:
            self.preview_edit.setPlainText(str(error))

    def _submit(self):
        if self._busy:
            return
        index = self.get_index()
        if not index.table_name:
            self.show_error("Choose a table.")
        elif not index.name:
            self.show_error("Enter an index name.")
            self.name_edit.setFocus()
        elif not index.columns:
            self.show_error("Select at least one column.")
            self.columns_table.setFocus()
        else:
            self.validation_label.clear()
            # The tab accepts the dialog only after the worker reports success.
            self.submitted.emit(index)

    def show_error(self, message: str):
        self.validation_label.setText(message)

    def set_busy(self, busy: bool):
        self._busy = busy
        for widget in (self.table_combo, self.name_edit, self.unique_check, self.columns_table, self.buttons):
            widget.setEnabled(not busy)
        self._update_move_buttons()

    def reject(self):
        # Closing the form while a write is running would hide its outcome.
        if not self._busy:
            super().reject()


class SchemaTab(QWidget):
    """Show index metadata and submit changes to the connection's worker."""

    def __init__(self, controller: ConnectionController, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.snapshot = SchemaSnapshot((), ())
        self.index_dialog: IndexDialog | None = None
        self._connection_key = None
        self._needs_refresh = False
        self._refresh_error: str | None = None

        self.active_label = QLabel("Connect a database to view its indexes.")
        self.active_label.setWordWrap(True)
        self.btn_refresh = QPushButton("Refresh")
        self.btn_create = QPushButton("Create index…")
        self.btn_delete = QPushButton("Delete index…")
        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_create.clicked.connect(self._new_index)
        self.btn_delete.clicked.connect(self._delete_index)
        buttons = QHBoxLayout()
        buttons.addWidget(self.btn_create)
        buttons.addWidget(self.btn_delete)
        buttons.addWidget(self.btn_refresh)
        buttons.addStretch()
        self.indexes_list = QListWidget()
        self.indexes_list.setObjectName("indexes_list")
        self.indexes_list.currentItemChanged.connect(self._selection_changed)
        self.details_edit = QPlainTextEdit()
        self.details_edit.setObjectName("index_details")
        self.details_edit.setReadOnly(True)
        split = QSplitter()
        split.addWidget(self.indexes_list)
        split.addWidget(self.details_edit)
        split.setStretchFactor(1, 1)
        self.message_label = QLabel()
        self.message_label.setObjectName("schema_message")
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

    def _connection_changed(self, active):
        key = (active.profile.id, active.file_path, active.read_only) if active else None
        if key != self._connection_key:
            self._connection_key = key
            self._needs_refresh = active is not None
            self._show_snapshot(SchemaSnapshot((), ()))
            self.message_label.clear()
            self._refresh_error = None
            if self.index_dialog is not None:
                self.index_dialog.set_busy(False)
                self.index_dialog.reject()
        if active:
            mode = "read-only" if active.read_only else "read/write"
            self.active_label.setText(f"Indexes in {active.file_path} ({mode})")
        else:
            self.active_label.setText("Connect a database to view its indexes.")

    def _set_busy(self, busy: bool):
        active = self.controller.active
        available = active is not None and not busy and not self.controller._closing
        self.btn_refresh.setEnabled(available)
        self.btn_create.setEnabled(available and not active.read_only and bool(self.snapshot.tables))
        selected = self.selected_index()
        self.btn_delete.setEnabled(
            available and not active.read_only and selected is not None and selected.is_user_created
        )
        self.btn_delete.setToolTip("Table constraint indexes must be changed through UC-04.")
        self.indexes_list.setEnabled(not busy)
        if self.index_dialog is not None:
            self.index_dialog.set_busy(busy)

    def refresh(self):
        if self.controller.active is None or self.controller.busy or self.controller._closing:
            return False
        accepted = self.controller.submit_database_operation(
            "schema.refresh",
            lambda manager: SchemaManager(SQLiteIndexRepository(manager.executor.connection)).get_schema(),
        )
        if accepted:
            self._needs_refresh = False
        return accepted

    def _operation_finished(self, name: str, success: bool):
        if name == "connect" and success:
            # Reopening the same file can still reveal changes made elsewhere.
            self._needs_refresh = self.controller.active is not None
        if name == "schema.delete" and not success:
            # Another application may have changed the schema since our last read.
            self._needs_refresh = self.controller.active is not None
        if name != "shutdown" and self._needs_refresh:
            self.refresh()

    def _operation_result(self, name: str, result):
        if name not in {"schema.refresh", "schema.create", "schema.delete"}:
            return
        selected_name = None
        if name == "schema.create" and self.index_dialog is not None:
            selected_name = self.index_dialog.get_index().name
        self._show_snapshot(result, selected_name)
        if name in {"schema.create", "schema.delete"}:
            self._refresh_error = None
            verb = "created" if name == "schema.create" else "deleted"
            message = f"Index {verb}."
            if result.in_transaction:
                message = f"Index {verb} in the current transaction. Commit it to save the change."
            self.message_label.setText(message)
            if name == "schema.create" and self.index_dialog is not None:
                self.index_dialog.accept()
        elif self._refresh_error:
            self.message_label.setText(self._refresh_error)
            self._refresh_error = None
        elif result.indexes:
            self.message_label.setText(f"Indexes: {len(result.indexes)}")
        else:
            self.message_label.setText("No indexes in this database.")

    def _operation_failed(self, name: str, message: str):
        if not name.startswith("schema."):
            return
        self.message_label.setText(message)
        if name == "schema.delete":
            self._refresh_error = message
        elif name == "schema.refresh":
            self._refresh_error = None
        if name == "schema.create" and self.index_dialog is not None:
            self.index_dialog.show_error(message)

    def _show_snapshot(self, snapshot: SchemaSnapshot, selected_name: str | None = None):
        previous = self.selected_index()
        if selected_name is None and previous is not None:
            selected_name = previous.name
        self.snapshot = snapshot
        self.indexes_list.clear()
        for index in snapshot.indexes:
            suffix = " · UNIQUE" if index.unique else ""
            if not index.is_user_created:
                suffix += " · table constraint"
            item = QListWidgetItem(f"{index.name} — {index.table_name}{suffix}")
            item.setData(Qt.ItemDataRole.UserRole, index.name)
            self.indexes_list.addItem(item)
            if index.name == selected_name:
                self.indexes_list.setCurrentItem(item)
        if self.indexes_list.currentItem() is None and snapshot.indexes:
            self.indexes_list.setCurrentRow(0)
        self._selection_changed()
        self._set_busy(self.controller.busy)

    def selected_index(self) -> Index | None:
        item = self.indexes_list.currentItem()
        if item is None:
            return None
        name = item.data(Qt.ItemDataRole.UserRole)
        return next((index for index in self.snapshot.indexes if index.name == name), None)

    def _selection_changed(self, *_):
        self._set_busy(self.controller.busy)
        index = self.selected_index()
        if index is None:
            self.details_edit.clear()
            return
        columns = ", ".join(
            f"{column.name} {'DESC' if column.descending else 'ASC'}" for column in index.columns
        )
        lines = [
            f"Index: {index.name}", f"Table: {index.table_name}",
            f"Columns: {columns or '(expression or internal key)'}",
            f"Unique: {'yes' if index.unique else 'no'}",
        ]
        if not index.is_user_created:
            lines.extend(["", "This index belongs to a table constraint. Change the table structure (UC-04) to modify it."])
        elif not index.can_edit:
            lines.extend(["", "This definition cannot be edited with the column form. Its original SQL is shown below."])
        if index.sql:
            lines.extend(["", index.sql])
        self.details_edit.setPlainText("\n".join(lines))

    def _new_index(self):
        if not self.btn_create.isEnabled():
            return
        if self.index_dialog is not None:
            self.index_dialog.raise_()
            self.index_dialog.activateWindow()
            return
        dialog = IndexDialog(self.snapshot.tables, self)
        self.index_dialog = dialog
        dialog.submitted.connect(self._create_index)
        dialog.finished.connect(lambda _: self._dialog_closed(dialog))
        dialog.open()

    def _dialog_closed(self, dialog: IndexDialog):
        if self.index_dialog is dialog:
            self.index_dialog = None
        dialog.deleteLater()

    def _create_index(self, index: Index):
        accepted = self.controller.submit_database_operation(
            "schema.create",
            lambda manager: SchemaManager(SQLiteIndexRepository(manager.executor.connection)).create_index(index),
        )
        if not accepted and self.index_dialog is not None:
            self.index_dialog.show_error("Another database operation is running. Try again when it finishes.")

    def _delete_index(self):
        index = self.selected_index()
        if index is None or not self.btn_delete.isEnabled():
            return
        answer = QMessageBox.question(
            self, "Delete index?",
            f'Delete index "{index.name}" from table "{index.table_name}"?\n\n'
            "The table and its rows will be kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.submit_database_operation(
                "schema.delete",
                lambda manager: SchemaManager(SQLiteIndexRepository(manager.executor.connection)).delete_index(index.name),
            )
