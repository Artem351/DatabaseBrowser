"""Connect the Qt interface to the connection logic."""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from .background import BackgroundTaskManager
from .errors import DatabaseBrowserError
from .models import ActiveConnection, ConnectionProfile
from .repositories import SQLiteConnectionProfileRepository
from .services import ConnectionManager


@dataclass
class _Completion:
    """Task result and connection state after the task finishes."""

    operation: str
    profiles: list[ConnectionProfile] | None
    active: ActiveConnection | None
    result: object = None
    error: str | None = None


class ConnectionController(QObject):
    """Accept UI actions and return results through signals."""

    profiles_changed = pyqtSignal(object)
    connection_changed = pyqtSignal(object)
    busy_changed = pyqtSignal(bool)
    error_occurred = pyqtSignal(str)
    operation_failed = pyqtSignal(str, str)
    operation_finished = pyqtSignal(str, bool)
    operation_result = pyqtSignal(str, object)
    shutdown_complete = pyqtSignal()
    # The signal delivers the result to Qt, where widgets can be updated.
    _completed = pyqtSignal(object)

    def __init__(self, storage_path: str | Path, parent=None):
        """Set up the task queue and initial UI state."""
        super().__init__(parent)
        self.storage_path = Path(storage_path)
        self.tasks = BackgroundTaskManager()
        self.profiles: list[ConnectionProfile] = []
        self.active: ActiveConnection | None = None
        self.busy = False
        self._manager: ConnectionManager | None = None
        # A shutdown request and a completed shutdown are separate states.
        self._closing = False
        self._closed = False
        self._completed.connect(self._on_completed)

    def _get_manager(self) -> ConnectionManager:
        """Create the manager in the worker when the first task starts."""
        if self._manager is None:
            self._manager = ConnectionManager(SQLiteConnectionProfileRepository(self.storage_path))
        return self._manager

    def submit_database_operation(self, name: str, operation: Callable[[ConnectionManager], object]) -> bool:
        """Start an operation; return False if the controller is busy or closing."""
        # Do not accept another action on the same connection while a task is running.
        if self.busy or self._closed or (self._closing and name != "shutdown"):
            return False
        self.busy = True
        self.busy_changed.emit(True)

        def run():
            """Run the task and collect the actual state even if it fails."""
            result = None
            error = None
            profiles = None
            manager = None
            try:
                # Do not try to reopen corrupt profile storage during shutdown.
                manager = self._manager if name == "shutdown" else self._get_manager()
                if manager is not None:
                    result = operation(manager)
            except DatabaseBrowserError as exception:
                error = str(exception)
            except Exception as exception:
                logging.getLogger(__name__).exception("Database operation failed")
                error = f"Unexpected error: {exception}"
            if manager is not None and name != "shutdown" and not name.startswith("schema."):
                # Schema operations cannot change profiles. An unrelated settings error
                # must not hide a successfully saved index from the interface.
                # For example, the connection may have closed even if deleting the profile failed.
                try:
                    profiles = manager.list_connections()
                except DatabaseBrowserError as exception:
                    error = f"{error + ' ' if error else ''}Cannot refresh saved connections: {exception}"
            return _Completion(name, profiles, manager.active_connection if manager else None, result, error)

        future = self.tasks.submit_task(run)
        # The Future callback does not access widgets directly.
        future.add_done_callback(lambda completed: self._completed.emit(completed.result()))
        return True

    def refresh(self):
        """Load the list of saved connections."""
        return self.submit_database_operation("refresh", lambda manager: None)

    def create(self, name: str, file_path: str):
        """Open a new file and save its profile."""
        return self.submit_database_operation("connect", lambda manager: manager.create_connection(ConnectionProfile(None, name, file_path)))

    def connect_saved(self, profile_id: int):
        """Open a database using a saved profile."""
        return self.submit_database_operation("connect", lambda manager: manager.connect_saved(profile_id))

    def edit(self, profile: ConnectionProfile):
        """Pass the edited profile for validation and saving."""
        return self.submit_database_operation("edit", lambda manager: manager.edit_connection(profile))

    def delete(self, profile_id: int):
        """Delete the selected profile through the manager."""
        return self.submit_database_operation("delete", lambda manager: manager.delete_connection(profile_id))

    @pyqtSlot(object)
    def _on_completed(self, completion: _Completion):
        """Update state and emit signals in the UI thread."""
        if completion.profiles is not None:
            self.profiles = completion.profiles
            self.profiles_changed.emit(self.profiles)
        self.active = completion.active
        self.connection_changed.emit(self.active)
        success = completion.error is None
        if completion.operation == "shutdown" and not success:
            # Restore actions before the UI receives the idle state.
            self._closing = False
        self.busy = False
        self.busy_changed.emit(False)
        if completion.error:
            self.operation_failed.emit(completion.operation, completion.error)
            self.error_occurred.emit(completion.error)
        else:
            self.operation_result.emit(completion.operation, completion.result)
        self.operation_finished.emit(completion.operation, success)
        if completion.operation == "shutdown":
            if success:
                self._closed = True
                self.tasks.shutdown(wait=False)
                self.shutdown_complete.emit()
        elif self._closing:
            self.request_shutdown()

    def request_shutdown(self):
        """Wait for the current task, then close the connection and worker."""
        if self._closed:
            return
        self._closing = True
        if not self.busy:
            self.submit_database_operation("shutdown", lambda manager: manager.close())
