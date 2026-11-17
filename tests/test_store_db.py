from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import time
import unittest

from store_db import StoreDB, StoreError


class StoreDBTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.sqlite3"
        self.db = StoreDB(self.path)
        self.db.add_product(1, 99, "KEY", "License", "Test product", 10000, 5)

    def tearDown(self):
        self.temp.cleanup()

    def order(self, provider="payos", buyer=10, quantity=1):
        row = self.db.reserve(1, buyer, "key", quantity, provider)
        if provider == "payos":
            self.db.bind_payment(row["code"], self.link(row))
        return self.db.order(row["code"])

    @staticmethod
    def link(row):
        return {"orderCode": row["code"], "amount": row["total"], "currency": "VND",
                "paymentLinkId": f"link{row['code']}", "checkoutUrl": "https://pay.payos.vn/test",
                "qrCode": "qr", "description": "DH1234567"}

    @staticmethod
    def remote(row, status="PAID", paid=None):
        return {"id": row["link_id"], "orderCode": row["code"], "amount": row["total"],
                "amountPaid": row["total"] if paid is None else paid, "status": status}

    def stock(self):
        return self.db.products(1)[0]["stock"]

    def test_concurrent_last_item_cannot_oversell(self):
        self.db.edit_product(1, 99, "KEY", stock_delta=-4)
        def attempt(buyer):
            try:
                return self.db.reserve(1, buyer, "KEY", 1, "manual")
            except StoreError:
                return None
        with ThreadPoolExecutor(max_workers=6) as pool:
            rows = list(pool.map(attempt, range(20, 26)))
        self.assertEqual(sum(r is not None for r in rows), 1)
        self.assertEqual(self.stock(), 0)

    def test_duplicate_webhooks_emit_one_paid_notice(self):
        row = self.order()
        def receive(_):
            return self.db.receive(row["code"], row["total"], row["link_id"], "same-reference")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(receive, range(4)))
        self.assertEqual(results.count("paid"), 1)
        self.assertEqual(len(self.db.notifications()), 1)
        self.assertEqual(self.stock(), 4)

    def test_wrong_link_currency_or_amount_cannot_mark_paid(self):
        row = self.order()
        self.assertEqual(self.db.receive(row["code"], row["total"], "wrong", "ref1"), "mismatch")
        self.assertEqual(self.db.receive(row["code"], row["total"], row["link_id"], "ref2", "USD"), "mismatch")
        self.assertEqual(self.db.receive(row["code"], 1, row["link_id"], "ref3"), "review")
        self.assertEqual(self.db.order(row["code"])["status"], "review")
        self.assertEqual(self.stock(), 4)

    def test_payment_before_binding_requests_retry(self):
        row = self.db.reserve(1, 10, "KEY", 1, "payos")
        self.assertEqual(self.db.receive(row["code"], row["total"], "link", "ref"), "retry")
        self.assertEqual(self.db.order(row["code"])["status"], "pending")

    def test_guild_isolation(self):
        row = self.order()
        with self.assertRaises(StoreError):
            self.db.order(row["code"], 2)
        with self.assertRaises(StoreError):
            self.db.reserve(2, 10, "KEY", 1, "manual")
        self.assertEqual(self.db.list_orders(2), [])

    def test_snapshot_price_and_restart(self):
        row = self.order()
        self.db.edit_product(1, 99, "KEY", price=50000)
        reopened = StoreDB(self.path)
        self.assertEqual(reopened.order(row["code"])["total"], 10000)
        self.assertEqual(reopened.products(1)[0]["stock"], 4)

    def test_cancel_returns_stock_once(self):
        row = self.order("manual", quantity=2)
        self.assertEqual(self.stock(), 3)
        self.db.manual_cancel(row["code"], 1, 10)
        with self.assertRaises(StoreError):
            self.db.manual_cancel(row["code"], 1, 10)
        self.assertEqual(self.stock(), 5)

    def test_expiry_does_not_release_automatic_orders_without_provider_confirmation(self):
        manual = self.order("manual", buyer=10)
        auto = self.order("payos", buyer=11)
        with self.db.connect(True) as db:
            db.execute("UPDATE orders SET expires_at=?", (int(time.time()) - 1,))
        self.db.maintenance()
        self.assertEqual(self.db.order(manual["code"])["status"], "expired")
        self.assertEqual(self.db.order(auto["code"])["status"], "pending")
        self.assertEqual(self.stock(), 4)

    def test_late_payment_after_remote_cancel_is_review_not_fulfilled(self):
        row = self.order()
        self.db.reconcile(row["code"], self.remote(row, "CANCELLED", 0))
        self.assertEqual(self.stock(), 5)
        self.assertEqual(self.db.receive(row["code"], row["total"], row["link_id"], "late"), "review")
        self.assertEqual(self.stock(), 5)
        with self.assertRaises(StoreError):
            self.db.advance(row["code"], 1, 99, "fulfilling")

    def test_partial_payment_then_verified_full_amount(self):
        row = self.order()
        self.db.receive(row["code"], 5000, row["link_id"], "part1")
        self.assertEqual(self.db.order(row["code"])["status"], "review")
        self.db.reconcile(row["code"], self.remote(row))
        self.assertEqual(self.db.order(row["code"])["status"], "paid")

    def test_fulfillment_requires_paid_and_manual_cannot_override_payos(self):
        row = self.order()
        with self.assertRaises(StoreError):
            self.db.advance(row["code"], 1, 99, "completed")
        with self.assertRaises(StoreError):
            self.db.manual_paid(row["code"], 1, 99)
        self.db.receive(row["code"], row["total"], row["link_id"], "paid")
        self.db.advance(row["code"], 1, 99, "fulfilling")
        self.db.advance(row["code"], 1, 99, "completed")
        self.assertEqual(self.db.revenue(1)["completed"], 10000)

    def test_reconciliation_cannot_apply_another_link(self):
        row = self.order()
        remote = self.remote(row)
        remote["id"] = "wrong-link"
        with self.assertRaises(StoreError):
            self.db.reconcile(row["code"], remote)
        self.assertEqual(self.db.order(row["code"])["status"], "pending")

    def test_payment_reference_cannot_be_reused_for_other_order(self):
        first, second = self.order(buyer=10), self.order(buyer=11)
        self.db.receive(first["code"], first["total"], first["link_id"], "unique")
        self.assertEqual(self.db.receive(second["code"], second["total"], second["link_id"], "unique"), "mismatch")
        self.assertEqual(self.db.order(second["code"])["status"], "pending")

    def test_partial_then_full_is_overpayment_and_stays_review(self):
        row = self.order()
        self.db.receive(row["code"], 5000, row["link_id"], "partial")
        self.db.receive(row["code"], 10000, row["link_id"], "full")
        self.assertEqual(self.db.order(row["code"])["status"], "review")
        self.db.reconcile(row["code"], self.remote(row))
        self.assertEqual(self.db.order(row["code"])["status"], "review")

    def test_partial_payments_sum_to_total(self):
        row = self.order()
        self.db.receive(row["code"], 5000, row["link_id"], "partial1")
        self.db.receive(row["code"], 5000, row["link_id"], "partial2")
        self.assertEqual(self.db.order(row["code"])["status"], "paid")

    def test_expired_unbound_order_can_be_resolved_with_audit(self):
        row = self.db.reserve(1, 10, "KEY", 1, "payos")
        with self.db.connect(True) as db:
            db.execute("UPDATE orders SET expires_at=? WHERE code=?", (int(time.time()) - 1, row["code"]))
        self.db.maintenance()
        self.assertEqual(self.db.order(row["code"])["status"], "review")
        self.db.resolve_unpaid(row["code"], 1, 99, "Checked merchant dashboard: no request or funds received")
        self.assertEqual(self.stock(), 5)
        with self.assertRaises(StoreError):
            self.db.resolve_unpaid(row["code"], 1, 99, "Repeated request should fail")

    def test_cannot_resolve_review_with_received_money(self):
        row = self.order()
        self.db.receive(row["code"], 100, row["link_id"], "partial")
        with self.db.connect(True) as db:
            db.execute("UPDATE orders SET expires_at=? WHERE code=?", (int(time.time()) - 1, row["code"]))
        with self.assertRaises(StoreError):
            self.db.resolve_unpaid(row["code"], 1, 99, "Cannot ignore actual funds")

    def test_reconciliation_does_not_reset_fulfillment_after_payment_review(self):
        row = self.order()
        self.db.receive(row["code"], row["total"], row["link_id"], "initial-payment")
        self.db.advance(row["code"], 1, 99, "fulfilling")
        self.db.reconcile(row["code"], self.remote(row, "PENDING", row["total"]))
        self.assertEqual(self.db.order(row["code"])["status"], "review")
        self.db.reconcile(row["code"], self.remote(row))
        self.assertEqual(self.db.order(row["code"])["status"], "review")
        with self.assertRaises(StoreError):
            self.db.advance(row["code"], 1, 99, "fulfilling")


if __name__ == "__main__":
    unittest.main()
