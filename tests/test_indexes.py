"""Index operations against real temporary SQLite files."""

import sqlite3
from contextlib import closing

import pytest

from database_browser.schema.errors import IndexOperationError
from database_browser.schema.models import Index, IndexColumn
from database_browser.schema.repositories import SQLiteIndexRepository
from database_browser.schema.services import SchemaManager


@pytest.fixture
def index_db(tmp_path):
    """Keep duplicate values and NULLs available for uniqueness checks."""
    path = tmp_path / "indexes.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE items(id INTEGER PRIMARY KEY, title TEXT, category TEXT, price INTEGER)"
    )
    connection.executemany(
        "INSERT INTO items(title, category, price) VALUES (?, ?, ?)",
        [("First", "books", 10), ("Second", "books", 20),
         (None, "games", 30), (None, "games", 40)],
    )
    connection.commit()
    manager = SchemaManager(SQLiteIndexRepository(connection))
    yield path, connection, manager
    connection.rollback()
    connection.close()


def definition(columns=("title",), *, name="ix_items", table="items", unique=False):
    return Index(name, table, tuple(IndexColumn(name) for name in columns), unique)


def stored_sql(connection, name="ix_items"):
    row = connection.execute(
        "SELECT sql FROM main.sqlite_schema WHERE type = 'index' AND name = ?", (name,),
    ).fetchone()
    return row[0] if row else None


def test_uc06_creates_index_and_returns_updated_schema(index_db):
    _, connection, manager = index_db
    snapshot = manager.create_index(definition())
    created = next(index for index in snapshot.indexes if index.name == "ix_items")
    assert created.table_name == "items"
    assert created.columns == (IndexColumn("title"),)
    assert created.is_user_created and created.can_edit
    assert not created.unique
    assert not snapshot.in_transaction
    assert not connection.in_transaction
    assert stored_sql(connection) is not None
    assert connection.execute("SELECT count(*) FROM items").fetchone() == (4,)


def test_uc06_preserves_composite_column_order_and_direction(index_db):
    _, connection, manager = index_db
    columns = (IndexColumn("category", True), IndexColumn("price"))
    snapshot = manager.create_index(Index("ix_items", "items", columns))
    assert snapshot.indexes[0].columns == columns
    keys = connection.execute(
        'SELECT name, "desc" FROM pragma_index_xinfo(?) WHERE "key" = 1 ORDER BY seqno',
        ("ix_items",),
    ).fetchall()
    assert keys == [("category", 1), ("price", 0)]


def test_uc06_unique_index_enforces_uniqueness_and_allows_nulls(index_db):
    _, connection, manager = index_db
    snapshot = manager.create_index(definition(unique=True))
    assert snapshot.indexes[0].unique
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO items(title) VALUES ('First')")
    connection.rollback()
    connection.execute("INSERT INTO items(title) VALUES (NULL)")
    assert connection.execute("SELECT count(*) FROM items WHERE title IS NULL").fetchone() == (3,)


def test_uc06_rejects_duplicates_without_leaving_an_index(index_db):
    _, connection, manager = index_db
    with pytest.raises(IndexOperationError, match="UNIQUE"):
        manager.create_index(definition(("category",), unique=True))
    assert stored_sql(connection) is None
    assert not connection.in_transaction
    assert connection.execute("SELECT count(*) FROM items").fetchone() == (4,)


def test_uc06_occupied_name_keeps_original_definition(index_db):
    _, connection, manager = index_db
    manager.create_index(definition())
    original = stored_sql(connection)
    with pytest.raises(IndexOperationError, match="already exists"):
        manager.create_index(definition(("price",)))
    assert stored_sql(connection) == original
    assert not connection.in_transaction


