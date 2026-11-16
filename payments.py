"""payOS adapter using the official SDK (not return URL parameters as proof)."""
import asyncio
import logging
import os
import time
import weakref
from urllib.parse import urlparse

from aiohttp import web

from store_db import StoreError

log = logging.getLogger(__name__)


class PaymentService:
    def __init__(self, db):
        self.db = db
        self.mode = os.getenv("PAYMENT_PROVIDER", "manual").lower()
        if self.mode not in ("manual", "payos"):
            raise ValueError("PAYMENT_PROVIDER must be manual or payos")
        self.client = None
        self.runner = None
        self.locks = weakref.WeakValueDictionary()
        if self.mode == "payos":
            from payos import PayOS

            fields = [os.getenv(k) for k in ("PAYOS_CLIENT_ID", "PAYOS_API_KEY", "PAYOS_CHECKSUM_KEY")]
            self.public_url = os.getenv("PAYMENT_PUBLIC_URL", "").rstrip("/")
            parsed = urlparse(self.public_url)
            if (not all(fields) or parsed.scheme != "https" or not parsed.netloc or parsed.query
                    or parsed.fragment or parsed.path not in ("", "/") or parsed.username):
                raise ValueError("payOS requires credentials and an HTTPS PAYMENT_PUBLIC_URL without query/fragment")
            self.client = PayOS(client_id=fields[0], api_key=fields[1], checksum_key=fields[2],
                                timeout=15.0, max_retries=0)

    async def start(self):
        if not self.client:
            return
        app = self.application()
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        try:
            await web.TCPSite(self.runner, os.getenv("PAYMENT_HOST", "127.0.0.1"),
                             int(os.getenv("PAYMENT_PORT", "8080"))).start()
        except BaseException:
            await self.runner.cleanup()
            self.runner = None
            raise

    def application(self):
        app = web.Application(client_max_size=64 * 1024)
        app.router.add_post("/webhooks/payos", self.webhook)
        app.router.add_get("/payment/return", self.return_page)
        app.router.add_get("/payment/cancel", self.return_page)
        app.router.add_get("/health", self.health)
        return app

    async def close(self):
        if self.runner:
            await self.runner.cleanup()
        if self.client:
            await asyncio.to_thread(self.client.close)

    async def health(self, request):
        return web.json_response({"ok": True})

    async def return_page(self, request):
        return web.Response(text="Bạn có thể quay lại Discord và dùng /store order để kiểm tra. "
                            "Trạng thái được xác minh trực tiếp với payOS.", content_type="text/plain")

    async def ensure_payment(self, order):
        if not self.client or order["provider"] != "payos":
            raise StoreError("Đơn này không dùng payOS hoặc payOS chưa được bật.")
        lock = self.locks.setdefault(order["code"], asyncio.Lock())
        async with lock:
            order = await asyncio.to_thread(self.db.order, order["code"])
            if order["status"] != "pending":
                raise StoreError("Chỉ tạo thanh toán cho đơn đang chờ.")
            if order["link_id"]:
                return order
            if order["expires_at"] <= int(time.time()):
                raise StoreError("Đơn đã qua thời gian tạo link. Nhờ quản trị viên kiểm tra tại payOS trước khi hủy.")
            from payos.types import CreatePaymentLinkRequest

            request = CreatePaymentLinkRequest(
                order_code=order["code"], amount=order["total"],
                description="DH" + str(order["code"])[-7:],  # 9 ASCII chars for bank compatibility.
                cancel_url=self.public_url + "/payment/cancel",
                return_url=self.public_url + "/payment/return", expired_at=order["expires_at"],
            )
            # Always reuse the same order code on retry. On ambiguous failure stock stays reserved.
            result = await asyncio.to_thread(self.client.payment_requests.create, request)
            data = result.model_dump_camel_case()
            parsed = urlparse(data["checkoutUrl"])
            if parsed.scheme != "https" or not parsed.hostname or not (
                    parsed.hostname == "payos.vn" or parsed.hostname.endswith(".payos.vn")):
                raise StoreError("payOS trả link thanh toán không hợp lệ.")
            await asyncio.to_thread(self.db.bind_payment, order["code"], data)
            return await asyncio.to_thread(self.db.order, order["code"])

    async def sync(self, order):
        if not self.client or order["provider"] != "payos":
            return order
        # SDK verifies API response signatures. GET also recovers a link created before a crash.
        remote = await asyncio.to_thread(self.client.payment_requests.get, order["code"])
        data = remote.model_dump_camel_case()
        if not order["link_id"]:
            # GET omits QR details; bind only verified identity for reconciliation/cancellation.
            await asyncio.to_thread(self.db.bind_payment, order["code"], {
                "orderCode": data["orderCode"], "amount": data["amount"], "currency": "VND",
                "paymentLinkId": data["id"], "checkoutUrl": "https://pay.payos.vn/web/" + data["id"],
                "qrCode": "", "description": "DH" + str(order["code"])[-7:],
            })
        await asyncio.to_thread(self.db.reconcile, order["code"], data)
        return await asyncio.to_thread(self.db.order, order["code"])

    async def cancel(self, order):
        if order["provider"] == "manual":
            raise StoreError("Dùng hủy đơn thủ công.")
        if not self.client:
            raise StoreError("Cần bật payOS để hủy đơn này an toàn.")
        if order["status"] != "pending":
            raise StoreError("Chỉ hủy đơn đang chờ. Đơn đã nhận tiền cần nhân viên xử lý.")
        await asyncio.to_thread(self.client.payment_requests.cancel, order["code"])
        # Re-fetch after cancellation: a transfer could win the race with the cancel request.
        return await self.sync(order)

    async def webhook(self, request):
        if not self.client:
            raise web.HTTPServiceUnavailable()
        raw = await request.read()
        try:
            # Pass raw bytes so the SDK can enforce the webhook envelope and signature checks.
            event = self.client.webhooks.verify(raw)
            data = event.model_dump_camel_case()
            if data.get("code") != "00":
                return web.json_response({"success": True})
            code, amount = data["orderCode"], data["amount"]
            link, reference = data["paymentLinkId"], data["reference"]
            if type(code) is not int or type(amount) is not int or amount <= 0 or not link or not reference:
                raise ValueError("Invalid payment identifiers")
        except Exception:
            log.warning("Rejected invalid payOS webhook")
            return web.json_response({"success": False}, status=400)
        try:
            result = await asyncio.to_thread(self.db.receive, code, amount, link, reference, data.get("currency"))
        except Exception:
            log.exception("Could not persist payment webhook")
            return web.json_response({"success": False}, status=503)
        if result == "retry":
            return web.json_response({"success": False}, status=503)
        # Unknown signed sample events (sent during webhook registration) are safely acknowledged.
        return web.json_response({"success": True})
