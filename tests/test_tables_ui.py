"""Table forms (UC-03, UC-04, UC-05) talking to the real SQLite worker."""

import sqlite3
from contextlib import closing
from threading import Event

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QMessageBox

from database_browser.controller import ConnectionController
from database_browser.schema.table_models import (
    CHECK, FOREIGN_KEY, PRIMARY_KEY, UNIQUE, ColumnDef, TableConstraint, TableDefinition,
)
from database_browser.schema.table_ui import (
    COL_AUTOINCREMENT, COL_DEFAULT, COL_NAME, COL_NOT_NULL, COL_PK, COL_TYPE, ConstraintDialog,
    TableDialog, describe_table,
)
from database_browser.ui import MainWindow, NotificationManager

CREATE, ALTER, DROP = "schema.table.create", "schema.table.alter", "schema.table.drop"


@pytest.fixture
def table_database(tmp_path):
    path = tmp_path / "tables.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE authors(id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        connection.execute(
            "CREATE TABLE books(id INTEGER PRIMARY KEY, title TEXT, author_id INTEGER REFERENCES authors(id))"
        )
        connection.execute("CREATE TABLE odd(name TEXT COLLATE NOCASE)")
        connection.executemany("INSERT INTO authors(name) VALUES (?)", [("Ann",), ("Bob",)])
        connection.execute("CREATE INDEX ix_books_title ON books(title)")
    return path


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    errors = []
    monkeypatch.setattr(NotificationManager, "show_error", lambda parent, message: errors.append(message))
    controller = ConnectionController(tmp_path / "settings.sqlite3")
    widget = MainWindow(controller)
    widget.connection_errors = errors
    qtbot.addWidget(widget)
    widget.show()
    widget.tab_widget.setCurrentWidget(widget.schema_tab)
    widget.schema_tab.sections.setCurrentWidget(widget.schema_tab.tables_tab)
    with qtbot.waitSignal(controller.operation_finished, timeout=5000):
        controller.refresh()
    yield widget
    if not controller._closed:
        with qtbot.waitSignal(controller.shutdown_complete, timeout=5000):
            widget.close()


def wait_operation(qtbot, controller, name, action):
    with qtbot.waitSignal(
        controller.operation_finished, timeout=5000,
        check_params_cb=lambda operation, success: operation == name,
    ) as result:
        action()
    return result.args[1]


def open_database(qtbot, window, path):
    controller = window.controller
    assert wait_operation(qtbot, controller, "connect", lambda: controller.create("Demo", str(path)))
    qtbot.waitUntil(
        lambda: not controller.busy and bool(window.schema_tab.tables_tab.snapshot.tables)
        and bool(window.schema_tab.snapshot.tables),
        timeout=5000,
    )


def save_button(dialog):
    return dialog.buttons.button(QDialogButtonBox.StandardButton.Save)


def save_dialog(qtbot, window, dialog, operation=CREATE):
    return wait_operation(
        qtbot, window.controller, operation,
        lambda: qtbot.mouseClick(save_button(dialog), Qt.MouseButton.LeftButton),
    )


def open_create_dialog(qtbot, tab, name="reviews", columns=()):
    qtbot.mouseClick(tab.btn_create, Qt.MouseButton.LeftButton)
    dialog = tab.table_dialog
    assert dialog is not None
    dialog.name_edit.setText(name)
    for column in columns:
        dialog.add_column(column)
    return dialog


def select_table(tab, name):
    for row in range(tab.tables_list.count()):
        if tab.tables_list.item(row).data(Qt.ItemDataRole.UserRole) == name:
            tab.tables_list.setCurrentRow(row)
            return
    raise AssertionError(f"{name} is not listed")


def open_edit_dialog(qtbot, tab, name):
    select_table(tab, name)
    qtbot.mouseClick(tab.btn_edit, Qt.MouseButton.LeftButton)
    assert tab.table_dialog is not None
    return tab.table_dialog


def table_names(path):
    with closing(sqlite3.connect(path)) as connection:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")}


REVIEW_COLUMNS = (
    ColumnDef("id", "INTEGER", primary_key=True),
    ColumnDef("book_id", "INTEGER", not_null=True),
    ColumnDef("stars", "INTEGER", default="5"),
)


# ---------------------------------------------------------------- UC-03


