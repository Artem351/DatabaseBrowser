"""UC-13 and UC-14 against real SQLite files and cursors"""

import sqlite3
from threading import Event
from concurrent.futures import ThreadPoolExecutor

import pytest

from database_browser.errors import DatabaseConnectionError
from database_browser.queries.errors import QueryCancelled, QueryError
from database_browser.queries.services import QueryManager
from database_browser.query_executor import QueryExecutor


@pytest.fixture
def query_db(tmp_path):
    path = tmp_path / 'queries.sqlite3'
    with sqlite3.connect(path) as con:
        con.execute('CREATE TABLE items(id INTEGER PRIMARY KEY, value TEXT)')
        con.executemany('INSERT INTO items VALUES(?, ?)', [(i, f'row {i}') for i in range(1, 24)])
    executor = QueryExecutor()
    executor.connect({'file_path': str(path)})
    yield path, executor, QueryManager(executor)
    executor.connection.rollback()
    executor.disconnect()


@pytest.mark.parametrize('size', [0, 1, 4, 5, 6, 10, 23])
def test_uc14_batches_have_no_missing_or_duplicate_rows(query_db, size):
    _, executor, manager = query_db
    result = manager.execute_query(f'SELECT id, value FROM items WHERE id <= {size} ORDER BY id', chunk_size=5)
    rows = list(result.rows)
    assert len(result.rows) <= 5
    while result.has_more:
        result = manager.fetch_next_chunk(result.result_id)
        assert len(result.rows) <= 5
        rows.extend(result.rows)
    assert rows == [(i, f'row {i}') for i in range(1, size + 1)]
    assert result.statistics.total_count == size
    assert result.statistics.is_count_calculated
    assert executor.get_result(result.result_id)._cursor is None


def test_uc13_columns_null_blob_duplicate_names_and_empty_result(query_db):
    _, _, manager = query_db
    result = manager.execute_query("SELECT NULL AS x, x'00ff' AS x, 1.5 AS amount WHERE 0")
    assert result.columns == ('x', 'x', 'amount')
    assert result.rows == ()
    assert result.statistics.total_count == 0
    result = manager.execute_query("SELECT NULL, x'00ff', 1.5")
    assert result.rows == ((None, b'\x00\xff', 1.5),)


@pytest.mark.parametrize('sql, message', [
    ('SELEC id FROM items', 'syntax'), ('SELECT * FROM missing', 'no such table'),
    ('SELECT 1; DELETE FROM items', 'one statement'), ('-- comment only', 'Enter one'),
])
def test_uc13_errors_do_not_execute_a_second_statement_and_connection_still_works(query_db, sql, message):
    _, executor, manager = query_db
    with pytest.raises(QueryError, match=message):
        manager.execute_query(sql)
    assert executor.connection.execute('SELECT COUNT(*) FROM items').fetchone() == (23,)
    assert manager.execute_query('SELECT 42').rows == ((42,),)


def test_uc13_writes_have_metrics_and_require_explicit_commit(query_db):
    path, executor, manager = query_db
    result = manager.execute_query("UPDATE items SET value='edited' WHERE id <= 2")
    assert result.columns == () and not result.has_more
    assert result.statistics.affected_rows == 2
    assert result.statistics.in_transaction
    with sqlite3.connect(path) as other:
        assert other.execute("SELECT COUNT(*) FROM items WHERE value='edited'").fetchone() == (0,)
    manager.finish_transaction(commit=True)
    with sqlite3.connect(path) as other:
        assert other.execute("SELECT COUNT(*) FROM items WHERE value='edited'").fetchone() == (2,)
    assert not executor.connection.in_transaction


def test_uc13_cte_write_also_requires_commit_and_reports_affected_rows(query_db):
    path, executor, manager = query_db
    result = manager.execute_query("WITH ids AS (SELECT 1 AS id) UPDATE items SET value='cte' WHERE id IN ids")
    assert result.statistics.affected_rows == 1
    assert result.statistics.in_transaction
    manager.finish_transaction(commit=False)
    assert executor.connection.execute('SELECT value FROM items WHERE id=1').fetchone() == ('row 1',)


@pytest.mark.parametrize('sql', [
    "; UPDATE items SET value='pending' WHERE id=1;",
    "; -- comment\n WITH one AS (SELECT 1 AS id) UPDATE items SET value='pending' WHERE id IN one;",
    "/* comment */ UPDATE items SET value='pending' WHERE id=1; -- end",
])
def test_uc13_comments_and_empty_separators_do_not_bypass_transactions(query_db, sql):
    _, executor, manager = query_db
    result = manager.execute_query(sql)
    assert result.statistics.in_transaction
    manager.finish_transaction(commit=False)
    assert executor.connection.execute('SELECT value FROM items WHERE id=1').fetchone() == ('row 1',)


