"""Errors that the index editor can explain to the user."""

from ..errors import DatabaseBrowserError


class IndexOperationError(DatabaseBrowserError):
    """The requested index operation could not be completed."""


class TableOperationError(DatabaseBrowserError):
    """The requested table operation could not be completed."""


class ViewOperationError(DatabaseBrowserError):
    """The requested view operation could not be completed."""
