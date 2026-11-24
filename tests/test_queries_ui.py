"""SQL editor integration, cancellation, and the disk-backed result grid"""

import sqlite3
from concurrent.futures import Future

import pytest
from PyQt6.QtCore import QTimer, Qt

from database_browser.controller import ConnectionController
from database_browser.queries.errors import QueryError
from database_browser.queries.grid import QueryTableModel
from database_browser.queries.result_set import ResultSet
from database_browser.queries.services import QueryManager
from database_browser.queries.ui import SqlEditorTab
from database_browser.ui import MainWindow, NotificationManager


@pytest.fixture
def sql_controller(qtbot, tmp_path, database_factory):
    controller = ConnectionController(tmp_path / 'settings.sqlite3')
    path = database_factory()
    with qtbot.waitSignal(controller.operation_finished, timeout=5000):
        controller.create('Test', str(path))
    yield controller, path
    qtbot.waitUntil(lambda: not controller.busy, timeout=5000)
    if not controller._closed:
        with qtbot.waitSignal(controller.operation_finished, timeout=5000):
            controller.submit_database_operation(
                'query.rollback', lambda manager: QueryManager(manager.executor).finish_transaction(commit=False)
            )
        with qtbot.waitSignal(controller.shutdown_complete, timeout=5000):
            controller.request_shutdown()


@pytest.fixture
def sql_tab(qtbot, sql_controller):
    tab = SqlEditorTab(sql_controller[0])
    qtbot.addWidget(tab)
    tab.show()
    yield tab
    tab.close_cache()


def run(qtbot, tab, sql, *, batch=500):
    tab.batch_size.setValue(batch)
    tab.editor.setPlainText(sql)
    with qtbot.waitSignal(tab.controller.operation_finished, timeout=5000,
                          check_params_cb=lambda name, success: name == 'query.execute'):
        qtbot.mouseClick(tab.btn_run, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not tab.controller.busy, timeout=5000)


def test_instant_future_still_delivers_its_result_after_submit_returns(qtbot, tmp_path, monkeypatch):
    controller = ConnectionController(tmp_path / 'settings.sqlite3')

    def finish_immediately(task):
        future = Future()
        future.set_result(task())
        return future

    monkeypatch.setattr(controller.tasks, 'submit_task', finish_immediately)
    received = []
    controller.operation_result.connect(lambda name, value: received.append(value))
    assert controller.submit_database_operation('query.test', lambda manager: 'ready')
    assert controller.busy and received == []
    qtbot.waitUntil(lambda: not controller.busy, timeout=5000)
    assert received == ['ready']
    with qtbot.waitSignal(controller.shutdown_complete, timeout=5000):
        controller.request_shutdown()


def test_uc13_editor_requires_connection_and_sql(qtbot, tmp_path):
    controller = ConnectionController(tmp_path / 'settings.sqlite3')
    tab = SqlEditorTab(controller)
    qtbot.addWidget(tab)
    tab.editor.setPlainText('SELECT 1')
    assert not tab.btn_run.isEnabled()
    tab.close_cache()
    with qtbot.waitSignal(controller.shutdown_complete, timeout=5000):
        controller.request_shutdown()


def test_uc13_select_displays_data_headers_and_statistics(qtbot, sql_tab):
    run(qtbot, sql_tab, 'SELECT id, value FROM sample')
    assert sql_tab.model.columns == ('id', 'value')
    assert sql_tab.model.row_values(0) == (1, 'original')
    assert 'Loaded: 1' in sql_tab.statistics_label.text()
    assert 'Total rows: 1' in sql_tab.count_label.text()
    assert not sql_tab.btn_next.isEnabled()


def test_uc13_empty_result_and_sql_error_remain_usable(qtbot, sql_tab):
    run(qtbot, sql_tab, 'SELECT id FROM sample WHERE 0')
    assert sql_tab.model.rowCount() == 0
    assert sql_tab.model.columnCount() == 1
    assert 'Empty result' in sql_tab.message_label.text()
    run(qtbot, sql_tab, 'SELECT * FROM missing')
    assert 'no such table' in sql_tab.message_label.text()
    assert sql_tab.editor.toPlainText() == 'SELECT * FROM missing'
    assert sql_tab.btn_run.isEnabled()
    run(qtbot, sql_tab, 'SELECT 42')
    assert sql_tab.model.row_values(0) == (42,)