def test_uc13_rollback_preserves_file_and_schema_change_is_reported(query_db):
    _, executor, manager = query_db
    manager.execute_query('BEGIN')
    result = manager.execute_query('CREATE TABLE extra(a INTEGER)')
    assert result.statistics.schema_changed and result.statistics.in_transaction
    assert manager.finish_transaction(commit=False).schema_changed
    assert executor.connection.execute("SELECT name FROM sqlite_schema WHERE name='extra'").fetchone() is None
    assert manager.execute_query('CREATE TABLE extra(a INTEGER)').statistics.schema_changed
    assert manager.execute_query('DROP TABLE extra').statistics.schema_changed


@pytest.mark.parametrize('sql, expected', [
    ('SELECT id FROM items ORDER BY id LIMIT 7 OFFSET 3;', 7),
    ('SELECT DISTINCT id % 3 FROM items; -- trailing comment', 3),
    ('WITH grouped AS (SELECT id % 4 AS g FROM items) SELECT g FROM grouped GROUP BY g;', 4),
    ("SELECT '-- ; not a comment' AS text, id FROM items /* actual comment */; /* end */", 23),
    ('VALUES(1), (2), (3);', 3),
])
def test_uc14_separate_count_respects_actual_query_and_does_not_consume_cursor(query_db, sql, expected):
    _, executor, manager = query_db
    result = manager.execute_query(sql, chunk_size=2)
    before = result.rows
    counted = manager.get_total_count(result.result_id)
    assert counted.statistics.total_count == expected
    assert counted.statistics.is_count_calculated
    assert counted.rows == ()
    assert counted.statistics.loaded_rows == len(before)
    next_batch = manager.fetch_next_chunk(result.result_id)
    assert next_batch.rows


def test_uc14_fetching_does_not_rerun_original_sql(query_db):
    _, executor, manager = query_db
    statements = []
    executor.connection.set_trace_callback(statements.append)
    sql = 'SELECT id FROM items ORDER BY id'
    result = manager.execute_query(sql, chunk_size=3)
    while result.has_more:
        result = manager.fetch_next_chunk(result.result_id)
    assert statements.count(sql) == 1


def test_uc14_returning_is_never_reexecuted_for_counting(query_db):
    _, executor, manager = query_db
    result = manager.execute_query('UPDATE items SET value=value || \'!\' WHERE id<=5 RETURNING id, value', chunk_size=2)
    assert not result.can_count
    counted = manager.get_total_count(result.result_id)
    assert not counted.statistics.is_count_calculated
    rows = list(result.rows)
    while result.has_more:
        result = manager.fetch_next_chunk(result.result_id)
        rows.extend(result.rows)
    assert result.statistics.affected_rows == 5
    assert len(rows) == 5
    assert executor.connection.execute('SELECT value FROM items WHERE id=1').fetchone() == ('row 1!',)
    manager.finish_transaction(commit=False)


def test_uc14_count_failure_keeps_original_cursor_and_can_finish_loading(query_db):
    _, executor, manager = query_db
    result = manager.execute_query('SELECT * FROM items', chunk_size=4)
    cancellation = Event()
    cancellation.set()
    counted = manager.get_total_count(result.result_id, cancellation)
    assert counted.statistics.count_error
    assert counted.has_more
    loaded = len(result.rows)
    while result.has_more:
        result = manager.fetch_next_chunk(result.result_id)
        loaded += len(result.rows)
    assert loaded == 23
    assert result.statistics.total_count == 23
    assert result.statistics.count_error is None


def test_uc14_late_fetch_error_keeps_loaded_count_and_releases_cursor(query_db):
    _, executor, manager = query_db

    def risky(value):
        if value >= 7:
            raise ValueError('late failure')
        return value

    executor.connection.create_function('risky', 1, risky)
    result = manager.execute_query('SELECT risky(id) FROM items ORDER BY id', chunk_size=3)
    assert result.rows == ((1,), (2,), (3,))
    with pytest.raises(QueryError, match='next batch'):
        manager.fetch_next_chunk(result.result_id)
    snapshot = executor.get_result(result.result_id).snapshot()
    assert snapshot.statistics.loaded_rows == 3
    assert snapshot.incomplete and not snapshot.has_more
    assert executor.get_result(result.result_id)._cursor is None
    assert manager.execute_query('SELECT 1').rows == ((1,),)


