"""Table use cases (UC-03, UC-04, UC-05), independent of Qt."""

import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from typing import Iterator

from .errors import TableOperationError
from .table_models import (
    CHECK, FOREIGN_KEY, PRIMARY_KEY, UNIQUE, TableConstraint, TableDefinition, TableSnapshot,
)
from .table_repository import ITableRepository
from .table_sql import (
    check_expression, check_type, create_table_sql, declared_keys, fold_name, is_rowid_alias,
    primary_key_columns,
)


@contextmanager
def _database_errors() -> Iterator[None]:
    try:
        yield
    except sqlite3.Error as error:
        raise TableOperationError(f"SQLite: {error}") from error


def _same_columns(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return {fold_name(name) for name in left} == {fold_name(name) for name in right} and len(left) == len(right)


def _normalized(definition: TableDefinition) -> TableDefinition:
    """Trim user input and validate the pieces that are written into SQL as text."""
    columns = tuple(replace(
        column,
        data_type=check_type(column.data_type),
        default=(
            check_expression(column.default, f'default value of column "{column.name}"')
            if column.default is not None and column.default.strip() else None
        ),
    ) for column in definition.columns)
    constraints = tuple(
        replace(c, expression=check_expression(c.expression or "", "CHECK expression")) if c.kind == CHECK else c
        for c in definition.constraints
    )
    return replace(definition, name=definition.name.strip(), columns=columns, constraints=constraints)


def _require_columns(names: tuple[str, ...], available: set[str], what: str) -> None:
    if not names:
        raise TableOperationError(f"{what} needs at least one column.")
    seen = set()
    for name in names:
        key = fold_name(name)
        if key not in available:
            raise TableOperationError(f'{what} uses column "{name}", which does not exist in this table.')
        if key in seen:
            raise TableOperationError(f'{what} lists column "{name}" more than once.')
        seen.add(key)


def validate_definition(definition: TableDefinition, others: tuple[TableDefinition, ...]) -> None:
    """Check a normalised definition against the other tables of the database."""
    name = definition.name
    if not name:
        raise TableOperationError("Enter a table name.")
    if "\x00" in name:
        raise TableOperationError("Names must not contain a null character.")
    if fold_name(name).startswith("sqlite_"):
        raise TableOperationError('Names starting with "sqlite_" are reserved for SQLite.')
    if any(fold_name(other.name) == fold_name(name) for other in others):
        raise TableOperationError(f'A table named "{name}" already exists.')
    if not definition.columns:
        raise TableOperationError("Add at least one column.")
    available: set[str] = set()
    for column in definition.columns:
        if not column.name.strip() or "\x00" in column.name:
            raise TableOperationError("Column names must not be empty or contain a null character.")
        key = fold_name(column.name)
        if key in available:
            raise TableOperationError(f'Column "{column.name}" is defined more than once.')
        available.add(key)
        if column.autoincrement and not is_rowid_alias(column):
            raise TableOperationError(
                f'AUTOINCREMENT on "{column.name}" needs the column type INTEGER and PRIMARY KEY.'
            )
    primary_count = sum(column.primary_key for column in definition.columns) + sum(
        constraint.kind == PRIMARY_KEY for constraint in definition.constraints
    )
    if primary_count > 1:
        raise TableOperationError(
            "A table can have only one PRIMARY KEY. For several columns, use a PRIMARY KEY constraint "
            "and clear the PK box of single columns."
        )
    for constraint in definition.constraints:
        if constraint.kind in {PRIMARY_KEY, UNIQUE}:
            _require_columns(constraint.columns, available, f"{constraint.kind} constraint")
        elif constraint.kind == FOREIGN_KEY:
            _validate_foreign_key(definition, constraint, others, available)
        elif constraint.kind != CHECK:
            raise TableOperationError(f"Unknown constraint type: {constraint.kind}")


def _validate_foreign_key(definition: TableDefinition, constraint: TableConstraint,
                          others: tuple[TableDefinition, ...], available: set[str]) -> None:
    _require_columns(constraint.columns, available, "FOREIGN KEY constraint")
    parent_name = (constraint.ref_table or "").strip()
    if not parent_name:
        raise TableOperationError("FOREIGN KEY constraint needs a referenced table.")
    if fold_name(parent_name) == fold_name(definition.name):
        parent_columns = {fold_name(column.name) for column in definition.columns}
        keys = declared_keys(definition)
        primary = primary_key_columns(definition)
    else:
        parent = next((other for other in others if fold_name(other.name) == fold_name(parent_name)), None)
        if parent is None:
            raise TableOperationError(f'The referenced table "{parent_name}" does not exist.')
        parent_columns = {fold_name(column.name) for column in parent.columns}
        keys = declared_keys(parent) + list(parent.unique_keys)
        primary = primary_key_columns(parent)
    if constraint.ref_columns:
        _require_columns(constraint.ref_columns, parent_columns, f'Reference to "{parent_name}"')
        target = constraint.ref_columns
    else:
        if not primary:
            raise TableOperationError(
                f'Table "{parent_name}" has no PRIMARY KEY. Name the referenced columns explicitly.'
            )
        target = primary
    if len(target) != len(constraint.columns):
        raise TableOperationError(
            f"FOREIGN KEY lists {len(constraint.columns)} column(s) but the reference has {len(target)}."
        )
    if not any(_same_columns(target, key) for key in keys):
        raise TableOperationError(
            f'The referenced columns of "{parent_name}" must be its PRIMARY KEY or have a UNIQUE constraint.'
        )


class TableManager:
    def __init__(self, repository: ITableRepository):
        self.repository = repository

    def get_tables(self) -> TableSnapshot:
        with _database_errors():
            return TableSnapshot(self.repository.find_all(), self.repository.in_transaction)

    def _snapshot(self, outer_transaction: bool) -> TableSnapshot:
        return TableSnapshot(self.repository.find_all(), outer_transaction)

    def create_table(self, definition: TableDefinition) -> TableSnapshot:
        """UC-03: validate, then create the table in one savepoint."""
        with _database_errors():
            outer_transaction = self.repository.in_transaction
            with self.repository.atomic_change():
                table = _normalized(definition)
                validate_definition(table, self.repository.find_all())
                # Build the statement first so a bad name is reported before SQLite sees it.
                create_table_sql(table)
                self.repository.save(table)
                snapshot = self._snapshot(outer_transaction)
            return snapshot

    def drop_table(self, name: str) -> TableSnapshot:
        """UC-05: delete the table together with its rows and indexes."""
        with _database_errors():
            outer_transaction = self.repository.in_transaction
            with self.repository.atomic_change():
                table = next((t for t in self.repository.find_all() if fold_name(t.name) == fold_name(name)), None)
                if table is None:
                    if fold_name(name).startswith("sqlite_"):
                        raise TableOperationError("SQLite internal tables cannot be deleted.")
                    raise TableOperationError(f'Table "{name}" does not exist. Refresh the schema.')
                self.repository.delete(table.name)
                snapshot = self._snapshot(outer_transaction)
            return snapshot

    def alter_table(self, definition: TableDefinition) -> TableSnapshot:
        """UC-04: apply the whole change to the table or nothing at all."""
        with _database_errors():
            outer_transaction = self.repository.in_transaction
            with self.repository.atomic_change(rebuild=True) as change:
                tables = self.repository.find_all()
                old = next((t for t in tables if fold_name(t.name) == fold_name(definition.name)), None)
                if old is None:
                    raise TableOperationError(f'Table "{definition.name}" does not exist. Refresh the schema.')
                if not old.editable:
                    raise TableOperationError(old.reason or "This table cannot be edited with this form.")
                new = replace(_normalized(definition), name=old.name)
                others = tuple(t for t in tables if t is not old)
                validate_definition(new, others)
                copy_map = self._copy_map(old, new)
                self._check_children(old, new, others)
                self._check_data(old, new, copy_map)
                self.repository.update(old, new, copy_map, change)
                snapshot = self._snapshot(outer_transaction)
            return snapshot

    @staticmethod
    def _copy_map(old: TableDefinition, new: TableDefinition) -> tuple[tuple[str, str], ...]:
        stored = {fold_name(column.name): column.name for column in old.columns}
        used: set[str] = set()
        pairs = []
        for column in new.columns:
            if column.original_name is None:
                continue
            key = fold_name(column.original_name)
            if key not in stored:
                raise TableOperationError(
                    f'Column "{column.original_name}" no longer exists in "{old.name}". Refresh the schema.'
                )
            if key in used:
                raise TableOperationError(f'Column "{stored[key]}" is used for more than one new column.')
            used.add(key)
            pairs.append((stored[key], column.name))
        return tuple(pairs)

    @staticmethod
    def _check_children(old: TableDefinition, new: TableDefinition, others: tuple[TableDefinition, ...]) -> None:
        """Keep the keys that other tables reference; otherwise their inserts would start failing."""
        unchanged = {
            fold_name(column.name) for column in new.columns
            if column.original_name is not None and fold_name(column.original_name) == fold_name(column.name)
        }
        keys = declared_keys(new) + [
            key for key in old.unique_keys if all(fold_name(name) in unchanged for name in key)
        ]
        for other in others:
            for constraint in other.constraints:
                if constraint.kind != FOREIGN_KEY or fold_name(constraint.ref_table or "") != fold_name(new.name):
                    continue
                target = constraint.ref_columns or primary_key_columns(new)
                if len(target) != len(constraint.columns) or not any(_same_columns(target, key) for key in keys):
                    raise TableOperationError(
                        f'Table "{other.name}" has a foreign key to "{old.name}" that needs '
                        f'({", ".join(target) or "a primary key"}) as a PRIMARY KEY or UNIQUE key. '
                        "Keep that key, or change the foreign key first."
                    )

    def _check_data(self, old: TableDefinition, new: TableDefinition, copy_map: tuple[tuple[str, str], ...]) -> None:
        """Explain data conflicts that are known before anything is changed."""
        if not self.repository.has_rows(old.name):
            return
        if not copy_map:
            raise TableOperationError(
                "Keep at least one existing column, or delete the table and create a new one. "
                "Otherwise its rows would be lost."
            )
        for column in new.columns:
            if (column.original_name is None and column.not_null and column.default is None
                    and not is_rowid_alias(column)):
                raise TableOperationError(
                    f'New column "{column.name}" is NOT NULL without a default value, '
                    "but the table already has rows. Add a default value."
                )
