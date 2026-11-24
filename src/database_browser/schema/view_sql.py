"""SQL generation and input checks for the view editor."""

from .errors import ViewOperationError
from .table_parser import _tokens
from .table_sql import fold_name


def quote_name(name: str) -> str:
    if not name or not name.strip() or "\x00" in name:
        raise ViewOperationError("Names must not be empty or contain a null character.")
    return '"' + name.replace('"', '""') + '"'


def check_name(name: str) -> None:
    if not name.strip():
        raise ViewOperationError("Enter a view name.")
    if "\x00" in name:
        raise ViewOperationError("Names must not contain a null character.")
    if fold_name(name).startswith("sqlite_"):
        raise ViewOperationError('Names starting with "sqlite_" are reserved for SQLite.')


def check_query(query: str) -> str:
    """Accept exactly one SELECT-like statement; return it without trailing semicolons."""
    text = query.strip()
    while text.endswith(";"):
        text = text[:-1].rstrip()
    if not text:
        raise ViewOperationError("Enter the defining SELECT query.")
    depth = 0
    first = None
    for token in _tokens(text):
        if token.kind in {"space", "comment"}:
            continue
        if first is None:
            first = token.text.upper() if token.kind == "word" else ""
        if token.kind == "lparen":
            depth += 1
        elif token.kind == "rparen":
            depth -= 1
            if depth < 0:
                break
        elif token.kind == "other" and token.text in {";", "'", '"', "`"}:
            # A semicolon would start a second statement; a lone quote is an unterminated string.
            depth = -1
            break
    if depth != 0:
        raise ViewOperationError(
            "The query has unbalanced parentheses, an unclosed quote, or a semicolon in the middle."
        )
    if first not in {"SELECT", "WITH", "VALUES"}:
        raise ViewOperationError("The defining query must be a SELECT statement.")
    return text


def split_view_sql(sql: str | None) -> tuple[str, str | None]:
    """Return (defining query, reason the form cannot edit it) from a stored CREATE VIEW."""
    unreadable = "an unreadable view definition"
    if not sql:
        return "", unreadable
    reason = None
    depth = 0
    for token in _tokens(sql):
        if token.kind in {"space", "comment"}:
            continue
        if token.kind == "lparen":
            if depth == 0:
                reason = "a column list after the view name"
            depth += 1
        elif token.kind == "rparen":
            depth -= 1
        elif token.kind == "word" and depth == 0 and token.text.upper() == "AS":
            return sql[token.end:].strip().rstrip(";").strip(), reason
    return "", unreadable


def create_view_sql(name: str, query: str) -> str:
    return f"CREATE VIEW main.{quote_name(name)} AS\n{query}"


def drop_view_sql(name: str) -> str:
    return f"DROP VIEW main.{quote_name(name)}"
