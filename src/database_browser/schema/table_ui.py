"""Table list and the create/edit forms for the Schema tab (UC-03, UC-04, UC-05)."""

from dataclasses import replace

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..controller import ConnectionController
from ..errors import DatabaseBrowserError
from .table_models import (
    CHECK, CONSTRAINT_KINDS, FOREIGN_KEY, REFERENTIAL_ACTIONS, ColumnDef, TableConstraint,
    TableDefinition, TableSnapshot,
)
from .table_repository import SQLiteTableRepository
from .table_services import TableManager
from .table_sql import create_table_sql, fold_name

OP_REFRESH = "schema.table.refresh"
OP_CREATE = "schema.table.create"
OP_ALTER = "schema.table.alter"
OP_DROP = "schema.table.drop"

COLUMN_TYPES = ["", "INTEGER", "TEXT", "REAL", "BLOB", "NUMERIC", "BOOLEAN", "DATE", "DATETIME"]
# Column positions in the editor grid.
COL_NAME, COL_TYPE, COL_PK, COL_NOT_NULL, COL_UNIQUE, COL_AUTOINCREMENT, COL_DEFAULT = range(7)


def describe_constraint(constraint: TableConstraint) -> str:
    """One-line text for the constraint list and the details panel."""
    columns = ", ".join(constraint.columns)
    if constraint.kind == CHECK:
        return constraint.expression or ""
    if constraint.kind == FOREIGN_KEY:
        target = ", ".join(constraint.ref_columns) or "primary key"
        text = f"({columns}) → {constraint.ref_table} ({target})"
        if constraint.on_delete:
            text += f" ON DELETE {constraint.on_delete}"
        if constraint.on_update:
            text += f" ON UPDATE {constraint.on_update}"
        return text
    return f"({columns})"


def describe_table(table: TableDefinition) -> str:
    lines = [f"Table: {table.name}", "", "Columns:"]
    for column in table.columns:
        parts = [column.name, column.data_type or "(no type)"]
        if column.primary_key:
            parts.append("PRIMARY KEY" + (" AUTOINCREMENT" if column.autoincrement else ""))
        if column.not_null:
            parts.append("NOT NULL")
        if column.unique:
            parts.append("UNIQUE")
        if column.default is not None:
            parts.append(f"DEFAULT {column.default}")
        lines.append("  " + " ".join(parts))
    if table.constraints:
        lines.extend(["", "Constraints:"])
        lines.extend(f"  {constraint.kind} {describe_constraint(constraint)}" for constraint in table.constraints)
    if table.referenced_by:
        lines.extend(["", "Referenced by: " + ", ".join(table.referenced_by)])
    if not table.editable and table.reason:
        lines.extend(["", table.reason])
    if table.sql:
        lines.extend(["", table.sql])
    return "\n".join(lines)


