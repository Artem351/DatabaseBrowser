# Implement SQLite connection management (UC-01, UC-16, UC-17)

Users can open an existing SQLite file, save and reopen its connection profile, edit a saved profile, and delete a profile without deleting the database file.

The change includes a PyQt6 Database tab and profile dialog, SQLite profile storage, file format and integrity checks, and one background database worker. Invalid files and storage failures are reported without crashing the application. Editing a saved profile keeps the existing session on its original file until reconnection.

Validation: 35 pytest tests passed, including service integration tests against temporary SQLite files and Qt interaction tests. Editable installation, dependency checks, window rendering and normal shutdown passed on Linux with Python 3.14.9; the same tests previously passed on Python 3.12.14. Both Python branches remain supported. Windows validation remains to be performed.

Scope: connection management only. Other tabs are disabled integration points. The project-wide CPU usage limit and executable packaging are outside this change.
