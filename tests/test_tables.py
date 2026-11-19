"""UC-03, UC-04 and UC-05 against real temporary SQLite files."""

import sqlite3
from dataclasses import replace

import pytest

from database_browser.schema.errors import TableOperationError
from database_browser.schema.table_models import (
    CHECK, FOREIGN_KEY, PRIMARY_KEY, UNIQUE, ColumnDef, TableConstraint, TableDefinition,
)
from database_browser.schema.table_parser import normalize_expression, parse_create_table
from database_browser.schema.table_repository import SQLiteTableRepository
from database_browser.schema.table_services import TableManager
from database_browser.schema.table_sql import create_table_sql


@pytest.fixture
def table_db(tmp_path):
    """Parent/child tables, a table with plain row ids, an index, a view and a trigger."""
    path = tmp_path / "tables.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE authors(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
                             born INTEGER CHECK (born > 0));
        CREATE TABLE books(id INTEGER PRIMARY KEY, title TEXT NOT NULL,
                           author_id INTEGER REFERENCES authors(id) ON DELETE CASCADE,
                           year INTEGER, UNIQUE (title, year));
        CREATE TABLE notes(body TEXT, tag TEXT);
        CREATE TABLE audit(msg TEXT);
        INSERT INTO authors(name, born) VALUES ('Ann', 1900), ('Bob', 1950);
        DELETE FROM authors WHERE name = 'Bob';
        INSERT INTO books(title, author_id, year) VALUES ('Clean Code', 1, 2008), ('UML', 1, 2004);
        INSERT INTO notes(body, tag) VALUES ('a', 'x'), ('b', 'y'), ('c', 'z');
        DELETE FROM notes WHERE body = 'b';
        CREATE INDEX ix_books_title ON books(title);
        CREATE INDEX ix_notes_tag ON notes(tag);
        CREATE VIEW v_authors AS SELECT id, name FROM authors;
        CREATE TRIGGER tr_notes AFTER INSERT ON notes BEGIN INSERT INTO audit(msg) VALUES ('added'); END;
        """
    )
    connection.commit()
    manager = TableManager(SQLiteTableRepository(connection))
    yield path, connection, manager
    connection.rollback()
    connection.close()


def schema_state(connection):
    return connection.execute("SELECT type, name, sql FROM sqlite_schema ORDER BY type, name").fetchall()


def get(manager, name):
    return next(table for table in manager.get_tables().tables if table.name == name)


def editing(table, **changes):
    """What the edit form submits: every stored column linked to itself."""
    columns = tuple(replace(column, original_name=column.name) for column in table.columns)
    return replace(table, columns=columns, **changes)


def without_sql(table):
    return replace(table, sql=None)


def new_table(name="t", columns=(ColumnDef("id", "INTEGER", primary_key=True),), constraints=()):
    return TableDefinition(name, tuple(columns), tuple(constraints))


# ---------------------------------------------------------------- parser


def test_parser_reads_checks_and_autoincrement():
    parsed = parse_create_table(
        "CREATE TABLE t(id INTEGER PRIMARY KEY AUTOINCREMENT, a INT CHECK (a > 0 AND a < (10)), "
        "b TEXT, CHECK (b != 'x,y'))"
    )
    assert parsed.autoincrement == {"id"}
    assert parsed.checks == ("a > 0 AND a < (10)", "b != 'x,y'")
    assert parsed.unsupported == ()


@pytest.mark.parametrize("sql, expected", [
    ("CREATE TABLE t(a TEXT COLLATE NOCASE)", "COLLATE clauses"),
    ("CREATE TABLE t(a INTEGER, CONSTRAINT pk PRIMARY KEY (a))", "named constraints"),
    ("CREATE TABLE t(a INTEGER UNIQUE ON CONFLICT REPLACE)", "ON CONFLICT clauses"),
    ("CREATE TABLE t(a INTEGER PRIMARY KEY DESC)", "ordering inside keys"),
    ("CREATE TABLE t(a INT, b INT, UNIQUE (a DESC, b))", "ordering inside keys"),
    ("CREATE TABLE t(a INT, b INT GENERATED ALWAYS AS (a + 1))", "generated columns"),
    ("CREATE TABLE t(a INT REFERENCES p(x) DEFERRABLE INITIALLY DEFERRED)", "deferred foreign keys"),
])
def test_parser_flags_clauses_the_form_cannot_keep(sql, expected):
    assert expected in parse_create_table(sql).unsupported


def test_parser_ignores_keywords_inside_strings_checks_and_defaults():
    parsed = parse_create_table(
        "CREATE TABLE t(a TEXT DEFAULT 'COLLATE desc', "
        "b INT CHECK (b IN (1, 2) AND 'as' = 'as' COLLATE NOCASE), c TEXT DEFAULT (lower('DESC')))"
    )
    assert parsed.unsupported == ()


def test_parser_normalizes_multiline_checks_and_flags_comments():
    parsed = parse_create_table("CREATE TABLE t(a INT,\n CHECK (a >\n   0 AND 'x  y' = 'x  y'))")
    assert parsed.checks == ("a > 0 AND 'x  y' = 'x  y'",)
    assert "comments inside CHECK constraints" in parse_create_table(
        "CREATE TABLE t(a INT, CHECK (a > 0 -- positive\n))"
    ).unsupported
    assert normalize_expression("  a\n=\t1 ") == "a = 1"


def test_parser_survives_missing_or_broken_sql():
    assert parse_create_table(None).unsupported
    assert parse_create_table("CREATE TABLE").unsupported


# ---------------------------------------------------------------- SQL generation


def test_sql_uses_column_level_primary_key_and_quotes_names():
    sql = create_table_sql(new_table('we"ird', [ColumnDef('co"l', "INTEGER", primary_key=True, autoincrement=True)]))
    assert sql == 'CREATE TABLE main."we""ird" (\n  "co""l" INTEGER PRIMARY KEY AUTOINCREMENT\n)'


def test_sql_for_rebuild_points_self_references_to_the_final_name():
    table = new_table("tree", [ColumnDef("id", "INTEGER", primary_key=True), ColumnDef("parent_id", "INTEGER")], [
        TableConstraint(FOREIGN_KEY, columns=("parent_id",), ref_table="Tree", ref_columns=("id",)),
    ])
    sql = create_table_sql(table, name="__tmp")
    assert 'CREATE TABLE main."__tmp"' in sql
    assert 'REFERENCES "tree" ("id")' in sql


def test_sql_requires_columns():
    with pytest.raises(TableOperationError, match="at least one column"):
        create_table_sql(new_table("t", []))


# ---------------------------------------------------------------- UC-03


def orders():
    return TableDefinition("orders", (
        ColumnDef("id", "INTEGER", primary_key=True, autoincrement=True),
        ColumnDef("code", "TEXT", not_null=True, unique=True),
        ColumnDef("author_id", "INTEGER"),
        ColumnDef("qty", "INTEGER", not_null=True, default="1"),
        ColumnDef("note", "TEXT", default="'none'"),
    ), (
        TableConstraint(UNIQUE, columns=("author_id", "qty")),
        TableConstraint(CHECK, expression="qty > 0"),
        TableConstraint(FOREIGN_KEY, columns=("author_id",), ref_table="authors", ref_columns=("id",),
                        on_delete="CASCADE", on_update="RESTRICT"),
    ))


def test_uc03_creates_table_and_reads_the_same_definition_back(table_db):
    path, connection, manager = table_db
    snapshot = manager.create_table(orders())
    assert "orders" in [table.name for table in snapshot.tables]
    created = get(manager, "orders")
    assert created.columns == orders().columns
    assert set(created.constraints) == set(orders().constraints)
    assert created.editable
    # The change is committed, so another connection sees it.
    other = sqlite3.connect(path)
    assert other.execute("SELECT name FROM sqlite_schema WHERE name = 'orders'").fetchone()
    other.close()


def test_uc03_created_constraints_are_enforced(table_db):
    _, connection, manager = table_db
    manager.create_table(orders())
    connection.execute("INSERT INTO orders(code, author_id) VALUES ('a', 1)")
    for sql in (
        "INSERT INTO orders(code) VALUES ('a')",  # UNIQUE
        "INSERT INTO orders(code) VALUES (NULL)",  # NOT NULL
        "INSERT INTO orders(code, qty) VALUES ('b', 0)",  # CHECK
        "INSERT INTO orders(code, author_id) VALUES ('c', 99)",  # FOREIGN KEY
    ):
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(sql)
    assert connection.execute("SELECT note, qty FROM orders").fetchall() == [("none", 1)]


def test_uc03_composite_primary_key_and_self_reference(table_db):
    _, _, manager = table_db
    manager.create_table(new_table("pairs", [ColumnDef("a", "INTEGER"), ColumnDef("b", "INTEGER")], [
        TableConstraint(PRIMARY_KEY, columns=("b", "a")),
    ]))
    pairs = get(manager, "pairs")
    assert pairs.constraints == (TableConstraint(PRIMARY_KEY, columns=("b", "a")),)
    assert not any(column.primary_key for column in pairs.columns)
    manager.create_table(new_table("tree", [ColumnDef("id", "INTEGER", primary_key=True), ColumnDef("parent", "INTEGER")], [
        TableConstraint(FOREIGN_KEY, columns=("parent",), ref_table="tree"),
    ]))
    assert get(manager, "tree").constraints[0].ref_table == "tree"


def test_uc03_trims_input_and_normalizes_types(table_db):
    _, _, manager = table_db
    manager.create_table(new_table("  spaced  ", [ColumnDef("v", "  VARCHAR(  20 ) ", default="  5 ")]))
    table = get(manager, "spaced")
    assert table.columns[0].data_type == "VARCHAR(20)"
    assert table.columns[0].default == "5"


@pytest.mark.parametrize("definition, message", [
    (new_table("  "), "Enter a table name"),
    (new_table("sqlite_mine"), "reserved"),
    (new_table("BOOKS"), "already exists"),
    (new_table("t", []), "at least one column"),
    (new_table("t", [ColumnDef("a"), ColumnDef("A")]), "more than once"),
    (new_table("t", [ColumnDef("  ")]), "Column names"),
    (new_table("t", [ColumnDef("a", "TEXT); DROP TABLE books; --")]), "not a valid column type"),
    (new_table("t", [ColumnDef("a", "TEXT", primary_key=True, autoincrement=True)]), "AUTOINCREMENT"),
    (new_table("t", [ColumnDef("a", primary_key=True), ColumnDef("b", primary_key=True)]), "only one PRIMARY KEY"),
    (new_table("t", [ColumnDef("a", primary_key=True), ColumnDef("b")],
               [TableConstraint(PRIMARY_KEY, columns=("b",))]), "only one PRIMARY KEY"),
    (new_table("t", [ColumnDef("a")], [TableConstraint(UNIQUE, columns=("zzz",))]), "does not exist"),
    (new_table("t", [ColumnDef("a")], [TableConstraint(UNIQUE, columns=("a", "A"))]), "more than once"),
    (new_table("t", [ColumnDef("a")], [TableConstraint(UNIQUE)]), "at least one column"),
    (new_table("t", [ColumnDef("a")], [TableConstraint(FOREIGN_KEY, columns=("a",), ref_table="nope")]),
     "does not exist"),
    (new_table("t", [ColumnDef("a"), ColumnDef("b")],
               [TableConstraint(FOREIGN_KEY, columns=("a", "b"), ref_table="authors", ref_columns=("id",))]),
     "FOREIGN KEY lists 2"),
    (new_table("t", [ColumnDef("a")],
               [TableConstraint(FOREIGN_KEY, columns=("a",), ref_table="books", ref_columns=("year",))]),
     "PRIMARY KEY or have a UNIQUE"),
    (new_table("t", [ColumnDef("a")], [TableConstraint(FOREIGN_KEY, columns=("a",), ref_table="notes")]),
     "has no PRIMARY KEY"),
    (new_table("t", [ColumnDef("a")], [TableConstraint(CHECK, expression="a > 0; DROP TABLE books")]),
     "semicolon"),
    (new_table("t", [ColumnDef("a")], [TableConstraint(CHECK, expression="a > 0 -- x")]), "comments"),
    (new_table("t", [ColumnDef("a")], [TableConstraint(CHECK, expression="(a > 0")]), "unbalanced"),
    (new_table("t", [ColumnDef("a")], [TableConstraint(CHECK, expression="  ")]), "Enter the CHECK"),
    (new_table("t", [ColumnDef("a", default="1), (2")]), "default value"),
])
def test_uc03_invalid_definitions_are_rejected_without_changes(table_db, definition, message):
    _, connection, manager = table_db
    before = schema_state(connection)
    with pytest.raises(TableOperationError, match=message):
        manager.create_table(definition)
    assert schema_state(connection) == before
    assert not connection.in_transaction


def test_uc03_sqlite_rejection_is_reported_and_nothing_is_created(table_db):
    _, connection, manager = table_db
    before = schema_state(connection)
    # SQLite refuses a default that depends on another column.
    with pytest.raises(TableOperationError, match="SQLite:"):
        manager.create_table(new_table("t", [ColumnDef("a"), ColumnDef("b", default="a")]))
    assert schema_state(connection) == before


def test_uc03_name_shared_with_an_index_is_reported(table_db):
    _, connection, manager = table_db
    before = schema_state(connection)
    with pytest.raises(TableOperationError, match="SQLite:"):
        manager.create_table(new_table("ix_books_title"))
    assert schema_state(connection) == before


def test_uc03_read_only_database_is_reported(table_db):
    path, _, _ = table_db
    read_only = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        with pytest.raises(TableOperationError, match="SQLite:"):
            TableManager(SQLiteTableRepository(read_only)).create_table(new_table("t"))
    finally:
        read_only.close()


def test_uc03_keeps_the_callers_transaction_open(table_db):
    _, connection, manager = table_db
    connection.execute("UPDATE notes SET tag = 'pending' WHERE body = 'a'")
    snapshot = manager.create_table(new_table("t"))
    assert snapshot.in_transaction and connection.in_transaction
    with pytest.raises(TableOperationError):
        manager.create_table(new_table("t"))
    # A failed create undoes only itself.
    assert connection.execute("SELECT tag FROM notes WHERE body = 'a'").fetchone() == ("pending",)
    connection.rollback()
    assert connection.execute("SELECT tag FROM notes WHERE body = 'a'").fetchone() == ("x",)
    assert get_names(manager).isdisjoint({"t"})


def get_names(manager):
    return {table.name for table in manager.get_tables().tables}


# ---------------------------------------------------------------- UC-05


def test_uc05_drops_table_with_rows_index_and_trigger(table_db):
    _, connection, manager = table_db
    snapshot = manager.drop_table("notes")
    assert "notes" not in [table.name for table in snapshot.tables]
    names = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema")}
    assert not names & {"notes", "ix_notes_tag", "tr_notes"}
    assert {"authors", "books", "audit"} <= names
    assert connection.execute("SELECT count(*) FROM books").fetchone() == (2,)


def test_uc05_name_match_ignores_ascii_case(table_db):
    _, _, manager = table_db
    manager.drop_table("NOTES")
    assert "notes" not in get_names(manager)


def test_uc05_reports_missing_and_internal_tables(table_db):
    _, connection, manager = table_db
    before = schema_state(connection)
    with pytest.raises(TableOperationError, match="does not exist"):
        manager.drop_table("ghost")
    with pytest.raises(TableOperationError, match="internal"):
        manager.drop_table("sqlite_sequence")
    assert schema_state(connection) == before


def test_uc05_lists_tables_that_reference_the_dropped_one(table_db):
    _, _, manager = table_db
    assert get(manager, "authors").referenced_by == ("books",)
    assert get(manager, "books").referenced_by == ()


def test_uc05_refused_drop_changes_nothing(table_db):
    _, connection, manager = table_db
    connection.execute("CREATE TABLE reviews(book_id INTEGER REFERENCES books(id))")
    connection.execute("INSERT INTO reviews VALUES (1)")
    connection.commit()
    before = schema_state(connection)
    with pytest.raises(TableOperationError, match="FOREIGN KEY constraint failed"):
        manager.drop_table("books")
    assert schema_state(connection) == before
    assert connection.execute("SELECT count(*) FROM books").fetchone() == (2,)


def test_uc05_cascade_children_lose_rows_which_the_ui_warns_about(table_db):
    _, connection, manager = table_db
    manager.drop_table("authors")
    assert connection.execute("SELECT count(*) FROM books").fetchone() == (0,)


def test_uc05_inside_a_transaction_can_be_rolled_back(table_db):
    _, connection, manager = table_db
    connection.execute("UPDATE notes SET tag = 'pending' WHERE body = 'a'")
    assert manager.drop_table("audit").in_transaction
    connection.rollback()
    assert "audit" in get_names(manager)


# ---------------------------------------------------------------- UC-04


def test_uc04_adds_column_with_default_and_keeps_rows(table_db):
    _, connection, manager = table_db
    books = editing(get(manager, "books"))
    changed = replace(books, columns=books.columns + (ColumnDef("lang", "TEXT", not_null=True, default="'en'"),))
    snapshot = manager.alter_table(changed)
    assert [column.name for column in get(manager, "books").columns][-1] == "lang"
    assert connection.execute("SELECT id, title, author_id, year, lang FROM books ORDER BY id").fetchall() == [
        (1, "Clean Code", 1, 2008, "en"), (2, "UML", 1, 2004, "en"),
    ]
    assert not snapshot.in_transaction


def test_uc04_renames_column_and_keeps_its_data(table_db):
    _, connection, manager = table_db
    notes = editing(get(manager, "notes"))
    columns = tuple(replace(c, name="text") if c.name == "body" else c for c in notes.columns)
    manager.alter_table(replace(notes, columns=columns))
    assert connection.execute("SELECT text FROM notes ORDER BY rowid").fetchall() == [("a",), ("c",)]


def test_uc04_drops_column_with_its_constraint(table_db):
    _, connection, manager = table_db
    authors = editing(get(manager, "authors"))
    manager.alter_table(replace(
        authors, columns=tuple(c for c in authors.columns if c.name != "born"),
        constraints=tuple(c for c in authors.constraints if c.kind != CHECK),
    ))
    assert [c.name for c in get(manager, "authors").columns] == ["id", "name"]
    assert connection.execute("SELECT id, name FROM authors").fetchall() == [(1, "Ann")]


def test_uc04_dropping_a_column_used_by_a_constraint_is_explained(table_db):
    _, connection, manager = table_db
    books = editing(get(manager, "books"))
    before = schema_state(connection)
    with pytest.raises(TableOperationError, match='UNIQUE constraint uses column "year"'):
        manager.alter_table(replace(books, columns=tuple(c for c in books.columns if c.name != "year")))
    assert schema_state(connection) == before


def test_uc04_changes_type_flags_and_constraints(table_db):
    _, connection, manager = table_db
    authors = editing(get(manager, "authors"))
    columns = tuple(replace(c, unique=False, data_type="VARCHAR(40)") if c.name == "name" else c for c in authors.columns)
    manager.alter_table(replace(authors, columns=columns))
    name = next(c for c in get(manager, "authors").columns if c.name == "name")
    assert name.data_type == "VARCHAR(40)" and not name.unique and name.not_null
    connection.execute("INSERT INTO authors(name) VALUES ('Ann')")  # no longer unique


def test_uc04_new_required_column_without_default_is_explained(table_db):
    _, connection, manager = table_db
    books = editing(get(manager, "books"))
    before = schema_state(connection)
    with pytest.raises(TableOperationError, match="Add a default value"):
        manager.alter_table(replace(books, columns=books.columns + (ColumnDef("isbn", "TEXT", not_null=True),)))
    assert schema_state(connection) == before


def test_uc04_new_required_column_is_fine_for_an_empty_table(table_db):
    _, connection, manager = table_db
    audit = editing(get(manager, "audit"))
    manager.alter_table(replace(audit, columns=audit.columns + (ColumnDef("n", "INT", not_null=True),)))
    assert [c.name for c in get(manager, "audit").columns] == ["msg", "n"]


@pytest.mark.parametrize("change, message", [
    (lambda c: replace(c, unique=True) if c.name == "year" else c, "UNIQUE constraint failed: books.year"),
    (lambda c: replace(c, not_null=True) if c.name == "author_id" else c, None),
])
def test_uc04_data_conflict_rolls_everything_back(table_db, change, message):
    _, connection, manager = table_db
    connection.execute("INSERT INTO books(title, author_id, year) VALUES ('Dup', NULL, 2008)")
    connection.commit()
    books = editing(get(manager, "books"))
    before = schema_state(connection)
    with pytest.raises(TableOperationError, match=message or "NOT NULL constraint failed: books.author_id") as caught:
        manager.alter_table(replace(books, columns=tuple(change(c) for c in books.columns)))
    assert "__rebuild" not in str(caught.value)
    assert schema_state(connection) == before
    assert connection.execute("SELECT count(*) FROM books").fetchone() == (3,)
    assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    assert connection.execute("PRAGMA legacy_alter_table").fetchone() == (0,)
    assert not connection.in_transaction


def test_uc04_check_violated_by_existing_rows_is_reported(table_db):
    _, connection, manager = table_db
    books = editing(get(manager, "books"))
    before = schema_state(connection)
    with pytest.raises(TableOperationError, match="CHECK constraint failed"):
        manager.alter_table(replace(books, constraints=books.constraints + (
            TableConstraint(CHECK, expression="year > 2005"),
        )))
    assert schema_state(connection) == before


def test_uc04_rebuild_keeps_indexes_triggers_views_and_row_ids(table_db):
    _, connection, manager = table_db
    index_sql = connection.execute("SELECT sql FROM sqlite_schema WHERE name = 'ix_books_title'").fetchone()
    manager.alter_table(editing(get(manager, "books")))
    assert connection.execute("SELECT sql FROM sqlite_schema WHERE name = 'ix_books_title'").fetchone() == index_sql
    # notes has no INTEGER PRIMARY KEY: row ids 1 and 3 must survive.
    notes = editing(get(manager, "notes"))
    manager.alter_table(replace(notes, columns=notes.columns + (ColumnDef("extra", "TEXT"),)))
    assert connection.execute("SELECT rowid, body FROM notes ORDER BY rowid").fetchall() == [(1, "a"), (3, "c")]
    connection.execute("INSERT INTO notes(body) VALUES ('d')")
    assert connection.execute("SELECT count(*) FROM audit").fetchone() == (1,)  # trigger still fires
    connection.commit()  # a parent table with foreign keys cannot be rebuilt in an open transaction
    assert connection.execute("SELECT count(*) FROM sqlite_schema WHERE name = 'ix_notes_tag'").fetchone() == (1,)
    manager.alter_table(editing(get(manager, "authors")))
    assert connection.execute("SELECT * FROM v_authors").fetchall() == [(1, "Ann")]


def test_uc04_rebuilding_a_parent_keeps_child_rows_and_foreign_keys(table_db):
    _, connection, manager = table_db
    authors = editing(get(manager, "authors"))
    manager.alter_table(replace(authors, columns=authors.columns + (ColumnDef("country", "TEXT", default="'FI'"),)))
    assert connection.execute("SELECT count(*) FROM books").fetchone() == (2,)
    assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO books(title, author_id) VALUES ('Orphan', 77)")
    connection.execute("DELETE FROM authors WHERE id = 1")  # ON DELETE CASCADE still works
    assert connection.execute("SELECT count(*) FROM books").fetchone() == (0,)


def test_uc04_autoincrement_counter_survives_a_rebuild(table_db):
    _, connection, manager = table_db
    # Bob (id 2) was deleted, so the counter is ahead of the largest id.
    manager.alter_table(editing(get(manager, "authors")))
    connection.execute("INSERT INTO authors(name) VALUES ('Cy')")
    assert connection.execute("SELECT id FROM authors WHERE name = 'Cy'").fetchone() == (3,)


def test_uc04_unchanged_tables_round_trip(table_db):
    _, connection, manager = table_db
    for name in ("authors", "books", "notes", "audit"):
        before = get(manager, name)
        manager.alter_table(editing(before))
        assert without_sql(get(manager, name)) == without_sql(before)


def test_uc04_self_reference_and_composite_primary_key_round_trip(table_db):
    _, connection, manager = table_db
    manager.create_table(new_table("tree", [ColumnDef("id", "INTEGER", primary_key=True), ColumnDef("parent", "INTEGER")], [
        TableConstraint(FOREIGN_KEY, columns=("parent",), ref_table="tree", ref_columns=("id",)),
    ]))
    manager.create_table(new_table("pairs", [ColumnDef("a", "INTEGER"), ColumnDef("b", "INTEGER")], [
        TableConstraint(PRIMARY_KEY, columns=("b", "a")),
    ]))
    connection.execute("INSERT INTO tree VALUES (1, NULL), (2, 1)")
    connection.execute("INSERT INTO pairs VALUES (1, 2)")
    connection.commit()
    for name in ("tree", "pairs"):
        table = editing(get(manager, name))
        manager.alter_table(replace(table, columns=table.columns + (ColumnDef("x", "TEXT"),)))
    assert connection.execute("SELECT * FROM tree ORDER BY id").fetchall() == [(1, None, None), (2, 1, None)]
    assert get(manager, "pairs").constraints == (TableConstraint(PRIMARY_KEY, columns=("b", "a")),)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO tree(id, parent) VALUES (3, 99)")


def test_uc04_index_follows_a_renamed_column_and_blocks_removal(table_db):
    _, connection, manager = table_db
    notes = editing(get(manager, "notes"))
    renamed = tuple(replace(c, name="label") if c.name == "tag" else c for c in notes.columns)
    manager.alter_table(replace(notes, columns=renamed))
    sql = connection.execute("SELECT sql FROM sqlite_schema WHERE name = 'ix_notes_tag'").fetchone()[0]
    assert '("label")' in sql
    notes = editing(get(manager, "notes"))
    before = schema_state(connection)
    with pytest.raises(TableOperationError, match='Index "ix_notes_tag" uses column "label"'):
        manager.alter_table(replace(notes, columns=tuple(c for c in notes.columns if c.name != "label")))
    assert schema_state(connection) == before


def rename(table, old, new):
    return replace(table, columns=tuple(replace(c, name=new) if c.name == old else c for c in table.columns))


def test_uc04_rename_keeps_check_constraints_working(table_db):
    _, connection, manager = table_db
    connection.execute(
        "CREATE TABLE scores(id INTEGER PRIMARY KEY, points INTEGER CHECK (points >= 0), bonus INTEGER, "
        "CHECK (points + bonus < 1000))"
    )
    connection.execute("INSERT INTO scores VALUES (1, 5, 1)")
    connection.commit()
    manager.alter_table(rename(editing(get(manager, "scores")), "points", "score"))
    checks = [c.expression for c in get(manager, "scores").constraints if c.kind == CHECK]
    assert len(checks) == 2 and not any("points" in expression for expression in checks)
    assert connection.execute("SELECT id, score, bonus FROM scores").fetchall() == [(1, 5, 1)]
    for sql in ("INSERT INTO scores VALUES (2, -1, 0)", "INSERT INTO scores VALUES (3, 999, 5)"):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(sql)
    # The rewritten constraints survive another edit.
    manager.alter_table(editing(get(manager, "scores")))
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        connection.execute("INSERT INTO scores VALUES (4, -1, 0)")


def test_uc04_rename_keeps_a_checked_edit_of_the_constraint(table_db):
    _, connection, manager = table_db
    connection.execute("CREATE TABLE scores(id INTEGER PRIMARY KEY, points INTEGER CHECK (points >= 0))")
    connection.commit()
    table = rename(editing(get(manager, "scores")), "points", "score")
    # The user replaced the old CHECK by one that already uses the new name.
    edited = replace(table, constraints=(TableConstraint(CHECK, expression="score >= 10"),))
    manager.alter_table(edited)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        connection.execute("INSERT INTO scores VALUES (1, 5)")


def test_uc04_rename_updates_views_triggers_and_indexes(table_db):
    _, connection, manager = table_db
    connection.execute("CREATE VIEW v_tags AS SELECT tag FROM notes")
    connection.execute("CREATE TRIGGER tr_tag AFTER INSERT ON notes BEGIN INSERT INTO audit(msg) VALUES (NEW.tag); END")
    connection.commit()
    manager.alter_table(rename(editing(get(manager, "notes")), "tag", "label"))
    assert connection.execute("SELECT * FROM v_tags ORDER BY 1").fetchall() == [("x",), ("z",)]
    connection.execute("INSERT INTO notes(body, label) VALUES ('d', 'q')")
    assert ("q",) in connection.execute("SELECT msg FROM audit").fetchall()
    assert connection.execute("SELECT rowid, label FROM notes WHERE body = 'c'").fetchone() == (3, "z")


def test_uc04_rename_follows_keys_and_keeps_foreign_key_column_order(table_db):
    _, connection, manager = table_db
    connection.execute("CREATE TABLE pk2(x INTEGER, y INTEGER, PRIMARY KEY (x, y))")
    connection.execute(
        "CREATE TABLE child(a INTEGER, b INTEGER, UNIQUE (b, a), FOREIGN KEY (b, a) REFERENCES pk2 (x, y))"
    )
    connection.execute("INSERT INTO pk2 VALUES (1, 2)")
    connection.execute("INSERT INTO child VALUES (2, 1)")
    connection.commit()
    manager.alter_table(rename(editing(get(manager, "child")), "a", "a2"))
    after = get(manager, "child")
    assert TableConstraint(UNIQUE, columns=("b", "a2")) in after.constraints
    assert TableConstraint(FOREIGN_KEY, columns=("b", "a2"), ref_table="pk2", ref_columns=("x", "y")) in after.constraints
    assert connection.execute("SELECT a2, b FROM child").fetchall() == [(2, 1)]
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO child VALUES (9, 9)")


def test_uc04_composite_foreign_key_order_is_kept_on_create_and_edit(table_db):
    _, _, manager = table_db
    manager.create_table(new_table("parent2", [ColumnDef("x", "INTEGER"), ColumnDef("y", "INTEGER")], [
        TableConstraint(PRIMARY_KEY, columns=("x", "y")),
    ]))
    key = TableConstraint(FOREIGN_KEY, columns=("b", "a"), ref_table="parent2", ref_columns=("y", "x"))
    manager.create_table(new_table("kid", [ColumnDef("a", "INTEGER"), ColumnDef("b", "INTEGER")], [key]))
    assert get(manager, "kid").constraints == (key,)
    manager.alter_table(editing(get(manager, "kid")))
    assert get(manager, "kid").constraints == (key,)


@pytest.mark.parametrize("scenario, message", [
    ("swap", "Cannot rename"),
    ("reuse_removed", "Cannot rename"),
    ("reuse_vacated", "was used by a column that is renamed"),
])
def test_uc04_ambiguous_renames_are_refused(table_db, scenario, message):
    _, connection, manager = table_db
    connection.execute("CREATE TABLE pair(a INTEGER, b INTEGER, c INTEGER)")
    connection.execute("INSERT INTO pair VALUES (1, 2, 3)")
    connection.commit()
    pair = editing(get(manager, "pair"))
    a, b, c = pair.columns
    columns = {
        "swap": (replace(a, name="b"), replace(b, name="a"), c),
        "reuse_removed": (replace(a, name="c"), b),
        "reuse_vacated": (replace(a, name="x"), b, c, ColumnDef("a", "TEXT")),
    }[scenario]
    before = schema_state(connection)
    with pytest.raises(TableOperationError, match=message):
        manager.alter_table(replace(pair, columns=columns))
    assert schema_state(connection) == before
    assert connection.execute("SELECT a, b, c FROM pair").fetchall() == [(1, 2, 3)]


def test_uc04_row_ids_survive_when_a_column_is_named_rowid(table_db):
    _, connection, manager = table_db
    connection.execute("CREATE TABLE r(rowid TEXT, v INTEGER)")
    connection.execute("INSERT INTO r(oid, rowid, v) VALUES (10, 'a', 1), (20, 'b', 2)")
    connection.commit()
    r = editing(get(manager, "r"))
    manager.alter_table(replace(r, columns=r.columns + (ColumnDef("w", "TEXT"),)))
    assert connection.execute("SELECT oid, rowid, v FROM r ORDER BY oid").fetchall() == [(10, "a", 1), (20, "b", 2)]


def test_uc04_row_ids_survive_when_the_integer_primary_key_is_removed(table_db):
    _, connection, manager = table_db
    connection.execute("CREATE TABLE p(id INTEGER PRIMARY KEY, v TEXT)")
    connection.execute("INSERT INTO p VALUES (10, 'a'), (20, 'b')")
    connection.commit()
    p = editing(get(manager, "p"))
    manager.alter_table(replace(p, columns=tuple(replace(c, primary_key=False) if c.name == "id" else c for c in p.columns)))
    assert connection.execute("SELECT rowid, id FROM p ORDER BY rowid").fetchall() == [(10, 10), (20, 20)]


def test_uc04_row_ids_that_no_name_can_reach_are_not_silently_renumbered(table_db):
    _, connection, manager = table_db
    connection.execute("CREATE TABLE hidden(rowid, oid, _rowid_, v)")
    connection.execute("INSERT INTO hidden(rowid, oid, _rowid_, v) VALUES (1, 2, 3, 4)")
    connection.commit()
    hidden = editing(get(manager, "hidden"))
    changed = replace(hidden, columns=hidden.columns + (ColumnDef("w"),))
    before = schema_state(connection)
    with pytest.raises(TableOperationError, match="row ids cannot be preserved"):
        manager.alter_table(changed)
    assert schema_state(connection) == before
    connection.execute("DELETE FROM hidden")
    connection.commit()
    manager.alter_table(changed)  # nothing to renumber
    assert [c.name for c in get(manager, "hidden").columns][-1] == "w"


def test_uc04_dropping_a_column_that_a_view_uses_is_refused(table_db):
    _, connection, manager = table_db
    authors = editing(get(manager, "authors"))
    before = schema_state(connection)
    with pytest.raises(TableOperationError, match='View "v_authors" would stop working'):
        manager.alter_table(replace(authors, columns=tuple(c for c in authors.columns if c.name != "name")))
    assert schema_state(connection) == before
    assert connection.execute("SELECT * FROM v_authors").fetchall() == [(1, "Ann")]
    assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)


def test_uc04_views_that_were_already_broken_do_not_block_other_changes(table_db):
    _, connection, manager = table_db
    connection.execute("CREATE VIEW v_gone AS SELECT * FROM missing_table")
    connection.commit()
    audit = editing(get(manager, "audit"))
    manager.alter_table(replace(audit, columns=audit.columns + (ColumnDef("extra", "TEXT"),)))
    assert [c.name for c in get(manager, "audit").columns] == ["msg", "extra"]


def test_uc04_stale_form_is_rejected_and_the_new_column_is_kept(table_db):
    path, connection, manager = table_db
    books = editing(get(manager, "books"))  # the form is opened here
    other = sqlite3.connect(path)
    other.execute("ALTER TABLE books ADD COLUMN isbn TEXT")
    other.execute("UPDATE books SET isbn = '111' WHERE id = 1")
    other.commit()
    other.close()
    before = schema_state(connection)
    with pytest.raises(TableOperationError, match="changed after this form was opened"):
        manager.alter_table(replace(books, columns=books.columns + (ColumnDef("lang", "TEXT"),)))
    assert schema_state(connection) == before
    assert connection.execute("SELECT isbn FROM books WHERE id = 1").fetchone() == ("111",)
    assert not connection.in_transaction
    # Opening the form again shows the new column; saving that form keeps its data.
    fresh = editing(get(manager, "books"))
    assert "isbn" in [column.name for column in fresh.columns]
    manager.alter_table(replace(fresh, columns=fresh.columns + (ColumnDef("lang", "TEXT"),)))
    assert connection.execute("SELECT isbn, lang FROM books WHERE id = 1").fetchone() == ("111", None)


@pytest.mark.parametrize("change", [
    "ALTER TABLE notes ADD COLUMN extra TEXT",
    "ALTER TABLE notes RENAME COLUMN body TO text",
])
def test_uc04_any_change_of_the_stored_table_makes_the_form_stale(table_db, change):
    path, connection, manager = table_db
    notes = editing(get(manager, "notes"))
    other = sqlite3.connect(path)
    other.execute(change)
    other.commit()
    other.close()
    with pytest.raises(TableOperationError, match="changed after this form was opened"):
        manager.alter_table(notes)


def test_uc04_data_changes_do_not_make_the_form_stale(table_db):
    path, connection, manager = table_db
    notes = editing(get(manager, "notes"))
    other = sqlite3.connect(path)
    other.execute("INSERT INTO notes(body, tag) VALUES ('new', 'n')")
    other.commit()
    other.close()
    manager.alter_table(replace(notes, columns=notes.columns + (ColumnDef("extra", "TEXT"),)))
    assert connection.execute("SELECT count(*) FROM notes").fetchone() == (3,)


def test_uc04_form_without_a_base_version_is_rejected(table_db):
    _, connection, manager = table_db
    notes = replace(editing(get(manager, "notes")), sql=None)
    before = schema_state(connection)
    with pytest.raises(TableOperationError, match="does not say which version"):
        manager.alter_table(notes)
    assert schema_state(connection) == before


@pytest.mark.parametrize("trigger", [
    "CREATE TRIGGER tr_posts AFTER INSERT ON posts BEGIN INSERT INTO audit(msg) VALUES (NEW.title); END",
    'CREATE TRIGGER tr_posts AFTER INSERT ON posts BEGIN INSERT INTO audit(msg) VALUES (NEW."title"); END',
    "CREATE TRIGGER tr_posts AFTER UPDATE OF title ON posts BEGIN SELECT 1; END",
    "CREATE TRIGGER tr_posts AFTER INSERT ON audit BEGIN UPDATE posts SET body = NEW.msg WHERE title = 'x'; END",
])
def test_uc04_removing_a_column_used_by_a_trigger_is_refused(table_db, trigger):
    _, connection, manager = table_db
    connection.execute("CREATE TABLE posts(id INTEGER PRIMARY KEY, title TEXT, body TEXT)")
    connection.execute(trigger)
    connection.commit()
    posts = editing(get(manager, "posts"))
    before = schema_state(connection)
    with pytest.raises(TableOperationError, match='Trigger "tr_posts" may use column "title"'):
        manager.alter_table(replace(posts, columns=tuple(c for c in posts.columns if c.name != "title")))
    assert schema_state(connection) == before
    assert not connection.in_transaction
    # Nothing was changed, so the trigger still works.
    connection.execute("INSERT INTO posts(title) VALUES ('t')")
    connection.execute("INSERT INTO audit(msg) VALUES ('m')")
    connection.rollback()


def test_uc04_columns_the_triggers_do_not_use_can_still_be_removed(table_db):
    _, connection, manager = table_db
    connection.execute("CREATE TABLE posts(id INTEGER PRIMARY KEY, title TEXT, body TEXT)")
    connection.execute("CREATE TRIGGER tr_posts AFTER INSERT ON posts BEGIN INSERT INTO audit(msg) VALUES (NEW.title); END")
    connection.commit()
    posts = editing(get(manager, "posts"))
    manager.alter_table(replace(posts, columns=tuple(c for c in posts.columns if c.name != "body")))
    connection.execute("INSERT INTO posts(title) VALUES ('t')")
    assert ("t",) in connection.execute("SELECT msg FROM audit").fetchall()
    connection.rollback()


def test_uc04_text_in_string_literals_is_not_a_column_reference(table_db):
    _, connection, manager = table_db
    connection.execute("CREATE TABLE posts(id INTEGER PRIMARY KEY, title TEXT)")
    connection.execute("CREATE TRIGGER tr_posts AFTER INSERT ON posts BEGIN INSERT INTO audit(msg) VALUES ('title'); END")
    connection.commit()
    posts = editing(get(manager, "posts"))
    manager.alter_table(replace(posts, columns=tuple(c for c in posts.columns if c.name != "title")))
    connection.execute("INSERT INTO posts DEFAULT VALUES")
    assert ("title",) in connection.execute("SELECT msg FROM audit").fetchall()
    connection.rollback()


def test_uc04_foreign_key_needing_missing_parents_is_rejected(table_db):
    _, connection, manager = table_db
    connection.execute("CREATE TABLE loose(id INTEGER PRIMARY KEY, author_id INTEGER)")
    connection.execute("INSERT INTO loose VALUES (1, 99)")
    connection.commit()
    loose = editing(get(manager, "loose"))
    before = schema_state(connection)
    with pytest.raises(TableOperationError, match="break foreign key constraints"):
        manager.alter_table(replace(loose, constraints=(
            TableConstraint(FOREIGN_KEY, columns=("author_id",), ref_table="authors", ref_columns=("id",)),
        )))
    assert schema_state(connection) == before
    assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)


def test_uc04_changing_a_key_that_children_reference_is_explained(table_db):
    _, connection, manager = table_db
    authors = editing(get(manager, "authors"))
    before = schema_state(connection)
    renamed = tuple(replace(c, name="author_pk") if c.name == "id" else c for c in authors.columns)
    with pytest.raises(TableOperationError, match='Table "books" has a foreign key'):
        manager.alter_table(replace(authors, columns=renamed))
    assert schema_state(connection) == before


def test_uc04_keeps_at_least_one_existing_column_when_rows_exist(table_db):
    _, _, manager = table_db
    notes = get(manager, "notes")
    with pytest.raises(TableOperationError, match="Keep at least one existing column"):
        manager.alter_table(replace(notes, columns=(ColumnDef("fresh", "TEXT"),)))


def test_uc04_stale_form_is_reported(table_db):
    _, _, manager = table_db
    notes = editing(get(manager, "notes"))
    ghost = replace(notes.columns[0], original_name="gone")
    with pytest.raises(TableOperationError, match='Column "gone" no longer exists'):
        manager.alter_table(replace(notes, columns=(ghost,) + notes.columns[1:]))
    with pytest.raises(TableOperationError, match="does not exist"):
        manager.alter_table(replace(notes, name="ghost_table"))


@pytest.mark.parametrize("sql", [
    "CREATE TABLE odd(name TEXT COLLATE NOCASE)",
    "CREATE TABLE odd(a INTEGER PRIMARY KEY, b) WITHOUT ROWID",
    "CREATE TABLE odd(a INT, b INT GENERATED ALWAYS AS (a * 2) VIRTUAL)",
    "CREATE TABLE odd(a INT, CONSTRAINT positive CHECK (a > 0))",
    "CREATE TABLE odd(a INT) STRICT",
])
def test_uc04_tables_the_form_cannot_represent_are_view_only(table_db, sql):
    _, connection, manager = table_db
    connection.execute(sql)
    connection.commit()
    odd = get(manager, "odd")
    assert not odd.editable and odd.reason
    before = schema_state(connection)
    with pytest.raises(TableOperationError, match="Cannot be edited"):
        manager.alter_table(editing(odd))
    assert schema_state(connection) == before
    manager.drop_table("odd")  # deleting is still possible


def test_uc04_foreign_key_tables_cannot_be_rebuilt_inside_a_transaction(table_db):
    _, connection, manager = table_db
    connection.execute("UPDATE notes SET tag = 'pending' WHERE body = 'a'")
    authors = editing(get(manager, "authors"))
    before = schema_state(connection)
    with pytest.raises(TableOperationError, match="transaction is open"):
        manager.alter_table(replace(authors, columns=authors.columns + (ColumnDef("x"),)))
    assert schema_state(connection) == before
    assert connection.execute("PRAGMA legacy_alter_table").fetchone() == (0,)
    assert connection.execute("SELECT tag FROM notes WHERE body = 'a'").fetchone() == ("pending",)


def test_uc04_plain_tables_can_be_rebuilt_inside_a_transaction_and_rolled_back(table_db):
    _, connection, manager = table_db
    connection.execute("INSERT INTO audit VALUES ('pending')")  # opens the caller's transaction
    audit = editing(get(manager, "audit"))
    snapshot = manager.alter_table(replace(audit, columns=audit.columns + (ColumnDef("extra", "TEXT"),)))
    assert snapshot.in_transaction
    assert [c.name for c in get(manager, "audit").columns] == ["msg", "extra"]
    assert connection.execute("SELECT msg, extra FROM audit").fetchall() == [("pending", None)]
    connection.rollback()
    assert [c.name for c in get(manager, "audit").columns] == ["msg"]
    assert connection.execute("SELECT count(*) FROM audit").fetchone() == (0,)
    assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    assert connection.execute("PRAGMA legacy_alter_table").fetchone() == (0,)