class ConstraintDialog(QDialog):
    """Define one table constraint: key, FOREIGN KEY or CHECK."""

    def __init__(self, columns: tuple[str, ...], ref_tables: tuple[str, ...], own_name: str,
                 parent=None, constraint: TableConstraint | None = None):
        super().__init__(parent)
        self.setWindowTitle("Edit constraint" if constraint else "Add constraint")
        self.resize(520, 460)
        self._initial = constraint
        # Key columns keep the order they have in the constraint (or the order they are ticked in).
        self._order: list[str] = []
        self._missing: list[str] = []
        self.kind_combo = QComboBox()
        self.kind_combo.setObjectName("constraint_kind")
        self.kind_combo.addItems(CONSTRAINT_KINDS)

        self.columns_list = QListWidget()
        self.columns_list.setObjectName("constraint_columns")
        for name in columns:
            item = QListWidgetItem(name)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.columns_list.addItem(item)
        self.columns_box = QWidget()
        box = QVBoxLayout(self.columns_box)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(QLabel("Columns (in the order you tick them):"))
        box.addWidget(self.columns_list)

        names = list(ref_tables)
        if own_name and all(fold_name(own_name) != fold_name(existing) for existing in names):
            names.append(own_name)
        self.ref_table_combo = QComboBox()
        self.ref_table_combo.setObjectName("constraint_ref_table")
        self.ref_table_combo.addItems(names)
        self.ref_columns_edit = QLineEdit()
        self.ref_columns_edit.setObjectName("constraint_ref_columns")
        self.ref_columns_edit.setPlaceholderText("Comma-separated; empty means the primary key")
        self.on_delete_combo = self._action_combo("constraint_on_delete")
        self.on_update_combo = self._action_combo("constraint_on_update")
        self.fk_box = QWidget()
        fk_form = QFormLayout(self.fk_box)
        fk_form.setContentsMargins(0, 0, 0, 0)
        fk_form.addRow("Referenced table:", self.ref_table_combo)
        fk_form.addRow("Referenced columns:", self.ref_columns_edit)
        fk_form.addRow("ON DELETE:", self.on_delete_combo)
        fk_form.addRow("ON UPDATE:", self.on_update_combo)

        self.expression_edit = QLineEdit()
        self.expression_edit.setObjectName("constraint_expression")
        self.expression_edit.setPlaceholderText("For example: price > 0")
        self.check_box = QWidget()
        check_form = QFormLayout(self.check_box)
        check_form.setContentsMargins(0, 0, 0, 0)
        check_form.addRow("Expression:", self.expression_edit)

        self.validation_label = QLabel()
        self.validation_label.setObjectName("constraint_validation")
        self.validation_label.setWordWrap(True)
        self.validation_label.setStyleSheet("color: #b42318")
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)

        form = QFormLayout()
        form.addRow("Type:", self.kind_combo)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.columns_box, 1)
        layout.addWidget(self.fk_box)
        layout.addWidget(self.check_box)
        layout.addWidget(self.validation_label)
        layout.addWidget(self.buttons)

        self.kind_combo.currentTextChanged.connect(self._kind_changed)
        if constraint is not None:
            self._load(constraint)
        self.columns_list.itemChanged.connect(self._item_changed)
        self._kind_changed(self.kind_combo.currentText())
        if self._missing:
            self.validation_label.setText(
                "Removed from this constraint because the table no longer has them: "
                + ", ".join(self._missing) + ". Check the columns before pressing OK."
            )

    @staticmethod
    def _action_combo(name: str) -> QComboBox:
        combo = QComboBox()
        combo.setObjectName(name)
        for action in REFERENTIAL_ACTIONS:
            combo.addItem(action or "NO ACTION", action)
        return combo

    def _load(self, constraint: TableConstraint):
        self.kind_combo.setCurrentText(constraint.kind)
        available = {self.columns_list.item(row).text() for row in range(self.columns_list.count())}
        self.columns_list.blockSignals(True)
        for row in range(self.columns_list.count()):
            item = self.columns_list.item(row)
            if item.text() in constraint.columns:
                item.setCheckState(Qt.CheckState.Checked)
        self.columns_list.blockSignals(False)
        self._order = [name for name in constraint.columns if name in available]
        self._missing = [name for name in constraint.columns if name not in available]
        self.expression_edit.setText(constraint.expression or "")
        if constraint.ref_table:
            self._select_ref_table(constraint.ref_table)
        self.ref_columns_edit.setText(", ".join(constraint.ref_columns))
        self.on_delete_combo.setCurrentIndex(max(0, self.on_delete_combo.findData(constraint.on_delete)))
        self.on_update_combo.setCurrentIndex(max(0, self.on_update_combo.findData(constraint.on_update)))

    def _select_ref_table(self, name: str):
        # Match like SQLite does (ASCII case-insensitive) so a loaded constraint keeps its table.
        for index in range(self.ref_table_combo.count()):
            if fold_name(self.ref_table_combo.itemText(index)) == fold_name(name):
                self.ref_table_combo.setCurrentIndex(index)
                return
        self.ref_table_combo.addItem(name)
        self.ref_table_combo.setCurrentIndex(self.ref_table_combo.count() - 1)

    def _item_changed(self, item: QListWidgetItem):
        name = item.text()
        if item.checkState() == Qt.CheckState.Checked:
            if name not in self._order:
                self._order.append(name)
        elif name in self._order:
            self._order.remove(name)

    def _kind_changed(self, kind: str):
        self.columns_box.setVisible(kind != CHECK)
        self.fk_box.setVisible(kind == FOREIGN_KEY)
        self.check_box.setVisible(kind == CHECK)
        self.validation_label.clear()

    def checked_columns(self) -> tuple[str, ...]:
        checked = {
            self.columns_list.item(row).text() for row in range(self.columns_list.count())
            if self.columns_list.item(row).checkState() == Qt.CheckState.Checked
        }
        return tuple(name for name in self._order if name in checked)

    def get_constraint(self) -> TableConstraint:
        kind = self.kind_combo.currentText()
        if kind == CHECK:
            return TableConstraint(CHECK, expression=self.expression_edit.text().strip())
        if kind == FOREIGN_KEY:
            ref_columns = tuple(part.strip() for part in self.ref_columns_edit.text().split(",") if part.strip())
            ref_table = self.ref_table_combo.currentText()
            initial = self._initial.ref_table if self._initial is not None else None
            if initial and fold_name(initial) == fold_name(ref_table):
                ref_table = initial  # keep the spelling of the stored constraint
            return TableConstraint(
                FOREIGN_KEY, columns=self.checked_columns(),
                ref_table=ref_table, ref_columns=ref_columns,
                on_delete=self.on_delete_combo.currentData() or "",
                on_update=self.on_update_combo.currentData() or "",
            )
        return TableConstraint(kind, columns=self.checked_columns())

    def _accept(self):
        constraint = self.get_constraint()
        if constraint.kind == CHECK:
            if not constraint.expression:
                self.validation_label.setText("Enter the CHECK expression.")
                self.expression_edit.setFocus()
                return
        elif not constraint.columns:
            self.validation_label.setText("Select at least one column.")
            return
        elif constraint.kind == FOREIGN_KEY and not constraint.ref_table:
            self.validation_label.setText("Choose the referenced table.")
            return
        self.accept()


