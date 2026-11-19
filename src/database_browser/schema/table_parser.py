"""Read the parts of a stored CREATE TABLE statement that PRAGMAs do not report."""

import re
from dataclasses import dataclass

_TOKEN = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<comment>--[^\r\n]*|/\*.*?(?:\*/|\Z))
  | (?P<string>'(?:''|[^'])*')
  | (?P<quoted>"(?:""|[^"])*"|`(?:``|[^`])*`|\[[^\]]*\])
  | (?P<word>[^\W\d][\w$]*)
  | (?P<number>\d[\w.]*|\.\d\w*)
  | (?P<lparen>\()
  | (?P<rparen>\))
  | (?P<comma>,)
  | (?P<other>.)
    """,
    re.VERBOSE | re.DOTALL,
)

# Clauses that the column form cannot represent without changing their meaning.
_UNSUPPORTED = {
    "CONSTRAINT": "named constraints",
    "COLLATE": "COLLATE clauses",
    "CONFLICT": "ON CONFLICT clauses",
    "DEFERRABLE": "deferred foreign keys",
    "MATCH": "MATCH clauses in foreign keys",
    "GENERATED": "generated columns",
    "AS": "generated columns",
    "ASC": "ordering inside keys",
    "DESC": "ordering inside keys",
}
_TABLE_CONSTRAINT_START = {"CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN"}


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class ParsedTable:
    """Facts taken from the statement text."""

    # Normalised expressions of table-level and column-level CHECK constraints.
    checks: tuple[str, ...]
    # Lower-cased names of columns declared with AUTOINCREMENT.
    autoincrement: frozenset[str]
    # Human-readable names of unsupported clauses (without duplicates).
    unsupported: tuple[str, ...]


def _tokens(sql: str) -> list[_Token]:
    return [_Token(m.lastgroup, m.group(), m.start(), m.end()) for m in _TOKEN.finditer(sql)]


def identifiers(sql: str) -> set[str]:
    """Names used in a statement: bare words and quoted identifiers, but no string literals or comments."""
    return {unquote(token.text) for token in _tokens(sql) if token.kind in {"word", "quoted"}}


def has_comment(sql: str) -> bool:
    return any(token.kind == "comment" for token in _tokens(sql))


def normalize_expression(expression: str) -> str:
    """Collapse whitespace outside quotes so an expression fits on one line."""
    return "".join(
        " " if token.kind == "space" else token.text for token in _tokens(expression.strip())
    )


def unquote(text: str) -> str:
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1].replace('""', '"')
    if len(text) >= 2 and text[0] == "`" and text[-1] == "`":
        return text[1:-1].replace("``", "`")
    if len(text) >= 2 and text[0] == "[" and text[-1] == "]":
        return text[1:-1]
    return text


def _matching(items: list[_Token], open_index: int) -> int:
    depth = 0
    for index in range(open_index, len(items)):
        if items[index].kind == "lparen":
            depth += 1
        elif items[index].kind == "rparen":
            depth -= 1
            if depth == 0:
                return index
    return len(items) - 1


def parse_create_table(sql: str | None) -> ParsedTable:
    if not sql:
        return ParsedTable((), frozenset(), ("an unreadable table definition",))
    tokens = [token for token in _tokens(sql) if token.kind not in {"space", "comment"}]
    opening = next((i for i, token in enumerate(tokens) if token.kind == "lparen"), None)
    if opening is None:
        return ParsedTable((), frozenset(), ("an unreadable table definition",))
    closing = _matching(tokens, opening)
    body = tokens[opening + 1:closing]

    items: list[list[_Token]] = [[]]
    depth = 0
    for token in body:
        if token.kind == "lparen":
            depth += 1
        elif token.kind == "rparen":
            depth -= 1
        if token.kind == "comma" and depth == 0:
            items.append([])
        else:
            items[-1].append(token)

    checks: list[str] = []
    autoincrement: set[str] = set()
    unsupported: list[str] = []
    for item in items:
        if not item:
            continue
        first = item[0]
        is_table_constraint = first.kind == "word" and first.text.upper() in _TABLE_CONSTRAINT_START
        column = None if is_table_constraint else unquote(first.text).lower()
        index = 0 if is_table_constraint else 1
        while index < len(item):
            token = item[index]
            word = token.text.upper() if token.kind == "word" else ""
            followed_by_paren = index + 1 < len(item) and item[index + 1].kind == "lparen"
            if word in {"CHECK", "DEFAULT"} and followed_by_paren:
                end = _matching(item, index + 1)
                if word == "CHECK":
                    expression = sql[item[index + 1].end:item[end].start]
                    if has_comment(expression):
                        unsupported.append("comments inside CHECK constraints")
                    checks.append(normalize_expression(expression))
                index = end + 1
                continue
            if word == "AUTOINCREMENT" and column is not None:
                autoincrement.add(column)
            elif word in _UNSUPPORTED:
                unsupported.append(_UNSUPPORTED[word])
            index += 1
    return ParsedTable(tuple(checks), frozenset(autoincrement), tuple(dict.fromkeys(unsupported)))
