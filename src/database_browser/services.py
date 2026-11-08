"""Use cases for connecting, editing, and deleting profiles."""

from dataclasses import replace
from pathlib import Path

from .errors import DatabaseConnectionError, ProfileNotFoundError
from .models import ActiveConnection, ConnectionProfile
from .query_executor import QueryExecutor
from .repositories import IConnectionProfileRepository


class ConnectionManager:
    """Implement UC-01, UC-16, and UC-17 without depending on Qt."""

    def __init__(self, repository: IConnectionProfileRepository, executor: QueryExecutor | None = None):
        """Receive the profile repository and connection handler."""
        self.repository = repository
        self.executor = executor or QueryExecutor()
        self._active_profile: ConnectionProfile | None = None

    @property
    def active_connection(self) -> ActiveConnection | None:
        """Return the actual open database state, not the latest saved profile."""
        if not self.executor.is_connected or self._active_profile is None:
            return None
        return ActiveConnection(self._active_profile, self.executor.file_path, self.executor.read_only)

    def list_connections(self) -> list[ConnectionProfile]:
        """Return the profile list for the Database tab."""
        return self.repository.find_all()

    def _prepare(self, profile: ConnectionProfile):
        """Check the DB type, reject the settings file, and validate the user file."""
        if profile.dbms_type != "sqlite":
            raise DatabaseConnectionError("Only SQLite is supported in this version.")
        storage_path = getattr(self.repository, "storage_path", None)
        if storage_path is not None:
            try:
                # Compare full paths so settings.sqlite3 cannot be selected through ../.
                if Path(profile.file_path).expanduser().resolve() == Path(storage_path).resolve():
                    raise DatabaseConnectionError("The application profile storage cannot be opened as a user database.")
            except (OSError, ValueError) as error:
                raise DatabaseConnectionError(f"Invalid database path: {error}") from error
        return self.executor.prepare_connection(profile.file_path)

    def verify_file(self, file_path: str) -> str:
        """Validate a file without switching the current connection."""
        prepared = self._prepare(ConnectionProfile(None, "", file_path))
        prepared.connection.close()
        return prepared.file_path

    def create_connection(self, profile: ConnectionProfile) -> ConnectionProfile:
        """UC-01: validate the database, save its profile, and open the connection."""
        prepared = self._prepare(profile)
        try:
            # Reuse the existing profile if this file has already been added.
            saved = next((item for item in self.repository.find_all() if item.file_path == prepared.file_path), None)
            if saved is None:
                saved = self.repository.save(replace(
                    profile, id=None, file_path=prepared.file_path,
                    name=profile.name.strip() or Path(prepared.file_path).stem,
                ))
            # Switch after saving so a storage error does not close the previous database.
            self.executor.activate(prepared)
        except Exception:
            # The new connection is not owned by the current session yet, so close it.
            prepared.connection.close()
            raise
        self._active_profile = saved
        return saved

    def connect_saved(self, profile_id: int) -> ConnectionProfile:
        """Open a saved profile and validate its file again."""
        profile = self.repository.find_by_id(profile_id)
        prepared = self._prepare(profile)
        try:
            self.executor.activate(prepared)
        except Exception:
            prepared.connection.close()
            raise
        self._active_profile = profile
        return profile

    def edit_connection(self, profile: ConnectionProfile) -> ConnectionProfile:
        """UC-16: validate the new file and update the saved settings."""
        if profile.id is None:
            raise ProfileNotFoundError("Select a saved connection to edit.")
        self.repository.find_by_id(profile.id)
        prepared = self._prepare(profile)
        prepared.connection.close()
        changed = replace(profile, file_path=prepared.file_path, name=profile.name.strip() or Path(prepared.file_path).stem)
        # Editing the record does not switch the connection that is already open.
        return self.repository.update(changed)

    def delete_connection(self, profile_id: int) -> None:
        """UC-17: close the matching connection and delete the profile record."""
        self.repository.find_by_id(profile_id)
        active = self.active_connection
        if active is not None and active.profile.id == profile_id:
            self.disconnect()
        # If deleting the record fails, the connection is already closed; report the actual state to the UI.
        self.repository.delete(profile_id)

    def disconnect(self) -> None:
        """Close the connection and clear the current session data."""
        self.executor.disconnect()
        self._active_profile = None

    def close(self) -> None:
        """Release the connection when the application exits."""
        self.disconnect()