class TableDialog(QDialog):
    """Collect a table definition without closing before SQLite accepts it."""

    submitted = pyqtSignal(object)

    def __init__(self, ref_tables: tuple[str, ...], parent=None, table: TableDefinition | None = None):
        super().__init__(parent)
        self._table = table
        self._ref_tables = ref_tables
        self._constraints: list[TableConstraint] = []
        self._busy = False
        self.setWindowTitle(f'Edit table "{table.name}"' if table else "Create table")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(940, 780)

        self.name_edit = QLineEdit()
        self.name_edit.setObjectName("table_name")
        form = QFormLayout()
        form.addRow("Table name:", self.name_edit)

        self.columns_table = QTableWidget(0, 7)
        self.columns_table.setObjectName("table_columns")
        self.columns_table.setHorizontalHeaderLabels(
            ["Name", "Type", "PK", "NOT NULL", "UNIQUE", "AUTOINCREMENT", "Default (SQL expression)"]
        )
        header = self.columns_table.horizontalHeader()
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_DEFAULT, QHeaderView.ResizeMode.Stretch)
        for column in (COL_TYPE, COL_PK, COL_NOT_NULL, COL_UNIQUE, COL_AUTOINCREMENT):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.columns_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.columns_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.btn_add_column = QPushButton("Add column")
        self.btn_remove_column = QPushButton("Remove column")
        self.btn_add_column.clicked.connect(lambda: self.add_column())
        self.btn_remove_column.clicked.connect(self.remove_selected_column)
        column_buttons = QHBoxLayout()
        column_buttons.addWidget(self.btn_add_column)
        column_buttons.addWidget(self.btn_remove_column)
        column_buttons.addStretch()

        self.constraints_table = QTableWidget(0, 2)
        self.constraints_table.setObjectName("table_constraints")
        self.constraints_table.setHorizontalHeaderLabels(["Constraint", "Definition"])
        self.constraints_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.constraints_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.constraints_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.constraints_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.constraints_table.setMaximumHeight(150)
        self.btn_add_constraint = QPushButton("Add constraint…")
        self.btn_edit_constraint = QPushButton("Edit constraint…")
        self.btn_remove_constraint = QPushButton("Remove constraint")
        self.btn_add_constraint.clicked.connect(lambda: self._open_constraint_dialog(None))
        self.btn_edit_constraint.clicked.connect(lambda: self._open_constraint_dialog(self.constraints_table.currentRow()))
        self.btn_remove_constraint.clicked.connect(self.remove_selected_constraint)
        constraint_buttons = QHBoxLayout()
        for button in (self.btn_add_constraint, self.btn_edit_constraint, self.btn_remove_constraint):
            constraint_buttons.addWidget(button)
        constraint_buttons.addStretch()

        self.preview_edit = QPlainTextEdit()
        self.preview_edit.setObjectName("table_sql_preview")
        self.preview_edit.setReadOnly(True)
        self.preview_edit.setMaximumHeight(150)
        self.validation_label = QLabel()
        self.validation_label.setObjectName("table_validation")
        self.validation_label.setWordWrap(True)
        self.validation_label.setStyleSheet("color: #b42318")
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self._submit)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(QLabel(
            "Columns. PK marks a single-column primary key; for several columns add a PRIMARY KEY constraint."
        ))
        layout.addWidget(self.columns_table, 1)
        layout.addLayout(column_buttons)
        layout.addWidget(QLabel("Table constraints (composite keys, FOREIGN KEY, CHECK)"))
        layout.addWidget(self.constraints_table)
        layout.addLayout(constraint_buttons)
        if table is not None:
            note = QLabel(
                "Saving rebuilds the table and copies its rows. The change is applied completely or not at all."
            )
            note.setWordWrap(True)
            layout.addWidget(note)
        layout.addWidget(QLabel("SQL preview"))
        layout.addWidget(self.preview_edit)
        layout.addWidget(self.validation_label)
        layout.addWidget(self.buttons)

        self.name_edit.textChanged.connect(self._update_preview)
        self.columns_table.itemChanged.connect(self._update_preview)
        if table is not None:
            self.name_edit.setText(table.name)
            for column in table.columns:
                self.add_column(column, original=column.name)
            self._constraints = list(table.constraints)
            self._show_constraints()
        self._update_preview()
        self.set_busy(False)

    # ----- columns -----

    def add_column(self, column: ColumnDef | None = None, *, original: str | None = None):
        grid = self.columns_table
        grid.blockSignals(True)
        row = grid.rowCount()
        grid.insertRow(row)
        name_item = QTableWidgetItem(column.name if column else "")
        # The stored name is kept so a rename is not mistaken for a new column.
        name_item.setData(Qt.ItemDataRole.UserRole, original)
        grid.setItem(row, COL_NAME, name_item)
        combo = QComboBox()
        combo.setEditable(True)
        combo.addItems(COLUMN_TYPES)
        combo.setCurrentText(column.data_type if column else "TEXT")
        combo.currentTextChanged.connect(self._update_preview)
        grid.setCellWidget(row, COL_TYPE, combo)
        flags = {
            COL_PK: column.primary_key if column else False,
            COL_NOT_NULL: column.not_null if column else False,
            COL_UNIQUE: column.unique if column else False,
            COL_AUTOINCREMENT: column.autoincrement if column else False,
        }
        for position, checked in flags.items():
            item = QTableWidgetItem()
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            grid.setItem(row, position, item)
        grid.setItem(row, COL_DEFAULT, QTableWidgetItem(column.default if column and column.default is not None else ""))
        grid.blockSignals(False)
        grid.setCurrentCell(row, COL_NAME)
        self._update_preview()

    def remove_selected_column(self):
        row = self.columns_table.currentRow()
        if self._busy or row < 0:
            return
        self.columns_table.removeRow(row)
        self._update_preview()

    def _checked(self, row: int, column: int) -> bool:
        return self.columns_table.item(row, column).checkState() == Qt.CheckState.Checked

    def column_names(self) -> tuple[str, ...]:
        return tuple(
            self.columns_table.item(row, COL_NAME).text() for row in range(self.columns_table.rowCount())
            if self.columns_table.item(row, COL_NAME).text().strip()
        )

    # ----- constraints -----

    def add_constraint(self, constraint: TableConstraint):
        self._constraints.append(constraint)
        self._show_constraints()
        self._update_preview()

    def remove_selected_constraint(self):
        row = self.constraints_table.currentRow()
        if self._busy or not 0 <= row < len(self._constraints):
            return
        del self._constraints[row]
        self._show_constraints()
        self._update_preview()

    def _show_constraints(self):
        self.constraints_table.setRowCount(0)
        self.constraints_table.setRowCount(len(self._constraints))
        for row, constraint in enumerate(self._constraints):
            self.constraints_table.setItem(row, 0, QTableWidgetItem(constraint.kind))
            self.constraints_table.setItem(row, 1, QTableWidgetItem(describe_constraint(constraint)))

    def _follow_renames(self, constraint: TableConstraint) -> TableConstraint:
        """Show a constraint with the current names of columns renamed in this form."""
        mapping = {}
        for row in range(self.columns_table.rowCount()):
            item = self.columns_table.item(row, COL_NAME)
            original = item.data(Qt.ItemDataRole.UserRole)
            if original is not None and item.text().strip() and fold_name(original) != fold_name(item.text()):
                mapping[fold_name(original)] = item.text()
        if not mapping:
            return constraint

        def follow(names: tuple[str, ...]) -> tuple[str, ...]:
            return tuple(mapping.get(fold_name(name), name) for name in names)

        own = fold_name(constraint.ref_table or "") == fold_name(self._own_name())
        return replace(
            constraint, columns=follow(constraint.columns),
            ref_columns=follow(constraint.ref_columns) if own else constraint.ref_columns,
        )

    def _own_name(self) -> str:
        return self._table.name if self._table is not None else self.name_edit.text().strip()

    def _open_constraint_dialog(self, row: int | None):
        if self._busy:
            return
        existing = None
        if row is not None:
            if not 0 <= row < len(self._constraints):
                return
            existing = self._follow_renames(self._constraints[row])
        dialog = ConstraintDialog(self.column_names(), self._ref_tables, self._own_name(), self, existing)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            if row is None:
                self._constraints.append(dialog.get_constraint())
            else:
                self._constraints[row] = dialog.get_constraint()
            self._show_constraints()
            self._update_preview()

    # ----- result -----

    def get_definition(self) -> TableDefinition:
        columns = []
        for row in range(self.columns_table.rowCount()):
            name_item = self.columns_table.item(row, COL_NAME)
            original = name_item.data(Qt.ItemDataRole.UserRole)
            # Stored names must stay exact; only names typed in this form are trimmed.
            name = name_item.text() if original is not None else name_item.text().strip()
            default = self.columns_table.item(row, COL_DEFAULT).text().strip()
            columns.append(ColumnDef(
                name=name,
                data_type=self.columns_table.cellWidget(row, COL_TYPE).currentText().strip(),
                not_null=self._checked(row, COL_NOT_NULL),
                primary_key=self._checked(row, COL_PK),
                unique=self._checked(row, COL_UNIQUE),
                autoincrement=self._checked(row, COL_AUTOINCREMENT),
                default=default or None,
                original_name=original,
            ))
        name = self._table.name if self._table is not None else self.name_edit.text().strip()
        # The stored statement the form started from lets the worker detect a changed table.
        base = self._table.sql if self._table is not None else None
        return TableDefinition(name, tuple(columns), tuple(self._constraints), sql=base)

    def _update_preview(self, *_):
        definition = self.get_definition()
        if not definition.name or not definition.columns:
            self.preview_edit.clear()
            return
        try:
            self.preview_edit.setPlainText(create_table_sql(definition))
        except (DatabaseBrowserError, ValueError) as error:
            self.preview_edit.setPlainText(str(error))

    def _submit(self):
        if self._busy:
            return
        definition = self.get_definition()
        if not definition.name:
            self.show_error("Enter a table name.")
            self.name_edit.setFocus()
        elif not definition.columns:
            self.show_error("Add at least one column.")
        elif any(not column.name.strip() for column in definition.columns):
            self.show_error("Every column needs a name.")
        else:
            self.validation_label.clear()
            # The tab accepts the dialog only after the worker reports success.
            self.submitted.emit(definition)

    def show_error(self, message: str):
        self.validation_label.setText(message)

    def set_busy(self, busy: bool):
        self._busy = busy
        for widget in (
            self.columns_table, self.constraints_table, self.buttons, self.btn_add_column,
            self.btn_remove_column, self.btn_add_constraint, self.btn_edit_constraint,
            self.btn_remove_constraint,
        ):
            widget.setEnabled(not busy)
        # An existing table keeps its name; renaming is not part of UC-04.
        self.name_edit.setEnabled(not busy and self._table is None)

    def reject(self):
        # Closing the form while a write is running would hide its outcome.
        if not self._busy:
            super().reject()


