"""Shared fixtures for temporary databases and the connection manager."""

import sqlite3
from contextlib import closing

import pytest


@pytest.fixture
def database_factory(tmp_path):
    """Give each test a helper for creating its own small database."""
    def create(name="sample.sqlite3"):
        """Create a table with one row in the test directory."""
        path = tmp_path / name
        # closing closes the connection; the inner context commits the insert.
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT)")
            connection.execute("INSERT INTO sample(value) VALUES ('original')")
        return path

    return create


@pytest.fixture
def manager(tmp_path):
    """Create separate profile storage and close the connection after the test."""
    from database_browser.repositories import SQLiteConnectionProfileRepository
    from database_browser.services import ConnectionManager

    instance = ConnectionManager(SQLiteConnectionProfileRepository(tmp_path / "settings.sqlite3"))
    yield instance
    # This runs after the test even if an assertion raises an exception.
    instance.close()

