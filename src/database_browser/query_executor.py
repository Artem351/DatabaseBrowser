"""Open, validate, and close the user database."""

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from threading import get_ident

from .errors import DatabaseConnectionError


@dataclass
class _PreparedConnection:
    """A validated connection that is not active yet."""

    connection: sqlite3.Connection
    file_path: str
    read_only: bool


class QueryExecutor:
    """Keep the open connection and restrict access to one thread."""

    def __init__(self):
        """Start without an open database or an assigned thread."""
        self._connection: sqlite3.Connection | None = None
        self._owner_thread: int | None = None
        self.file_path: str | None = None
        self.read_only = False

    def _check_thread(self):
        """Remember the first calling thread and check later calls."""
        current = get_ident()
        if self._owner_thread is None:
            self._owner_thread = current
        elif self._owner_thread != current:
            raise DatabaseConnectionError("Database access must run in the owning worker thread.")

    @property
    def connection(self) -> sqlite3.Connection:
        """Return the connection for repositories in the same worker thread."""
        self._check_thread()
        if self._connection is None:
            raise DatabaseConnectionError("No database is connected.")
        return self._connection

    @property
    def is_connected(self) -> bool:
        """Return whether a connection is open."""
        return self._connection is not None

    def prepare_connection(self, file_path: str, *, read_only: bool = False) -> _PreparedConnection:
        """Validate a new file while keeping the current connection open."""
        self._check_thread()
        connection = None
        try:
            if not file_path.strip():
                raise DatabaseConnectionError("Select an existing SQLite database file.")
            path = Path(file_path).expanduser().resolve(strict=True)
            if not path.is_file():
                raise DatabaseConnectionError("The selected path is not a regular file.")
            # The .db extension proves nothing, so check the file header first.
            with path.open("rb") as source:
                if source.read(16) != b"SQLite format 3\x00":
                    raise DatabaseConnectionError("The file has no SQLite database header.")
            mode = "ro" if read_only else "rw"
            try:
                # The URI escapes spaces, #, and %. The rw mode never creates a missing file.
                connection = sqlite3.connect(path.as_uri() + f"?mode={mode}", uri=True, timeout=2)
            except sqlite3.OperationalError:
                if read_only:
                    raise
                # If writing is unavailable, try opening the database for reading.
                connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
                read_only = True
            # Check the whole structure; only the number of error messages is limited.
            check = connection.execute("PRAGMA integrity_check(1)").fetchall()
            if check != [("ok",)]:
                raise DatabaseConnectionError("SQLite integrity check failed: " + str(check))
            connection.execute("PRAGMA foreign_keys = ON")
            return _PreparedConnection(connection, str(path), read_only)
        except (OSError, ValueError, sqlite3.Error, DatabaseConnectionError) as error:
            if connection is not None:
                connection.close()
            if isinstance(error, DatabaseConnectionError):
                raise
            raise DatabaseConnectionError(f"Cannot open SQLite database: {error}") from error

    def activate(self, prepared: _PreparedConnection) -> None:
        """Close the old connection and activate the validated one."""
        self._check_thread()
        # If closing fails, the fields stay unchanged and the previous session remains.
        self.disconnect()
        self._connection = prepared.connection
        self.file_path = prepared.file_path
        self.read_only = prepared.read_only

    def connect(self, config: dict) -> None:
        """Open the file from the settings and switch to it."""
        prepared = self.prepare_connection(config["file_path"], read_only=config.get("read_only", False))
        try:
            self.activate(prepared)
        except Exception:
            prepared.connection.close()
            raise

    def disconnect(self) -> None:
        """Close the database if there is no pending transaction."""
        self._check_thread()
        if self._connection is not None:
            if self._connection.in_transaction:
                # An automatic rollback here could silently discard changes.
                raise DatabaseConnectionError("Finish or roll back the active transaction before disconnecting.")
            try:
                self._connection.close()
            except sqlite3.Error as error:
                raise DatabaseConnectionError(f"Cannot close database: {error}") from error
        self._connection = None
        self.file_path = None
        self.read_only = False

