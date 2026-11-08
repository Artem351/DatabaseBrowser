"""A queue for database operations in a background thread."""

from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, TypeVar

T = TypeVar("T")


class BackgroundTaskManager:
    """Run tasks one at a time in a single background thread."""

    def __init__(self):
        """Create one worker for all operations on the open database."""
        # sqlite3.Connection must stay in the thread that uses it.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="database-browser")

    def submit_task(self, task: Callable[[], T]) -> Future[T]:
        """Queue a function and return a Future with its result."""
        return self._pool.submit(task)

    def shutdown(self, *, wait: bool = True) -> None:
        """Shut down the worker after the connection has been released."""
        self._pool.shutdown(wait=wait, cancel_futures=True)
