"""The worker keeps the cursor; the interface receives only row batches"""

import sqlite3
from threading import Event
from time import perf_counter
from uuid import uuid4

from .errors import QueryCancelled, QueryError
from .models import QueryResult, QueryStatistics
from .sql import cancellable, first_keyword, schema_version, statement_body

_READ_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
_DML_ACTIONS = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}
_VOLATILE = {'random', 'randomblob', 'changes', 'total_changes', 'last_insert_rowid', 'load_extension'}


class ResultSet:
    """Read one executed statement in fixed portions without rerunning it"""

    def __init__(self, connection, sql: str, chunk_size: int, cancellation: Event | None = None):
        self.connection = connection
        self.sql = statement_body(sql)
        self.chunk_size = chunk_size
        self.result_id = uuid4().hex
        self.columns = ()
        self.loaded_rows = 0
        self.has_more = False
        self.incomplete = False
        self.affected_rows = None
        self.total_count = None
        self.count_error = None
        self.count_seconds = self.fetch_seconds = 0.0
        self.schema_changed = False
        self.can_count = False
        self._cursor = None
        self._dml = False
        keyword = first_keyword(self.sql)
        actions = []
        volatile = False

        def authorize(action, first, second, database, source):
            nonlocal volatile
            actions.append(action)
            if action == sqlite3.SQLITE_FUNCTION and (second or '').lower() in _VOLATILE:
                volatile = True
            if (keyword != 'EXPLAIN' and action in _DML_ACTIONS
                    and first not in {'sqlite_master', 'sqlite_temp_master'} and source is None):
                self._dml = True
            return sqlite3.SQLITE_OK

        started = perf_counter()
        cursor = connection.cursor()
        try:
            with cancellable(connection, cancellation):
                before = schema_version(connection)
                connection.set_authorizer(authorize)
                try:
                    if keyword == 'WITH':
                        # EXPLAIN compiles a CTE without changing data
                        probe = connection.execute('EXPLAIN ' + self.sql)
                        probe.close()
                        if self._dml and not connection.in_transaction:
                            # sqlite3 does not start an implicit transaction for WITH ... UPDATE
                            connection.execute('BEGIN')
                    # execute rejects scripts, so a second statement is never run accidentally
                    cursor.execute(self.sql)
                finally:
                    connection.set_authorizer(None)
                self.schema_changed = schema_version(connection) != before
            self.execution_seconds = perf_counter() - started
            if cursor.description is not None:
                self.columns = tuple(item[0] for item in cursor.description)
                self._cursor = cursor
                self.has_more = True
                self.can_count = (keyword != 'EXPLAIN' and bool(actions)
                                  and set(actions) <= _READ_ACTIONS and not volatile)
            else:
                self._update_affected(cursor)
                cursor.close()
        except (sqlite3.Error, QueryError) as error:
            cursor.close()
            if cancellation is not None and cancellation.is_set():
                raise QueryCancelled('Operation stopped') from error
            if isinstance(error, QueryError):
                raise
            raise QueryError(f'SQLite: {error}') from error

    def _update_affected(self, cursor):
        if cursor.rowcount >= 0:
            self.affected_rows = cursor.rowcount
        elif self._dml:
            # Python reports -1 for some WITH ... UPDATE statements
            self.affected_rows = self.connection.execute('SELECT changes()').fetchone()[0]

    def snapshot(self, rows: tuple[tuple, ...] = ()) -> QueryResult:
        stats = QueryStatistics(
            self.execution_seconds, self.fetch_seconds, self.affected_rows, self.loaded_rows,
            self.total_count, self.total_count is not None, self.count_seconds, self.count_error,
            self.connection.in_transaction, self.schema_changed,
        )
        return QueryResult(self.result_id, self.columns, rows, self.has_more, self.can_count, self.incomplete, stats)

    def fetch_next_chunk(self, cancellation: Event | None = None) -> QueryResult:
        if not self.has_more or self._cursor is None:
            return self.snapshot()
        started = perf_counter()
        try:
            with cancellable(self.connection, cancellation):
                rows = tuple(self._cursor.fetchmany(self.chunk_size))
            self.loaded_rows += len(rows)
            # An exact multiple is finished by the next empty fetch, without a lookahead row
            if len(rows) < self.chunk_size:
                self.has_more = False
                self.total_count = self.loaded_rows
                self.count_error = None
                self._update_affected(self._cursor)
                self._cursor.close()
                self._cursor = None
            self.fetch_seconds += perf_counter() - started
            return self.snapshot(rows)
        except (sqlite3.Error, QueryError) as error:
            self.fetch_seconds += perf_counter() - started
            self.close()
            if cancellation is not None and cancellation.is_set():
                raise QueryCancelled('Loading stopped; previously loaded rows are still available') from error
            raise QueryError(f'Cannot load the next batch: {error}') from error

    def get_total_count(self, cancellation: Event | None = None) -> QueryResult:
        if self.total_count is not None:
            return self.snapshot()
        if not self.can_count:
            self.count_error = 'Total will be known when all returned rows have been loaded'
            return self.snapshot()
        started = perf_counter()
        cursor = self.connection.cursor()

        def read_only(action, first, second, database, source):
            return sqlite3.SQLITE_OK if action in _READ_ACTIONS else sqlite3.SQLITE_DENY

        try:
            with cancellable(self.connection, cancellation):
                self.connection.set_authorizer(read_only)
                try:
                    cursor.execute(f'SELECT COUNT(*) FROM (\n{self.sql}\n) AS query_result')
                    self.total_count = cursor.fetchone()[0]
                finally:
                    self.connection.set_authorizer(None)
            self.count_error = None
        except (sqlite3.Error, QueryError) as error:
            # A failed count must not close the original result cursor
            self.count_error = 'Counting stopped' if cancellation is not None and cancellation.is_set() else str(error)
        finally:
            cursor.close()
            self.count_seconds = perf_counter() - started
        return self.snapshot()

    def close(self):
        if self._cursor is not None:
            self._cursor.close()
            self._cursor = None
        if self.has_more:
            self.incomplete = True
        self.has_more = False
