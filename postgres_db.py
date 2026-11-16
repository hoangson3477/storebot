"""PostgreSQL transport for the shared, transaction-based store domain operations."""
from contextlib import contextmanager

from store_db import StoreDB


class SQLConnection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, parameters=()):
        # Shared queries are application-owned SQL, never user-supplied SQL strings.
        if sql.startswith("INSERT OR IGNORE INTO outbox"):
            sql = sql.replace("INSERT OR IGNORE", "INSERT", 1) + " ON CONFLICT(order_code,status) DO NOTHING"
        return self.connection.execute(sql.replace("?", "%s"), parameters)


class PostgresStoreDB(StoreDB):
    def __init__(self, url, *, sslmode="require"):
        import psycopg
        if sslmode != "require":
            from psycopg.conninfo import conninfo_to_dict
            if sslmode != "disable" or conninfo_to_dict(url).get("host") not in ("127.0.0.1", "localhost"):
                raise ValueError("Only local integration tests may disable TLS")
        self.url = url
        self.sslmode = sslmode
        self.integrity_errors = (psycopg.errors.UniqueViolation,)
        with self.connect() as db:
            db.execute("SELECT code FROM orders LIMIT 0")

    @contextmanager
    def connect(self, write=False):
        import psycopg
        from psycopg.rows import dict_row
        # Supabase's pooler pools physical server connections. No automatic prepared statements.
        with psycopg.connect(self.url, row_factory=dict_row, prepare_threshold=None,
                             sslmode=self.sslmode, connect_timeout=10) as connection:
            connection.execute("SET LOCAL search_path TO storebot, pg_catalog")
            connection.execute("SET LOCAL statement_timeout = '15s'")
            connection.execute("SET LOCAL lock_timeout = '10s'")
            if write:
                # Matches SQLite's serialized writes across processes/connections, not just threads.
                # Deliberate MVP tradeoff: correct stock/order transitions, modest write throughput.
                connection.execute("SELECT pg_advisory_xact_lock(347734770001)")
            yield SQLConnection(connection)
