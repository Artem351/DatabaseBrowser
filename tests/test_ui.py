"""Tests for forms, buttons, and Qt interaction with the worker thread."""

from threading import Event, get_ident

import pytest
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QMessageBox

from database_browser.controller import ConnectionController
from database_browser.ui import ConnectionDialog, DatabaseTab, NotificationManager


@pytest.fixture
def controller(qtbot, tmp_path):
    """Create the controller and wait for the initial list to load."""
    instance = ConnectionController(tmp_path / "settings.sqlite3")
    with qtbot.waitSignal(instance.operation_finished, timeout=5000):
        instance.refresh()
    yield instance
    if not instance._closed:
        with qtbot.waitSignal(instance.shutdown_complete, timeout=5000):
            instance.request_shutdown()


@pytest.fixture
def tab(qtbot, controller, monkeypatch):
    """Show the tab and collect errors instead of opening modal dialogs."""
    errors = []
    # A real error dialog would wait for OK and block the test.
    monkeypatch.setattr(NotificationManager, "show_error", lambda parent, message: errors.append(message))
    widget = DatabaseTab(controller)
    widget.errors = errors
    qtbot.addWidget(widget)
    widget.show()
    return widget


def connect(qtbot, controller, path):
    """Open a test database and wait for the background operation."""
    # Wait for a signal instead of a fixed delay, since machine speed can vary.
    with qtbot.waitSignal(controller.operation_finished, timeout=5000) as outcome:
        controller.create("Demo", str(path))
    assert outcome.args == ["connect", True]


def test_uc01_new_connection_form_updates_interface(qtbot, tab, controller, database_factory, monkeypatch):
    """Confirming the form adds the profile and active path to the UI."""
    path = database_factory()

    def accept(dialog):
        """Fill the form automatically during the test."""
        dialog.name_edit.setText("My database")
        dialog.path_edit.setText(str(path))
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ConnectionDialog, "exec", accept)
    with qtbot.waitSignal(controller.operation_finished, timeout=5000):
        qtbot.mouseClick(tab.btn_new, Qt.MouseButton.LeftButton)
    assert tab.connections_list.count() == 1
    assert tab.connections_list.item(0).text() == "My database"
    assert str(path) in tab.active_label.text()
    assert not tab.errors


def test_uc01_cancel_new_connection_has_no_effect(qtbot, tab, controller, monkeypatch):
    """Cancelling creation adds no profile and starts no connection."""
    monkeypatch.setattr(ConnectionDialog, "exec", lambda dialog: QDialog.DialogCode.Rejected)
    qtbot.mouseClick(tab.btn_new, Qt.MouseButton.LeftButton)
    assert controller.profiles == []
    assert controller.active is None
    assert not controller.busy


def test_uc01_error_is_shown_without_losing_active_connection(qtbot, tab, controller, database_factory, tmp_path):
    """An opening error is visible and the previous database stays active."""
    path = database_factory()
    connect(qtbot, controller, path)
    with qtbot.waitSignal(controller.operation_finished, timeout=5000) as outcome:
        controller.create("Broken", str(tmp_path / "missing.sqlite3"))
    assert outcome.args == ["connect", False]
    assert tab.errors
    assert tab.connections_list.count() == 1
    assert str(path) in tab.active_label.text()


def test_uc16_edit_form_saves_new_profile_and_explains_reconnect(qtbot, tab, controller, database_factory, monkeypatch):
    """The form loads old settings, saves new ones, and reminds the user to reconnect."""
    first = database_factory()
    connect(qtbot, controller, first)
    second = database_factory("second.sqlite3")

    def accept(dialog):
        """Check the initial form values and enter new ones."""
        assert dialog.path_edit.text() == str(first)
        dialog.path_edit.setText(str(second))
        dialog.name_edit.setText("Changed")
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ConnectionDialog, "exec", accept)
    with qtbot.waitSignal(controller.operation_finished, timeout=5000):
        qtbot.mouseClick(tab.btn_edit_connection, Qt.MouseButton.LeftButton)
    assert controller.profiles[0].file_path == str(second)
    assert tab.connections_list.item(0).text() == "Changed"
    assert str(first) in tab.active_label.text()
    assert "Reconnect" in tab.active_label.text()


