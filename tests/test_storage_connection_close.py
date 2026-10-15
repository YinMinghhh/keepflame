import sqlite3

from keepflame.storage.sqlite import SQLiteStorage


def test_connect_closes_connection(tmp_path):
    store = SQLiteStorage(tmp_path / "s.sqlite3")
    with store._connect() as connection:
        connection.execute("SELECT 1")
    try:
        connection.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return
    raise AssertionError("connection left open after _connect() block")
