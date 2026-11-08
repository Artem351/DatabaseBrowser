"""Read SQLite metadata and execute index DDL in the owning worker."""

import sqlite3
from contextlib import contextmanager
from typing import ContextManager, Iterator, Protocol
from uuid import uuid4

from .models import Index, IndexColumn, TableInfo
from .sql import create_index_sql


class IIndexRepository(Protocol):
    @property
    def in_transaction(self) -> bool: ...

    def list_tables(self) -> tuple[TableInfo, ...]: ...

    def find_all(self) -> tuple[Index, ...]: ...

    def save(self, index: Index) -> None: ...

    def atomic_change(self) -> ContextManager[None]: ...


class SQLiteIndexRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    @property
    def in_transaction(self) -> bool:
        return self.connection.in_transaction

    def list_tables(self) -> tuple[TableInfo, ...]:
        # Virtual tables and their shadow tables do not support ordinary indexes.
        names = self.connection.execute(
            "SELECT name FROM pragma_table_list "
            "WHERE schema = 'main' AND type = 'table' "
            "AND name NOT GLOB 'sqlite_*' ORDER BY name"
        ).fetchall()
        tables = []
        for (name,) in names:
            columns = self.connection.execute(
                "SELECT name FROM pragma_table_xinfo(?, 'main') WHERE hidden != 1 ORDER BY cid",
                (name,),
            ).fetchall()
            tables.append(TableInfo(name, tuple(row[0] for row in columns)))
        return tuple(tables)

    def find_all(self) -> tuple[Index, ...]:
        indexes = []
        for table in self.list_tables():
            entries = self.connection.execute(
                'SELECT name, "unique", origin, partial FROM pragma_index_list(?, \'main\')',
                (table.name,),
            ).fetchall()
            for name, unique, origin, partial in entries:
                keys = self.connection.execute(
                    'SELECT cid, name, "desc", coll FROM pragma_index_xinfo(?, \'main\') '
                    'WHERE "key" = 1 ORDER BY seqno',
                    (name,),
                ).fetchall()
                definition = self.connection.execute(
                    "SELECT sql FROM main.sqlite_schema WHERE type = 'index' AND name = ?",
                    (name,),
                ).fetchone()
                # Keep special definitions visible, but don't flatten them in a simple form.
                editable = not partial and all(
                    cid >= 0 and column is not None and collation == "BINARY"
                    for cid, column, _, collation in keys
                )
                columns = tuple(
                    IndexColumn(column if column is not None else "<expression>", bool(descending))
                    for _, column, descending, _ in keys
                )
                indexes.append(Index(
                    name, table.name, columns, bool(unique), origin,
                    definition[0] if definition else None, editable,
                ))
        return tuple(sorted(indexes, key=lambda index: index.name))

    def save(self, index: Index) -> None:
        self.connection.execute(create_index_sql(index))

    @contextmanager
    def atomic_change(self) -> Iterator[None]:
        # A private savepoint leaves any transaction opened by the SQL editor alone.
        name = "index_" + uuid4().hex
        self.connection.execute(f"SAVEPOINT {name}")
        try:
            yield
            self.connection.execute(f"RELEASE {name}")
        except Exception:
            if self.connection.in_transaction:
                self.connection.execute(f"ROLLBACK TO {name}")
                self.connection.execute(f"RELEASE {name}")
            raise
