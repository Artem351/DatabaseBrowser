"""Plain data for views, shared by the worker and the interface."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ViewDefinition:
    """A view as the user edits it plus facts read from the database."""

    name: str
    # The defining SELECT without the "CREATE VIEW name AS" prefix.
    query: str
    # The facts below are filled when the view is read from SQLite.
    columns: tuple[str, ...] = ()
    # Name of the stored view an edit form changes; None for a new view.
    original_name: str | None = None
    # The stored CREATE VIEW statement the form started from; used to detect a changed view.
    sql: str | None = None
    editable: bool = True
    reason: str | None = None
    # Why the view cannot be queried right now (for example, a table it reads was dropped).
    error: str | None = None
    used_by: tuple[str, ...] = ()
    triggers: tuple[str, ...] = ()


@dataclass(frozen=True)
class ViewSnapshot:
    views: tuple[ViewDefinition, ...]
    in_transaction: bool = False