@pytest.mark.parametrize(
    "index, message",
    [
        (definition(table="missing"), "Table"),
        (definition(("missing",)), "Column"),
        (definition(name=""), "Names"),
        (definition(name="   "), "Names"),
        (definition(name="bad\x00name"), "Names"),
        (definition(()), "at least one column"),
        (definition(("title", "TITLE")), "more than once"),
    ],
)
def test_uc06_invalid_definition_does_not_change_schema(index_db, index, message):
    _, connection, manager = index_db
    before = connection.execute("SELECT type, name, sql FROM sqlite_schema ORDER BY name").fetchall()
    with pytest.raises(IndexOperationError, match=message):
        manager.create_index(index)
    after = connection.execute("SELECT type, name, sql FROM sqlite_schema ORDER BY name").fetchall()
    assert after == before
    assert not connection.in_transaction


def test_uc06_supports_quotes_keywords_and_unicode_in_names(index_db):
    _, connection, manager = index_db
    connection.execute('CREATE TABLE "Таблица ""заказы""" ("select" TEXT, "поле ""имя""" TEXT)')
    index = definition(
        ("select", 'поле "имя"'), name='индекс "заказы"', table='Таблица "заказы"',
    )
    snapshot = manager.create_index(index)
    created = next(item for item in snapshot.indexes if item.name == index.name)
    assert created.table_name == index.table_name
    assert created.columns == index.columns
    assert stored_sql(connection, index.name) is not None
    assert connection.execute("SELECT count(*) FROM items").fetchone() == (4,)


def test_uc06_accepts_ascii_identifier_case_without_changing_metadata(index_db):
    _, _, manager = index_db
    snapshot = manager.create_index(definition(("TITLE",), table="ITEMS"))
    assert snapshot.indexes[0].table_name == "items"
    assert snapshot.indexes[0].columns == (IndexColumn("title"),)


def test_uc06_rejects_non_ascii_column_typo_instead_of_creating_an_expression(index_db):
    _, connection, manager = index_db
    connection.execute('CREATE TABLE "База" ("Имя" TEXT)')
    with pytest.raises(IndexOperationError, match="Column"):
        manager.create_index(definition(("имя",), table="База"))
    assert stored_sql(connection) is None


def test_uc06_uses_main_database_when_a_temporary_table_has_the_same_name(index_db):
    _, connection, manager = index_db
    connection.execute("CREATE TEMP TABLE items(other_column TEXT)")
    manager.create_index(definition())
    assert stored_sql(connection) is not None
    assert connection.execute("SELECT name FROM temp.sqlite_schema WHERE type = 'index'").fetchall() == []


def test_uc06_can_index_generated_columns(index_db):
    _, connection, manager = index_db
    connection.execute(
        "CREATE TABLE totals(amount INTEGER, doubled INTEGER GENERATED ALWAYS AS (amount * 2) VIRTUAL)"
    )
    snapshot = manager.create_index(definition(("doubled",), table="totals"))
    created = next(index for index in snapshot.indexes if index.name == "ix_items")
    assert created.columns == (IndexColumn("doubled"),)


def test_uc06_saved_index_survives_reopening_the_file(index_db):
    path, _, manager = index_db
    manager.create_index(definition(unique=True))
    with closing(sqlite3.connect(path)) as reopened:
        assert stored_sql(reopened) is not None
        with pytest.raises(sqlite3.IntegrityError):
            reopened.execute("INSERT INTO items(title) VALUES ('First')")


def test_uc06_read_only_error_keeps_connection_usable(index_db):
    path, _, _ = index_db
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        manager = SchemaManager(SQLiteIndexRepository(connection))
        assert manager.get_schema().tables
        with pytest.raises(IndexOperationError, match="readonly"):
            manager.create_index(definition())
        assert not connection.in_transaction
        assert stored_sql(connection) is None
        assert connection.execute("SELECT count(*) FROM items").fetchone() == (4,)