def test_uc03_creates_table_and_shows_it_in_both_sections(qtbot, window, table_database):
    open_database(qtbot, window, table_database)
    tab = window.schema_tab.tables_tab
    dialog = open_create_dialog(qtbot, tab, columns=REVIEW_COLUMNS)
    assert 'CREATE TABLE main."reviews"' in dialog.preview_edit.toPlainText()
    assert save_dialog(qtbot, window, dialog)
    assert tab.table_dialog is None
    assert tab.selected_table().name == "reviews"
    assert "stars" in tab.details_edit.toPlainText()
    assert tab.message_label.text() == "Table created."
    assert "reviews" in table_names(table_database)
    # The index form must offer the new table.
    qtbot.waitUntil(lambda: any(t.name == "reviews" for t in window.schema_tab.snapshot.tables), timeout=5000)


def test_uc03_cancel_leaves_database_unchanged(qtbot, window, table_database):
    open_database(qtbot, window, table_database)
    tab = window.schema_tab.tables_tab
    dialog = open_create_dialog(qtbot, tab, columns=REVIEW_COLUMNS)
    qtbot.mouseClick(dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel), Qt.MouseButton.LeftButton)
    assert tab.table_dialog is None
    assert not window.controller.busy
    assert "reviews" not in table_names(table_database)


@pytest.mark.parametrize("failure", ["duplicate_name", "bad_check"])
def test_uc03_error_keeps_form_and_allows_correction(qtbot, window, table_database, failure):
    open_database(qtbot, window, table_database)
    tab = window.schema_tab.tables_tab
    name = "authors" if failure == "duplicate_name" else "reviews"
    dialog = open_create_dialog(qtbot, tab, name=name, columns=REVIEW_COLUMNS)
    if failure == "bad_check":
        dialog.add_constraint(TableConstraint(CHECK, expression="stars > 0; DROP TABLE authors"))
    assert not save_dialog(qtbot, window, dialog)
    assert tab.table_dialog is dialog
    assert dialog.isVisible()
    assert dialog.validation_label.text()
    assert not window.connection_errors
    assert save_button(dialog).isEnabled()
    assert dialog.columns_table.rowCount() == 3  # the typed columns are still there
    dialog.name_edit.setText("reviews")
    if failure == "bad_check":
        dialog.constraints_table.setCurrentCell(0, 0)
        dialog.remove_selected_constraint()
    assert save_dialog(qtbot, window, dialog)
    assert tab.selected_table().name == "reviews"
    assert table_names(table_database) >= {"authors", "reviews"}


def test_form_requires_name_and_columns(qtbot):
    dialog = TableDialog(())
    qtbot.addWidget(dialog)
    dialog.show()
    submissions = []
    dialog.submitted.connect(submissions.append)
    qtbot.mouseClick(save_button(dialog), Qt.MouseButton.LeftButton)
    assert "name" in dialog.validation_label.text()
    dialog.name_edit.setText("t")
    qtbot.mouseClick(save_button(dialog), Qt.MouseButton.LeftButton)
    assert "column" in dialog.validation_label.text()
    dialog.add_column()
    qtbot.mouseClick(save_button(dialog), Qt.MouseButton.LeftButton)
    assert "name" in dialog.validation_label.text()
    assert not submissions
    dialog.columns_table.item(0, COL_NAME).setText("a")
    qtbot.mouseClick(save_button(dialog), Qt.MouseButton.LeftButton)
    assert len(submissions) == 1


def test_form_builds_definition_from_the_grid(qtbot):
    dialog = TableDialog(("authors",))
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.name_edit.setText("  items  ")
    dialog.add_column()
    dialog.columns_table.item(0, COL_NAME).setText(" id ")
    dialog.columns_table.cellWidget(0, COL_TYPE).setCurrentText("INTEGER")
    for column in (COL_PK, COL_NOT_NULL, COL_AUTOINCREMENT):
        dialog.columns_table.item(0, column).setCheckState(Qt.CheckState.Checked)
    dialog.add_column()
    dialog.columns_table.item(1, COL_NAME).setText("label")
    dialog.columns_table.item(1, COL_DEFAULT).setText(" 'x' ")
    definition = dialog.get_definition()
    assert definition.name == "items"
    first, second = definition.columns
    assert (first.name, first.data_type, first.primary_key, first.not_null, first.autoincrement) == (
        "id", "INTEGER", True, True, True)
    assert (second.name, second.data_type, second.default) == ("label", "TEXT", "'x'")
    assert first.original_name is None and second.original_name is None
    assert 'PRIMARY KEY AUTOINCREMENT NOT NULL' in dialog.preview_edit.toPlainText()
    dialog.columns_table.setCurrentCell(1, COL_NAME)
    dialog.remove_selected_column()
    assert [c.name for c in dialog.get_definition().columns] == ["id"]


