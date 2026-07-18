import sqlite3

import pytest

from core.db import close_all_pool_conns, get_conn, get_pool_conn


def test_get_conn_context_manager_closes_connection():
    with get_conn() as conn:
        conn.execute("SELECT 1")

    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_close_all_pool_conns_closes_pooled_connections():
    with get_pool_conn() as conn:
        conn.execute("SELECT 1")

    close_all_pool_conns()

    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
