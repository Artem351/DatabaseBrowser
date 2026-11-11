"""Errors that the index editor can explain to the user."""

from ..errors import DatabaseBrowserError


class IndexOperationError(DatabaseBrowserError):
    """The requested index operation could not be completed."""