def test_uc14_next_batches_and_separate_count_keep_previous_rows(qtbot, sql_tab):
    run(qtbot, sql_tab,
        'WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<12) SELECT x FROM n', batch=5)
    assert sql_tab.model.rowCount() == 5
    assert 'Total rows: 12' in sql_tab.count_label.text()
    assert sql_tab.btn_next.isEnabled()
    for count in (10, 12):
        qtbot.mouseClick(sql_tab.btn_next, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(lambda: not sql_tab.controller.busy, timeout=5000)
        assert sql_tab.model.rowCount() == count
    assert [sql_tab.model.row_values(i)[0] for i in range(12)] == list(range(1, 13))
    assert not sql_tab.btn_next.isEnabled()


def test_uc14_stop_keeps_already_loaded_grid(qtbot, sql_tab):
    run(qtbot, sql_tab, 'SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3', batch=1)
    qtbot.mouseClick(sql_tab.btn_stop, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not sql_tab.controller.busy, timeout=5000)
    assert sql_tab.model.row_values(0) == (1,)
    assert sql_tab.model.rowCount() == 1
    assert not sql_tab.btn_next.isEnabled()
    assert 'stopped' in sql_tab.message_label.text().lower()


def test_uc14_failed_count_does_not_hide_rows_or_disable_loading(qtbot, sql_tab, monkeypatch):
    original = ResultSet.get_total_count

    def missing_count_table(result, cancellation=None):
        saved = result.sql
        result.sql = 'SELECT * FROM missing_count_table'
        try:
            return original(result, cancellation)
        finally:
            result.sql = saved

    monkeypatch.setattr(ResultSet, 'get_total_count', missing_count_table)
    run(qtbot, sql_tab, 'SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3', batch=1)
    assert 'unavailable' in sql_tab.count_label.text()
    assert sql_tab.model.row_values(0) == (1,)
    assert sql_tab.btn_next.isEnabled()
    qtbot.mouseClick(sql_tab.btn_next, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not sql_tab.controller.busy, timeout=5000)
    assert sql_tab.model.row_values(1) == (2,)


def test_uc14_fetch_error_retains_rows_and_marks_result_incomplete(qtbot, sql_tab, monkeypatch):
    run(qtbot, sql_tab, 'SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3', batch=1)

    def fail(manager, result_id, cancellation=None):
        manager.executor.get_result(result_id).close()
        raise QueryError('disk read failed')

    monkeypatch.setattr(QueryManager, 'fetch_next_chunk', fail)
    qtbot.mouseClick(sql_tab.btn_next, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not sql_tab.controller.busy, timeout=5000)
    assert sql_tab.model.row_values(0) == (1,)
    assert sql_tab.model.rowCount() == 1
    assert 'incomplete' in sql_tab.message_label.text()
    assert not sql_tab.btn_next.isEnabled()
    assert sql_tab.btn_run.isEnabled()


def test_uc13_commit_and_rollback_buttons_control_persistence(qtbot, sql_tab, sql_controller):
    path = sql_controller[1]
    run(qtbot, sql_tab, "UPDATE sample SET value='edited'")
    assert sql_tab.btn_commit.isEnabled() and sql_tab.btn_rollback.isEnabled()
    assert 'Affected: 1' in sql_tab.statistics_label.text()
    with sqlite3.connect(path) as con:
        assert con.execute('SELECT value FROM sample').fetchone() == ('original',)
    qtbot.mouseClick(sql_tab.btn_rollback, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not sql_tab.controller.busy, timeout=5000)
    run(qtbot, sql_tab, "UPDATE sample SET value='saved'")
    qtbot.mouseClick(sql_tab.btn_commit, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not sql_tab.controller.busy, timeout=5000)
    with sqlite3.connect(path) as con:
        assert con.execute('SELECT value FROM sample').fetchone() == ('saved',)
    assert not sql_tab.btn_commit.isEnabled()


def test_uc13_editor_is_responsive_and_heavy_query_can_be_stopped(qtbot, sql_tab):
    sql_tab.editor.setPlainText(
        'WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000000) '
        'SELECT sum(x) FROM n'
    )
    qtbot.mouseClick(sql_tab.btn_run, Qt.MouseButton.LeftButton)
    heartbeat = []
    QTimer.singleShot(0, lambda: heartbeat.append(True))
    try:
        qtbot.waitUntil(lambda: bool(heartbeat), timeout=1000)
        assert sql_tab.controller.busy
        assert not sql_tab.btn_run.isEnabled()
        assert sql_tab.btn_stop.isEnabled()
    finally:
        sql_tab.cancel_pending()
    qtbot.waitUntil(lambda: not sql_tab.controller.busy, timeout=5000)
    assert 'stopped' in sql_tab.message_label.text().lower()
    run(qtbot, sql_tab, 'SELECT 7')
    assert sql_tab.model.row_values(0) == (7,)


def test_uc14_count_can_be_stopped_without_losing_first_batch(qtbot, sql_tab):
    sql_tab.batch_size.setValue(5)
    sql_tab.editor.setPlainText(
        'WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000000) SELECT x FROM n'
    )
    qtbot.mouseClick(sql_tab.btn_run, Qt.MouseButton.LeftButton)
    try:
        qtbot.waitUntil(lambda: sql_tab._pending_operation == 'query.count', timeout=5000)
        assert sql_tab.model.rowCount() == 5
        qtbot.mouseClick(sql_tab.btn_stop, Qt.MouseButton.LeftButton)
    finally:
        sql_tab.cancel_pending()
    qtbot.waitUntil(lambda: not sql_tab.controller.busy, timeout=5000)
    assert sql_tab.model.rowCount() == 5
    assert sql_tab.model.row_values(0) == (1,)
    assert not sql_tab.btn_next.isEnabled()


def test_uc14_reconnect_to_same_file_invalidates_previous_result(qtbot, sql_tab, sql_controller):
    controller, path = sql_controller
    run(qtbot, sql_tab, 'SELECT 1 UNION ALL SELECT 2', batch=1)
    assert sql_tab.model.rowCount() == 1
    controller.connect_saved(controller.active.profile.id)
    qtbot.waitUntil(lambda: not controller.busy, timeout=5000)
    assert sql_tab.result is None
    assert sql_tab.model.rowCount() == 0


def test_sql_editor_is_enabled_and_ddl_refreshes_both_schema_sections(qtbot, sql_controller, monkeypatch):
    controller, path = sql_controller
    monkeypatch.setattr(NotificationManager, 'show_error', lambda *args: None)
    window = MainWindow(controller)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: not controller.busy, timeout=5000)
    assert window.tab_widget.isTabEnabled(2)
    assert not window.tab_widget.isTabEnabled(3)
    run(qtbot, window.sql_tab, 'CREATE TABLE new_table(x INTEGER)')
    assert 'new_table' in {table.name for table in window.schema_tab.tables_tab.snapshot.tables}
    assert 'new_table' in {table.name for table in window.schema_tab.snapshot.tables}
    with qtbot.waitSignal(controller.shutdown_complete, timeout=5000):
        window.close()


