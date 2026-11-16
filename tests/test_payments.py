import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from store_db import StoreDB

try:
    from aiohttp.test_utils import AioHTTPTestCase
    from payos import PayOS
    from payments import PaymentService
    AVAILABLE = True
except ImportError:
    AioHTTPTestCase = unittest.IsolatedAsyncioTestCase
    AVAILABLE = False


@unittest.skipUnless(AVAILABLE, "Install requirements.txt for payOS HTTP/SDK tests")
class PaymentTests(AioHTTPTestCase):
    async def get_application(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict("os.environ", {"PAYMENT_PROVIDER": "payos", "PAYOS_CLIENT_ID": "test-client",
            "PAYOS_API_KEY": "test-api-key", "PAYOS_CHECKSUM_KEY": "test-checksum-key",
            "PAYMENT_PUBLIC_URL": "https://merchant.example"})
        self.env.start()
        self.db = StoreDB(Path(self.temp.name) / "test.sqlite3")
        self.db.add_product(1, 9, "TEST", "Test", "", 10000, 5)
        self.order = self.db.reserve(1, 10, "TEST", 1, "payos")
        self.link_id = "124c33293c934a85be5b7f8761a27a07"
        self.db.bind_payment(self.order["code"], {"orderCode": self.order["code"], "amount": 10000,
            "currency": "VND", "paymentLinkId": self.link_id,
            "checkoutUrl": "https://pay.payos.vn/web/" + self.link_id, "qrCode": "TEST", "description": "DH1234567"})
        self.service = PaymentService(self.db)
        return self.service.application()

    async def asyncTearDown(self):
        await super().asyncTearDown()
        await self.service.close()
        self.env.stop()
        self.temp.cleanup()

    def event(self, amount=10000, code=None, link=None, reference="TESTREF"):
        data = {"orderCode": code or self.order["code"], "amount": amount, "description": "DH1234567",
                "accountNumber": "12345678", "reference": reference, "transactionDateTime": "2026-11-16 10:00:00",
                "currency": "VND", "paymentLinkId": link or self.link_id, "code": "00", "desc": "success",
                "counterAccountBankId": "", "counterAccountBankName": "", "counterAccountName": "",
                "counterAccountNumber": "", "virtualAccountName": "", "virtualAccountNumber": ""}
        signature = self.service.client.crypto.create_signature_from_object(data, "test-checksum-key")
        return {"code": "00", "desc": "success", "success": True, "data": data, "signature": signature}

    async def test_signed_webhook_paid_and_duplicate(self):
        event = self.event()
        for _ in range(2):
            response = await self.client.post("/webhooks/payos", json=event)
            self.assertEqual(response.status, 200)
        self.assertEqual(self.db.order(self.order["code"])["status"], "paid")
        self.assertEqual(len(self.db.notifications()), 1)

    async def test_modified_payload_rejected(self):
        event = self.event()
        event["data"]["amount"] = 9000
        response = await self.client.post("/webhooks/payos", json=event)
        self.assertEqual(response.status, 400)
        self.assertEqual(self.db.order(self.order["code"])["status"], "pending")

    async def test_invalid_json_rejected(self):
        response = await self.client.post("/webhooks/payos", data=b"not-json")
        self.assertEqual(response.status, 400)

    async def test_success_envelope_cannot_override_signature(self):
        event = self.event()
        event["success"] = False
        response = await self.client.post("/webhooks/payos", json=event)
        self.assertIn(response.status, (200, 400))
        self.assertEqual(self.db.order(self.order["code"])["status"], "pending")

    async def test_signed_wrong_amount_is_review(self):
        response = await self.client.post("/webhooks/payos", json=self.event(amount=9999))
        self.assertEqual(response.status, 200)
        self.assertEqual(self.db.order(self.order["code"])["status"], "review")

    async def test_signed_unknown_sample_has_no_effect(self):
        response = await self.client.post("/webhooks/payos", json=self.event(code=123))
        self.assertEqual(response.status, 200)
        self.assertEqual(self.db.order(self.order["code"])["status"], "pending")

    async def test_return_url_is_not_payment_proof(self):
        response = await self.client.get(f"/payment/return?orderCode={self.order['code']}&status=PAID")
        self.assertEqual(response.status, 200)
        self.assertEqual(self.db.order(self.order["code"])["status"], "pending")

    async def test_database_failure_requests_provider_retry(self):
        with patch.object(self.db, "receive", side_effect=OSError("disk unavailable")):
            response = await self.client.post("/webhooks/payos", json=self.event())
        self.assertEqual(response.status, 503)
        self.assertEqual(self.db.order(self.order["code"])["status"], "pending")

    async def test_create_timeout_retains_order_and_stock(self):
        order = self.db.reserve(1, 11, "TEST", 1, "payos")
        with patch.object(self.service.client.payment_requests, "create", side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                await self.service.ensure_payment(order)
        self.assertEqual(self.db.order(order["code"])["status"], "pending")
        self.assertEqual(self.db.products(1)[0]["stock"], 3)

    async def test_create_uses_same_order_and_provider_qr(self):
        order = self.db.reserve(1, 11, "TEST", 1, "payos")
        returned = {"orderCode": order["code"], "amount": order["total"], "currency": "VND",
            "paymentLinkId": "another-link", "checkoutUrl": "https://pay.payos.vn/web/another-link",
            "qrCode": "PROVIDER-QR", "description": "PROVIDER-DESCRIPTION"}
        result = Mock()
        result.model_dump_camel_case.return_value = returned
        with patch.object(self.service.client.payment_requests, "create", return_value=result) as create:
            first = await self.service.ensure_payment(order)
            second = await self.service.ensure_payment(order)
        self.assertEqual(create.call_count, 1)
        self.assertEqual(create.call_args.args[0].order_code, order["code"])
        self.assertEqual(first["qr_code"], "PROVIDER-QR")
        self.assertEqual(second["description"], "PROVIDER-DESCRIPTION")


if __name__ == "__main__":
    unittest.main()
