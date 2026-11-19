"""Table use cases (UC-03, UC-04, UC-05), independent of Qt."""

import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from typing import Iterator

from .errors import TableOperationError
from .table_models import (
    CHECK, FOREIGN_KEY, PRIMARY_KEY, UNIQUE, TableConstraint, TableDefinition, TableSnapshot,
)
from .table_parser import identifiers
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
                self._check_not_stale(old, definition)
                if not old.editable:
                    raise TableOperationError(old.reason or "This table cannot be edited with this form.")
                new = replace(_normalized(definition), name=old.name)
                copy_map = self._copy_map(old, new)
                renames = self._renames(old, new)
                # Constraints saved before a rename still name the old column.
                new = self._follow_renames(new, renames)
                others = tuple(t for t in tables if t is not old)
                validate_definition(new, others)
                self._check_rename_targets(old, new, renames)
                self._check_children(old, new, others)
                self._check_data(old, new, copy_map)
                self._check_triggers(old, copy_map)
                broken_before = self.repository.broken_views()
                if renames:
                    # SQLite rewrites CHECK constraints, indexes, views and triggers that use the column.
                    self.repository.rename_columns(old.name, renames)
                    renamed = self.repository.find_by_name(old.name)
                    new = self._after_rename(old, renamed, new, renames)
                    old, copy_map = renamed, self._copy_map(renamed, new)
                self.repository.update(old, new, copy_map, change)
                self._check_views(broken_before)
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
    def _check_not_stale(old: TableDefinition, definition: TableDefinition) -> None:
        """The form edits the table as it was when the form opened; refuse if that is no longer true."""
        if definition.sql is None:
            raise TableOperationError(
                "The form does not say which version of the table it edits. Close it and open it again."
            )
        if definition.sql != old.sql:
            raise TableOperationError(
                f'Table "{old.name}" was changed after this form was opened, for example by another '
                "connection. Close the form and open it again so those changes are not lost."
            )

    def _check_triggers(self, old: TableDefinition, copy_map: tuple[tuple[str, str], ...]) -> None:
        """SQLite keeps no dependencies for triggers, so read their text for removed columns."""
        kept = {fold_name(old_name) for old_name, _ in copy_map}
        removed = [column.name for column in old.columns if fold_name(column.name) not in kept]
        if not removed:
            return
        table = fold_name(old.name)
        for name, owner, sql in self.repository.triggers():
            words = {fold_name(word) for word in identifiers(sql)}
            # A trigger on another table matters only when its text mentions this table.
            if fold_name(owner) != table and table not in words:
                continue
            for column in removed:
                if fold_name(column) in words:
                    raise TableOperationError(
                        f'Trigger "{name}" may use column "{column}", which this change removes. '
                        "Change or delete the trigger first."
                    )

    @staticmethod
    def _renames(old: TableDefinition, new: TableDefinition) -> tuple[tuple[str, str], ...]:
        """Pairs (stored name, new name) for columns whose name changes beyond letter case."""
        stored = {fold_name(column.name): column.name for column in old.columns}
        return tuple(
            (stored[fold_name(column.original_name)], column.name)
            for column in new.columns
            if column.original_name is not None and fold_name(column.original_name) in stored
            and fold_name(column.original_name) != fold_name(column.name)
        )

    @staticmethod
    def _follow_renames(new: TableDefinition, renames: tuple[tuple[str, str], ...]) -> TableDefinition:
        """Point key and foreign key columns at the new names of renamed columns."""
        if not renames:
            return new
        mapping = {fold_name(old_name): new_name for old_name, new_name in renames}

        def follow(names: tuple[str, ...]) -> tuple[str, ...]:
            return tuple(mapping.get(fold_name(name), name) for name in names)

        constraints = []
        for constraint in new.constraints:
            if constraint.kind in {PRIMARY_KEY, UNIQUE}:
                constraint = replace(constraint, columns=follow(constraint.columns))
            elif constraint.kind == FOREIGN_KEY:
                own = fold_name(constraint.ref_table or "") == fold_name(new.name)
                constraint = replace(
                    constraint, columns=follow(constraint.columns),
                    ref_columns=follow(constraint.ref_columns) if own else constraint.ref_columns,
                )
            constraints.append(constraint)
        return replace(new, constraints=tuple(constraints))

    @staticmethod
    def _check_rename_targets(old: TableDefinition, new: TableDefinition,
                              renames: tuple[tuple[str, str], ...]) -> None:
        """Refuse renames whose meaning would be ambiguous (swaps, reuse of a removed column's name)."""
        stored = {fold_name(column.name) for column in old.columns}
        vacated = {fold_name(old_name) for old_name, _ in renames}
        for old_name, new_name in renames:
            if fold_name(new_name) in stored:
                raise TableOperationError(
                    f'Cannot rename "{old_name}" to "{new_name}": the table has a column with that name now. '
                    "Rename, swap or remove such columns in separate steps."
                )
        for column in new.columns:
            keeps_name = column.original_name is not None and fold_name(column.original_name) == fold_name(column.name)
            if fold_name(column.name) in vacated and not keeps_name:
                raise TableOperationError(
                    f'The name "{column.name}" was used by a column that is renamed in this change. '
                    "Use it in a separate step."
                )

    @staticmethod
    def _after_rename(old: TableDefinition, renamed: TableDefinition | None, new: TableDefinition,
                      renames: tuple[tuple[str, str], ...]) -> TableDefinition:
        """Adopt the names and CHECK texts that SQLite wrote while renaming."""
        if renamed is None:
            raise TableOperationError(f'Table "{old.name}" disappeared while it was being changed.')
        done = {fold_name(old_name) for old_name, _ in renames}
        columns = tuple(
            replace(column, original_name=column.name)
            if column.original_name is not None and fold_name(column.original_name) in done else column
            for column in new.columns
        )
        before = [c.expression for c in old.constraints if c.kind == CHECK]
        after = [c.expression for c in renamed.constraints if c.kind == CHECK]
        rewritten = dict(zip(before, after)) if len(before) == len(after) else {}
        constraints = tuple(
            replace(c, expression=rewritten.get(c.expression, c.expression)) if c.kind == CHECK else c
            for c in new.constraints
        )
        return replace(new, columns=columns, constraints=constraints)

    def _check_views(self, broken_before: dict[str, str]) -> None:
        """A change must not leave a working view broken."""
        for name, reason in self.repository.broken_views().items():
            if name not in broken_before:
                raise TableOperationError(
                    f'View "{name}" would stop working ({reason}). Change or delete the view first.'
                )

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