def test_sql_errors_are_only_shown_in_sql_editor(qtbot, sql_controller, monkeypatch):
    controller, path = sql_controller
    messages = []
    monkeypatch.setattr(NotificationManager, 'show_error', lambda *args: messages.append(args))
    window = MainWindow(controller)
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: not controller.busy, timeout=5000)
    run(qtbot, window.sql_tab, 'SELECT * FROM missing')
    assert 'no such table' in window.sql_tab.message_label.text()
    assert messages == []
    with qtbot.waitSignal(controller.shutdown_complete, timeout=5000):
        window.close()


def test_result_grid_uses_bounded_memory_cache_and_keeps_old_rows(qtbot):
    model = QueryTableModel(cache_batches=2)
    model.reset_result(('id', 'value'))
    try:
        for start in range(0, 10000, 100):
            model.append_rows([(i, f'row {i}') for i in range(start, start + 100)])
        assert model.rowCount() == 10000
        assert len(model._cache) <= 2
        assert sum(len(batch) for batch in model._cache.values()) <= 200
        for i in (0, 9999, 101, 5000):
            assert model.row_values(i) == (i, f'row {i}')
            assert len(model._cache) <= 2
        model.reset_result(('new',))
        assert model.rowCount() == 0
        assert not model._cache
    finally:
        model.close_cache()


def test_result_grid_displays_null_blob_and_truncates_long_cells(qtbot):
    model = QueryTableModel()
    model.reset_result(('null', 'blob', 'text'))
    try:
        model.append_rows([(None, b'\x00\xff', 'x' * 10000)])
        assert model.data(model.index(0, 0)) == 'NULL'
        assert model.data(model.index(0, 1)) == 'BLOB (2 bytes): 00ff'
        assert len(model.data(model.index(0, 2))) == 257
        assert model.row_values(0)[2] == 'x' * 10000
    finally:
        model.close_cache()


def test_cache_write_failure_keeps_previous_rows(qtbot, monkeypatch):
    model = QueryTableModel()
    model.reset_result(('id',))
    model.append_rows([(1,)])
    original_file = model._file

    class FullDisk:
        def __getattr__(self, name):
            return getattr(original_file, name)

        def write(self, value):
            raise OSError('No space left on device')

    monkeypatch.setattr(model, '_file', FullDisk())
    try:
        with pytest.raises(OSError, match='No space'):
            model.append_rows([(2,)])
        assert model.rowCount() == 1
        assert model.row_values(0) == (1,)
    finally:
        model.close_cache()


def test_main_window_close_stops_heavy_query_and_closes_worker(qtbot, sql_controller):
    controller, path = sql_controller
    window = MainWindow(controller)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: not controller.busy, timeout=5000)
    window.sql_tab.editor.setPlainText(
        'WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000000) '
        'SELECT sum(x) FROM n'
    )
    qtbot.mouseClick(window.sql_tab.btn_run, Qt.MouseButton.LeftButton)
    with qtbot.waitSignal(controller.shutdown_complete, timeout=5000):
        window.close()
    assert controller._closed
    assert window.sql_tab.model._file.closed


def test_schema_edit_closes_result_but_keeps_loaded_grid(qtbot, sql_tab):
    run(qtbot, sql_tab, 'SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3', batch=1)
    controller = sql_tab.controller
    controller.submit_database_operation(
        'schema.table.create', lambda manager: manager.executor.connection.execute('CREATE TABLE extra(a)').close()
    )
    qtbot.waitUntil(lambda: not controller.busy, timeout=5000)
    assert sql_tab.model.row_values(0) == (1,)
    assert not sql_tab.btn_next.isEnabled()
    assert 'schema change' in sql_tab.message_label.text()
