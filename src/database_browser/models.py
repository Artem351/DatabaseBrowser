"""Data for saved profiles and the current connection."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ConnectionProfile:
    """A saved database entry without the sqlite3.Connection itself."""

    # A new profile gets its ID after insertion into the settings table.
    id: int | None
    name: str
    file_path: str
    dbms_type: str = "sqlite"


@dataclass(frozen=True)
class ActiveConnection:
    """A snapshot of the active connection for the UI."""

    profile: ConnectionProfile
    file_path: str
    read_only: bool

