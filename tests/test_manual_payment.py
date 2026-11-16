import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlparse, parse_qs

from manual_payment import BankDetails
from store_db import StoreDB, StoreError


class ManualPaymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.db"
        self.db = StoreDB(self.path)
        self.db.add_product(1, 99, "TEST", "Test", "", 10000, 20)
        self.bank = BankDetails("970436", "00123456789", "TEST STORE")

    def tearDown(self):
        self.temp.cleanup()

    def test_code_qr_snapshot_and_restart(self):
        row = self.db.reserve(1, 10, "TEST", 2, "manual", 86400, self.bank)
        self.assertRegex(row["description"], r"^[A-Z2-9]{6}$")
        query = parse_qs(urlparse(row["checkout_url"]).query)
        self.assertEqual(query["amount"], ["20000"])
        self.assertEqual(query["addInfo"], [row["description"]])
        self.assertIn("00123456789", row["checkout_url"])
        reopened = StoreDB(self.path)
        self.assertEqual(reopened.order(row["code"])["checkout_url"], row["checkout_url"])
        self.assertEqual(reopened.transfer_order(row["description"].lower(), 1)["code"], row["code"])
        with self.assertRaises(StoreError):
            reopened.transfer_order(row["description"], 2)
        self.assertEqual(row["status"], "pending")
        self.db.manual_paid(row["code"], 1, 99)
        self.assertEqual(self.db.order(row["code"])["status"], "paid")
        with self.assertRaises(StoreError):
            self.db.manual_paid(row["code"], 1, 99)

    def test_random_collision_retries_and_does_not_reuse_cancelled_memo(self):
        first = self.db.reserve(1, 10, "TEST", 1, "manual", bank=self.bank)
        self.db.manual_cancel(first["code"], 1, 10)
        next_memo = "ZZZZZZ" if first["description"] != "ZZZZZZ" else "AAAAAA"
        with patch("store_db.secrets.choice", side_effect=list(first["description"] + next_memo)):
            second = self.db.reserve(1, 11, "TEST", 1, "manual", bank=self.bank)
        self.assertEqual(second["description"], next_memo)

    def test_missing_bank_configuration_is_rejected(self):
        with patch.dict(os.environ, {"BANK_BIN": "", "BANK_ACCOUNT": "", "BANK_ACCOUNT_NAME": ""}):
            with self.assertRaises(ValueError):
                BankDetails.from_env()

    def test_qr_encodes_name_and_rejects_invalid_memo(self):
        url = self.bank.qr_url(10000, "ABC234")
        self.assertEqual(parse_qs(urlparse(url).query)["accountName"], ["TEST STORE"])
        with self.assertRaises(ValueError):
            self.bank.qr_url(10000, "bad&memo")
