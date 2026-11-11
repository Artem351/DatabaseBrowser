"""Errors from file checks, connections, and profile storage."""

class DatabaseBrowserError(Exception):
    """Base application error that can be shown to the user."""


class DatabaseConnectionError(DatabaseBrowserError):
    """Failed to validate, open, or close a database."""


class ProfileStorageError(DatabaseBrowserError):
    """Failed to read or modify saved profiles."""


class ProfileNotFoundError(DatabaseBrowserError):
    """No profile exists with the given ID."""

