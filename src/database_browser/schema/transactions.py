"""Run one change in a private savepoint, leaving any outer transaction alone."""

import sqlite3
from contextlib import contextmanager
from typing import Iterator
from uuid import uuid4


@contextmanager
def savepoint(connection: sqlite3.Connection) -> Iterator[None]:
    """Release (commit, if nothing else is open) on success; roll back only this change on error."""
    name = "change_" + uuid4().hex
    connection.execute(f"SAVEPOINT {name}")
    try:
        yield
        connection.execute(f"RELEASE {name}")
    except Exception:
        if connection.in_transaction:
            connection.execute(f"ROLLBACK TO {name}")
            connection.execute(f"RELEASE {name}")
        raise