def test_form_constraint_list_is_editable_and_previewed(qtbot, monkeypatch):
    dialog = TableDialog(("authors",))
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.name_edit.setText("t")
    dialog.add_column(ColumnDef("a", "INTEGER"))
    dialog.add_column(ColumnDef("b", "INTEGER"))

    def accept_unique(self):
        self.kind_combo.setCurrentText(UNIQUE)
        for row in range(self.columns_list.count()):
            self.columns_list.item(row).setCheckState(Qt.CheckState.Checked)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ConstraintDialog, "exec", accept_unique)
    qtbot.mouseClick(dialog.btn_add_constraint, Qt.MouseButton.LeftButton)
    assert dialog.get_definition().constraints == (TableConstraint(UNIQUE, columns=("a", "b")),)
    assert dialog.constraints_table.item(0, 0).text() == UNIQUE
    assert 'UNIQUE ("a", "b")' in dialog.preview_edit.toPlainText()
    dialog.constraints_table.setCurrentCell(0, 0)
    qtbot.mouseClick(dialog.btn_remove_constraint, Qt.MouseButton.LeftButton)
    assert not dialog.get_definition().constraints
    assert dialog.constraints_table.rowCount() == 0


def test_constraint_form_shows_only_relevant_fields(qtbot):
    dialog = ConstraintDialog(("a", "b"), ("authors",), "t")
    qtbot.addWidget(dialog)
    dialog.show()
    for kind, columns, fk, check in [
        (PRIMARY_KEY, True, False, False), (UNIQUE, True, False, False),
        (FOREIGN_KEY, True, True, False), (CHECK, False, False, True),
    ]:
        dialog.kind_combo.setCurrentText(kind)
        assert (not dialog.columns_box.isHidden(), not dialog.fk_box.isHidden(), not dialog.check_box.isHidden()) == (
            columns, fk, check)


def test_constraint_form_builds_each_kind_and_validates(qtbot):
    dialog = ConstraintDialog(("a", "b"), ("authors",), "t")
    qtbot.addWidget(dialog)
    dialog.show()
    accepted = []
    dialog.accepted.connect(lambda: accepted.append(True))
    ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
    qtbot.mouseClick(ok, Qt.MouseButton.LeftButton)
    assert "column" in dialog.validation_label.text() and not accepted
    dialog.kind_combo.setCurrentText(CHECK)
    qtbot.mouseClick(ok, Qt.MouseButton.LeftButton)
    assert "expression" in dialog.validation_label.text() and not accepted
    dialog.expression_edit.setText(" a > 0 ")
    assert dialog.get_constraint() == TableConstraint(CHECK, expression="a > 0")
    dialog.kind_combo.setCurrentText(FOREIGN_KEY)
    dialog.columns_list.item(1).setCheckState(Qt.CheckState.Checked)
    dialog.ref_table_combo.setCurrentText("authors")
    dialog.ref_columns_edit.setText(" id , ")
    dialog.on_delete_combo.setCurrentIndex(dialog.on_delete_combo.findData("CASCADE"))
    assert dialog.get_constraint() == TableConstraint(
        FOREIGN_KEY, columns=("b",), ref_table="authors", ref_columns=("id",), on_delete="CASCADE")
    assert [dialog.ref_table_combo.itemText(i) for i in range(dialog.ref_table_combo.count())] == ["authors", "t"]
    qtbot.mouseClick(ok, Qt.MouseButton.LeftButton)
    assert accepted


def test_constraint_form_loads_an_existing_constraint(qtbot):
    existing = TableConstraint(
        FOREIGN_KEY, columns=("b",), ref_table="authors", ref_columns=("id",), on_update="SET NULL")
    dialog = ConstraintDialog(("a", "b"), ("authors",), "t", constraint=existing)
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.kind_combo.currentText() == FOREIGN_KEY
    assert dialog.get_constraint() == existing


# ---------------------------------------------------------------- UC-04


def stored_table(name, columns, constraints=()):
    return TableDefinition(name, tuple(columns), tuple(constraints))


