"""Coordinate SQL execution, paging, counting, and explicit transactions"""

import sqlite3
from threading import Event

from .errors import QueryError
from .models import QueryResult, TransactionResult

DEFAULT_CHUNK_SIZE = 500


class QueryManager:
    def __init__(self, executor):
        self.executor = executor

    def execute_query(self, sql: str, *, chunk_size: int = DEFAULT_CHUNK_SIZE,
                      cancellation: Event | None = None) -> QueryResult:
        result = self.executor.execute(sql, chunk_size=chunk_size, cancellation=cancellation)
        return result.fetch_next_chunk(cancellation)

    def fetch_next_chunk(self, result_id: str, cancellation: Event | None = None) -> QueryResult:
        return self.executor.get_result(result_id).fetch_next_chunk(cancellation)

    def get_total_count(self, result_id: str, cancellation: Event | None = None) -> QueryResult:
        return self.executor.get_result(result_id).get_total_count(cancellation)

    def stop_viewing(self, result_id: str) -> QueryResult:
        result = self.executor.get_result(result_id)
        result.close()
        return result.snapshot()

    def finish_transaction(self, *, commit: bool) -> TransactionResult:
        connection = self.executor.connection
        # Closing RETURNING finishes the statement, but never commits it by itself
        self.executor.close_result()
        try:
            if commit:
                connection.commit()
            else:
                connection.rollback()
        except sqlite3.Error as error:
            raise QueryError(f'Cannot {"commit" if commit else "roll back"}: {error}') from error
        return TransactionResult(connection.in_transaction, schema_changed=not commit)