@pytest.mark.parametrize("unique", [False, True])
def test_uc06_keeps_unrelated_pending_changes_inside_outer_transaction(index_db, unique):
    path, connection, manager = index_db
    connection.execute("UPDATE items SET title = 'pending' WHERE id = 1")
    if unique:
        with pytest.raises(IndexOperationError, match="UNIQUE"):
            manager.create_index(definition(("category",), unique=True))
    else:
        snapshot = manager.create_index(definition(("price",)))
        assert snapshot.in_transaction
        assert stored_sql(connection) is not None
    assert connection.in_transaction
    assert connection.execute("SELECT title FROM items WHERE id = 1").fetchone() == ("pending",)
    # A separate connection must not see an accidental COMMIT from index creation.
    with closing(sqlite3.connect(path)) as observer:
        assert observer.execute("SELECT title FROM items WHERE id = 1").fetchone() == ("First",)
    connection.rollback()
    assert stored_sql(connection) is None
    assert connection.execute("SELECT title FROM items WHERE id = 1").fetchone() == ("First",)


def test_uc06_failed_schema_refresh_rolls_back_creation(index_db, monkeypatch):
    _, connection, manager = index_db

    def fail_refresh():
        raise sqlite3.OperationalError("cannot read updated schema")

    with monkeypatch.context() as patch:
        patch.setattr(manager.repository, "find_all", fail_refresh)
        with pytest.raises(IndexOperationError, match="cannot read updated schema"):
            manager.create_index(definition())
    assert stored_sql(connection) is None
    assert not connection.in_transaction
    assert manager.create_index(definition()).indexes


@pytest.mark.parametrize("outer_transaction", [False, True])
def test_uc06_release_error_rolls_back_only_the_index_change(index_db, outer_transaction):
    _, connection, manager = index_db
    if outer_transaction:
        connection.execute("UPDATE items SET title = 'pending' WHERE id = 1")
    reject_release = True

    def authorizer(action, operation, name, database, trigger):
        nonlocal reject_release
        # Refuse the first RELEASE, then allow the rollback and cleanup statements.
        if action == sqlite3.SQLITE_SAVEPOINT and operation == "RELEASE" and reject_release:
            reject_release = False
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorizer)
    try:
        with pytest.raises(IndexOperationError, match="not authorized"):
            manager.create_index(definition())
    finally:
        connection.set_authorizer(None)
    assert stored_sql(connection) is None
    assert connection.in_transaction == outer_transaction
    expected_title = "pending" if outer_transaction else "First"
    assert connection.execute("SELECT title FROM items WHERE id = 1").fetchone() == (expected_title,)


def test_metadata_distinguishes_constraint_indexes_from_user_unique_indexes(index_db):
    _, connection, manager = index_db
    connection.execute("CREATE TABLE constraints(name TEXT PRIMARY KEY, code TEXT UNIQUE)")
    snapshot = manager.create_index(definition(("code",), table="constraints", unique=True))
    indexes = [index for index in snapshot.indexes if index.table_name == "constraints"]
    assert {index.origin for index in indexes} == {"c", "u", "pk"}
    assert all(index.unique for index in indexes)
    assert all(not index.can_edit for index in indexes if index.origin in {"u", "pk"})
    user_index = next(index for index in indexes if index.origin == "c")
    assert user_index.can_edit


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE INDEX ix_special ON items(title) WHERE title IS NOT NULL",
        "CREATE INDEX ix_special ON items(lower(title))",
        "CREATE INDEX ix_special ON items(title COLLATE NOCASE)",
    ],
)
def test_metadata_keeps_special_definitions_visible_without_simplifying_them(index_db, sql):
    _, connection, manager = index_db
    connection.execute(sql)
    index = next(index for index in manager.get_schema().indexes if index.name == "ix_special")
    assert index.is_user_created
    assert not index.can_edit
    assert index.sql == sql


def test_metadata_handles_without_rowid_primary_key(index_db):
    _, connection, manager = index_db
    connection.execute("CREATE TABLE pairs(a TEXT, b TEXT, PRIMARY KEY(a, b)) WITHOUT ROWID")
    primary_key = next(index for index in manager.get_schema().indexes if index.table_name == "pairs")
    assert primary_key.origin == "pk"
    assert primary_key.columns == (IndexColumn("a"), IndexColumn("b"))
    assert not primary_key.can_edit


