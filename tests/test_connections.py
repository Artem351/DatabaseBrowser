"""Tests for UC-01, UC-16, and UC-17 using temporary SQLite files."""

import sqlite3
from contextlib import closing

import pytest

from database_browser.errors import DatabaseConnectionError, ProfileNotFoundError, ProfileStorageError
from database_browser.models import ConnectionProfile


def profile(path, name="Demo", identifier=None):
    """Keep input profile creation short in tests."""
    return ConnectionProfile(identifier, name, str(path))


def test_uc01_connects_and_saves_profile(manager, database_factory):
    """A valid database opens, its profile is saved, and its data is available."""
    path = database_factory()
    saved = manager.create_connection(profile(path))
    assert saved.id is not None
    assert manager.list_connections() == [saved]
    assert manager.active_connection.profile == saved
    assert manager.executor.connection.execute("SELECT value FROM sample").fetchone() == ("original",)


def test_uc01_reuses_saved_profile(manager, database_factory):
    """A saved profile can be opened again after disconnecting."""
    saved = manager.create_connection(profile(database_factory()))
    manager.disconnect()
    assert manager.connect_saved(saved.id) == saved
    assert len(manager.list_connections()) == 1


def test_uc01_same_file_does_not_create_duplicate(manager, database_factory):
    """Opening the same file again does not add a second profile."""
    path = database_factory()
    first = manager.create_connection(profile(path))
    second = manager.create_connection(profile(path, "Another name"))
    assert first == second
    assert manager.list_connections() == [first]


# Run the same check for five types of invalid files.
@pytest.mark.parametrize("kind", ["missing", "text", "corrupt", "directory", "empty"])
def test_uc01_rejects_invalid_files_and_keeps_existing_connection(manager, database_factory, tmp_path, kind):
    """An invalid file must not disrupt the current connection."""
    original = manager.create_connection(profile(database_factory()))
    bad = tmp_path / "invalid.sqlite3"
    if kind == "text":
        bad.write_text("This is not a database")
    elif kind == "corrupt":
        bad.write_bytes(b"SQLite format 3\x00" + b"broken" * 100)
    elif kind == "directory":
        bad.mkdir()
    elif kind == "empty":
        bad.touch()
    with pytest.raises(DatabaseConnectionError):
        manager.create_connection(profile(bad))
    assert manager.active_connection.profile == original
    assert manager.list_connections() == [original]
    if kind == "missing":
        assert not bad.exists()


def test_uc01_supports_unicode_and_uri_characters(manager, database_factory):
    """Cyrillic letters, spaces, and special characters in paths are supported."""
    path = database_factory("База данных #1 % тест.sqlite3")
    saved = manager.create_connection(profile(path, "Учебная база"))
    assert saved.name == "Учебная база"
    assert manager.active_connection.file_path == str(path.resolve())


def test_uc01_storage_error_keeps_previous_connection(manager, database_factory, monkeypatch):
    """The previous database stays open if saving the new profile fails."""
    original = manager.create_connection(profile(database_factory()))

    def fail(_):
        """Simulate profile storage that cannot be written to."""
        raise ProfileStorageError("storage is read-only")

    # This reproduces the error on Windows and Linux regardless of directory permissions.
    monkeypatch.setattr(manager.repository, "save", fail)
    with pytest.raises(ProfileStorageError):
        manager.create_connection(profile(database_factory("second.sqlite3")))
    assert manager.active_connection.profile == original
    assert manager.executor.connection.execute("SELECT count(*) FROM sample").fetchone() == (1,)


def test_uc01_profiles_survive_repository_restart(manager, database_factory):
    """Profiles stay on disk when a new repository object is created."""
    from database_browser.repositories import SQLiteConnectionProfileRepository
    saved = manager.create_connection(profile(database_factory()))
    reopened = SQLiteConnectionProfileRepository(manager.repository.storage_path)
    assert reopened.find_by_id(saved.id) == saved


def test_uc01_read_only_connection_is_supported(manager, database_factory):
    """A read-only connection allows SELECT but rejects DELETE."""
    path = database_factory()
    manager.executor.connect({"file_path": str(path), "read_only": True})
    assert manager.executor.read_only is True
    assert manager.executor.connection.execute("SELECT count(*) FROM sample").fetchone() == (1,)
    with pytest.raises(sqlite3.OperationalError):
        manager.executor.connection.execute("DELETE FROM sample")
    # sqlite3 may still consider the transaction open after a failed write.
    manager.executor.connection.rollback()


def test_uc01_does_not_allow_opening_profile_storage_as_user_database(manager):
    """The profile settings database cannot be opened as a user database."""
    with pytest.raises(DatabaseConnectionError):
        manager.create_connection(profile(manager.repository.storage_path))


def test_uc16_updates_saved_path_after_validation(manager, database_factory):
    """The validated path and name are saved in the same profile."""
    first = manager.create_connection(profile(database_factory()))
    manager.disconnect()
    second = database_factory("second.sqlite3")
    changed = manager.edit_connection(profile(second, "Changed", first.id))
    assert changed.file_path == str(second.resolve())
    assert manager.repository.find_by_id(first.id) == changed


