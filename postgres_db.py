"""PostgreSQL transport for the shared, transaction-based store domain operations."""
from contextlib import contextmanager
import logging
import time

from store_db import StoreDB, DatabaseUnavailable
from network_health import temporary_dns_error

log = logging.getLogger(__name__)


def connect_with_dns_retry(connect):
    """Retry connection establishment only. Never replay a transaction or commit."""
    for attempt in range(1, 4):
        try:
            connection = connect()
        except Exception as exc:
            if not temporary_dns_error(exc):
                raise
            if attempt == 3:
                log.exception("db.dns_unavailable attempts=%s transaction_started=false", attempt)
                raise DatabaseUnavailable(
                    "Host đang lỗi DNS khi kết nối database. Vui lòng thử lại sau; "
                    "nếu đang mua hàng, kiểm tra /store orders trước khi đặt lại.") from exc
            delay = 2 ** (attempt - 1)
            log.warning("db.dns_retry attempt=%s max_attempts=3 delay_seconds=%s transaction_started=false", attempt, delay)
            time.sleep(delay)  # Only called inside DB workers / initial startup.
        else:
            if attempt > 1:
                log.info("db.dns_recovered attempt=%s", attempt)
            return connection


class SQLConnection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, parameters=()):
        # Shared queries are application-owned SQL, never user-supplied SQL strings.
        if sql.startswith("INSERT OR IGNORE INTO outbox"):
            sql = sql.replace("INSERT OR IGNORE", "INSERT", 1) + " ON CONFLICT(order_code,status) DO NOTHING"
        return self.connection.execute(sql.replace("?", "%s"), parameters)


class PostgresStoreDB(StoreDB):
    def init_tickets(self):
        super().init_tickets()
        with self.connect(True) as db:
            for table in ("tickets", "ticket_settings"):
                db.execute(f"ALTER TABLE storebot.{table} ENABLE ROW LEVEL SECURITY")
                db.execute(f"REVOKE ALL ON storebot.{table} FROM PUBLIC, anon, authenticated")

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
        connection = connect_with_dns_retry(lambda: psycopg.connect(
            self.url, row_factory=dict_row, prepare_threshold=None, sslmode=self.sslmode, connect_timeout=10))
        with connection:
            connection.execute("SET LOCAL search_path TO storebot, pg_catalog")
            connection.execute("SET LOCAL statement_timeout = '15s'")
            connection.execute("SET LOCAL lock_timeout = '10s'")
            if write:
                # Matches SQLite's serialized writes across processes/connections, not just threads.
                # Deliberate MVP tradeoff: correct stock/order transitions, modest write throughput.
                connection.execute("SELECT pg_advisory_xact_lock(347734770001)")
            yield SQLConnection(connection)
