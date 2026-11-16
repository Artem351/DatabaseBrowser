"""Plain data shared by the schema worker and the interface."""

from dataclasses import dataclass


@dataclass(frozen=True)
class IndexColumn:
    name: str
    descending: bool = False


@dataclass(frozen=True)
class Index:
    name: str
    table_name: str
    columns: tuple[IndexColumn, ...]
    unique: bool = False
    origin: str = "c"
    sql: str | None = None
    editable: bool = True

    @property
    def is_user_created(self) -> bool:
        return self.origin == "c"

    @property
    def can_edit(self) -> bool:
        return self.is_user_created and self.editable


@dataclass(frozen=True)
class TableInfo:
    name: str
    columns: tuple[str, ...]


@dataclass(frozen=True)
class SchemaSnapshot:
    tables: tuple[TableInfo, ...]
    indexes: tuple[Index, ...]
    in_transaction: bool = False