def test_uc16_cancel_edit_keeps_original(qtbot, tab, controller, database_factory, monkeypatch):
    """Cancelling an edit leaves the profile unchanged."""
    connect(qtbot, controller, database_factory())
    before = controller.profiles.copy()
    monkeypatch.setattr(ConnectionDialog, "exec", lambda dialog: QDialog.DialogCode.Rejected)
    qtbot.mouseClick(tab.btn_edit_connection, Qt.MouseButton.LeftButton)
    assert controller.profiles == before
    assert not controller.busy


def test_uc17_cancel_delete_keeps_profile_and_connection(qtbot, tab, controller, database_factory, monkeypatch):
    """Answering No keeps the profile and the open connection."""
    path = database_factory()
    connect(qtbot, controller, path)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    qtbot.mouseClick(tab.btn_delete_connection, Qt.MouseButton.LeftButton)
    assert len(controller.profiles) == 1
    assert controller.active is not None
    assert path.exists()


def test_uc17_confirm_delete_updates_ui_without_deleting_file(qtbot, tab, controller, database_factory, monkeypatch):
    """Confirming deletion removes the entry from the UI but keeps the file."""
    path = database_factory()
    connect(qtbot, controller, path)

    def confirm(*args):
        """Check the warning about keeping the file and confirm deletion."""
        assert "NOT be deleted" in args[2]
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", confirm)
    with qtbot.waitSignal(controller.operation_finished, timeout=5000):
        qtbot.mouseClick(tab.btn_delete_connection, Qt.MouseButton.LeftButton)
    assert tab.connections_list.count() == 0
    assert controller.active is None
    assert tab.active_label.text() == "Not connected"
    assert path.exists()


def test_connection_dialog_does_not_accept_empty_path(qtbot):
    """An empty path prevents confirming the form."""
    dialog = ConnectionDialog()
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.mouseClick(dialog.buttons.button(QDialogButtonBox.StandardButton.Save), Qt.MouseButton.LeftButton)
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.validation_label.text() == "Choose a database file."


def test_database_work_runs_off_ui_thread_and_keeps_ui_responsive(qtbot, tab, controller):
    """A background task blocks the buttons but not Qt event processing."""
    started = Event()
    release = Event()
    results = []
    controller.operation_result.connect(lambda name, result: results.append((name, result)))

    def background_probe(manager):
        """Wait for the test to release the task and return the worker thread ID."""
        started.set()
        assert release.wait(3)
        return get_ident()

    main_thread = get_ident()
    try:
        assert controller.submit_database_operation("probe", background_probe)
        qtbot.waitUntil(started.is_set, timeout=2000)
        assert not tab.btn_new.isEnabled()
        heartbeat = []
        # The timer must fire while the background task is still waiting on the Event.
        QTimer.singleShot(0, lambda: heartbeat.append(True))
        qtbot.waitUntil(lambda: bool(heartbeat), timeout=1000)
        assert controller.busy
        with qtbot.waitSignal(controller.operation_finished, timeout=5000):
            release.set()
        assert results[-1][0] == "probe"
        assert results[-1][1] != main_thread
        assert tab.btn_new.isEnabled()
    finally:
        # Release the worker even if an assertion fails.
        release.set()


def test_shutdown_still_works_after_profile_storage_initialization_failure(qtbot, tmp_path):
    """The application can exit even if profile storage is corrupt."""
    path = tmp_path / "broken-settings.sqlite3"
    path.write_text("not SQLite")
    controller = ConnectionController(path)
    try:
        with qtbot.waitSignal(controller.error_occurred, timeout=5000):
            controller.refresh()
        with qtbot.waitSignal(controller.shutdown_complete, timeout=1000):
            controller.request_shutdown()
    finally:
        if not controller._closed:
            controller.tasks.shutdown()
