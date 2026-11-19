"""Read table definitions and run table DDL in the owning worker thread."""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import ContextManager, Iterator, Protocol
from uuid import uuid4

from .errors import TableOperationError
from .table_models import (
    CHECK, FOREIGN_KEY, PRIMARY_KEY, UNIQUE, ColumnDef, TableConstraint, TableDefinition,
)
from .table_parser import parse_create_table
from .table_sql import create_table_sql, drop_table_sql, fold_name, is_rowid_alias, quote_name

_ROWID_NAMES = ("rowid", "oid", "_rowid_")


def _free_rowid_name(columns) -> str | None:
    """A name for the internal row id that no column of the table hides."""
    taken = {fold_name(column.name) for column in columns}
    return next((name for name in _ROWID_NAMES if name not in taken), None)


class ITableRepository(Protocol):
    @property
    def in_transaction(self) -> bool: ...

    def find_all(self) -> tuple[TableDefinition, ...]: ...

    def find_by_name(self, name: str) -> TableDefinition | None: ...

    def has_rows(self, name: str) -> bool: ...

    def broken_views(self) -> dict[str, str]: ...

    def triggers(self) -> list[tuple[str, str, str]]: ...

    def rename_columns(self, table: str, renames: tuple[tuple[str, str], ...]) -> None: ...

    def save(self, table: TableDefinition) -> None: ...

    def update(self, old: TableDefinition, new: TableDefinition, copy_map: tuple[tuple[str, str], ...],
               change: "ChangeContext") -> None: ...

    def delete(self, name: str) -> None: ...

    def atomic_change(self, *, rebuild: bool = False) -> ContextManager["ChangeContext"]: ...


@dataclass(frozen=True)
class ChangeContext:
    """What the connection allowed when a change started."""

    # Foreign keys were enforced before the change began.
    foreign_keys_enforced: bool = False
    # Enforcement could be switched off for the rebuild. SQLite cannot do it inside a transaction.
    foreign_keys_disabled: bool = False


class SQLiteTableRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    @property
    def in_transaction(self) -> bool:
        return self.connection.in_transaction

    # ----- reading -----

    def _foreign_keys(self, table: str) -> list[TableConstraint]:
        rows = self.connection.execute(
            'SELECT id, seq, "table", "from", "to", on_update, on_delete '
            "FROM pragma_foreign_key_list(?, 'main') ORDER BY id, seq",
            (table,),
        ).fetchall()
        grouped: dict[int, list[tuple]] = {}
        for row in rows:
            grouped.setdefault(row[0], []).append(row)
        constraints = []
        for group in grouped.values():
            def action(value: str | None) -> str:
                return "" if value in (None, "NO ACTION") else value
            constraints.append(TableConstraint(
                FOREIGN_KEY,
                columns=tuple(row[3] for row in group),
                ref_table=group[0][2],
                ref_columns=tuple(row[4] for row in group if row[4] is not None),
                on_update=action(group[0][5]),
                on_delete=action(group[0][6]),
            ))
        return constraints

    def _unique_keys(self, table: str) -> tuple[list[tuple[str, ...]], list[tuple[str, ...]]]:
        """Return (keys from UNIQUE constraints, every full unique key)."""
        declared: list[tuple[str, ...]] = []
        every: list[tuple[str, ...]] = []
        entries = self.connection.execute(
            'SELECT name, "unique", origin, partial FROM pragma_index_list(?, \'main\')', (table,),
        ).fetchall()
        for name, unique, origin, partial in entries:
            if not unique or partial:
                continue
            columns = self.connection.execute(
                "SELECT name FROM pragma_index_info(?, 'main') ORDER BY seqno", (name,),
            ).fetchall()
            if any(column[0] is None for column in columns):
                continue  # Expression indexes cannot be a parent key.
            key = tuple(column[0] for column in columns)
            every.append(key)
            if origin == "u":
                declared.append(key)
        return declared, every

    def _read(self, name: str, without_rowid: bool, strict: bool, sql: str | None) -> TableDefinition:
        info = self.connection.execute(
            "SELECT name, type, \"notnull\", dflt_value, pk, hidden FROM pragma_table_xinfo(?, 'main') ORDER BY cid",
            (name,),
        ).fetchall()
        parsed = parse_create_table(sql)
        reasons = list(parsed.unsupported)
        if without_rowid:
            reasons.append("WITHOUT ROWID tables")
        if strict:
            reasons.append("STRICT tables")
        if any(row[5] in (2, 3) for row in info):
            reasons.append("generated columns")
        declared, every = self._unique_keys(name)
        single_unique = {key[0] for key in declared if len(key) == 1}
        primary = sorted((row[4], row[0]) for row in info if row[4] > 0)
        constraints: list[TableConstraint] = []
        if len(primary) > 1:
            constraints.append(TableConstraint(PRIMARY_KEY, columns=tuple(column for _, column in primary)))
        single_primary = primary[0][1] if len(primary) == 1 else None
        columns = tuple(ColumnDef(
            row[0], row[1] or "", bool(row[2]),
            primary_key=row[0] == single_primary,
            unique=row[0] in single_unique,
            autoincrement=row[0].lower() in parsed.autoincrement,
            default=row[3],
        ) for row in info)
        constraints.extend(
            TableConstraint(UNIQUE, columns=key) for key in declared if len(key) > 1
        )
        constraints.extend(self._foreign_keys(name))
        constraints.extend(TableConstraint(CHECK, expression=expression) for expression in parsed.checks)
        keys = tuple(dict.fromkeys(every + ([tuple(column for _, column in primary)] if primary else [])))
        return TableDefinition(
            name, columns, tuple(constraints), editable=not reasons,
            reason=("Cannot be edited with this form: " + ", ".join(reasons) + ".") if reasons else None,
            sql=sql, unique_keys=keys,
        )

    def find_all(self) -> tuple[TableDefinition, ...]:
        # Virtual tables and shadow tables are not ordinary tables.
        rows = self.connection.execute(
            "SELECT name, wr, strict FROM pragma_table_list "
            "WHERE schema = 'main' AND type = 'table' AND name NOT GLOB 'sqlite_*' ORDER BY name"
        ).fetchall()
        tables = []
        for name, without_rowid, strict in rows:
            sql = self.connection.execute(
                "SELECT sql FROM main.sqlite_schema WHERE type = 'table' AND name = ?", (name,),
            ).fetchone()
            tables.append(self._read(name, bool(without_rowid), bool(strict), sql[0] if sql else None))
        parents: dict[str, list[str]] = {}
        for table in tables:
            for constraint in table.constraints:
                if constraint.kind == FOREIGN_KEY and fold_name(constraint.ref_table or "") != fold_name(table.name):
                    parents.setdefault(fold_name(constraint.ref_table or ""), []).append(table.name)
        return tuple(
            _with_references(table, tuple(dict.fromkeys(parents.get(fold_name(table.name), ()))))
            for table in tables
        )

    def find_by_name(self, name: str) -> TableDefinition | None:
        return next((table for table in self.find_all() if fold_name(table.name) == fold_name(name)), None)

    def has_rows(self, name: str) -> bool:
        return self.connection.execute(f"SELECT EXISTS (SELECT 1 FROM main.{quote_name(name)})").fetchone()[0] == 1

    def broken_views(self) -> dict[str, str]:
        """Views that cannot be prepared now, with SQLite's reason."""
        names = [row[0] for row in self.connection.execute(
            "SELECT name FROM main.sqlite_schema WHERE type = 'view' ORDER BY name"
        )]
        broken = {}
        for name in names:
            try:
                self.connection.execute(f"SELECT * FROM main.{quote_name(name)} LIMIT 0").fetchall()
            except sqlite3.Error as error:
                broken[name] = str(error)
        return broken

    def triggers(self) -> list[tuple[str, str, str]]:
        """Every trigger as (name, table it is attached to, CREATE TRIGGER text)."""
        rows = self.connection.execute(
            "SELECT name, tbl_name, sql FROM main.sqlite_schema WHERE type = 'trigger' AND sql IS NOT NULL ORDER BY name"
        ).fetchall()
        return [tuple(row) for row in rows]

    # ----- writing -----

    def rename_columns(self, table: str, renames: tuple[tuple[str, str], ...]) -> None:
        """Let SQLite rename the columns; it also rewrites CHECK, indexes, views and triggers."""
        for old_name, new_name in renames:
            self.connection.execute(
                f"ALTER TABLE main.{quote_name(table)} RENAME COLUMN {quote_name(old_name)} TO {quote_name(new_name)}"
            )

    def save(self, table: TableDefinition) -> None:
        self.connection.execute(create_table_sql(table))

    def delete(self, name: str) -> None:
        self.connection.execute(drop_table_sql(name))

    @contextmanager
    def atomic_change(self, *, rebuild: bool = False) -> Iterator[ChangeContext]:
        """Run one change in a private savepoint, leaving any outer transaction alone."""
        connection = self.connection
        enforced = connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        legacy = connection.execute("PRAGMA legacy_alter_table").fetchone()[0]
        disabled = False
        if rebuild:
            # Dropping the old table would run ON DELETE actions in child tables, so enforcement
            # must be off. SQLite ignores this pragma inside a transaction.
            if enforced and not connection.in_transaction:
                connection.execute("PRAGMA foreign_keys = OFF")
                disabled = True
            # Otherwise RENAME re-checks every view and trigger while the old table is missing.
            connection.execute("PRAGMA legacy_alter_table = ON")
        name = "table_" + uuid4().hex
        try:
            connection.execute(f"SAVEPOINT {name}")
            try:
                yield ChangeContext(enforced, disabled)
                connection.execute(f"RELEASE {name}")
            except Exception:
                if connection.in_transaction:
                    connection.execute(f"ROLLBACK TO {name}")
                    connection.execute(f"RELEASE {name}")
                raise
        finally:
            if rebuild:
                connection.execute(f"PRAGMA legacy_alter_table = {1 if legacy else 0}")
                if disabled:
                    connection.execute("PRAGMA foreign_keys = ON")

    def _dependents(self, table: str) -> list[tuple[str, str, str]]:
        """Indexes and triggers that DROP TABLE would remove; they are recreated afterwards."""
        rows = self.connection.execute(
            "SELECT type, name, sql FROM main.sqlite_schema "
            "WHERE type IN ('index', 'trigger') AND tbl_name = ? AND sql IS NOT NULL "
            "ORDER BY CASE type WHEN 'index' THEN 0 ELSE 1 END, name",
            (table,),
        ).fetchall()
        return [tuple(row) for row in rows]

    def _index_columns(self, index: str) -> list[str]:
        rows = self.connection.execute(
            "SELECT name FROM pragma_index_info(?, 'main') ORDER BY seqno", (index,),
        ).fetchall()
        return [row[0] for row in rows if row[0] is not None]

    def _violations(self) -> set[tuple]:
        return {tuple(row) for row in self.connection.execute("PRAGMA foreign_key_check").fetchall()}

    def update(self, old: TableDefinition, new: TableDefinition, copy_map: tuple[tuple[str, str], ...],
               change: ChangeContext) -> None:
        """Rebuild the table with its data, following SQLite's recommended ALTER procedure."""
        connection = self.connection
        final = old.name
        temp = "__rebuild_" + uuid4().hex[:12]
        has_foreign_keys = any(c.kind == FOREIGN_KEY for c in old.constraints + new.constraints)
        if change.foreign_keys_enforced and not change.foreign_keys_disabled and (
            has_foreign_keys or old.referenced_by
        ):
            raise TableOperationError(
                "A transaction is open and foreign keys are enforced, so a table with foreign keys "
                "cannot be rebuilt safely. Commit or roll back the transaction first."
            )
        dependents = self._dependents(final)
        kept = {fold_name(old_name): new_name for old_name, new_name in copy_map}
        for kind, name, _ in dependents:
            if kind != "index":
                continue
            for column in self._index_columns(name):
                if kept.get(fold_name(column)) != column:
                    raise TableOperationError(
                        f'Index "{name}" uses column "{column}", which is removed or renamed. '
                        "Change or delete the index first (UC-07, UC-08)."
                    )
        before = self._violations() if change.foreign_keys_enforced else set()
        try:
            connection.execute(create_table_sql(new, name=temp))
            self._copy_rows(final, temp, old, new, copy_map)
            self._keep_sequence(final, temp, new)
            connection.execute(drop_table_sql(final))
            connection.execute(f"ALTER TABLE main.{quote_name(temp)} RENAME TO {quote_name(final)}")
            for kind, name, sql in dependents:
                try:
                    connection.execute(sql)
                except sqlite3.Error as error:
                    raise TableOperationError(f'Cannot recreate {kind} "{name}": {error}') from error
            if change.foreign_keys_enforced:
                broken = self._violations() - before
                if broken:
                    raise TableOperationError(
                        "The change would break foreign key constraints "
                        f"({len(broken)} row(s) have no matching parent row)."
                    )
        except sqlite3.Error as error:
            raise TableOperationError("SQLite: " + str(error).replace(temp, final)) from error

    def _copy_rows(self, final: str, temp: str, old: TableDefinition, new: TableDefinition,
                   copy_map: tuple[tuple[str, str], ...]) -> None:
        if not copy_map:
            return
        old_columns = [quote_name(old_name) for old_name, _ in copy_map]
        new_columns = [quote_name(new_name) for _, new_name in copy_map]
        # Row ids are data too. An INTEGER PRIMARY KEY of the new table gets them from its column;
        # otherwise they are copied under a name that no column hides (rowid, oid or _rowid_).
        if not any(is_rowid_alias(column) for column in new.columns):
            source = _free_rowid_name(old.columns)
            target = _free_rowid_name(new.columns)
            if source is not None and target is not None:
                old_columns.insert(0, source)
                new_columns.insert(0, target)
            elif self.has_rows(final):
                raise TableOperationError(
                    "The table has columns named rowid, oid and _rowid_, so its internal row ids "
                    "cannot be preserved. Rename one of these columns first."
                )
        self.connection.execute(
            f"INSERT INTO main.{quote_name(temp)} ({', '.join(new_columns)}) "
            f"SELECT {', '.join(old_columns)} FROM main.{quote_name(final)}"
        )

    def _keep_sequence(self, final: str, temp: str, new: TableDefinition) -> None:
        """Keep the AUTOINCREMENT counter, which can be higher than the largest stored id."""
        if not any(column.autoincrement for column in new.columns):
            return
        exists = self.connection.execute(
            "SELECT 1 FROM main.sqlite_schema WHERE name = 'sqlite_sequence'"
        ).fetchone()
        if not exists:
            return
        row = self.connection.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (final,)).fetchone()
        if row is None:
            return
        updated = self.connection.execute(
            "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = ?", (row[0], temp),
        ).rowcount
        if updated == 0:
            self.connection.execute("INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)", (temp, row[0]))


def _with_references(table: TableDefinition, referenced_by: tuple[str, ...]) -> TableDefinition:
    return replace(table, referenced_by=referenced_by)
