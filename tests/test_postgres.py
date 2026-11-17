"""Run the same domain contract against a disposable PostgreSQL database in CI."""
import os
from pathlib import Path
import unittest

import test_store_db
import test_tickets


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "Set TEST_DATABASE_URL to a disposable local PostgreSQL database")
class PostgresContractTests(test_store_db.StoreDBTests):
    def setUp(self):
        import psycopg
        from postgres_db import PostgresStoreDB
        url = os.environ["TEST_DATABASE_URL"]
        from psycopg.conninfo import conninfo_to_dict
        if conninfo_to_dict(url).get("host") not in ("localhost", "127.0.0.1"):
            raise RuntimeError("Destructive test fixture only accepts local disposable PostgreSQL")
        with psycopg.connect(url) as connection:
            connection.execute(Path("sql/schema.sql").read_text(encoding="utf-8"), prepare=False)
            connection.execute("TRUNCATE storebot.outbox,storebot.audit,storebot.payment_events,storebot.orders,storebot.products,storebot.settings RESTART IDENTITY CASCADE")
        self.db = PostgresStoreDB(url, sslmode="disable")
        self.db.add_product(1, 99, "KEY", "License", "Test product", 10000, 5)

    def tearDown(self):
        pass

    def test_snapshot_price_and_restart(self):
        from postgres_db import PostgresStoreDB
        row = self.order()
        self.db.edit_product(1, 99, "KEY", price=50000)
        reopened = PostgresStoreDB(os.environ["TEST_DATABASE_URL"], sslmode="disable")
        self.assertEqual(reopened.order(row["code"])["total"], 10000)
        self.assertEqual(reopened.products(1)[0]["stock"], 4)


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "Set TEST_DATABASE_URL to a disposable local PostgreSQL database")
class PostgresTicketContractTests(test_tickets.TicketDBTests):
    def setUp(self):
        PostgresContractTests.setUp(self)
        self.db.init_tickets()
        self.db.configure(1, 99, 50, 60)
        self.db.configure_tickets(1, 99, 70)
        self.order = self.db.reserve(1, 10, "KEY", 1, "manual", ttl=86400)

    def tearDown(self):
        pass

    def test_persists_after_restart_without_changing_order_or_stock(self):
        from postgres_db import PostgresStoreDB
        self.bind()
        reopened = PostgresStoreDB(os.environ["TEST_DATABASE_URL"], sslmode="disable")
        reopened.init_tickets()
        self.assertEqual(reopened.ticket_category(1), 70)
        self.assertEqual(reopened.settings(1)["staff_role_id"], 50)
        self.assertEqual(reopened.ticket(1, code=self.order["code"])["channel_id"], 100)
        self.assertEqual(reopened.order(self.order["code"]), self.order)
        self.assertEqual(reopened.products(1)[0]["stock"], 4)
