"""Read view definitions and run view DDL in the owning worker thread."""

import sqlite3
from contextlib import contextmanager
from typing import ContextManager, Iterator, Protocol

from .table_parser import identifiers
from .table_sql import fold_name
from .transactions import savepoint
from .view_models import ViewDefinition
from .view_sql import create_view_sql, drop_view_sql, quote_name, split_view_sql


class IViewRepository(Protocol):
    @property
    def in_transaction(self) -> bool: ...

    def find_all(self) -> tuple[ViewDefinition, ...]: ...

    def object_names(self) -> set[str]: ...

    def triggers(self) -> list[tuple[str, str, str]]: ...

    def save(self, name: str, query: str) -> None: ...

    def delete(self, name: str) -> None: ...

    def execute_ddl(self, sql: str) -> None: ...

    def columns_of(self, name: str) -> tuple[str, ...]: ...

    def atomic_change(self) -> ContextManager[None]: ...


class SQLiteViewRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    @property
    def in_transaction(self) -> bool:
        return self.connection.in_transaction

    def columns_of(self, name: str) -> tuple[str, ...]:
        """Result columns of the view; raises sqlite3.Error when the view cannot be queried."""
        cursor = self.connection.execute(f"SELECT * FROM main.{quote_name(name)} LIMIT 0")
        return tuple(column[0] for column in cursor.description or ())

    def triggers(self) -> list[tuple[str, str, str]]:
        """Every trigger as (name, table or view it is attached to, CREATE TRIGGER text)."""
        rows = self.connection.execute(
            "SELECT name, tbl_name, sql FROM main.sqlite_schema WHERE type = 'trigger' AND sql IS NOT NULL ORDER BY name"
        ).fetchall()
        return [tuple(row) for row in rows]

    def object_names(self) -> set[str]:
        """Names that tables, views and indexes share (ASCII-folded)."""
        rows = self.connection.execute(
            "SELECT name FROM main.sqlite_schema WHERE type IN ('table', 'view', 'index')"
        ).fetchall()
        return {fold_name(row[0]) for row in rows}

    def find_all(self) -> tuple[ViewDefinition, ...]:
        rows = self.connection.execute(
            "SELECT name, sql FROM main.sqlite_schema WHERE type = 'view' ORDER BY name"
        ).fetchall()
        words = {name: {fold_name(word) for word in identifiers(sql or "")} for name, sql in rows}
        triggers = self.triggers()
        views = []
        for name, sql in rows:
            query, reason = split_view_sql(sql)
            try:
                columns, error = self.columns_of(name), None
            except sqlite3.Error as failure:
                columns, error = (), str(failure)
            views.append(ViewDefinition(
                name, query, columns, sql=sql, editable=reason is None,
                reason=f"Cannot be edited with this form: {reason}." if reason else None,
                error=error,
                used_by=tuple(other for other, _ in rows if other != name and fold_name(name) in words[other]),
                triggers=tuple(trigger for trigger, owner, _ in triggers if fold_name(owner) == fold_name(name)),
            ))
        return tuple(views)

    def save(self, name: str, query: str) -> None:
        self.connection.execute(create_view_sql(name, query))

    def delete(self, name: str) -> None:
        self.connection.execute(drop_view_sql(name))

    def execute_ddl(self, sql: str) -> None:
        """Run a stored statement again, for example a trigger that DROP VIEW removed."""
        self.connection.execute(sql)

    @contextmanager
    def atomic_change(self) -> Iterator[None]:
        with savepoint(self.connection):
            yield
