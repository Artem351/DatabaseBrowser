"""Index forms talking to the real SQLite worker."""

import sqlite3
from contextlib import closing
from threading import Event

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialogButtonBox

from database_browser.controller import ConnectionController
from database_browser.errors import ProfileStorageError
from database_browser.schema.models import TableInfo
from database_browser.schema.ui import IndexDialog
from database_browser.services import ConnectionManager
from database_browser.ui import MainWindow, NotificationManager


@pytest.fixture
def index_database(tmp_path):
    path = tmp_path / "indexes.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, category TEXT, label TEXT)")
        connection.executemany(
            "INSERT INTO items(category, label) VALUES (?, ?)",
            [("books", "first"), ("books", "second")],
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
    qtbot.waitUntil(lambda: not controller.busy and bool(window.schema_tab.snapshot.tables), timeout=5000)


def fill_dialog(qtbot, tab, name="ix_category", columns=("category",), unique=False):
    qtbot.mouseClick(tab.btn_create, Qt.MouseButton.LeftButton)
    dialog = tab.index_dialog
    assert dialog is not None
    dialog.name_edit.setText(name)
    dialog.unique_check.setChecked(unique)
    for row in range(dialog.columns_table.rowCount()):
        item = dialog.columns_table.item(row, 0)
        item.setCheckState(Qt.CheckState.Checked if item.text() in columns else Qt.CheckState.Unchecked)
    return dialog


def save_button(dialog):
    return dialog.buttons.button(QDialogButtonBox.StandardButton.Save)


def test_uc06_creates_index_and_selects_it_in_schema(qtbot, window, index_database):
    open_database(qtbot, window, index_database)
    tab = window.schema_tab
    dialog = fill_dialog(qtbot, tab)
    assert '"category" ASC' in dialog.preview_edit.toPlainText()
    assert wait_operation(qtbot, window.controller, "schema.create", lambda: qtbot.mouseClick(save_button(dialog), Qt.MouseButton.LeftButton))
    assert tab.index_dialog is None
    assert tab.selected_index().name == "ix_category"
    assert "CREATE INDEX" in tab.details_edit.toPlainText()
    with closing(sqlite3.connect(index_database)) as connection:
        assert connection.execute("SELECT name FROM sqlite_schema WHERE type='index'").fetchall() == [("ix_category",)]


def test_uc06_cancel_leaves_database_unchanged(qtbot, window, index_database):
    open_database(qtbot, window, index_database)
    dialog = fill_dialog(qtbot, window.schema_tab)
    qtbot.mouseClick(dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel), Qt.MouseButton.LeftButton)
    assert window.schema_tab.index_dialog is None
    assert not window.controller.busy
    with closing(sqlite3.connect(index_database)) as connection:
        assert connection.execute("SELECT count(*) FROM sqlite_schema WHERE type='index'").fetchone() == (0,)


def test_uc06_success_does_not_depend_on_profile_storage(qtbot, window, index_database, monkeypatch):
    open_database(qtbot, window, index_database)
    dialog = fill_dialog(qtbot, window.schema_tab)

    def unavailable_profiles(manager):
        raise ProfileStorageError("settings temporarily unavailable")

    # A schema write does not change profiles, so their storage is not needed here.
    monkeypatch.setattr(ConnectionManager, "list_connections", unavailable_profiles)
    assert wait_operation(qtbot, window.controller, "schema.create", lambda: qtbot.mouseClick(save_button(dialog), Qt.MouseButton.LeftButton))
    assert window.schema_tab.index_dialog is None
    assert window.schema_tab.selected_index().name == "ix_category"


@pytest.mark.parametrize("failure", ["duplicate_name", "unique_values"])
def test_uc06_error_keeps_form_and_allows_correction(qtbot, window, index_database, failure):
    if failure == "duplicate_name":
        with closing(sqlite3.connect(index_database)) as connection:
            connection.execute("CREATE INDEX ix_category ON items(label)")
    open_database(qtbot, window, index_database)
    tab = window.schema_tab
    dialog = fill_dialog(qtbot, tab, unique=failure == "unique_values")
    assert not wait_operation(qtbot, window.controller, "schema.create", lambda: qtbot.mouseClick(save_button(dialog), Qt.MouseButton.LeftButton))
    assert tab.index_dialog is dialog
    assert dialog.isVisible()
    assert dialog.name_edit.text() == "ix_category"
    assert dialog.validation_label.text()
    assert not window.connection_errors
    assert save_button(dialog).isEnabled()
    dialog.name_edit.setText("ix_corrected")
    dialog.unique_check.setChecked(False)
    assert wait_operation(qtbot, window.controller, "schema.create", lambda: qtbot.mouseClick(save_button(dialog), Qt.MouseButton.LeftButton))
    assert tab.selected_index().name == "ix_corrected"


