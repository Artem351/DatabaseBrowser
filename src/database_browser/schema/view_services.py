"""View use cases (UC-09, UC-10, UC-11), independent of Qt."""

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .errors import ViewOperationError
from .table_parser import identifiers
from .table_sql import fold_name
from .view_models import ViewDefinition, ViewSnapshot
from .view_repository import IViewRepository
from .view_sql import check_name, check_query


@contextmanager
def _database_errors() -> Iterator[None]:
    try:
        yield
    except sqlite3.Error as error:
        raise ViewOperationError(f"SQLite: {error}") from error


class ViewManager:
    def __init__(self, repository: IViewRepository):
        self.repository = repository

    def get_views(self) -> ViewSnapshot:
        with _database_errors():
            return ViewSnapshot(self.repository.find_all(), self.repository.in_transaction)

    def _snapshot(self, outer_transaction: bool) -> ViewSnapshot:
        return ViewSnapshot(self.repository.find_all(), outer_transaction)

    def _check_name_free(self, name: str, ignore: str | None = None) -> None:
        taken = self.repository.object_names()
        if ignore is not None:
            taken.discard(fold_name(ignore))
        if fold_name(name) in taken:
            raise ViewOperationError(f'A table, index or view named "{name}" already exists.')

    def _verify(self, name: str) -> tuple[str, ...]:
        """SQLite checks a view's query only when it is used, so try it before keeping the view."""
        try:
            return self.repository.columns_of(name)
        except sqlite3.Error as error:
            raise ViewOperationError(f"The query is not valid: {error}") from error

    def create_view(self, definition: ViewDefinition) -> ViewSnapshot:
        """UC-09: create the view in one savepoint, or change nothing."""
        with _database_errors():
            outer_transaction = self.repository.in_transaction
            with self.repository.atomic_change():
                name = definition.name.strip()
                check_name(name)
                query = check_query(definition.query)
                self._check_name_free(name)
                self.repository.save(name, query)
                self._verify(name)
                snapshot = self._snapshot(outer_transaction)
            return snapshot

    def drop_view(self, name: str) -> ViewSnapshot:
        """UC-11: delete the view; the tables it reads are not touched."""
        with _database_errors():
            outer_transaction = self.repository.in_transaction
            with self.repository.atomic_change():
                view = next((v for v in self.repository.find_all() if fold_name(v.name) == fold_name(name)), None)
                if view is None:
                    raise ViewOperationError(f'View "{name}" does not exist. Refresh the schema.')
                self.repository.delete(view.name)
                snapshot = self._snapshot(outer_transaction)
            return snapshot

    def alter_view(self, definition: ViewDefinition) -> ViewSnapshot:
        """UC-10: replace the name or query as a single operation; on failure keep the old view."""
        with _database_errors():
            outer_transaction = self.repository.in_transaction
            with self.repository.atomic_change():
                if definition.original_name is None:
                    raise ViewOperationError(
                        "The form does not say which view it edits. Close it and open it again."
                    )
                views = self.repository.find_all()
                old = next((v for v in views if fold_name(v.name) == fold_name(definition.original_name)), None)
                if old is None:
                    raise ViewOperationError(
                        f'View "{definition.original_name}" does not exist. Refresh the schema.'
                    )
                self._check_not_stale(old, definition)
                if not old.editable:
                    raise ViewOperationError(old.reason or "This view cannot be edited with this form.")
                name = definition.name if definition.name == old.name else definition.name.strip()
                check_name(name)
                query = check_query(definition.query)
                if name == old.name and query == old.query:
                    return self._snapshot(outer_transaction)
                renamed = fold_name(name) != fold_name(old.name)
                if renamed:
                    self._check_name_free(name, ignore=old.name)
                    self._check_rename(old, views)
                own_triggers = [(n, sql) for n, owner, sql in self.repository.triggers()
                                if fold_name(owner) == fold_name(old.name)]
                broken_before = {fold_name(v.name) for v in views if v.error}
                self.repository.delete(old.name)
                self.repository.save(name, query)
                columns = self._verify(name)
                self._check_triggers(old, columns, own_triggers)
                for _, sql in own_triggers:
                    self.repository.execute_ddl(sql)
                self._check_dependents(broken_before)
                snapshot = self._snapshot(outer_transaction)
            return snapshot

    @staticmethod
    def _check_not_stale(old: ViewDefinition, definition: ViewDefinition) -> None:
        if definition.sql is None:
            raise ViewOperationError(
                "The form does not say which version of the view it edits. Close it and open it again."
            )
        if definition.sql != old.sql:
            raise ViewOperationError(
                f'View "{old.name}" was changed after this form was opened, for example by another '
                "connection. Close the form and open it again so those changes are not lost."
            )

    def _check_rename(self, old: ViewDefinition, views: tuple[ViewDefinition, ...]) -> None:
        """SQLite does not rewrite references to a renamed view, so refuse when something has any."""
        if old.triggers:
            raise ViewOperationError(
                f'View "{old.name}" has triggers ({", ".join(old.triggers)}). Delete them first, '
                "or keep the name."
            )
        if old.used_by:
            raise ViewOperationError(
                f'View "{old.used_by[0]}" uses view "{old.name}". Change it first, or keep the name.'
            )
        for name, owner, sql in self.repository.triggers():
            if fold_name(old.name) in {fold_name(word) for word in identifiers(sql)}:
                raise ViewOperationError(
                    f'Trigger "{name}" mentions view "{old.name}". Change it first, or keep the name.'
                )

    @staticmethod
    def _check_triggers(old: ViewDefinition, columns: tuple[str, ...], triggers: list[tuple[str, str]]) -> None:
        """Triggers on the view must not lose a column they read."""
        kept = {fold_name(column) for column in columns}
        removed = [column for column in old.columns if fold_name(column) not in kept]
        for name, sql in triggers:
            words = {fold_name(word) for word in identifiers(sql)}
            for column in removed:
                if fold_name(column) in words:
                    raise ViewOperationError(
                        f'Trigger "{name}" on this view may use column "{column}", which the new query '
                        "no longer returns. Change or delete the trigger first."
                    )

    def _check_dependents(self, broken_before: set[str]) -> None:
        """A change must not leave another working view broken."""
        for view in self.repository.find_all():
            if view.error and fold_name(view.name) not in broken_before:
                raise ViewOperationError(
                    f'View "{view.name}" would stop working ({view.error}). Change or delete it first.'
                )
