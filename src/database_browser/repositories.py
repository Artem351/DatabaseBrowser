"""Read and save profiles in a separate SQLite settings database."""

import sqlite3
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from .errors import ProfileNotFoundError, ProfileStorageError
from .models import ConnectionProfile


class IConnectionProfileRepository(ABC):
    """Profile storage interface from the class diagram."""

    @abstractmethod
    def find_all(self) -> list[ConnectionProfile]:
        """Return all saved profiles."""
        ...

    @abstractmethod
    def find_by_id(self, id: int) -> ConnectionProfile:
        """Find a profile by ID or report that it is missing."""
        ...

    @abstractmethod
    def save(self, profile: ConnectionProfile) -> ConnectionProfile:
        """Save a new profile and return it with its assigned ID."""
        ...

    @abstractmethod
    def update(self, profile: ConnectionProfile) -> ConnectionProfile:
        """Update an existing profile."""
        ...

    @abstractmethod
    def delete(self, profile_id: int) -> None:
        """Delete a profile record by ID."""
        ...


class SQLiteConnectionProfileRepository(IConnectionProfileRepository):
    """Store profiles separately from user databases."""

    def __init__(self, storage_path: str | Path):
        """Create the storage directory and profile table on first use."""
        self.storage_path = Path(storage_path).expanduser().resolve()
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ProfileStorageError(f"Cannot create profile directory: {error}") from error
        with self._connection() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS connection_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    file_path TEXT NOT NULL UNIQUE,
                    dbms_type TEXT NOT NULL DEFAULT 'sqlite'
                )
            """)

    @contextmanager
    def _connection(self):
        """Open the settings database for one operation and close it afterwards."""
        connection = None
        try:
            connection = sqlite3.connect(self.storage_path, timeout=2)
            connection.row_factory = sqlite3.Row
            # This context commits on success and rolls back on failure.
            with connection:
                yield connection
        except sqlite3.Error as error:
            raise ProfileStorageError(f"Cannot access saved connections: {error}") from error
        finally:
            if connection is not None:
                # The sqlite3 context itself does not close the connection.
                connection.close()

    @staticmethod
    def _profile(row: sqlite3.Row) -> ConnectionProfile:
        """Convert a database row to a ConnectionProfile."""
        return ConnectionProfile(row["id"], row["name"], row["file_path"], row["dbms_type"])

    def find_all(self) -> list[ConnectionProfile]:
        """Read profiles in the order they were added."""
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM connection_profiles ORDER BY id").fetchall()
            return [self._profile(row) for row in rows]

    def find_by_id(self, id: int) -> ConnectionProfile:
        """Read one profile by ID."""
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM connection_profiles WHERE id = ?", (id,)).fetchone()
            if row is None:
                raise ProfileNotFoundError(f"Connection profile {id} was not found.")
            return self._profile(row)

    def save(self, profile: ConnectionProfile) -> ConnectionProfile:
        """Insert a profile and get the new row ID."""
        with self._connection() as connection:
            # Pass values separately from SQL, including names containing quotes.
            cursor = connection.execute(
                "INSERT INTO connection_profiles(name, file_path, dbms_type) VALUES (?, ?, ?)",
                (profile.name, profile.file_path, profile.dbms_type),
            )
            return replace(profile, id=cursor.lastrowid)

    def update(self, profile: ConnectionProfile) -> ConnectionProfile:
        """Save new profile settings while keeping the same ID."""
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE connection_profiles SET name = ?, file_path = ?, dbms_type = ? WHERE id = ?",
                (profile.name, profile.file_path, profile.dbms_type, profile.id),
            )
            if cursor.rowcount != 1:
                raise ProfileNotFoundError(f"Connection profile {profile.id} was not found.")
        return profile

    def delete(self, profile_id: int) -> None:
        """Delete only the record in the settings table."""
        with self._connection() as connection:
            cursor = connection.execute("DELETE FROM connection_profiles WHERE id = ?", (profile_id,))
            if cursor.rowcount != 1:
                raise ProfileNotFoundError(f"Connection profile {profile_id} was not found.")

