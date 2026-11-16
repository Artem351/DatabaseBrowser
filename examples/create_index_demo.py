"""Create a small database for trying all three index scenarios."""

import sqlite3
from contextlib import closing
from pathlib import Path

path = Path("index_demo.sqlite3").resolve()
if path.exists():
    raise SystemExit(f"File already exists; left unchanged: {path}")

with closing(sqlite3.connect(path)) as connection, connection:
    connection.execute(
        "CREATE TABLE books("
        "id INTEGER PRIMARY KEY, title TEXT NOT NULL, genre TEXT, isbn TEXT UNIQUE)"
    )
    connection.executemany(
        "INSERT INTO books(title, genre, isbn) VALUES (?, ?, ?)",
        [
            ("Clean Code", "programming", "9780132350884"),
            ("Applying UML and Patterns", "programming", "9780131489066"),
            ("The Hobbit", "fiction", "9780261103344"),
            ("A Study in Scarlet", "fiction", None),
        ],
    )
    connection.execute("CREATE INDEX ix_books_genre ON books(genre)")

print(f"Created: {path}")
print("Try changing ix_books_genre to UNIQUE: repeated genres should keep the old index intact.")
