"""Durable store state. Each write takes a SQLite transaction, including stock changes."""
from contextlib import contextmanager
from pathlib import Path
import secrets
import sqlite3
import time


class StoreError(ValueError):
    pass


class StoreDB:
    integrity_errors = (sqlite3.IntegrityError,)
    def __init__(self, path="data/store.sqlite3"):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (
                guild_id INTEGER PRIMARY KEY, staff_role_id INTEGER, log_channel_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, sku TEXT NOT NULL,
                name TEXT NOT NULL, description TEXT NOT NULL, price INTEGER NOT NULL CHECK(price>0),
                stock INTEGER NOT NULL CHECK(stock>=0), active INTEGER NOT NULL DEFAULT 1,
                UNIQUE(guild_id,sku)
            );
            CREATE TABLE IF NOT EXISTS orders (
                code INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, buyer_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL REFERENCES products(id), name TEXT NOT NULL,
                unit_price INTEGER NOT NULL, quantity INTEGER NOT NULL, total INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', provider TEXT NOT NULL,
                reserved INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL, link_id TEXT UNIQUE, checkout_url TEXT,
                qr_code TEXT, description TEXT, account_number TEXT, account_name TEXT,
                paid_at INTEGER, review_reason TEXT, checked_at INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS orders_guild ON orders(guild_id,created_at);
            CREATE UNIQUE INDEX IF NOT EXISTS manual_transfer_code ON orders(description)
                WHERE provider='manual' AND description IS NOT NULL;
            CREATE TABLE IF NOT EXISTS payment_events (
                reference TEXT PRIMARY KEY, order_code INTEGER NOT NULL,
                amount INTEGER NOT NULL, created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, actor_id INTEGER,
                action TEXT NOT NULL, detail TEXT NOT NULL, created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY, order_code INTEGER NOT NULL REFERENCES orders(code),
                status TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, retry_at INTEGER NOT NULL DEFAULT 0,
                UNIQUE(order_code,status)
            );
            """)

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def audit(db, guild, actor, action, detail):
        db.execute("INSERT INTO audit(guild_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
                   (guild, actor, action, detail, int(time.time())))

    @staticmethod
    def notice(db, code, status):
        db.execute("INSERT OR IGNORE INTO outbox(order_code,status) VALUES(?,?)", (code, status))

    def settings(self, guild):
        with self.connect() as db:
            row = db.execute("SELECT * FROM settings WHERE guild_id=?", (guild,)).fetchone()
            return dict(row) if row else {}

    def configure(self, guild, actor, staff, channel):
        with self.connect(True) as db:
            db.execute("INSERT INTO settings VALUES(?,?,?) ON CONFLICT(guild_id) DO UPDATE SET "
                       "staff_role_id=excluded.staff_role_id,log_channel_id=excluded.log_channel_id",
                       (guild, staff, channel))
            self.audit(db, guild, actor, "configure", f"staff={staff}, log={channel}")

    def add_product(self, guild, actor, sku, name, description, price, stock):
        if not (1 <= len(sku) <= 32 and sku.isascii() and all(c.isalnum() or c in "-_" for c in sku)):
            raise StoreError("SKU cần 1–32 ký tự chữ/số ASCII, dấu - hoặc _.")
        if not 1 <= len(name.strip()) <= 100 or len(description) > 1000:
            raise StoreError("Tên cần 1–100 ký tự; mô tả tối đa 1000 ký tự.")
        if not 1000 <= price <= 1_000_000_000 or not 0 <= stock <= 1_000_000:
            raise StoreError("Giá từ 1.000 đến 1 tỷ VND; tồn kho từ 0 đến 1 triệu.")
        with self.connect(True) as db:
            try:
                db.execute("INSERT INTO products(guild_id,sku,name,description,price,stock) VALUES(?,?,?,?,?,?)",
                           (guild, sku.upper(), name.strip(), description, price, stock))
            except self.integrity_errors as exc:
                raise StoreError("SKU đã tồn tại trong server.") from exc
            self.audit(db, guild, actor, "add_product", sku.upper())

    def edit_product(self, guild, actor, sku, price=None, stock_delta=0, active=None):
        if price is not None and not 1000 <= price <= 1_000_000_000:
            raise StoreError("Giá từ 1.000 đến 1 tỷ VND.")
        with self.connect(True) as db:
            row = db.execute("SELECT * FROM products WHERE guild_id=? AND sku=?", (guild, sku.upper())).fetchone()
            if not row:
                raise StoreError("Không tìm thấy SKU.")
            stock = row["stock"] + stock_delta
            if not 0 <= stock <= 1_000_000:
                raise StoreError("Điều chỉnh làm tồn kho âm hoặc vượt giới hạn.")
            db.execute("UPDATE products SET price=?,stock=?,active=? WHERE id=?",
                       (price if price is not None else row["price"], stock,
                        int(active) if active is not None else row["active"], row["id"]))
            self.audit(db, guild, actor, "edit_product", f"{sku}: price={price}, delta={stock_delta}, active={active}")

    def products(self, guild, page=1):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM products WHERE guild_id=? AND active=1 ORDER BY id LIMIT 10 OFFSET ?",
                (guild, (page - 1) * 10))]

    def reserve(self, guild, buyer, sku, quantity, provider, ttl=900, bank=None):
        if not 1 <= quantity <= 100:
            raise StoreError("Số lượng từ 1 đến 100.")
        if provider not in ("manual", "payos"):
            raise StoreError("Nhà cung cấp thanh toán không hợp lệ.")
        now = int(time.time())
        with self.connect(True) as db:
            count = db.execute("SELECT count(*) AS total FROM orders WHERE guild_id=? AND buyer_id=? "
                               "AND status IN ('pending','review')", (guild, buyer)).fetchone()["total"]
            if count >= 3:
                raise StoreError("Bạn đang có 3 đơn chờ xử lý. Hãy thanh toán hoặc hủy đơn cũ.")
            product = db.execute("SELECT * FROM products WHERE guild_id=? AND sku=? AND active=1",
                                 (guild, sku.upper())).fetchone()
            if not product or product["stock"] < quantity:
                raise StoreError("Sản phẩm không tồn tại, đã ẩn hoặc không đủ tồn kho.")
            total = product["price"] * quantity
            if total > 1_000_000_000:
                raise StoreError("Tổng đơn tối đa 1 tỷ VND.")
            while True:
                code = 1_000_000_000_000 + secrets.randbelow(8_000_000_000_000)
                if not db.execute("SELECT 1 FROM orders WHERE code=?", (code,)).fetchone():
                    break
            memo, qr_url, account, name = None, None, None, None
            if provider == "manual":
                for _ in range(100):
                    memo = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
                    if not db.execute("SELECT 1 FROM orders WHERE provider='manual' AND description=?", (memo,)).fetchone():
                        break
                else:
                    raise StoreError("Không tạo được mã chuyển khoản duy nhất.")
                if bank:
                    qr_url, account, name = bank.qr_url(total, memo), bank.account, bank.name
            db.execute("UPDATE products SET stock=stock-? WHERE id=?", (quantity, product["id"]))
            db.execute("INSERT INTO orders(code,guild_id,buyer_id,product_id,name,unit_price,quantity,total,"
                       "provider,created_at,expires_at,description,checkout_url,account_number,account_name) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (code, guild, buyer, product["id"], product["name"], product["price"], quantity,
                        total, provider, now, now + ttl, memo, qr_url, account, name))
            self.audit(db, guild, buyer, "reserve", str(code))
            if provider == "manual":
                self.notice(db, code, "pending")
            return dict(db.execute("SELECT * FROM orders WHERE code=?", (code,)).fetchone())

    def order(self, code, guild=None):
        with self.connect() as db:
            row = db.execute("SELECT * FROM orders WHERE code=?", (code,)).fetchone()
            if not row or (guild is not None and row["guild_id"] != guild):
                raise StoreError("Không tìm thấy đơn trong server này.")
            return dict(row)

    def list_orders(self, guild, buyer=None, page=1):
        with self.connect() as db:
            query = "SELECT * FROM orders WHERE guild_id=?"
            args = [guild]
            if buyer is not None:
                query += " AND buyer_id=?"
                args.append(buyer)
            return [dict(r) for r in db.execute(query + " ORDER BY created_at DESC,code DESC LIMIT 10 OFFSET ?",
                                                (*args, (page - 1) * 10))]

    def transfer_order(self, memo, guild):
        with self.connect() as db:
            row = db.execute("SELECT * FROM orders WHERE guild_id=? AND provider='manual' AND description=?",
                             (guild, memo.upper())).fetchone()
            if not row:
                raise StoreError("Không tìm thấy mã chuyển khoản trong server này.")
            return dict(row)

    def bind_payment(self, code, data):
        with self.connect(True) as db:
            row = db.execute("SELECT * FROM orders WHERE code=?", (code,)).fetchone()
            if not row or row["provider"] != "payos":
                raise StoreError("Đơn không dùng payOS.")
            if data["orderCode"] != code or data["amount"] != row["total"] or data.get("currency") != "VND":
                raise StoreError("Dữ liệu payOS không khớp mã đơn, tiền hoặc loại tiền.")
            if row["link_id"] and row["link_id"] != data["paymentLinkId"]:
                raise StoreError("Link thanh toán không khớp.")
            db.execute("UPDATE orders SET link_id=?,checkout_url=?,qr_code=?,description=?,"
                       "account_number=?,account_name=? WHERE code=?",
                       (data["paymentLinkId"], data["checkoutUrl"], data["qrCode"], data["description"],
                        data.get("accountNumber", ""), data.get("accountName", ""), code))

    def _review(self, db, row, reason):
        if row["status"] != "completed":
            db.execute("UPDATE orders SET status='review',review_reason=? WHERE code=?", (reason, row["code"]))
        else:
            db.execute("UPDATE orders SET review_reason=? WHERE code=?", (reason, row["code"]))
        self.notice(db, row["code"], "review")
        self.audit(db, row["guild_id"], None, "payment_review", f"{row['code']}: {reason}")

    def receive(self, code, amount, link_id, reference, currency="VND"):
        """Only called after SDK signature verification; duplicates cannot deliver twice."""
        with self.connect(True) as db:
            row = db.execute("SELECT * FROM orders WHERE code=?", (code,)).fetchone()
            if not row or row["provider"] != "payos":
                return "unknown"
            if not row["link_id"]:
                if row["status"] in ("cancelled", "expired"):
                    db.execute("UPDATE orders SET link_id=? WHERE code=?", (link_id, code))
                    row = db.execute("SELECT * FROM orders WHERE code=?", (code,)).fetchone()
                else:
                    return "retry"  # Creation may still be in flight; provider must retry.
            if row["link_id"] != link_id or currency != "VND":
                self.audit(db, row["guild_id"], None, "webhook_mismatch", str(code))
                return "mismatch"
            old = db.execute("SELECT * FROM payment_events WHERE reference=?", (reference,)).fetchone()
            if old:
                return "duplicate" if old["order_code"] == code and old["amount"] == amount else "mismatch"
            db.execute("INSERT INTO payment_events VALUES(?,?,?,?)", (reference, code, amount, int(time.time())))
            received = db.execute("SELECT sum(amount) AS total FROM payment_events WHERE order_code=?", (code,)).fetchone()["total"]
            if received != row["total"]:
                self._review(db, row, "Số tiền webhook không khớp; cần đối soát số tiền tích lũy tại payOS.")
                return "review"
            return self._paid(db, row)

    def _paid(self, db, row):
        if row["status"] in ("paid", "fulfilling", "completed"):
            return "duplicate"
        if row["status"] == "review" and row["paid_at"] is not None:
            # An already-paid order may have been in fulfillment before a payment dispute.
            # Do not silently reset its progress or authorize fulfillment again.
            return "review"
        if not row["reserved"]:
            self._review(db, row, "Nhận tiền sau khi đơn đã giải phóng kho; cần nhân viên xử lý.")
            return "review"
        db.execute("UPDATE orders SET status='paid',paid_at=?,review_reason=NULL WHERE code=?",
                   (int(time.time()), row["code"]))
        self.notice(db, row["code"], "paid")
        self.audit(db, row["guild_id"], None, "paid", str(row["code"]))
        return "paid"

    def reconcile(self, code, remote):
        with self.connect(True) as db:
            row = db.execute("SELECT * FROM orders WHERE code=?", (code,)).fetchone()
            if not row or row["provider"] != "payos" or not row["link_id"]:
                return "unbound"
            if remote["id"] != row["link_id"] or remote["orderCode"] != code or remote["amount"] != row["total"]:
                raise StoreError("Kết quả đối soát không khớp đơn hàng.")
            status, paid = remote["status"], remote["amountPaid"]
            received = db.execute("SELECT coalesce(sum(amount),0) AS total FROM payment_events WHERE order_code=?", (code,)).fetchone()["total"]
            if status == "PAID" and paid == row["total"] and received <= row["total"]:
                return self._paid(db, row)
            if paid:
                self._review(db, row, "Số tiền/trạng thái tại payOS cần kiểm tra thủ công.")
                return "review"
            if status in ("CANCELLED", "EXPIRED") and row["status"] == "pending":
                return self._release(db, row, "cancelled" if status == "CANCELLED" else "expired")
            return row["status"]

    def _release(self, db, row, status):
        if row["reserved"]:
            db.execute("UPDATE products SET stock=stock+? WHERE id=?", (row["quantity"], row["product_id"]))
        db.execute("UPDATE orders SET status=?,reserved=0 WHERE code=?", (status, row["code"]))
        self.notice(db, row["code"], status)
        self.audit(db, row["guild_id"], None, status, str(row["code"]))
        return status

    def manual_paid(self, code, guild, actor):
        with self.connect(True) as db:
            row = db.execute("SELECT * FROM orders WHERE code=? AND guild_id=?", (code, guild)).fetchone()
            if not row or row["provider"] != "manual" or row["status"] != "pending":
                raise StoreError("Chỉ xác nhận thủ công đơn manual đang chờ thanh toán.")
            if row["expires_at"] <= int(time.time()):
                raise StoreError("Đơn đã hết thời gian thanh toán.")
            result = self._paid(db, row)
            self.audit(db, guild, actor, "manual_paid", str(code))
            return result

    def manual_cancel(self, code, guild, actor):
        with self.connect(True) as db:
            row = db.execute("SELECT * FROM orders WHERE code=? AND guild_id=?", (code, guild)).fetchone()
            if not row or row["provider"] != "manual" or row["status"] != "pending":
                raise StoreError("Đơn không thể hủy thủ công.")
            self.audit(db, guild, actor, "manual_cancel", str(code))
            return self._release(db, row, "cancelled")

    def advance(self, code, guild, actor, status):
        expected = {"fulfilling": "paid", "completed": "fulfilling"}.get(status)
        if expected is None:
            raise StoreError("Trạng thái không hợp lệ.")
        with self.connect(True) as db:
            count = db.execute("UPDATE orders SET status=? WHERE code=? AND guild_id=? AND status=?",
                               (status, code, guild, expected)).rowcount
            if not count:
                raise StoreError(f"Đơn phải ở trạng thái {expected} để chuyển sang {status}.")
            self.notice(db, code, status)
            self.audit(db, guild, actor, status, str(code))

    def maintenance(self):
        now = int(time.time())
        with self.connect(True) as db:
            for row in db.execute("SELECT * FROM orders WHERE provider='manual' AND status='pending' AND expires_at<=?", (now,)).fetchall():
                self._release(db, row, "expired")
            for row in db.execute("SELECT * FROM orders WHERE provider='payos' AND status='pending' AND link_id IS NULL AND expires_at<=?", (now,)).fetchall():
                self._review(db, row, "Không lấy được link thanh toán trước hạn; admin cần đối soát và giải phóng kho bằng resolve.")
            rows = db.execute("SELECT * FROM orders WHERE provider='payos' AND status IN ('pending','review') "
                              "AND checked_at<? ORDER BY checked_at LIMIT 10", (now - 60,)).fetchall()
            for row in rows:
                db.execute("UPDATE orders SET checked_at=? WHERE code=?", (now, row["code"]))
            return [dict(r) for r in rows]

    def notifications(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT n.id AS notice_id,n.status AS notice_status,n.attempts,o.* "
                "FROM outbox n JOIN orders o ON o.code=n.order_code WHERE n.delivered=0 AND n.attempts<6 "
                "AND n.retry_at<=? ORDER BY n.id LIMIT 20", (int(time.time()),))]

    def notification_done(self, notice_id, ok):
        with self.connect(True) as db:
            db.execute("UPDATE outbox SET delivered=?,attempts=attempts+1,retry_at=? WHERE id=?",
                       (int(ok), int(time.time()) + 60, notice_id))

    def revenue(self, guild):
        with self.connect() as db:
            return dict(db.execute("SELECT count(*) AS orders,coalesce(sum(total),0) AS collected,"
                "coalesce(sum(CASE WHEN status='completed' THEN total ELSE 0 END),0) AS completed "
                "FROM orders WHERE guild_id=? AND status IN ('paid','fulfilling','completed')", (guild,)).fetchone())

    def resolve_unpaid(self, code, guild, actor, note):
        """Admin attests dashboard/bank inspection; never pretend to perform a refund."""
        if len(note.strip()) < 10:
            raise StoreError("Ghi lý do/đối soát ít nhất 10 ký tự.")
        with self.connect(True) as db:
            row = db.execute("SELECT * FROM orders WHERE code=? AND guild_id=?", (code, guild)).fetchone()
            if not row or row["status"] != "review" or row["expires_at"] > int(time.time()) or row["link_id"]:
                raise StoreError("Chỉ đóng đơn review đã quá hạn, sau khi kiểm tra tại ngân hàng/payOS.")
            received = db.execute("SELECT count(*) AS total FROM payment_events WHERE order_code=?", (code,)).fetchone()["total"]
            if received or row["paid_at"]:
                raise StoreError("Có ghi nhận tiền; không được giải phóng đơn bằng resolve unpaid.")
            self.audit(db, guild, actor, "resolve_unpaid", f"{code}: {note[:1000]}")
            return self._release(db, row, "cancelled")


def open_store():
    import os
    url = os.getenv("DATABASE_URL")
    if url:
        from postgres_db import PostgresStoreDB
        return PostgresStoreDB(url)
    if os.getenv("STORE_BACKEND", "postgres").lower() != "sqlite":
        raise StoreError("Điền DATABASE_URL Supabase hoặc đặt STORE_BACKEND=sqlite để thử local.")
    return StoreDB(os.getenv("STORE_DB_PATH", "data/store.sqlite3"))
