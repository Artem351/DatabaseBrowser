"""Errors that can be displayed in the SQL editor"""

from ..errors import DatabaseBrowserError


class QueryError(DatabaseBrowserError):
    """An SQL statement or a request for more rows failed"""


class QueryCancelled(QueryError):
    """The user stopped a database operation"""
