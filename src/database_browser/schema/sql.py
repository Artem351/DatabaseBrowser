"""SQL generation for the column-based index editor."""

from .errors import IndexOperationError
from .models import Index


def quote_identifier(name: str) -> str:
    if not name or not name.strip() or "\x00" in name:
        raise IndexOperationError("Names must not be empty or contain a null character.")
    return '"' + name.replace('"', '""') + '"'


def create_index_sql(index: Index) -> str:
    columns = ", ".join(
        f"{quote_identifier(column.name)} {'DESC' if column.descending else 'ASC'}"
        for column in index.columns
    )
    if not columns:
        raise IndexOperationError("Choose at least one column.")
    unique = "UNIQUE " if index.unique else ""
    # Qualifying the index also selects the table's schema in SQLite.
    return (
        f"CREATE {unique}INDEX main.{quote_identifier(index.name)} "
        f"ON {quote_identifier(index.table_name)} ({columns})"
    )
