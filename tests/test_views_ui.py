"""View forms (UC-09, UC-10, UC-11) talking to the real SQLite worker."""

import sqlite3
from contextlib import closing
from threading import Event

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialogButtonBox, QMessageBox

from database_browser.controller import ConnectionController
from database_browser.schema.view_models import ViewDefinition
from database_browser.schema.view_ui import ViewDialog, describe_view
from database_browser.ui import MainWindow, NotificationManager

CREATE, ALTER, DROP = "schema.view.create", "schema.view.alter", "schema.view.drop"


@pytest.fixture
def view_database(tmp_path):
    path = tmp_path / "views.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE authors(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE books(id INTEGER PRIMARY KEY, title TEXT, author_id INTEGER, year INTEGER);
            INSERT INTO authors(name) VALUES ('Ann'), ('Bob');
            INSERT INTO books(title, author_id, year) VALUES ('Clean Code', 1, 2008), ('UML', 2, 2004);
            CREATE VIEW v_books AS SELECT b.id AS id, b.title AS title, b.year AS year, a.name AS author
                FROM books b JOIN authors a ON a.id = b.author_id;
            CREATE VIEW v_recent AS SELECT id, title FROM v_books WHERE year >= 2005;
            CREATE VIEW v_authors AS SELECT id, name FROM authors;
            CREATE TRIGGER tr_v_authors INSTEAD OF INSERT ON v_authors
                BEGIN INSERT INTO authors(name) VALUES (NEW.name); END;
            """
        )
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
    widget.schema_tab.sections.setCurrentWidget(widget.schema_tab.views_tab)
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


def open_database(qtbot, window, path, views=True):
    controller = window.controller
    assert wait_operation(qtbot, controller, "connect", lambda: controller.create("Demo", str(path)))
    qtbot.waitUntil(
        lambda: not controller.busy and bool(window.schema_tab.snapshot.tables)
        and bool(window.schema_tab.tables_tab.snapshot.tables)
        and (bool(window.schema_tab.views_tab.snapshot.views) or not views),
        timeout=5000,
    )


def idle(qtbot, window, dialog=None):
    """A failed change refreshes the list; the form is locked until that finishes."""
    qtbot.waitUntil(
        lambda: not window.controller.busy and (dialog is None or save_button(dialog).isEnabled()),
        timeout=5000,
    )


def save_button(dialog):
    return dialog.buttons.button(QDialogButtonBox.StandardButton.Save)


def save_dialog(qtbot, window, dialog, operation=CREATE):
    return wait_operation(
        qtbot, window.controller, operation,
        lambda: qtbot.mouseClick(save_button(dialog), Qt.MouseButton.LeftButton),
    )


def open_create_dialog(qtbot, tab, name="v_names", query="SELECT name FROM authors"):
    qtbot.mouseClick(tab.btn_create, Qt.MouseButton.LeftButton)
    dialog = tab.view_dialog
    assert dialog is not None
    dialog.name_edit.setText(name)
    dialog.query_edit.setPlainText(query)
    return dialog


def select_view(tab, name):
    for row in range(tab.views_list.count()):
        if tab.views_list.item(row).data(Qt.ItemDataRole.UserRole) == name:
            tab.views_list.setCurrentRow(row)
            return
    raise AssertionError(f"{name} is not listed")


def open_edit_dialog(qtbot, tab, name):
    select_view(tab, name)
    qtbot.mouseClick(tab.btn_edit, Qt.MouseButton.LeftButton)
    assert tab.view_dialog is not None
    return tab.view_dialog


def listed(tab):
    return [tab.views_list.item(row).data(Qt.ItemDataRole.UserRole) for row in range(tab.views_list.count())]


def view_names(path):
    with closing(sqlite3.connect(path)) as connection:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'view'")}


def stored_view(name="v", query="SELECT 1"):
    return ViewDefinition(name, query, ("1",), sql=f'CREATE VIEW "{name}" AS\n{query}')


# ---------------------------------------------------------------- form


def test_form_requires_name_and_query(qtbot):
    dialog = ViewDialog()
    qtbot.addWidget(dialog)
    dialog.show()
    submissions = []
    dialog.submitted.connect(submissions.append)
    qtbot.mouseClick(save_button(dialog), Qt.MouseButton.LeftButton)
    assert "name" in dialog.validation_label.text()
    dialog.name_edit.setText("  v  ")
    qtbot.mouseClick(save_button(dialog), Qt.MouseButton.LeftButton)
    assert "query" in dialog.validation_label.text()
    assert not submissions
    dialog.query_edit.setPlainText("  SELECT 1  ")
    qtbot.mouseClick(save_button(dialog), Qt.MouseButton.LeftButton)
    assert [(d.name, d.query, d.original_name, d.sql) for d in submissions] == [("v", "SELECT 1", None, None)]
    assert 'CREATE VIEW main."v" AS' in dialog.preview_edit.toPlainText()


def test_edit_form_loads_the_view_and_remembers_the_stored_version(qtbot):
    view = stored_view("v_x", "SELECT id FROM t")
    dialog = ViewDialog(view=view)
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.name_edit.text() == "v_x" and dialog.query_edit.toPlainText() == "SELECT id FROM t"
    assert dialog.windowTitle() == 'Edit view "v_x"'
    definition = dialog.get_definition()
    assert (definition.name, definition.original_name, definition.sql) == ("v_x", "v_x", view.sql)
    dialog.name_edit.setText("  v_y ")
    assert dialog.get_definition().name == "v_y" and dialog.get_definition().original_name == "v_x"


def test_edit_form_keeps_a_stored_name_exactly(qtbot):
    dialog = ViewDialog(view=stored_view(" odd name ", "SELECT 1"))
    qtbot.addWidget(dialog)
    assert dialog.get_definition().name == " odd name "


def test_form_preview_does_not_fail_on_unusable_names(qtbot):
    dialog = ViewDialog()
    qtbot.addWidget(dialog)
    dialog.name_edit.setText("a\x00b")
    dialog.query_edit.setPlainText("SELECT 1")
    assert "null" in dialog.preview_edit.toPlainText()


def test_details_text_lists_columns_query_and_dependencies():
    view = ViewDefinition(
        "v", "SELECT 1 AS one", ("one",), error="no such table: main.t", used_by=("w",), triggers=("tr",),
        editable=False, reason="Cannot be edited with this form: a column list after the view name.",
    )
    text = describe_view(view)
    assert "cannot be queried now: no such table: main.t" in text
    assert "Columns: one" in text and "SELECT 1 AS one" in text
    assert "Used by views: w" in text and "Triggers: tr" in text and "column list" in text


# ---------------------------------------------------------------- UC-09


def test_uc09_creates_view_and_lists_it(qtbot, window, view_database):
    open_database(qtbot, window, view_database)
    tab = window.schema_tab.views_tab
    dialog = open_create_dialog(qtbot, tab)
    assert 'CREATE VIEW main."v_names"' in dialog.preview_edit.toPlainText()
    assert save_dialog(qtbot, window, dialog)
    assert tab.view_dialog is None
    assert tab.selected_view().name == "v_names"
    assert "Columns: name" in tab.details_edit.toPlainText()
    assert tab.message_label.text() == "View created."
    assert "v_names" in view_names(view_database)


def test_uc09_cancel_leaves_database_unchanged(qtbot, window, view_database):
    open_database(qtbot, window, view_database)
    tab = window.schema_tab.views_tab
    dialog = open_create_dialog(qtbot, tab)
    qtbot.mouseClick(dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel), Qt.MouseButton.LeftButton)
    assert tab.view_dialog is None and not window.controller.busy
    assert "v_names" not in view_names(view_database)


@pytest.mark.parametrize("name, query, message", [
    ("books", "SELECT 1", "already exists"),
    ("v_bad", "SELECT nope FROM authors", "no such column: nope"),
    ("v_bad", "SELECT 1; DROP TABLE authors", "semicolon"),
])
def test_uc09_error_keeps_form_and_allows_correction(qtbot, window, view_database, name, query, message):
    open_database(qtbot, window, view_database)
    tab = window.schema_tab.views_tab
    dialog = open_create_dialog(qtbot, tab, name=name, query=query)
    assert not save_dialog(qtbot, window, dialog)
    assert tab.view_dialog is dialog and dialog.isVisible()
    assert message in dialog.validation_label.text()
    assert not window.connection_errors
    idle(qtbot, window, dialog)
    assert dialog.query_edit.toPlainText() == query  # the typed text is still there
    dialog.name_edit.setText("v_ok")
    dialog.query_edit.setPlainText("SELECT id FROM authors")
    assert save_dialog(qtbot, window, dialog)
    assert tab.selected_view().name == "v_ok"
    assert "v_ok" in view_names(view_database)
    with closing(sqlite3.connect(view_database)) as connection:
        assert connection.execute("SELECT count(*) FROM authors").fetchone() == (2,)


# ---------------------------------------------------------------- UC-10


def test_uc10_changes_the_query(qtbot, window, view_database):
    open_database(qtbot, window, view_database)
    tab = window.schema_tab.views_tab
    dialog = open_edit_dialog(qtbot, tab, "v_recent")
    assert dialog.name_edit.text() == "v_recent" and "year >= 2005" in dialog.query_edit.toPlainText()
    dialog.query_edit.setPlainText("SELECT id, title, year FROM v_books")
    assert save_dialog(qtbot, window, dialog, ALTER)
    assert tab.view_dialog is None and tab.selected_view().name == "v_recent"
    assert "Columns: id, title, year" in tab.details_edit.toPlainText()
    assert tab.message_label.text() == "View updated."
    with closing(sqlite3.connect(view_database)) as connection:
        assert connection.execute("SELECT count(*) FROM v_recent").fetchone() == (2,)


def test_uc10_renames_a_view(qtbot, window, view_database):
    open_database(qtbot, window, view_database)
    tab = window.schema_tab.views_tab
    dialog = open_edit_dialog(qtbot, tab, "v_recent")
    dialog.name_edit.setText("v_new")
    assert save_dialog(qtbot, window, dialog, ALTER)
    assert "v_new" in listed(tab) and "v_recent" not in listed(tab)
    assert tab.selected_view().name == "v_new"
    assert {"v_new", "v_books"} <= view_names(view_database) and "v_recent" not in view_names(view_database)


def test_uc10_error_keeps_form_and_old_view(qtbot, window, view_database):
    open_database(qtbot, window, view_database)
    tab = window.schema_tab.views_tab
    dialog = open_edit_dialog(qtbot, tab, "v_recent")
    dialog.query_edit.setPlainText("SELECT nope FROM books")
    assert not save_dialog(qtbot, window, dialog, ALTER)
    assert tab.view_dialog is dialog and dialog.isVisible()
    assert "no such column: nope" in dialog.validation_label.text()
    assert not window.connection_errors
    with closing(sqlite3.connect(view_database)) as connection:
        assert connection.execute("SELECT * FROM v_recent").fetchall() == [(1, "Clean Code")]
    idle(qtbot, window, dialog)  # the refresh after a failed change is done
    dialog.query_edit.setPlainText("SELECT id FROM books")
    assert save_dialog(qtbot, window, dialog, ALTER)
    assert "Columns: id" in tab.details_edit.toPlainText()


def test_uc10_change_that_would_break_another_view_is_refused(qtbot, window, view_database):
    open_database(qtbot, window, view_database)
    tab = window.schema_tab.views_tab
    dialog = open_edit_dialog(qtbot, tab, "v_books")
    dialog.query_edit.setPlainText("SELECT id, title FROM books")
    assert not save_dialog(qtbot, window, dialog, ALTER)
    assert 'View "v_recent" would stop working' in dialog.validation_label.text()
    with closing(sqlite3.connect(view_database)) as connection:
        assert connection.execute("SELECT count(*) FROM v_recent").fetchone() == (1,)


def test_uc10_stale_form_is_rejected_and_the_external_change_survives(qtbot, window, view_database):
    open_database(qtbot, window, view_database)
    tab = window.schema_tab.views_tab
    dialog = open_edit_dialog(qtbot, tab, "v_recent")
    with closing(sqlite3.connect(view_database)) as connection, connection:
        connection.execute("DROP VIEW v_recent")
        connection.execute("CREATE VIEW v_recent AS SELECT id, title, year FROM v_books")
    dialog.query_edit.setPlainText("SELECT id FROM v_books")
    assert not save_dialog(qtbot, window, dialog, ALTER)
    assert tab.view_dialog is dialog
    assert "changed after this form was opened" in dialog.validation_label.text()
    with closing(sqlite3.connect(view_database)) as connection:
        assert [row[1] for row in connection.execute("PRAGMA table_info(v_recent)")] == ["id", "title", "year"]
    idle(qtbot, window, dialog)
    assert "year" in tab.selected_view().columns  # the list shows the external version
    qtbot.mouseClick(dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel), Qt.MouseButton.LeftButton)
    dialog = open_edit_dialog(qtbot, tab, "v_recent")
    assert "year" in dialog.query_edit.toPlainText()
    dialog.query_edit.setPlainText("SELECT id FROM v_books")
    assert save_dialog(qtbot, window, dialog, ALTER)


def test_uc10_views_the_form_cannot_keep_are_view_only(qtbot, window, view_database):
    with closing(sqlite3.connect(view_database)) as connection, connection:
        connection.execute("CREATE VIEW v_cols(x, y) AS SELECT id, name FROM authors")
    open_database(qtbot, window, view_database)
    tab = window.schema_tab.views_tab
    select_view(tab, "v_cols")
    assert not tab.btn_edit.isEnabled() and "column list" in tab.btn_edit.toolTip()
    assert "view only" in tab.views_list.currentItem().text()
    assert tab.btn_drop.isEnabled()
    select_view(tab, "v_books")
    assert tab.btn_edit.isEnabled()


def test_uc10_broken_view_is_flagged_and_can_be_repaired(qtbot, window, view_database):
    with closing(sqlite3.connect(view_database)) as connection, connection:
        connection.execute("CREATE VIEW v_gone AS SELECT * FROM missing_table")
    open_database(qtbot, window, view_database)
    tab = window.schema_tab.views_tab
    select_view(tab, "v_gone")
    assert "broken" in tab.views_list.currentItem().text()
    assert "missing_table" in tab.details_edit.toPlainText()
    assert "1 cannot be queried" in tab.message_label.text()
    dialog = open_edit_dialog(qtbot, tab, "v_gone")
    dialog.query_edit.setPlainText("SELECT id FROM authors")
    assert save_dialog(qtbot, window, dialog, ALTER)
    assert "broken" not in tab.views_list.currentItem().text()


# ---------------------------------------------------------------- UC-11


def test_uc11_confirmation_deletes_view_and_warns_about_dependents(qtbot, window, view_database, monkeypatch):
    open_database(qtbot, window, view_database)
    tab = window.schema_tab.views_tab
    select_view(tab, "v_books")
    asked = []

    def confirm(parent, title, message, buttons, default):
        asked.append(message)
        assert default == QMessageBox.StandardButton.No
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", confirm)
    assert wait_operation(qtbot, window.controller, DROP, lambda: qtbot.mouseClick(tab.btn_drop, Qt.MouseButton.LeftButton))
    assert '"v_books"' in asked[0] and "tables it reads are not changed" in asked[0]
    assert "v_recent" in asked[0]
    assert "v_books" not in view_names(view_database)
    assert "v_books" not in listed(tab) and tab.message_label.text() == "View deleted."
    with closing(sqlite3.connect(view_database)) as connection:
        assert connection.execute("SELECT count(*) FROM books").fetchone() == (2,)


def test_uc11_warning_names_the_triggers_that_go_with_the_view(qtbot, window, view_database, monkeypatch):
    open_database(qtbot, window, view_database)
    tab = window.schema_tab.views_tab
    select_view(tab, "v_authors")
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda parent, title, message, *rest: asked.append(message) or QMessageBox.StandardButton.No)
    qtbot.mouseClick(tab.btn_drop, Qt.MouseButton.LeftButton)
    assert "tr_v_authors" in asked[0]
    assert "v_authors" in view_names(view_database)


def test_uc11_cancel_keeps_view(qtbot, window, view_database, monkeypatch):
    open_database(qtbot, window, view_database)
    tab = window.schema_tab.views_tab
    select_view(tab, "v_recent")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    qtbot.mouseClick(tab.btn_drop, Qt.MouseButton.LeftButton)
    assert not window.controller.busy
    assert tab.selected_view().name == "v_recent" and "v_recent" in view_names(view_database)


def test_uc11_view_deleted_elsewhere_refreshes_the_list(qtbot, window, view_database, monkeypatch):
    open_database(qtbot, window, view_database)
    tab = window.schema_tab.views_tab
    select_view(tab, "v_recent")
    with closing(sqlite3.connect(view_database)) as connection, connection:
        connection.execute("DROP VIEW v_recent")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    assert not wait_operation(qtbot, window.controller, DROP, lambda: qtbot.mouseClick(tab.btn_drop, Qt.MouseButton.LeftButton))
    qtbot.waitUntil(lambda: not window.controller.busy and "v_recent" not in listed(tab), timeout=5000)
    assert "does not exist" in tab.message_label.text()


# ---------------------------------------------------------------- other sections and connection state


def test_changing_a_table_refreshes_the_views_section(qtbot, window, view_database, monkeypatch):
    open_database(qtbot, window, view_database)
    tables = window.schema_tab.tables_tab
    views = window.schema_tab.views_tab
    assert "broken" not in views.views_list.item(0).text()
    for row in range(tables.tables_list.count()):
        if tables.tables_list.item(row).data(Qt.ItemDataRole.UserRole) == "books":
            tables.tables_list.setCurrentRow(row)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    assert wait_operation(qtbot, window.controller, "schema.table.drop", lambda: qtbot.mouseClick(tables.btn_drop, Qt.MouseButton.LeftButton))
    qtbot.waitUntil(
        lambda: not window.controller.busy and any("broken" in views.views_list.item(r).text() for r in range(views.views_list.count())),
        timeout=5000,
    )


def test_actions_are_disabled_without_a_connection(qtbot, window):
    tab = window.schema_tab.views_tab
    for button in (tab.btn_create, tab.btn_edit, tab.btn_drop, tab.btn_refresh):
        assert not button.isEnabled()


def test_database_without_views_allows_creating_the_first_one(qtbot, window, tmp_path):
    path = tmp_path / "plain.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE t(a)")
    assert wait_operation(qtbot, window.controller, "connect", lambda: window.controller.create("Plain", str(path)))
    qtbot.waitUntil(lambda: not window.controller.busy, timeout=5000)
    tab = window.schema_tab.views_tab
    assert tab.btn_create.isEnabled() and not tab.btn_edit.isEnabled() and not tab.btn_drop.isEnabled()
    assert tab.message_label.text() == "No views in this database."
    dialog = open_create_dialog(qtbot, tab, name="first", query="SELECT a FROM t")
    assert save_dialog(qtbot, window, dialog)
    assert "first" in view_names(path)


def test_read_only_connection_allows_viewing_only(qtbot, window, view_database):
    open_database(qtbot, window, view_database)

    def reopen_read_only(manager):
        manager.executor.connect({"file_path": str(view_database), "read_only": True})

    assert wait_operation(qtbot, window.controller, "read-only", lambda: window.controller.submit_database_operation("read-only", reopen_read_only))
    tab = window.schema_tab.views_tab
    qtbot.waitUntil(lambda: not window.controller.busy and tab.selected_view() is not None, timeout=5000)
    assert tab.btn_refresh.isEnabled()
    assert not tab.btn_create.isEnabled() and not tab.btn_edit.isEnabled() and not tab.btn_drop.isEnabled()


def test_worker_disables_actions_until_operation_finishes(qtbot, window, view_database):
    open_database(qtbot, window, view_database)
    tab = window.schema_tab.views_tab
    started, release = Event(), Event()

    def hold_worker(manager):
        started.set()
        assert release.wait(3)

    try:
        window.controller.submit_database_operation("probe", hold_worker)
        qtbot.waitUntil(started.is_set, timeout=1000)
        for button in (tab.btn_create, tab.btn_edit, tab.btn_drop, tab.btn_refresh):
            assert not button.isEnabled()
        assert not tab.views_list.isEnabled()
        assert wait_operation(qtbot, window.controller, "probe", release.set)
    finally:
        release.set()
    assert tab.btn_create.isEnabled() and tab.btn_drop.isEnabled()


def test_switching_database_clears_old_views(qtbot, window, view_database, database_factory):
    open_database(qtbot, window, view_database)
    tab = window.schema_tab.views_tab
    assert listed(tab) == ["v_authors", "v_books", "v_recent"]
    assert wait_operation(qtbot, window.controller, "connect", lambda: window.controller.create("Other", str(database_factory("other.sqlite3"))))
    qtbot.waitUntil(lambda: not window.controller.busy and tab.views_list.count() == 0, timeout=5000)
    assert tab.message_label.text() == "No views in this database."


def test_open_transaction_is_reported_after_a_change(qtbot, window, view_database):
    open_database(qtbot, window, view_database)
    tab = window.schema_tab.views_tab

    def begin(manager):
        manager.executor.connection.execute("UPDATE authors SET name = 'Anna' WHERE id = 1")

    assert wait_operation(qtbot, window.controller, "schema.begin", lambda: window.controller.submit_database_operation("schema.begin", begin))
    try:
        dialog = open_create_dialog(qtbot, tab)
        assert save_dialog(qtbot, window, dialog)
        assert "Commit it to save the change" in tab.message_label.text()
    finally:
        qtbot.waitUntil(lambda: not window.controller.busy, timeout=5000)
        assert wait_operation(qtbot, window.controller, "schema.rollback", lambda: window.controller.submit_database_operation("schema.rollback", lambda manager: manager.executor.connection.rollback()))
    assert "v_names" not in view_names(view_database)
