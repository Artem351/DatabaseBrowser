"""SQL generation and input checks for the table editor."""

import re
from string import ascii_lowercase, ascii_uppercase

from .errors import TableOperationError
from .table_models import ColumnDef, FOREIGN_KEY, CHECK, PRIMARY_KEY, TableConstraint, TableDefinition, UNIQUE
from .table_parser import _tokens

_FOLD = str.maketrans(ascii_uppercase, ascii_lowercase)
# Plain type names such as INTEGER, VARCHAR(20), DECIMAL(10, 2) or DOUBLE PRECISION.
_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?: [A-Za-z_][A-Za-z0-9_]*)*(?:\(\s*[+-]?\d+\s*(?:,\s*[+-]?\d+\s*)?\))?")


def fold_name(name: str) -> str:
    """SQLite folds only ASCII letters in identifiers."""
    return name.translate(_FOLD)


def quote_name(name: str) -> str:
    if not name or not name.strip() or "\x00" in name:
        raise TableOperationError("Names must not be empty or contain a null character.")
    return '"' + name.replace('"', '""') + '"'


def check_type(data_type: str) -> str:
    cleaned = " ".join(data_type.split())
    # "VARCHAR( 20 )" and "DECIMAL(10 , 2)" are stored as VARCHAR(20) and DECIMAL(10,2).
    cleaned = re.sub(r"\s*\(\s*", "(", cleaned)
    cleaned = re.sub(r"\s*,\s*", ",", cleaned)
    cleaned = re.sub(r"\s*\)", ")", cleaned)
    if cleaned and not _TYPE.fullmatch(cleaned):
        raise TableOperationError(
            f'"{data_type}" is not a valid column type. Use a plain type name such as TEXT or VARCHAR(20).'
        )
    return cleaned


def check_expression(expression: str, what: str) -> str:
    """Accept one self-contained SQL expression that cannot escape its parentheses."""
    text = expression.strip()
    if not text:
        raise TableOperationError(f"Enter the {what}.")
    depth = 0
    for token in _tokens(text):
        if token.kind == "comment":
            raise TableOperationError(f"The {what} must not contain comments.")
        if token.kind == "lparen":
            depth += 1
        elif token.kind == "rparen":
            depth -= 1
            if depth < 0:
                break
        elif token.kind == "other" and token.text in {";", "'", '"', "`"}:
            # A lone quote is an unterminated string; a semicolon would end the statement.
            depth = -1
            break
    if depth != 0:
        raise TableOperationError(f"The {what} has unbalanced parentheses, an unclosed quote, or a semicolon.")
    return text


def is_rowid_alias(column: ColumnDef) -> bool:
    return column.primary_key and column.data_type.strip().upper() == "INTEGER"


def declared_keys(definition: TableDefinition) -> list[tuple[str, ...]]:
    """Primary and unique keys that the definition itself declares."""
    keys: list[tuple[str, ...]] = []
    primary = tuple(column.name for column in definition.columns if column.primary_key)
    keys.extend([primary] if primary else [])
    keys.extend((column.name,) for column in definition.columns if column.unique)
    for constraint in definition.constraints:
        if constraint.kind in {PRIMARY_KEY, UNIQUE} and constraint.columns:
            keys.append(constraint.columns)
    return keys


def primary_key_columns(definition: TableDefinition) -> tuple[str, ...]:
    for column in definition.columns:
        if column.primary_key:
            return (column.name,)
    for constraint in definition.constraints:
        if constraint.kind == PRIMARY_KEY:
            return constraint.columns
    return ()


def _constraint_sql(constraint: TableConstraint, own_name: str, target: str) -> str:
    if constraint.kind in {PRIMARY_KEY, UNIQUE}:
        columns = ", ".join(quote_name(name) for name in constraint.columns)
        return f"{constraint.kind} ({columns})"
    if constraint.kind == CHECK:
        return f"CHECK ({constraint.expression})"
    if constraint.kind == FOREIGN_KEY:
        columns = ", ".join(quote_name(name) for name in constraint.columns)
        # A self-reference must name the final table, not the temporary rebuild name.
        parent = own_name if fold_name(constraint.ref_table or "") == fold_name(own_name) else constraint.ref_table
        sql = f"FOREIGN KEY ({columns}) REFERENCES {quote_name(parent or '')}"
        if constraint.ref_columns:
            sql += " (" + ", ".join(quote_name(name) for name in constraint.ref_columns) + ")"
        if constraint.on_delete:
            sql += f" ON DELETE {constraint.on_delete}"
        if constraint.on_update:
            sql += f" ON UPDATE {constraint.on_update}"
        return sql
    raise TableOperationError(f"Unknown constraint type: {constraint.kind}")


def create_table_sql(definition: TableDefinition, *, name: str | None = None) -> str:
    """Build CREATE TABLE; `name` replaces the table name while rebuilding."""
    if not definition.columns:
        raise TableOperationError("Add at least one column.")
    target = name if name is not None else definition.name
    lines = []
    for column in definition.columns:
        parts = [quote_name(column.name)]
        if column.data_type.strip():
            parts.append(column.data_type.strip())
        if column.primary_key:
            parts.append("PRIMARY KEY")
            if column.autoincrement:
                parts.append("AUTOINCREMENT")
        if column.not_null:
            parts.append("NOT NULL")
        if column.unique:
            parts.append("UNIQUE")
        if column.default is not None:
            parts.append(f"DEFAULT ({column.default})")
        lines.append(" ".join(parts))
    lines.extend(_constraint_sql(constraint, definition.name, target) for constraint in definition.constraints)
    return f"CREATE TABLE main.{quote_name(target)} (\n  " + ",\n  ".join(lines) + "\n)"


def drop_table_sql(name: str) -> str:
    return f"DROP TABLE main.{quote_name(name)}"