class TablesTab(QWidget):
    """List tables and submit create, edit, and delete operations to the connection's worker."""

    schema_changed = pyqtSignal()

    def __init__(self, controller: ConnectionController, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.snapshot = TableSnapshot(())
        self.table_dialog: TableDialog | None = None
        self._connection_key = None
        self._needs_refresh = False
        self._refresh_error: str | None = None

        self.active_label = QLabel("Connect a database to view its tables.")
        self.active_label.setWordWrap(True)
        self.btn_create = QPushButton("Create table…")
        self.btn_edit = QPushButton("Edit table…")
        self.btn_drop = QPushButton("Delete table…")
        self.btn_refresh = QPushButton("Refresh")
        self.btn_create.clicked.connect(self._new_table)
        self.btn_edit.clicked.connect(self._edit_table)
        self.btn_drop.clicked.connect(self._drop_table)
        self.btn_refresh.clicked.connect(self.refresh)
        buttons = QHBoxLayout()
        for button in (self.btn_create, self.btn_edit, self.btn_drop, self.btn_refresh):
            buttons.addWidget(button)
        buttons.addStretch()
        self.tables_list = QListWidget()
        self.tables_list.setObjectName("tables_list")
        self.tables_list.currentItemChanged.connect(self._selection_changed)
        self.details_edit = QPlainTextEdit()
        self.details_edit.setObjectName("table_details")
        self.details_edit.setReadOnly(True)
        split = QSplitter()
        split.addWidget(self.tables_list)
        split.addWidget(self.details_edit)
        split.setStretchFactor(1, 1)
        self.message_label = QLabel()
        self.message_label.setObjectName("tables_message")
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

    def _connection_changed(self, active):
        key = (active.profile.id, active.file_path, active.read_only) if active else None
        if key != self._connection_key:
            self._connection_key = key
            self._needs_refresh = active is not None
            self._show_snapshot(TableSnapshot(()))
            self.message_label.clear()
            self._refresh_error = None
            if self.table_dialog is not None:
                self.table_dialog.set_busy(False)
                self.table_dialog.reject()
        if active:
            mode = "read-only" if active.read_only else "read/write"
            self.active_label.setText(f"Tables in {active.file_path} ({mode})")
        else:
            self.active_label.setText("Connect a database to view its tables.")

    def _set_busy(self, busy: bool):
        active = self.controller.active
        available = active is not None and not busy and not self.controller._closing
        writable = available and not active.read_only
        selected = self.selected_table()
        self.btn_refresh.setEnabled(available)
        self.btn_create.setEnabled(writable)
        self.btn_edit.setEnabled(writable and selected is not None and selected.editable)
        self.btn_edit.setToolTip(selected.reason if selected is not None and selected.reason else "")
        self.btn_drop.setEnabled(writable and selected is not None)
        self.tables_list.setEnabled(not busy)
        if self.table_dialog is not None:
            self.table_dialog.set_busy(busy)

    def refresh(self):
        if self.controller.active is None or self.controller.busy or self.controller._closing:
            return False
        accepted = self.controller.submit_database_operation(
            OP_REFRESH,
            lambda manager: TableManager(SQLiteTableRepository(manager.executor.connection)).get_tables(),
        )
        if accepted:
            self._needs_refresh = False
        return accepted

    # ----- results -----

    def _operation_finished(self, name: str, success: bool):
        if name == "connect" and success:
            # Reopening the same file can still reveal changes made elsewhere.
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
        if name in {OP_CREATE, OP_ALTER} and self.table_dialog is not None:
            selected_name = self.table_dialog.get_definition().name
        self._show_snapshot(result, selected_name)
        if name != OP_REFRESH:
            self._refresh_error = None
            verb = {OP_CREATE: "created", OP_ALTER: "updated", OP_DROP: "deleted"}[name]
            message = f"Table {verb}."
            if result.in_transaction:
                message = f"Table {verb} in the current transaction. Commit it to save the change."
            self.message_label.setText(message)
            if name in {OP_CREATE, OP_ALTER} and self.table_dialog is not None:
                self.table_dialog.accept()
            # The index list and the index form depend on the set of tables.
            self.schema_changed.emit()
        elif self._refresh_error:
            self.message_label.setText(self._refresh_error)
            self._refresh_error = None
        elif result.tables:
            self.message_label.setText(f"Tables: {len(result.tables)}")
        else:
            self.message_label.setText("No tables in this database.")

    def _operation_failed(self, name: str, message: str):
        if not name.startswith("schema.table."):
            return
        self.message_label.setText(message)
        if name in {OP_ALTER, OP_DROP}:
            self._refresh_error = message
        elif name == OP_REFRESH:
            self._refresh_error = None
        if name in {OP_CREATE, OP_ALTER} and self.table_dialog is not None:
            self.table_dialog.show_error(message)

    # ----- list -----

    def _show_snapshot(self, snapshot: TableSnapshot, selected_name: str | None = None):
        previous = self.selected_table()
        if selected_name is None and previous is not None:
            selected_name = previous.name
        self.snapshot = snapshot
        self.tables_list.clear()
        for table in snapshot.tables:
            suffix = "" if table.editable else " · view only"
            item = QListWidgetItem(f"{table.name}{suffix}")
            item.setData(Qt.ItemDataRole.UserRole, table.name)
            self.tables_list.addItem(item)
            if table.name == selected_name:
                self.tables_list.setCurrentItem(item)
        if self.tables_list.currentItem() is None and snapshot.tables:
            self.tables_list.setCurrentRow(0)
        self._selection_changed()
        self._set_busy(self.controller.busy)

    def selected_table(self) -> TableDefinition | None:
        item = self.tables_list.currentItem()
        if item is None:
            return None
        name = item.data(Qt.ItemDataRole.UserRole)
        return next((table for table in self.snapshot.tables if table.name == name), None)

    def _selection_changed(self, *_):
        self._set_busy(self.controller.busy)
        table = self.selected_table()
        if table is None:
            self.details_edit.clear()
        else:
            self.details_edit.setPlainText(describe_table(table))

    # ----- actions -----

    def _new_table(self):
        if not self.btn_create.isEnabled():
            return
        self._open_dialog()

    def _edit_table(self):
        table = self.selected_table()
        if table is None or not self.btn_edit.isEnabled():
            return
        self._open_dialog(table)

    def _open_dialog(self, table: TableDefinition | None = None):
        if self.table_dialog is not None:
            self.table_dialog.raise_()
            self.table_dialog.activateWindow()
            return
        others = tuple(item.name for item in self.snapshot.tables if table is None or item.name != table.name)
        dialog = TableDialog(others, self, table=table)
        self.table_dialog = dialog
        dialog.submitted.connect(self._alter_table if table is not None else self._create_table)
        dialog.finished.connect(lambda _: self._dialog_closed(dialog))
        dialog.open()

    def _dialog_closed(self, dialog: TableDialog):
        if self.table_dialog is dialog:
            self.table_dialog = None
        dialog.deleteLater()

    def _create_table(self, definition: TableDefinition):
        accepted = self.controller.submit_database_operation(
            OP_CREATE,
            lambda manager: TableManager(SQLiteTableRepository(manager.executor.connection)).create_table(definition),
        )
        if not accepted and self.table_dialog is not None:
            self.table_dialog.show_error("Another database operation is running. Try again when it finishes.")

    def _alter_table(self, definition: TableDefinition):
        accepted = self.controller.submit_database_operation(
            OP_ALTER,
            lambda manager: TableManager(SQLiteTableRepository(manager.executor.connection)).alter_table(definition),
        )
        if not accepted and self.table_dialog is not None:
            self.table_dialog.show_error("Another database operation is running. Try again when it finishes.")

    def _drop_table(self):
        table = self.selected_table()
        if table is None or not self.btn_drop.isEnabled():
            return
        text = f'Delete table "{table.name}" together with all its rows?\n\nThis cannot be undone.'
        if table.referenced_by:
            text += (
                "\n\nTables with foreign keys to it: " + ", ".join(table.referenced_by) +
                ". Their rows may be deleted by ON DELETE actions, or the deletion may be refused."
            )
        answer = QMessageBox.question(
            self, "Delete table?", text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.submit_database_operation(
                OP_DROP,
                lambda manager: TableManager(SQLiteTableRepository(manager.executor.connection)).drop_table(table.name),
            )