def test_uc16_invalid_new_path_leaves_profile_and_session_unchanged(manager, database_factory, tmp_path):
    """An invalid new path changes neither the profile nor the current session."""
    saved = manager.create_connection(profile(database_factory()))
    with pytest.raises(DatabaseConnectionError):
        manager.edit_connection(profile(tmp_path / "missing.db", "Changed", saved.id))
    assert manager.repository.find_by_id(saved.id) == saved
    assert manager.active_connection.profile == saved


def test_uc16_active_session_stays_on_original_file_until_reconnect(manager, database_factory):
    """A saved path change takes effect after reconnecting."""
    old = manager.create_connection(profile(database_factory()))
    new_path = database_factory("second.sqlite3")
    changed = manager.edit_connection(profile(new_path, "New profile", old.id))
    assert manager.active_connection.profile == old
    assert manager.active_connection.file_path == old.file_path
    manager.connect_saved(changed.id)
    assert manager.active_connection.profile == changed


def test_uc16_duplicate_path_is_rejected(manager, database_factory):
    """Two different profiles cannot have the same saved path."""
    first = manager.create_connection(profile(database_factory()))
    second = manager.create_connection(profile(database_factory("second.sqlite3")))
    with pytest.raises(ProfileStorageError):
        manager.edit_connection(profile(first.file_path, "Duplicate", second.id))
    assert manager.repository.find_by_id(second.id) == second


def test_uc16_update_failure_preserves_profile_and_session(manager, database_factory, monkeypatch):
    """A failed update keeps the previous settings."""
    saved = manager.create_connection(profile(database_factory()))

    def fail(_):
        """Simulate a failed profile update on disk."""
        raise ProfileStorageError("cannot update")

    monkeypatch.setattr(manager.repository, "update", fail)
    with pytest.raises(ProfileStorageError):
        manager.edit_connection(profile(database_factory("new.sqlite3"), "New", saved.id))
    assert manager.repository.find_by_id(saved.id) == saved
    assert manager.active_connection.profile == saved


def test_uc17_deletes_active_profile_but_keeps_database(manager, database_factory):
    """Deleting the active profile closes the connection but keeps the file and data."""
    path = database_factory()
    saved = manager.create_connection(profile(path))
    manager.delete_connection(saved.id)
    assert manager.list_connections() == []
    assert manager.active_connection is None
    # Check both the file and the data stored inside it.
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT value FROM sample").fetchone() == ("original",)


def test_uc17_deleting_inactive_profile_keeps_other_connection(manager, database_factory):
    """Deleting another profile does not close the current database."""
    first = manager.create_connection(profile(database_factory()))
    second = manager.create_connection(profile(database_factory("second.sqlite3")))
    manager.delete_connection(first.id)
    assert manager.active_connection.profile == second
    assert manager.list_connections() == [second]


def test_uc17_close_failure_does_not_delete_profile(manager, database_factory, monkeypatch):
    """The profile must remain if closing the connection fails."""
    saved = manager.create_connection(profile(database_factory()))

    def fail_close():
        """Simulate a connection closing error."""
        raise DatabaseConnectionError("cannot close")

    with monkeypatch.context() as context:
        context.setattr(manager.executor, "disconnect", fail_close)
        with pytest.raises(DatabaseConnectionError):
            manager.delete_connection(saved.id)
        assert manager.repository.find_by_id(saved.id) == saved
        assert manager.active_connection.profile == saved


def test_uc17_delete_failure_reports_disconnected_actual_state(manager, database_factory, monkeypatch):
    """A failed record deletion must not hide that the connection is already closed."""
    saved = manager.create_connection(profile(database_factory()))

    def fail(_):
        """Simulate a failed profile deletion from storage."""
        raise ProfileStorageError("cannot delete profile")

    monkeypatch.setattr(manager.repository, "delete", fail)
    with pytest.raises(ProfileStorageError):
        manager.delete_connection(saved.id)
    assert manager.repository.find_by_id(saved.id) == saved
    assert manager.active_connection is None


@pytest.mark.parametrize("operation", ["connect_saved", "delete_connection"])
def test_unknown_profile_has_clear_error(manager, operation):
    """An unknown ID produces a clear error."""
    with pytest.raises(ProfileNotFoundError):
        getattr(manager, operation)(999)


def test_uc17_pending_transaction_prevents_silent_data_loss(manager, database_factory):
    """A connection with uncommitted changes cannot be closed."""
    saved = manager.create_connection(profile(database_factory()))
    manager.executor.connection.execute("UPDATE sample SET value = 'pending'")
    with pytest.raises(DatabaseConnectionError, match="transaction"):
        manager.delete_connection(saved.id)
    assert manager.repository.find_by_id(saved.id) == saved
    assert manager.active_connection.profile == saved
    manager.executor.connection.rollback()
