"""Check the exported script against SQLite and exercise the save dialog flow"""

import sqlite3
from contextlib import closing

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFileDialog, QMessageBox

from database_browser.controller import ConnectionController
from database_browser.query_executor import QueryExecutor
from database_browser.schema import export
from database_browser.schema.export import SchemaExportError, export_schema
from database_browser.schema.export_ui import OP_EXPORT
from database_browser.ui import MainWindow


def schema_objects(connection):
    return connection.execute(
        "SELECT type, name, sql FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*' ORDER BY name"
    ).fetchall()


def test_export_restores_definitions_without_data(tmp_path):
    source = tmp_path / "source.sqlite3"
    output = tmp_path / "схема.sql"
    with closing(sqlite3.connect(source)) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE authors(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
            CREATE TABLE "книги"(
                title TEXT NOT NULL CHECK (length(title) > 0),
                author_id INTEGER REFERENCES authors(id),
                normalized TEXT GENERATED ALWAYS AS (lower(title)) VIRTUAL
            ) STRICT;
            CREATE INDEX book_titles ON "книги"(lower(title) DESC) WHERE author_id > 0;
            CREATE VIEW "каталог" AS SELECT title, name FROM "книги" JOIN authors ON authors.id = author_id;
            CREATE VIEW titles AS SELECT title FROM "каталог";
            INSERT INTO authors(name) VALUES ('Author row data');
            INSERT INTO "книги"(title, author_id) VALUES ('Book row data', 1);
            ANALYZE;
            CREATE TEMP TABLE scratch(note TEXT);
            ATTACH DATABASE ':memory:' AS extra;
            CREATE TABLE extra.unrelated(note TEXT);
            """
        )
        expected = schema_objects(connection)
        assert export_schema(connection, output) == output
        assert schema_objects(connection) == expected
        assert connection.execute('SELECT title FROM "книги"').fetchall() == [("Book row data",)]
        assert not connection.in_transaction

    script = output.read_text(encoding="utf-8")
    assert "Book row data" not in script and "Author row data" not in script
    assert "sqlite_sequence" not in script and "sqlite_stat1" not in script
    with closing(sqlite3.connect(":memory:")) as restored:
        restored.execute("PRAGMA foreign_keys = ON")
        restored.executescript(script)
        assert schema_objects(restored) == expected
        assert restored.execute("SELECT * FROM titles").fetchall() == []
        assert restored.execute("SELECT * FROM authors").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            restored.execute('INSERT INTO "книги"(title, author_id) VALUES (\'orphan\', 99)')
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            restored.execute('INSERT INTO "книги"(title) VALUES (\'\')')


def test_export_recreates_virtual_table_without_its_shadow_tables(tmp_path):
    output = tmp_path / "schema.sql"
    with closing(sqlite3.connect(":memory:")) as connection:
        try:
            connection.execute("CREATE VIRTUAL TABLE articles USING fts5(body)")
        except sqlite3.OperationalError as error:
            if "no such module" in str(error):
                pytest.skip("This SQLite build has no FTS5 module")
            raise
        connection.execute("INSERT INTO articles VALUES ('indexed row')")
        export_schema(connection, output)

    script = output.read_text(encoding="utf-8")
    assert "articles_data" not in script and "indexed row" not in script
    with closing(sqlite3.connect(":memory:")) as restored:
        restored.executescript(script)
        assert restored.execute("SELECT * FROM articles").fetchall() == []


@pytest.mark.parametrize("comment", ["-- last comment", "/* comment to the end of the statement"])
def test_export_separates_definitions_after_a_trailing_comment(tmp_path, comment):
    output = tmp_path / "schema.sql"
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute(f"CREATE VIEW a AS SELECT 7 AS value {comment}")
        connection.execute("CREATE VIEW b AS SELECT value FROM a")
        export_schema(connection, output)
    with closing(sqlite3.connect(":memory:")) as restored:
        restored.executescript(output.read_text(encoding="utf-8"))
        assert restored.execute("SELECT value FROM b").fetchone() == (7,)


def test_export_keeps_an_open_transaction(tmp_path):
    source = tmp_path / "source.sqlite3"
    output = tmp_path / "schema.sql"
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("BEGIN")
        connection.execute("CREATE TABLE pending(value TEXT)")
        export_schema(connection, output)
        assert connection.in_transaction
        assert "CREATE TABLE pending" in output.read_text(encoding="utf-8")
        connection.rollback()
        assert not schema_objects(connection)


def test_export_empty_database_creates_a_valid_script(tmp_path):
    output = tmp_path / "empty.sql"
    with closing(sqlite3.connect(":memory:")) as connection:
        export_schema(connection, output)
        connection.executescript(output.read_text(encoding="utf-8"))
        assert not schema_objects(connection)


def test_failed_schema_read_keeps_the_old_file(tmp_path):
    source = tmp_path / "source.sqlite3"
    output = tmp_path / "schema.sql"
    output.write_text("-- previous script", encoding="utf-8")
    with closing(sqlite3.connect(source)) as connection, closing(sqlite3.connect(source, timeout=0)) as reader:
        connection.execute("CREATE TABLE t(value TEXT)")
        connection.execute("BEGIN EXCLUSIVE")
        with pytest.raises(SchemaExportError, match="Cannot read the SQLite schema"):
            export_schema(reader, output, overwrite=True)
        assert not reader.in_transaction
    assert output.read_text(encoding="utf-8") == "-- previous script"
    assert not list(tmp_path.glob(".schema-*.tmp"))


def test_export_will_not_replace_a_file_without_confirmation(tmp_path):
    output = tmp_path / "schema.sql"
    output.write_text("-- previous script", encoding="utf-8")
    with closing(sqlite3.connect(":memory:")) as connection:
        with pytest.raises(SchemaExportError, match="already exists"):
            export_schema(connection, output)
    assert output.read_text(encoding="utf-8") == "-- previous script"
    assert not list(tmp_path.glob(".schema-*.tmp"))


def test_failed_write_keeps_the_old_file(tmp_path, monkeypatch):
    output = tmp_path / "schema.sql"
    output.write_text("-- previous script", encoding="utf-8")

    def fail_flush(_):
        raise OSError("disk full")

    monkeypatch.setattr(export.os, "fsync", fail_flush)
    with closing(sqlite3.connect(":memory:")) as connection:
        with pytest.raises(SchemaExportError, match="disk full"):
            export_schema(connection, output, overwrite=True)
    assert output.read_text(encoding="utf-8") == "-- previous script"
    assert not list(tmp_path.glob(".schema-*.tmp"))


def test_export_refuses_to_replace_an_open_database(tmp_path):
    source = tmp_path / "database.sql"
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("CREATE TABLE t(value TEXT)")
        before = source.read_bytes()
        with pytest.raises(SchemaExportError, match="destination is an open database"):
            export_schema(connection, source, overwrite=True)
        assert source.read_bytes() == before


@pytest.fixture
def window(qtbot, tmp_path):
    controller = ConnectionController(tmp_path / "settings.sqlite3")
    widget = MainWindow(controller)
    qtbot.addWidget(widget)
    widget.show()
    widget.tab_widget.setCurrentWidget(widget.schema_tab)
    widget.schema_tab.sections.setCurrentWidget(widget.schema_tab.export_tab)
    yield widget
    with qtbot.waitSignal(controller.shutdown_complete, timeout=5000):
        widget.close()


def open_database(qtbot, window, path):
    controller = window.controller
    with qtbot.waitSignal(
        controller.operation_finished, timeout=5000,
        check_params_cb=lambda name, ok: name == "connect" and ok,
    ):
        controller.create("Export example", str(path))
    qtbot.waitUntil(window.schema_tab.export_tab.btn_export.isEnabled, timeout=5000)


def save_script(qtbot, window):
    with qtbot.waitSignal(
        window.controller.operation_finished, timeout=5000,
        check_params_cb=lambda name, ok: name == OP_EXPORT,
    ) as result:
        qtbot.mouseClick(window.schema_tab.export_tab.btn_export, Qt.MouseButton.LeftButton)
    return result.args[1]


@pytest.mark.parametrize("read_only", [False, True])
def test_export_from_schema_tab(qtbot, window, database_factory, tmp_path, monkeypatch, read_only):
    path = database_factory()
    prepare = QueryExecutor.prepare_connection

    def open_with_mode(executor, file_path):
        return prepare(executor, file_path, read_only=read_only)

    monkeypatch.setattr(QueryExecutor, "prepare_connection", open_with_mode)
    tab = window.schema_tab.export_tab
    assert not tab.btn_export.isEnabled()
    open_database(qtbot, window, path)
    assert window.controller.active.read_only == read_only
    output = tmp_path / "снимок.sql"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(output.with_suffix("")), ""))
    assert save_script(qtbot, window)
    assert "CREATE TABLE sample" in output.read_text(encoding="utf-8")
    assert str(output) in tab.message_label.text()


def test_cancel_save_does_not_submit_export(qtbot, window, database_factory, tmp_path, monkeypatch):
    open_database(qtbot, window, database_factory())
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: ("", ""))
    qtbot.mouseClick(window.schema_tab.export_tab.btn_export, Qt.MouseButton.LeftButton)
    assert not window.controller.busy
    assert not list(tmp_path.glob("*.sql"))


@pytest.mark.parametrize("confirm", [False, True])
def test_save_dialog_confirms_replacement(qtbot, window, database_factory, tmp_path, monkeypatch, confirm):
    open_database(qtbot, window, database_factory())
    output = tmp_path / "schema.sql"
    output.write_text("-- previous script", encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(output), ""))
    prompts = []

    def answer(parent, title, message, buttons, default):
        prompts.append(message)
        assert default == QMessageBox.StandardButton.No
        return QMessageBox.StandardButton.Yes if confirm else QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", answer)
    if confirm:
        assert save_script(qtbot, window)
        assert "CREATE TABLE sample" in output.read_text(encoding="utf-8")
    else:
        qtbot.mouseClick(window.schema_tab.export_tab.btn_export, Qt.MouseButton.LeftButton)
        assert not window.controller.busy
        assert output.read_text(encoding="utf-8") == "-- previous script"
    assert len(prompts) == 1 and str(output) in prompts[0]


def test_save_error_is_shown_in_export_tab(qtbot, window, database_factory, tmp_path, monkeypatch):
    open_database(qtbot, window, database_factory())
    output = tmp_path / "missing" / "schema.sql"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(output), ""))
    assert not save_script(qtbot, window)
    assert "Cannot save the schema" in window.schema_tab.export_tab.message_label.text()
    assert "Cannot save the schema" not in window.schema_tab.message_label.text()
    assert not output.exists()
