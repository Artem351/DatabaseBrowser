"""Plain query data passed from the worker to Qt"""

from dataclasses import dataclass


@dataclass(frozen=True)
class QueryStatistics:
    execution_seconds: float
    fetch_seconds: float
    affected_rows: int | None
    loaded_rows: int
    total_count: int | None
    is_count_calculated: bool
    count_seconds: float
    count_error: str | None
    in_transaction: bool
    schema_changed: bool


@dataclass(frozen=True)
class QueryResult:
    result_id: str
    columns: tuple[str, ...]
    rows: tuple[tuple, ...]
    has_more: bool
    can_count: bool
    incomplete: bool
    statistics: QueryStatistics


@dataclass(frozen=True)
class TransactionResult:
    in_transaction: bool
    schema_changed: bool = False