def test_edit_form_loads_table_and_tracks_renames(qtbot):
    table = stored_table("items", [ColumnDef("id", "INTEGER", primary_key=True), ColumnDef("label", "TEXT")], [
        TableConstraint(CHECK, expression="id > 0"),
    ])
    dialog = TableDialog((), table=table)
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.name_edit.text() == "items" and not dialog.name_edit.isEnabled()
    assert dialog.constraints_table.rowCount() == 1
    dialog.columns_table.item(1, COL_NAME).setText("title")
    dialog.add_column(ColumnDef("extra", "TEXT"))
    definition = dialog.get_definition()
    assert definition.name == "items"
    assert [(c.name, c.original_name) for c in definition.columns] == [
        ("id", "id"), ("title", "label"), ("extra", None)]
    assert "id > 0" in dialog.preview_edit.toPlainText()


def test_uc04_adds_column_and_keeps_rows(qtbot, window, table_database):
    open_database(qtbot, window, table_database)
    tab = window.schema_tab.tables_tab
    dialog = open_edit_dialog(qtbot, tab, "authors")
    dialog.add_column(ColumnDef("country", "TEXT", not_null=True, default="'FI'"))
    assert save_dialog(qtbot, window, dialog, ALTER)
    assert tab.table_dialog is None
    assert tab.selected_table().name == "authors"
    assert "country" in tab.details_edit.toPlainText()
    assert tab.message_label.text() == "Table updated."
    with closing(sqlite3.connect(table_database)) as connection:
        assert connection.execute("SELECT id, name, country FROM authors ORDER BY id").fetchall() == [
            (1, "Ann", "FI"), (2, "Bob", "FI")]
        # The parent was rebuilt but the child table and its index are untouched.
        assert connection.execute("SELECT count(*) FROM sqlite_schema WHERE name = 'ix_books_title'").fetchone() == (1,)


def test_uc04_error_keeps_form_and_structure(qtbot, window, table_database):
    open_database(qtbot, window, table_database)
    tab = window.schema_tab.tables_tab
    dialog = open_edit_dialog(qtbot, tab, "authors")
    dialog.add_column(ColumnDef("country", "TEXT", not_null=True))  # rows exist, no default
    assert not save_dialog(qtbot, window, dialog, ALTER)
    assert tab.table_dialog is dialog and dialog.isVisible()
    assert "default value" in dialog.validation_label.text()
    assert not window.connection_errors
    with closing(sqlite3.connect(table_database)) as connection:
        assert [row[1] for row in connection.execute("PRAGMA table_info(authors)")] == ["id", "name"]
    # A failed change triggers a refresh; the form is locked until it finishes.
    qtbot.waitUntil(lambda: not window.controller.busy and save_button(dialog).isEnabled(), timeout=5000)
    dialog.columns_table.item(2, COL_NAME).setText("country")
    dialog.columns_table.item(2, COL_DEFAULT).setText("'FI'")
    assert save_dialog(qtbot, window, dialog, ALTER)
    assert [c.name for c in tab.selected_table().columns] == ["id", "name", "country"]


def test_uc04_cancel_keeps_table(qtbot, window, table_database):
    open_database(qtbot, window, table_database)
    tab = window.schema_tab.tables_tab
    before = tab.selected_table()
    dialog = open_edit_dialog(qtbot, tab, "authors")
    dialog.add_column(ColumnDef("x", "TEXT"))
    qtbot.mouseClick(dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel), Qt.MouseButton.LeftButton)
    assert tab.table_dialog is None
    assert tab.selected_table() == before
    with closing(sqlite3.connect(table_database)) as connection:
        assert [row[1] for row in connection.execute("PRAGMA table_info(authors)")] == ["id", "name"]


def test_uc04_tables_the_form_cannot_keep_are_view_only(qtbot, window, table_database):
    open_database(qtbot, window, table_database)
    tab = window.schema_tab.tables_tab
    select_table(tab, "odd")
    assert not tab.btn_edit.isEnabled()
    assert "COLLATE" in tab.btn_edit.toolTip()
    assert "COLLATE" in tab.details_edit.toPlainText()
    assert tab.btn_drop.isEnabled()
    select_table(tab, "authors")
    assert tab.btn_edit.isEnabled()


# ---------------------------------------------------------------- UC-05