def test_editor_requires_name_and_columns(qtbot):
    dialog = IndexDialog((TableInfo("items", ("id", "value")),))
    qtbot.addWidget(dialog)
    dialog.show()
    submissions = []
    dialog.submitted.connect(submissions.append)
    qtbot.mouseClick(save_button(dialog), Qt.MouseButton.LeftButton)
    assert "name" in dialog.validation_label.text()
    dialog.name_edit.setText("ix_value")
    qtbot.mouseClick(save_button(dialog), Qt.MouseButton.LeftButton)
    assert "column" in dialog.validation_label.text()
    assert not submissions


def test_editor_preserves_column_order_and_direction(qtbot):
    dialog = IndexDialog((TableInfo("items", ("id", "category", "label")),))
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.name_edit.setText("ix_ordered")
    dialog.columns_table.item(2, 0).setCheckState(Qt.CheckState.Checked)
    dialog.columns_table.cellWidget(2, 1).setCurrentText("DESC")
    dialog.columns_table.setCurrentCell(2, 0)
    qtbot.mouseClick(dialog.btn_up, Qt.MouseButton.LeftButton)
    dialog.columns_table.item(2, 0).setCheckState(Qt.CheckState.Checked)
    assert [(column.name, column.descending) for column in dialog.get_index().columns] == [("label", True), ("category", False)]
    assert '"label" DESC, "category" ASC' in dialog.preview_edit.toPlainText()


def test_switching_database_clears_old_indexes(qtbot, window, index_database, database_factory):
    open_database(qtbot, window, index_database)
    dialog = fill_dialog(qtbot, window.schema_tab)
    assert wait_operation(qtbot, window.controller, "schema.create", lambda: qtbot.mouseClick(save_button(dialog), Qt.MouseButton.LeftButton))
    open_database(qtbot, window, database_factory("other.sqlite3"))
    assert not window.schema_tab.snapshot.indexes
    assert window.schema_tab.snapshot.tables[0].name == "sample"
    assert window.schema_tab.details_edit.toPlainText() == ""


def test_reopening_same_database_refreshes_external_changes(qtbot, window, index_database):
    open_database(qtbot, window, index_database)
    with closing(sqlite3.connect(index_database)) as connection:
        connection.execute("CREATE INDEX external_index ON items(label)")
    profile = window.controller.active.profile
    assert wait_operation(qtbot, window.controller, "connect", lambda: window.controller.connect_saved(profile.id))
    qtbot.waitUntil(lambda: not window.controller.busy, timeout=5000)
    assert [index.name for index in window.schema_tab.snapshot.indexes] == ["external_index"]


def test_create_disabled_without_connection_or_tables(qtbot, window, tmp_path):
    assert not window.schema_tab.btn_create.isEnabled()
    assert not window.schema_tab.btn_refresh.isEnabled()
    path = tmp_path / "empty.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA user_version=1")
    assert wait_operation(qtbot, window.controller, "connect", lambda: window.controller.create("Empty", str(path)))
    qtbot.waitUntil(lambda: not window.controller.busy, timeout=5000)
    assert window.schema_tab.btn_refresh.isEnabled()
    assert not window.schema_tab.btn_create.isEnabled()


def test_read_only_connection_allows_viewing_only(qtbot, window, index_database):
    open_database(qtbot, window, index_database)

    def reopen_read_only(manager):
        manager.executor.connect({"file_path": str(index_database), "read_only": True})

    assert wait_operation(qtbot, window.controller, "read-only", lambda: window.controller.submit_database_operation("read-only", reopen_read_only))
    qtbot.waitUntil(lambda: not window.controller.busy, timeout=5000)
    assert window.schema_tab.btn_refresh.isEnabled()
    assert not window.schema_tab.btn_create.isEnabled()


def test_worker_disables_actions_until_operation_finishes(qtbot, window, index_database):
    open_database(qtbot, window, index_database)
    started = Event()
    release = Event()

    def hold_worker(manager):
        started.set()
        assert release.wait(3)

    try:
        window.controller.submit_database_operation("probe", hold_worker)
        qtbot.waitUntil(started.is_set, timeout=1000)
        assert not window.schema_tab.btn_create.isEnabled()
        assert not window.schema_tab.btn_refresh.isEnabled()
        assert not window.schema_tab.indexes_list.isEnabled()
        assert wait_operation(qtbot, window.controller, "probe", release.set)
    finally:
        release.set()
    assert window.schema_tab.btn_create.isEnabled()


def test_failed_shutdown_restores_schema_actions(qtbot, window, index_database):
    open_database(qtbot, window, index_database)

    def start_transaction(manager):
        manager.executor.connection.execute("UPDATE items SET label='pending' WHERE id=1")

    assert wait_operation(qtbot, window.controller, "schema.begin", lambda: window.controller.submit_database_operation("schema.begin", start_transaction))
    try:
        assert not wait_operation(qtbot, window.controller, "shutdown", window.controller.request_shutdown)
        assert window.schema_tab.btn_refresh.isEnabled()
        assert window.schema_tab.btn_create.isEnabled()
    finally:
        assert wait_operation(qtbot, window.controller, "schema.rollback", lambda: window.controller.submit_database_operation("schema.rollback", lambda manager: manager.executor.connection.rollback()))