def test_uc14_stop_keeps_loaded_rows_and_releases_cursor(query_db):
    _, executor, manager = query_db
    result = manager.execute_query('SELECT id FROM items', chunk_size=2)
    stopped = manager.stop_viewing(result.result_id)
    assert stopped.statistics.loaded_rows == 2
    assert stopped.incomplete and not stopped.has_more
    assert executor.get_result(result.result_id)._cursor is None


def test_uc14_old_result_cannot_be_loaded_after_new_query_or_reconnect(query_db):
    path, executor, manager = query_db
    old = manager.execute_query('SELECT * FROM items', chunk_size=2)
    new = manager.execute_query('SELECT * FROM items', chunk_size=2)
    with pytest.raises(QueryError, match='no longer active'):
        manager.fetch_next_chunk(old.result_id)
    executor.connect({'file_path': str(path)})
    with pytest.raises(QueryError, match='no longer active'):
        manager.fetch_next_chunk(new.result_id)


def test_uc13_read_only_allows_select_but_rejects_writes(query_db):
    path, executor, manager = query_db
    executor.connect({'file_path': str(path), 'read_only': True})
    assert manager.execute_query('SELECT COUNT(*) FROM items').rows == ((23,),)
    with pytest.raises(QueryError, match='readonly'):
        manager.execute_query("DELETE FROM items")


@pytest.mark.parametrize('size', [0, -1, True, 5001, 1.5])
def test_uc14_invalid_batch_sizes_are_rejected(query_db, size):
    with pytest.raises(QueryError, match='Batch size'):
        query_db[2].execute_query('SELECT 1', chunk_size=size)


def test_uc13_cancellation_stops_heavy_sql_and_connection_can_be_reused(query_db):
    _, executor, manager = query_db
    event = Event()
    steps = 0

    def tick(value):
        nonlocal steps
        steps += 1
        if steps == 50:
            event.set()
        return value

    executor.connection.create_function('tick', 1, tick)
    with pytest.raises(QueryCancelled):
        manager.execute_query(
            'WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000) '
            'SELECT sum(tick(x)) FROM n', cancellation=event,
        )
    assert steps < 1000000
    assert manager.execute_query('SELECT 42').rows == ((42,),)


def test_uc13_worker_ownership_is_enforced(query_db):
    _, _, manager = query_db
    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(DatabaseConnectionError, match='owning worker'):
            pool.submit(manager.execute_query, 'SELECT 1').result()


def test_uc13_explain_of_a_write_does_not_report_previous_affected_rows(query_db):
    _, _, manager = query_db
    manager.execute_query("UPDATE items SET value='changed' WHERE id<4")
    manager.finish_transaction(commit=True)
    result = manager.execute_query('EXPLAIN DELETE FROM items')
    assert result.statistics.affected_rows is None


def test_uc14_volatile_filter_is_not_repeated_for_a_count(query_db):
    _, _, manager = query_db
    result = manager.execute_query('SELECT id FROM items WHERE random() != 0', chunk_size=2)
    assert not result.can_count
    assert not manager.get_total_count(result.result_id).statistics.is_count_calculated


def test_uc13_stopping_returning_keeps_changes_pending_until_rollback(query_db):
    _, executor, manager = query_db
    result = manager.execute_query("UPDATE items SET value='pending' RETURNING id", chunk_size=2)
    manager.stop_viewing(result.result_id)
    assert executor.connection.in_transaction
    assert executor.connection.execute("SELECT COUNT(*) FROM items WHERE value='pending'").fetchone() == (23,)
    manager.finish_transaction(commit=False)
    assert executor.connection.execute("SELECT COUNT(*) FROM items WHERE value='pending'").fetchone() == (0,)


def test_uc13_locked_database_error_keeps_connection_usable(query_db):
    path, executor, manager = query_db
    executor.connection.execute('PRAGMA busy_timeout=1')
    other = sqlite3.connect(path)
    try:
        other.execute('BEGIN EXCLUSIVE')
        with pytest.raises(QueryError, match='locked'):
            manager.execute_query("UPDATE items SET value='blocked'")
    finally:
        other.rollback()
        other.close()
    manager.finish_transaction(commit=False)
    assert manager.execute_query('SELECT COUNT(*) FROM items').rows == ((23,),)