def test_uc05_confirmation_deletes_table_and_updates_both_sections(qtbot, window, table_database, monkeypatch):
    open_database(qtbot, window, table_database)
    tab = window.schema_tab.tables_tab
    select_table(tab, "books")
    asked = []

    def confirm(parent, title, message, buttons, default):
        asked.append(message)
        assert default == QMessageBox.StandardButton.No
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", confirm)
    assert wait_operation(qtbot, window.controller, DROP, lambda: qtbot.mouseClick(tab.btn_drop, Qt.MouseButton.LeftButton))
    assert '"books"' in asked[0] and "all its rows" in asked[0]
    assert "books" not in table_names(table_database)
    assert [row for row in range(tab.tables_list.count()) if tab.tables_list.item(row).data(Qt.ItemDataRole.UserRole) == "books"] == []
    assert tab.message_label.text() == "Table deleted."
    # Its index disappears from the Indexes section too.
    qtbot.waitUntil(lambda: not any(i.name == "ix_books_title" for i in window.schema_tab.snapshot.indexes), timeout=5000)


def test_uc05_warning_names_tables_that_reference_the_table(qtbot, window, table_database, monkeypatch):
    open_database(qtbot, window, table_database)
    tab = window.schema_tab.tables_tab
    select_table(tab, "authors")
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda parent, title, message, *rest: asked.append(message) or QMessageBox.StandardButton.No)
    qtbot.mouseClick(tab.btn_drop, Qt.MouseButton.LeftButton)
    assert "books" in asked[0] and "foreign keys" in asked[0]
    assert "authors" in table_names(table_database)


def test_uc05_cancel_keeps_table(qtbot, window, table_database, monkeypatch):
    open_database(qtbot, window, table_database)
    tab = window.schema_tab.tables_tab
    select_table(tab, "authors")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    qtbot.mouseClick(tab.btn_drop, Qt.MouseButton.LeftButton)
    assert not window.controller.busy
    assert tab.selected_table().name == "authors"
    assert "authors" in table_names(table_database)


def test_uc05_refused_deletion_reports_reason_and_keeps_table(qtbot, window, table_database, monkeypatch):
    with closing(sqlite3.connect(table_database)) as connection, connection:
        connection.execute("INSERT INTO books(title, author_id) VALUES ('t', 1)")
    open_database(qtbot, window, table_database)
    tab = window.schema_tab.tables_tab
    select_table(tab, "authors")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    assert not wait_operation(qtbot, window.controller, DROP, lambda: qtbot.mouseClick(tab.btn_drop, Qt.MouseButton.LeftButton))
    qtbot.waitUntil(lambda: not window.controller.busy, timeout=5000)
    assert "FOREIGN KEY" in tab.message_label.text()
    assert "authors" in table_names(table_database)
    assert tab.selected_table().name == "authors"


def test_uc05_table_deleted_elsewhere_refreshes_the_list(qtbot, window, table_database, monkeypatch):
    open_database(qtbot, window, table_database)
    tab = window.schema_tab.tables_tab
    select_table(tab, "odd")
    with closing(sqlite3.connect(table_database)) as connection, connection:
        connection.execute("DROP TABLE odd")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    assert not wait_operation(qtbot, window.controller, DROP, lambda: qtbot.mouseClick(tab.btn_drop, Qt.MouseButton.LeftButton))
    qtbot.waitUntil(lambda: not window.controller.busy and "odd" not in [t.name for t in tab.snapshot.tables], timeout=5000)
    assert "does not exist" in tab.message_label.text()


# ---------------------------------------------------------------- connection state


def test_actions_are_disabled_without_a_connection(qtbot, window):
    tab = window.schema_tab.tables_tab
    for button in (tab.btn_create, tab.btn_edit, tab.btn_drop, tab.btn_refresh):
        assert not button.isEnabled()


def test_empty_database_allows_creating_the_first_table(qtbot, window, tmp_path):
    path = tmp_path / "empty.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA user_version=1")
    assert wait_operation(qtbot, window.controller, "connect", lambda: window.controller.create("Empty", str(path)))
    qtbot.waitUntil(lambda: not window.controller.busy, timeout=5000)
    tab = window.schema_tab.tables_tab
    assert tab.btn_create.isEnabled() and not tab.btn_edit.isEnabled() and not tab.btn_drop.isEnabled()
    assert tab.message_label.text() == "No tables in this database."
    dialog = open_create_dialog(qtbot, tab, name="first", columns=[ColumnDef("id", "INTEGER", primary_key=True)])
    assert save_dialog(qtbot, window, dialog)
    assert "first" in table_names(path)