def test_empty_database_has_empty_schema(tmp_path):
    with closing(sqlite3.connect(tmp_path / "empty.sqlite3")) as connection:
        snapshot = SchemaManager(SQLiteIndexRepository(connection)).get_schema()
        assert snapshot.tables == ()
        assert snapshot.indexes == ()


@pytest.mark.parametrize("unique", [False, True])
def test_uc08_deletes_user_index_without_changing_table_or_rows(index_db, unique):
    path, connection, manager = index_db
    manager.create_index(definition(unique=unique))
    table_sql = connection.execute("SELECT sql FROM sqlite_schema WHERE name = 'items'").fetchone()
    rows = connection.execute("SELECT * FROM items ORDER BY id").fetchall()
    snapshot = manager.delete_index("ix_items")
    assert all(index.name != "ix_items" for index in snapshot.indexes)
    assert not snapshot.in_transaction
    assert not connection.in_transaction
    assert stored_sql(connection) is None
    assert connection.execute("SELECT sql FROM sqlite_schema WHERE name = 'items'").fetchone() == table_sql
    assert connection.execute("SELECT * FROM items ORDER BY id").fetchall() == rows
    with closing(sqlite3.connect(path)) as reopened:
        assert stored_sql(reopened) is None
        assert reopened.execute("SELECT * FROM items ORDER BY id").fetchall() == rows


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE INDEX ix_special ON items(title) WHERE title IS NOT NULL",
        "CREATE INDEX ix_special ON items(lower(title))",
        "CREATE INDEX ix_special ON items(title COLLATE NOCASE)",
    ],
)
def test_uc08_can_delete_special_user_indexes(index_db, sql):
    _, connection, manager = index_db
    connection.execute(sql)
    assert not next(index for index in manager.get_schema().indexes if index.name == "ix_special").can_edit
    manager.delete_index("ix_special")
    assert stored_sql(connection, "ix_special") is None
    assert connection.execute("SELECT count(*) FROM items").fetchone() == (4,)


@pytest.mark.parametrize(
    "table_sql",
    [
        "CREATE TABLE protected(name TEXT UNIQUE)",
        "CREATE TABLE protected(a TEXT, b TEXT, PRIMARY KEY(a, b))",
        "CREATE TABLE protected(a TEXT, b TEXT, PRIMARY KEY(a, b)) WITHOUT ROWID",
    ],
)
def test_uc08_refuses_to_delete_an_index_owned_by_a_constraint(index_db, table_sql):
    _, connection, manager = index_db
    connection.execute(table_sql)
    index = next(index for index in manager.get_schema().indexes if index.table_name == "protected")
    before = connection.execute("SELECT name, sql FROM sqlite_schema ORDER BY name").fetchall()
    with pytest.raises(IndexOperationError, match="UC-04"):
        manager.delete_index(index.name)
    assert connection.execute("SELECT name, sql FROM sqlite_schema ORDER BY name").fetchall() == before
    assert any(item.name == index.name for item in manager.get_schema().indexes)
    assert not connection.in_transaction


def test_uc08_missing_index_reports_error_without_changing_schema(index_db):
    _, connection, manager = index_db
    manager.create_index(definition())
    before = stored_sql(connection)
    with pytest.raises(IndexOperationError, match="does not exist"):
        manager.delete_index("missing")
    assert stored_sql(connection) == before
    assert not connection.in_transaction


def test_uc08_deletes_quoted_name_without_treating_it_as_sql(index_db):
    _, connection, manager = index_db
    name = 'Удалить "индекс"; DROP TABLE items; --'
    manager.create_index(definition(name=name))
    manager.delete_index(name)
    assert stored_sql(connection, name) is None
    assert connection.execute("SELECT count(*) FROM items").fetchone() == (4,)


def test_uc08_accepts_ascii_case_in_index_name(index_db):
    _, connection, manager = index_db
    manager.create_index(definition())
    manager.delete_index("IX_ITEMS")
    assert stored_sql(connection) is None


