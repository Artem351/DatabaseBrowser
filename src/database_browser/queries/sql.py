"""Small SQL helpers; SQLite itself validates the statement"""

import re
from contextlib import contextmanager
from threading import Event

from .errors import QueryCancelled, QueryError

_TOKEN = re.compile(
    r'''(?P<comment>--[^\r\n]*|/\*.*?(?:\*/|\Z))'''
    r'''|(?P<quoted>'(?:''|[^'])*'|"(?:""|[^"])*"|`(?:``|[^`])*`|\[[^\]]*\])'''
    r'''|(?P<space>\s+)|(?P<word>[A-Za-z_][A-Za-z0-9_$]*)|(?P<other>.)''',
    re.DOTALL,
)


def statement_body(sql: str) -> str:
    """Remove trailing comments and the final separator without changing quoted text"""
    tokens = [token for token in _TOKEN.finditer(sql) if token.lastgroup not in {'space', 'comment'}]
    while tokens and tokens[0].group() == ';':
        tokens.pop(0)
    while tokens and tokens[-1].group() == ';':
        tokens.pop()
    if not tokens:
        raise QueryError('Enter one SQL statement')
    return sql[tokens[0].start():tokens[-1].end()]


def first_keyword(sql: str) -> str:
    return next((token.group().upper() for token in _TOKEN.finditer(sql)
                 if token.lastgroup not in {'space', 'comment'}), '')


@contextmanager
def cancellable(connection, cancellation: Event | None):
    """Only the worker touches SQLite; the UI sets an ordinary Event"""
    if cancellation is not None and cancellation.is_set():
        raise QueryCancelled('Operation stopped')
    if cancellation is not None:
        connection.set_progress_handler(lambda: int(cancellation.is_set()), 1000)
    try:
        yield
    finally:
        if cancellation is not None:
            connection.set_progress_handler(None, 0)


def schema_version(connection) -> tuple:
    return tuple(
        (name, connection.execute(f'PRAGMA {name}.schema_version').fetchone()[0])
        for name in ('main', 'temp')
    ) + tuple(connection.execute('PRAGMA database_list'))