def test_read_only_connection_allows_viewing_only(qtbot, window, table_database):
    open_database(qtbot, window, table_database)

    def reopen_read_only(manager):
        manager.executor.connect({"file_path": str(table_database), "read_only": True})

    assert wait_operation(qtbot, window.controller, "read-only", lambda: window.controller.submit_database_operation("read-only", reopen_read_only))
    tab = window.schema_tab.tables_tab
    qtbot.waitUntil(lambda: not window.controller.busy and tab.selected_table() is not None, timeout=5000)
    assert tab.btn_refresh.isEnabled()
    assert not tab.btn_create.isEnabled()
    assert not tab.btn_edit.isEnabled()
    assert not tab.btn_drop.isEnabled()


def test_worker_disables_actions_until_operation_finishes(qtbot, window, table_database):
    open_database(qtbot, window, table_database)
    tab = window.schema_tab.tables_tab
    started, release = Event(), Event()

    def hold_worker(manager):
        started.set()
        assert release.wait(3)

    try:
        window.controller.submit_database_operation("probe", hold_worker)
        qtbot.waitUntil(started.is_set, timeout=1000)
        for button in (tab.btn_create, tab.btn_edit, tab.btn_drop, tab.btn_refresh):
            assert not button.isEnabled()
        assert not tab.tables_list.isEnabled()
        assert wait_operation(qtbot, window.controller, "probe", release.set)
    finally:
        release.set()
    assert tab.btn_create.isEnabled() and tab.btn_drop.isEnabled()


def test_switching_database_clears_old_tables(qtbot, window, table_database, database_factory):
    open_database(qtbot, window, table_database)
    tab = window.schema_tab.tables_tab
    assert tab.tables_list.count() == 3
    assert wait_operation(qtbot, window.controller, "connect", lambda: window.controller.create("Other", str(database_factory("other.sqlite3"))))
    qtbot.waitUntil(lambda: not window.controller.busy and [t.name for t in tab.snapshot.tables] == ["sample"], timeout=5000)
    assert tab.tables_list.count() == 1


def test_reopening_same_database_refreshes_external_changes(qtbot, window, table_database):
    open_database(qtbot, window, table_database)
    with closing(sqlite3.connect(table_database)) as connection, connection:
        connection.execute("CREATE TABLE external(a)")
    profile = window.controller.active.profile
    assert wait_operation(qtbot, window.controller, "connect", lambda: window.controller.connect_saved(profile.id))
    qtbot.waitUntil(lambda: not window.controller.busy and "external" in [t.name for t in window.schema_tab.tables_tab.snapshot.tables], timeout=5000)


def test_open_transaction_is_reported_after_a_change(qtbot, window, table_database):
    open_database(qtbot, window, table_database)
    tab = window.schema_tab.tables_tab

    def begin(manager):
        manager.executor.connection.execute("UPDATE authors SET name = 'Anna' WHERE id = 1")

    assert wait_operation(qtbot, window.controller, "schema.begin", lambda: window.controller.submit_database_operation("schema.begin", begin))
    try:
        dialog = open_create_dialog(qtbot, tab, columns=REVIEW_COLUMNS)
        assert save_dialog(qtbot, window, dialog)
        assert "Commit it to save the change" in tab.message_label.text()
    finally:
        # The index list is refreshed after a table change; wait so the rollback is accepted.
        qtbot.waitUntil(lambda: not window.controller.busy, timeout=5000)
        assert wait_operation(qtbot, window.controller, "schema.rollback", lambda: window.controller.submit_database_operation("schema.rollback", lambda manager: manager.executor.connection.rollback()))
    assert "reviews" not in table_names(table_database)


def test_details_text_lists_columns_constraints_and_references():
    table = TableDefinition(
        "authors", (ColumnDef("id", "INTEGER", primary_key=True, autoincrement=True), ColumnDef("n", "TEXT", not_null=True, default="'x'")),
        (TableConstraint(CHECK, expression="id > 0"),), referenced_by=("books",), sql="CREATE TABLE authors(...)",
    )
    text = describe_table(table)
    assert "id INTEGER PRIMARY KEY AUTOINCREMENT" in text
    assert "n TEXT NOT NULL DEFAULT 'x'" in text
    assert "CHECK id > 0" in text and "Referenced by: books" in text
