"""Save the definitions of the current SQLite schema as a SQL script"""

import os
import sqlite3
from pathlib import Path
from tempfile import NamedTemporaryFile

from ..errors import DatabaseBrowserError


class SchemaExportError(DatabaseBrowserError):
    """The schema could not be read or the script could not be saved"""


def _terminate(sql: str, name: str) -> str:
    #A trailing comment must not swallow the separator between statements
    for ending in ("", ";", "\n;", "*/;"):
        statement = sql + ending
        if sqlite3.complete_statement(statement):
            return statement
    raise SchemaExportError(f'Incomplete SQLite definition for "{name}"')


def _read_script(connection: sqlite3.Connection) -> str:
    #Read table kinds and DDL from the same snapshot without ending the caller's transaction
    connection.execute("SAVEPOINT schema_export")
    try:
        shadows = {
            row[1] for row in connection.execute("PRAGMA main.table_list")
            if row[2] == "shadow"
        }
        rows = connection.execute(
            """
            SELECT name, tbl_name, sql
            FROM main.sqlite_schema
            WHERE type IN ('table', 'index', 'view')
              AND sql IS NOT NULL
              AND name NOT GLOB 'sqlite_*'
            ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END, name
            """
        ).fetchall()
    finally:
        connection.execute("RELEASE schema_export")
    statements = ["-- SQLite schema"]
    statements.extend(_terminate(sql, name) for name, table, sql in rows if table not in shadows)
    return "\n\n".join(statements) + "\n"


def _same_file(destination: Path, source: Path) -> bool:
    if destination.resolve() == source.resolve():
        return True
    try:
        return destination.samefile(source)
    except FileNotFoundError:
        return False


def _write_script(destination: Path, script: str, overwrite: bool) -> None:
    temporary = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=destination.parent,
            prefix=".schema-", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(script)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            temporary.replace(destination)
        elif os.name == "nt":
            temporary.rename(destination)
        else:
            #On Unix, rename can overwrite a file created after the save dialog
            os.link(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def export_schema(connection: sqlite3.Connection, destination: Path, *, overwrite: bool = False) -> Path:
    try:
        script = _read_script(connection)
        database_paths = [Path(row[2]) for row in connection.execute("PRAGMA database_list") if row[2]]
    except sqlite3.Error as error:
        raise SchemaExportError(f"Cannot read the SQLite schema: {error}") from error

    try:
        if any(_same_file(destination, source) for source in database_paths):
            raise SchemaExportError("Choose a different file. The destination is an open database")
        _write_script(destination, script, overwrite)
    except FileExistsError as error:
        raise SchemaExportError(f'"{destination}" already exists. Choose it again to confirm replacement') from error
    except (OSError, ValueError) as error:
        raise SchemaExportError(f'Cannot save the schema to "{destination}": {error}') from error
    return destination
