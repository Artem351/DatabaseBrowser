"""UC-09, UC-10 and UC-11 against real temporary SQLite files."""

import sqlite3
from dataclasses import replace

import pytest

from database_browser.schema.errors import ViewOperationError
from database_browser.schema.view_models import ViewDefinition
from database_browser.schema.view_repository import SQLiteViewRepository
from database_browser.schema.view_services import ViewManager
from database_browser.schema.view_sql import check_name, check_query, split_view_sql


@pytest.fixture
def view_db(tmp_path):
    """Two tables, a view on a view, and a view with an INSTEAD OF trigger."""
    path = tmp_path / "views.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE authors(id INTEGER PRIMARY KEY, name TEXT NOT NULL, born INTEGER);
        CREATE TABLE books(id INTEGER PRIMARY KEY, title TEXT, author_id INTEGER, year INTEGER);
        CREATE TABLE log(msg TEXT);
        INSERT INTO authors(name, born) VALUES ('Ann', 1900), ('Bob', 1950);
        INSERT INTO books(title, author_id, year) VALUES ('Clean Code', 1, 2008), ('UML', 2, 2004);
        CREATE VIEW v_books AS
            SELECT b.id AS id, b.title AS title, b.year AS year, a.name AS author
            FROM books b JOIN authors a ON a.id = b.author_id;
        CREATE VIEW v_recent AS SELECT id, title FROM v_books WHERE year >= 2005;
        CREATE VIEW v_authors AS SELECT id, name FROM authors;
        CREATE TRIGGER tr_v_authors INSTEAD OF INSERT ON v_authors
            BEGIN INSERT INTO authors(name) VALUES (NEW.name); END;
        """
    )
    connection.commit()
    manager = ViewManager(SQLiteViewRepository(connection))
    yield path, connection, manager
    connection.rollback()
    connection.close()


def schema_state(connection):
    return connection.execute("SELECT type, name, sql FROM sqlite_schema ORDER BY type, name").fetchall()


def get(manager, name):
    return next(view for view in manager.get_views().views if view.name == name)


def editing(view, **changes):
    """What the edit form submits."""
    return replace(view, original_name=view.name, **changes)


def new_view(name="v_new", query="SELECT id FROM authors"):
    return ViewDefinition(name, query)


# ---------------------------------------------------------------- SQL helpers


@pytest.mark.parametrize("sql, query, reason", [
    ("CREATE VIEW v AS SELECT 1", "SELECT 1", None),
    ('CREATE VIEW "a as b" AS\nSELECT 1;', "SELECT 1", None),
    ("CREATE VIEW v AS WITH c AS (SELECT 1) SELECT * FROM c", "WITH c AS (SELECT 1) SELECT * FROM c", None),
    ("CREATE VIEW v(x, y) AS SELECT 1, 2", "SELECT 1, 2", "a column list after the view name"),
])
def test_stored_view_text_is_split_into_name_part_and_query(sql, query, reason):
    assert split_view_sql(sql) == (query, reason)


def test_unreadable_stored_text_is_reported():
    assert split_view_sql(None)[1] and split_view_sql("CREATE VIEW v")[1]


@pytest.mark.parametrize("query", [
    "SELECT 1", "SELECT 1;", "  select 1 ;; ", "WITH x AS (SELECT 1) SELECT * FROM x",
    "VALUES (1), (2)", "-- all rows\nSELECT 1", "SELECT ';' AS semicolon",
])
def test_check_query_accepts_select_statements(query):
    assert check_query(query)


@pytest.mark.parametrize("query, message", [
    ("", "Enter the defining"),
    ("   ;  ", "Enter the defining"),
    ("DELETE FROM books", "must be a SELECT"),
    ("INSERT INTO log VALUES (1)", "must be a SELECT"),
    ("SELECT 1; DROP TABLE books", "semicolon"),
    ("SELECT (1", "unbalanced"),
    ("SELECT 'abc", "unclosed quote"),
])
def test_check_query_rejects_everything_else(query, message):
    with pytest.raises(ViewOperationError, match=message):
        check_query(query)


@pytest.mark.parametrize("name, message", [("", "Enter a view name"), ("  ", "Enter a view name"),
                                          ("SQLite_x", "reserved"), ("a\x00b", "null")])
def test_check_name_rejects_bad_names(name, message):
    with pytest.raises(ViewOperationError, match=message):
        check_name(name)


# ---------------------------------------------------------------- reading


def test_views_are_read_with_columns_query_and_dependencies(view_db):
    _, _, manager = view_db
    books = get(manager, "v_books")
    assert books.columns == ("id", "title", "year", "author")
    assert books.query.startswith("SELECT b.id AS id")
    assert books.used_by == ("v_recent",) and books.error is None and books.editable
    assert get(manager, "v_authors").triggers == ("tr_v_authors",)
    assert get(manager, "v_recent").used_by == ()


def test_broken_and_unreadable_views_are_flagged(view_db):
    _, connection, manager = view_db
    connection.execute("CREATE VIEW v_gone AS SELECT * FROM missing_table")
    connection.execute("CREATE VIEW v_cols(x, y) AS SELECT id, name FROM authors")
    connection.commit()
    gone = get(manager, "v_gone")
    assert "missing_table" in gone.error and gone.columns == () and gone.editable
    cols = get(manager, "v_cols")
    assert not cols.editable and "column list" in cols.reason and cols.columns == ("x", "y")


# ---------------------------------------------------------------- UC-09


def test_uc09_creates_view_and_it_can_be_queried(view_db):
    path, connection, manager = view_db
    snapshot = manager.create_view(new_view("v_names", "SELECT name FROM authors ORDER BY name;"))
    assert "v_names" in [view.name for view in snapshot.views]
    created = get(manager, "v_names")
    assert created.columns == ("name",) and created.query == "SELECT name FROM authors ORDER BY name"
    assert connection.execute("SELECT * FROM v_names").fetchall() == [("Ann",), ("Bob",)]
    other = sqlite3.connect(path)  # committed, so another connection sees it
    assert other.execute("SELECT count(*) FROM sqlite_schema WHERE name = 'v_names'").fetchone() == (1,)
    other.close()


def test_uc09_trims_the_name_and_accepts_with_queries(view_db):
    _, connection, manager = view_db
    manager.create_view(new_view("  spaced  ", "WITH x AS (SELECT 1 AS one) SELECT one FROM x"))
    assert connection.execute('SELECT * FROM "spaced"').fetchall() == [(1,)]


def test_uc09_unusual_names_are_quoted(view_db):
    _, connection, manager = view_db
    manager.create_view(new_view('we"ird name'))
    assert connection.execute('SELECT count(*) FROM "we""ird name"').fetchone() == (2,)


@pytest.mark.parametrize("definition, message", [
    (new_view("  "), "Enter a view name"),
    (new_view("sqlite_mine"), "reserved"),
    (new_view("books"), "already exists"),
    (new_view("V_BOOKS"), "already exists"),
    (new_view("v_x", ""), "Enter the defining"),
    (new_view("v_x", "DELETE FROM books"), "must be a SELECT"),
    (new_view("v_x", "SELECT 1; DROP TABLE books"), "semicolon"),
    (new_view("v_x", "SELECT nope FROM books"), "The query is not valid: no such column: nope"),
    (new_view("v_x", "SELECT * FROM missing_table"), "The query is not valid: no such table"),
    (new_view("v_x", "SELECT 1 FROM"), "SQLite: incomplete input"),  # a syntax error stops CREATE VIEW itself
])
def test_uc09_invalid_definitions_are_rejected_without_changes(view_db, definition, message):
    _, connection, manager = view_db
    before = schema_state(connection)
    with pytest.raises(ViewOperationError, match=message):
        manager.create_view(definition)
    assert schema_state(connection) == before
    assert not connection.in_transaction


def test_uc09_name_shared_with_an_index_is_refused(view_db):
    _, connection, manager = view_db
    connection.execute("CREATE INDEX ix_books_title ON books(title)")
    connection.commit()
    with pytest.raises(ViewOperationError, match="already exists"):
        manager.create_view(new_view("ix_books_title"))


def test_uc09_read_only_database_is_reported(view_db):
    path, _, _ = view_db
    read_only = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        with pytest.raises(ViewOperationError, match="SQLite:"):
            ViewManager(SQLiteViewRepository(read_only)).create_view(new_view())
    finally:
        read_only.close()


def test_uc09_keeps_the_callers_transaction_open(view_db):
    _, connection, manager = view_db
    connection.execute("UPDATE authors SET born = 1 WHERE id = 1")
    assert manager.create_view(new_view("v_one")).in_transaction and connection.in_transaction
    with pytest.raises(ViewOperationError):
        manager.create_view(new_view("v_one"))  # a failed create undoes only itself
    assert connection.execute("SELECT born FROM authors WHERE id = 1").fetchone() == (1,)
    connection.rollback()
    assert connection.execute("SELECT born FROM authors WHERE id = 1").fetchone() == (1900,)
    assert "v_one" not in [view.name for view in manager.get_views().views]


# ---------------------------------------------------------------- UC-11


def test_uc11_drops_the_view_and_keeps_source_data(view_db):
    _, connection, manager = view_db
    snapshot = manager.drop_view("v_recent")
    assert "v_recent" not in [view.name for view in snapshot.views]
    assert connection.execute("SELECT count(*) FROM books").fetchone() == (2,)
    assert connection.execute("SELECT count(*) FROM v_books").fetchone() == (2,)


def test_uc11_name_match_ignores_ascii_case(view_db):
    _, _, manager = view_db
    manager.drop_view("V_RECENT")
    assert "v_recent" not in [view.name for view in manager.get_views().views]


def test_uc11_drops_the_triggers_of_the_view_with_it(view_db):
    _, connection, manager = view_db
    manager.drop_view("v_authors")
    assert connection.execute("SELECT count(*) FROM sqlite_schema WHERE name = 'tr_v_authors'").fetchone() == (0,)


def test_uc11_views_that_used_the_deleted_view_are_reported_broken(view_db):
    _, _, manager = view_db
    manager.drop_view("v_books")
    assert "v_books" in get(manager, "v_recent").error


def test_uc11_reports_missing_views(view_db):
    _, connection, manager = view_db
    before = schema_state(connection)
    with pytest.raises(ViewOperationError, match="does not exist"):
        manager.drop_view("ghost")
    with pytest.raises(ViewOperationError, match="does not exist"):
        manager.drop_view("books")  # a table is not a view
    assert schema_state(connection) == before


def test_uc11_inside_a_transaction_can_be_rolled_back(view_db):
    _, connection, manager = view_db
    connection.execute("UPDATE authors SET born = 1 WHERE id = 1")
    assert manager.drop_view("v_recent").in_transaction
    connection.rollback()
    assert "v_recent" in [view.name for view in manager.get_views().views]


# ---------------------------------------------------------------- UC-10


def test_uc10_changes_the_query_and_dependents_keep_working(view_db):
    _, connection, manager = view_db
    books = editing(get(manager, "v_books"))
    snapshot = manager.alter_view(replace(books, query="SELECT id, title, year, author_id FROM books"))
    changed = get(manager, "v_books")
    assert changed.columns == ("id", "title", "year", "author_id")
    assert connection.execute("SELECT * FROM v_recent").fetchall() == [(1, "Clean Code")]
    assert not snapshot.in_transaction


def test_uc10_renames_a_view_nothing_depends_on(view_db):
    _, connection, manager = view_db
    manager.alter_view(replace(editing(get(manager, "v_recent")), name="v_new_books"))
    names = [view.name for view in manager.get_views().views]
    assert "v_new_books" in names and "v_recent" not in names
    assert connection.execute("SELECT * FROM v_new_books").fetchall() == [(1, "Clean Code")]


def test_uc10_changes_name_and_query_together(view_db):
    _, connection, manager = view_db
    manager.alter_view(replace(editing(get(manager, "v_recent")), name="v_old", query="SELECT id FROM books"))
    assert connection.execute("SELECT * FROM v_old ORDER BY id").fetchall() == [(1,), (2,)]


def test_uc10_case_only_rename_is_allowed_even_when_others_use_the_view(view_db):
    _, connection, manager = view_db
    manager.alter_view(replace(editing(get(manager, "v_books")), name="V_BOOKS"))
    assert "V_BOOKS" in [view.name for view in manager.get_views().views]
    assert connection.execute("SELECT * FROM v_recent").fetchall() == [(1, "Clean Code")]


def test_uc10_unchanged_form_changes_nothing(view_db):
    _, connection, manager = view_db
    before = schema_state(connection)
    snapshot = manager.alter_view(editing(get(manager, "v_books")))
    assert schema_state(connection) == before
    assert "v_books" in [view.name for view in snapshot.views]


def test_uc10_rejected_definition_keeps_the_old_view(view_db):
    _, connection, manager = view_db
    before = schema_state(connection)
    books = editing(get(manager, "v_books"))
    for query, message in [("SELECT nope FROM books", "no such column: nope"),
                           ("SELECT * FROM missing_table", "no such table"),
                           ("DELETE FROM books", "must be a SELECT")]:
        with pytest.raises(ViewOperationError, match=message):
            manager.alter_view(replace(books, query=query))
        assert schema_state(connection) == before
    assert connection.execute("SELECT count(*) FROM v_books").fetchone() == (2,)
    assert not connection.in_transaction


def test_uc10_change_that_breaks_another_view_is_refused(view_db):
    _, connection, manager = view_db
    before = schema_state(connection)
    books = editing(get(manager, "v_books"))
    with pytest.raises(ViewOperationError, match='View "v_recent" would stop working'):
        manager.alter_view(replace(books, query="SELECT id, title FROM books"))
    assert schema_state(connection) == before
    assert connection.execute("SELECT * FROM v_recent").fetchall() == [(1, "Clean Code")]


def test_uc10_views_that_were_already_broken_do_not_block_changes(view_db):
    _, connection, manager = view_db
    connection.execute("CREATE VIEW v_gone AS SELECT * FROM missing_table")
    connection.commit()
    manager.alter_view(replace(editing(get(manager, "v_authors")), query="SELECT id, name, born FROM authors"))
    assert get(manager, "v_authors").columns == ("id", "name", "born")


def test_uc10_a_broken_view_can_be_repaired(view_db):
    _, connection, manager = view_db
    connection.execute("CREATE VIEW v_gone AS SELECT * FROM missing_table")
    connection.commit()
    gone = editing(get(manager, "v_gone"))
    with pytest.raises(ViewOperationError, match="The query is not valid"):
        manager.alter_view(replace(gone, query="SELECT * FROM still_missing"))
    manager.alter_view(replace(gone, query="SELECT id FROM authors"))
    fixed = get(manager, "v_gone")
    assert fixed.error is None and fixed.columns == ("id",)


def test_uc10_triggers_of_the_view_survive_a_query_change(view_db):
    _, connection, manager = view_db
    manager.alter_view(replace(editing(get(manager, "v_authors")), query="SELECT id, name, born FROM authors"))
    assert get(manager, "v_authors").triggers == ("tr_v_authors",)
    connection.execute("INSERT INTO v_authors(name) VALUES ('Cy')")
    assert connection.execute("SELECT name FROM authors WHERE name = 'Cy'").fetchone() == ("Cy",)


def test_uc10_trigger_that_reads_a_removed_column_blocks_the_change(view_db):
    _, connection, manager = view_db
    before = schema_state(connection)
    with pytest.raises(ViewOperationError, match='Trigger "tr_v_authors" on this view may use column "name"'):
        manager.alter_view(replace(editing(get(manager, "v_authors")), query="SELECT id FROM authors"))
    assert schema_state(connection) == before
    connection.execute("INSERT INTO v_authors(name) VALUES ('Cy')")  # still works


@pytest.mark.parametrize("view, message", [
    ("v_authors", 'has triggers \\(tr_v_authors\\)'),
    ("v_books", 'View "v_recent" uses view "v_books"'),
])
def test_uc10_rename_is_refused_while_something_depends_on_the_name(view_db, view, message):
    _, connection, manager = view_db
    before = schema_state(connection)
    with pytest.raises(ViewOperationError, match=message):
        manager.alter_view(replace(editing(get(manager, view)), name="v_renamed"))
    assert schema_state(connection) == before


def test_uc10_rename_is_refused_when_a_trigger_elsewhere_mentions_the_view(view_db):
    _, connection, manager = view_db
    connection.execute(
        "CREATE TRIGGER tr_log AFTER INSERT ON log BEGIN INSERT INTO authors(name) SELECT title FROM v_recent; END"
    )
    connection.commit()
    before = schema_state(connection)
    with pytest.raises(ViewOperationError, match='Trigger "tr_log" mentions view "v_recent"'):
        manager.alter_view(replace(editing(get(manager, "v_recent")), name="v_other"))
    assert schema_state(connection) == before


def test_uc10_new_name_must_be_free(view_db):
    _, connection, manager = view_db
    before = schema_state(connection)
    for name in ("books", "V_BOOKS"):
        with pytest.raises(ViewOperationError, match="already exists"):
            manager.alter_view(replace(editing(get(manager, "v_recent")), name=name))
    assert schema_state(connection) == before


def test_uc10_stale_form_is_rejected_and_the_other_change_is_kept(view_db):
    path, connection, manager = view_db
    form = editing(get(manager, "v_authors"))  # the form is opened here
    other = sqlite3.connect(path)
    other.execute("DROP VIEW v_recent")
    other.execute("ALTER TABLE authors ADD COLUMN country TEXT")
    other.execute("DROP TRIGGER tr_v_authors")
    other.execute("DROP VIEW v_authors")
    other.execute("CREATE VIEW v_authors AS SELECT id, name, country FROM authors")
    other.commit()
    other.close()
    before = schema_state(connection)
    with pytest.raises(ViewOperationError, match="changed after this form was opened"):
        manager.alter_view(replace(form, query="SELECT id FROM authors"))
    assert schema_state(connection) == before
    fresh = editing(get(manager, "v_authors"))
    assert fresh.columns == ("id", "name", "country")
    manager.alter_view(replace(fresh, query="SELECT id, country FROM authors"))
    assert get(manager, "v_authors").columns == ("id", "country")


def test_uc10_form_without_a_base_version_or_original_name_is_rejected(view_db):
    _, connection, manager = view_db
    books = editing(get(manager, "v_books"))
    before = schema_state(connection)
    with pytest.raises(ViewOperationError, match="does not say which version"):
        manager.alter_view(replace(books, sql=None))
    with pytest.raises(ViewOperationError, match="does not say which view"):
        manager.alter_view(replace(books, original_name=None))
    with pytest.raises(ViewOperationError, match="does not exist"):
        manager.alter_view(replace(books, original_name="ghost"))
    assert schema_state(connection) == before


def test_uc10_views_the_form_cannot_represent_are_view_only(view_db):
    _, connection, manager = view_db
    connection.execute("CREATE VIEW v_cols(x, y) AS SELECT id, name FROM authors")
    connection.commit()
    cols = editing(get(manager, "v_cols"))
    before = schema_state(connection)
    with pytest.raises(ViewOperationError, match="Cannot be edited"):
        manager.alter_view(replace(cols, query="SELECT id, name FROM authors WHERE id > 0"))
    assert schema_state(connection) == before
    manager.drop_view("v_cols")  # deleting is still possible


def test_uc10_inside_a_transaction_the_change_can_be_rolled_back(view_db):
    _, connection, manager = view_db
    connection.execute("UPDATE authors SET born = 1 WHERE id = 1")
    snapshot = manager.alter_view(replace(editing(get(manager, "v_recent")), query="SELECT id FROM books"))
    assert snapshot.in_transaction
    connection.rollback()
    assert get(manager, "v_recent").columns == ("id", "title")
