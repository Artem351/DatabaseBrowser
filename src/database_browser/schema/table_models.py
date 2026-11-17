"""Plain data for table definitions, shared by the worker and the interface."""

from dataclasses import dataclass

PRIMARY_KEY = "PRIMARY KEY"
UNIQUE = "UNIQUE"
FOREIGN_KEY = "FOREIGN KEY"
CHECK = "CHECK"
CONSTRAINT_KINDS = (PRIMARY_KEY, UNIQUE, FOREIGN_KEY, CHECK)
# NO ACTION is SQLite's default, so it is stored as an empty string.
REFERENTIAL_ACTIONS = ("", "CASCADE", "SET NULL", "SET DEFAULT", "RESTRICT")


@dataclass(frozen=True)
class ColumnDef:
    """One column; `original_name` links an edited column to the stored one."""

    name: str
    data_type: str = ""
    not_null: bool = False
    primary_key: bool = False
    unique: bool = False
    autoincrement: bool = False
    # SQL expression text; it is written as DEFAULT (<expression>).
    default: str | None = None
    # None for a new column. A different name means the column is renamed.
    original_name: str | None = None


@dataclass(frozen=True)
class TableConstraint:
    """A table-level constraint (composite keys, CHECK and FOREIGN KEY)."""

    kind: str
    columns: tuple[str, ...] = ()
    expression: str | None = None
    ref_table: str | None = None
    # Empty means "the primary key of the referenced table".
    ref_columns: tuple[str, ...] = ()
    on_delete: str = ""
    on_update: str = ""


@dataclass(frozen=True)
class TableDefinition:
    """A table as the user edits it plus facts read from the database."""

    name: str
    columns: tuple[ColumnDef, ...]
    constraints: tuple[TableConstraint, ...] = ()
    # The remaining fields are filled when the table is read from SQLite.
    editable: bool = True
    reason: str | None = None
    sql: str | None = None
    referenced_by: tuple[str, ...] = ()
    unique_keys: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class TableSnapshot:
    tables: tuple[TableDefinition, ...]
    in_transaction: bool = False
