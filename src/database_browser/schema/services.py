"""Index use cases, independent of Qt."""

import sqlite3
from contextlib import contextmanager
from string import ascii_lowercase, ascii_uppercase
from typing import Iterator

from .errors import IndexOperationError
from .models import Index, SchemaSnapshot
from .repositories import IIndexRepository
from .sql import create_index_sql


def _identifier_key(name: str) -> str:
    # SQLite folds ASCII letters in identifiers, not every Unicode alphabet.
    return name.translate(str.maketrans(ascii_uppercase, ascii_lowercase))


@contextmanager
def _database_errors() -> Iterator[None]:
    try:
        yield
    except sqlite3.Error as error:
        raise IndexOperationError(f"SQLite: {error}") from error


class SchemaManager:
    def __init__(self, repository: IIndexRepository):
        self.repository = repository

    def get_schema(self) -> SchemaSnapshot:
        with _database_errors():
            return SchemaSnapshot(
                self.repository.list_tables(), self.repository.find_all(),
                self.repository.in_transaction,
            )

    def _validate(self, index: Index) -> None:
        create_index_sql(index)
        table = next((
            table for table in self.repository.list_tables()
            if _identifier_key(table.name) == _identifier_key(index.table_name)
        ), None)
        if table is None:
            raise IndexOperationError(f'Table "{index.table_name}" does not exist.')
        available = {_identifier_key(name) for name in table.columns}
        selected = set()
        for column in index.columns:
            name = _identifier_key(column.name)
            # Without this check, SQLite can interpret a quoted typo as a string literal.
            if name not in available:
                raise IndexOperationError(f'Column "{column.name}" does not exist in "{table.name}".')
            if name in selected:
                raise IndexOperationError(f'Column "{column.name}" is selected more than once.')
            selected.add(name)

    def create_index(self, index: Index) -> SchemaSnapshot:
        with _database_errors():
            self._validate(index)
            outer_transaction = self.repository.in_transaction
            with self.repository.atomic_change():
                self.repository.save(index)
                # Read before RELEASE: if this fails, the change rolls back too.
                snapshot = self.get_schema()
            return SchemaSnapshot(snapshot.tables, snapshot.indexes, outer_transaction)
