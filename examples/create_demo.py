"""A small database for manually testing the application."""

import sqlite3
from contextlib import closing
from pathlib import Path

path = Path("demo.sqlite3").resolve()
# Running the script again must not change the existing database.
if path.exists():
    raise SystemExit(f"File already exists; left unchanged: {path}")

with closing(sqlite3.connect(path)) as connection, connection:
    connection.execute("CREATE TABLE books(id INTEGER PRIMARY KEY, title TEXT NOT NULL)")
    connection.executemany("INSERT INTO books(title) VALUES (?)", [("Clean Code",), ("Applying UML and Patterns",)])

print(f"Created: {path}")