def test_uc08_does_not_delete_same_named_temporary_index(index_db):
    _, connection, manager = index_db
    manager.create_index(definition())
    connection.execute("CREATE TEMP TABLE other_table(value TEXT)")
    connection.execute("CREATE INDEX temp.ix_items ON other_table(value)")
    manager.delete_index("ix_items")
    assert stored_sql(connection) is None
    assert connection.execute("SELECT name FROM temp.sqlite_schema WHERE type = 'index'").fetchall() == [("ix_items",)]


def test_uc08_read_only_error_preserves_index_and_connection(index_db):
    path, _, manager = index_db
    manager.create_index(definition())
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        readonly_manager = SchemaManager(SQLiteIndexRepository(connection))
        before = stored_sql(connection)
        with pytest.raises(IndexOperationError, match="readonly"):
            readonly_manager.delete_index("ix_items")
        assert stored_sql(connection) == before
        assert not connection.in_transaction
        assert connection.execute("SELECT count(*) FROM items").fetchone() == (4,)


def test_uc08_delete_stays_inside_existing_transaction(index_db):
    path, connection, manager = index_db
    manager.create_index(definition())
    original_sql = stored_sql(connection)
    connection.execute("UPDATE items SET title = 'pending' WHERE id = 1")
    snapshot = manager.delete_index("ix_items")
    assert snapshot.in_transaction
    assert connection.in_transaction
    assert stored_sql(connection) is None
    assert connection.execute("SELECT title FROM items WHERE id = 1").fetchone() == ("pending",)
    with closing(sqlite3.connect(path)) as observer:
        assert stored_sql(observer) == original_sql
        assert observer.execute("SELECT title FROM items WHERE id = 1").fetchone() == ("First",)
    connection.rollback()
    assert stored_sql(connection) == original_sql
    assert connection.execute("SELECT title FROM items WHERE id = 1").fetchone() == ("First",)


def test_uc08_failed_schema_refresh_restores_deleted_index(index_db, monkeypatch):
    _, connection, manager = index_db
    manager.create_index(definition())
    before = stored_sql(connection)

    def fail_refresh():
        raise sqlite3.OperationalError("cannot read updated schema")

    with monkeypatch.context() as patch:
        patch.setattr(manager, "get_schema", fail_refresh)
        with pytest.raises(IndexOperationError, match="cannot read updated schema"):
            manager.delete_index("ix_items")
    assert stored_sql(connection) == before
    assert not connection.in_transaction
    assert manager.delete_index("ix_items").indexes == ()


@pytest.mark.parametrize("outer_transaction", [False, True])
def test_uc08_release_error_restores_index_and_preserves_outer_transaction(index_db, outer_transaction):
    _, connection, manager = index_db
    manager.create_index(definition())
    before = stored_sql(connection)
    if outer_transaction:
        connection.execute("UPDATE items SET title = 'pending' WHERE id = 1")
    reject_release = True

    def authorizer(action, operation, name, database, trigger):
        nonlocal reject_release
        if action == sqlite3.SQLITE_SAVEPOINT and operation == "RELEASE" and reject_release:
            reject_release = False
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorizer)
    try:
        with pytest.raises(IndexOperationError, match="not authorized"):
            manager.delete_index("ix_items")
    finally:
        connection.set_authorizer(None)
    assert stored_sql(connection) == before
    assert connection.in_transaction == outer_transaction
    expected_title = "pending" if outer_transaction else "First"
    assert connection.execute("SELECT title FROM items WHERE id = 1").fetchone() == (expected_title,)


def test_uc08_sqlite_rejection_keeps_the_index(index_db):
    _, connection, manager = index_db
    manager.create_index(definition())
    before = stored_sql(connection)

    def authorizer(action, operation, name, database, trigger):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_DROP_INDEX else sqlite3.SQLITE_OK

    connection.set_authorizer(authorizer)
    try:
        with pytest.raises(IndexOperationError, match="not authorized"):
            manager.delete_index("ix_items")
    finally:
        connection.set_authorizer(None)
    assert stored_sql(connection) == before
    assert not connection.in_transaction
