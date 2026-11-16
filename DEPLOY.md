# Chạy storebot từ GitHub trên host/VPS

**Thanh toán hiện dùng QR + người bán xác nhận:** đọc [MANUAL_PAYMENT.md](MANUAL_PAYMENT.md), điền BANK_BIN/BANK_ACCOUNT/BANK_ACCOUNT_NAME và giữ PAYMENT_PROVIDER=manual. Không cần làm mục payOS, cổng HTTP hoặc tên miền ở phía dưới cho chế độ manual.

Repo: https://github.com/hoangson3477/storebot

## 1. Supabase đã chuẩn bị

Project **wargame** (`jdlognzbxuegidvrzcxk`) có schema riêng **storebot** với 6 bảng: settings, products, orders, payment_events, audit, outbox. Bảng game giữ nguyên. Schema không dùng Data API công khai; anon/authenticated không có quyền truy cập. RLS đã bật; kết nối backend bằng tài khoản database có quyền trong schema (ban đầu là postgres/owner).

Bot kết nối PostgreSQL trực tiếp, không dùng Supabase publishable/service-role API key. Trong Supabase Dashboard → Connect, copy **Session pooler connection URI**, thay mật khẩu database đã URL-encode vào URI và lưu thành `DATABASE_URL` trên host. Chọn Session pooler cho VPS IPv4. Transaction pooler cũng có thể dùng vì adapter tắt prepared statements và giữ mỗi thao tác trong transaction.

Ví dụ cấu trúc (không phải credential thật):

```dotenv
DATABASE_URL=postgresql://postgres.jdlognzbxuegidvrzcxk:URL_ENCODED_PASSWORD@POOLER_HOST:5432/postgres
STORE_BACKEND=postgres
```

**Lấy POOLER_HOST từ Dashboard, không đoán tên host.** Không gửi mật khẩu vào GitHub. PostgreSQL/Supabase yêu cầu TLS trong adapter. Không cần chuyển dữ liệu cũ vì chưa production. `sql/schema.sql` là bootstrap cho database mới; không chạy lại để reset dữ liệu.

## 2. Biến môi trường của host

Copy `.env.example` thành `.env` trên host hoặc nhập từng biến trong dashboard host. Các biến cần điền:

| Biến | Lấy ở đâu / mục đích |
| --- | --- |
| DISCORD_TOKEN | Discord Developer Portal → Bot → Token |
| DATABASE_URL | Supabase → Connect → Session pooler, dùng mật khẩu database |
| AI_BASE_URL, AI_API_KEY, AI_MODEL | Nhà cung cấp AI tương thích Chat Completions/JSON mode, cho lệnh setup |
| TEST_GUILD_ID | ID server thử nghiệm; để trống khi muốn sync global |
| PAYMENT_PROVIDER | manual để thử store, payos để bật QR tự động |
| PAYOS_CLIENT_ID, PAYOS_API_KEY, PAYOS_CHECKSUM_KEY | Kênh thanh toán trên my.payos.vn |
| PAYMENT_PUBLIC_URL | HTTPS origin công khai của host, ví dụ https://pay.example.com |
| PAYMENT_HOST | 0.0.0.0 nếu host/container cần truy cập qua mạng; 127.0.0.1 nếu proxy cùng máy |
| PAYMENT_PORT | 8080 mặc định; phải khớp cổng host/proxy |

Discord: bật Message Content Intent để dùng `!setup`, `!store`. Mời bot với scopes `bot`, `applications.commands`; quyền Manage Channels, Manage Roles, View Channels, Send Messages, Embed Links, Attach Files, Read Message History. Đặt role bot cao hơn role bot quản lý.

## 3. Kết nối GitHub với host

Chọn branch `main`, Python 3.12 (khuyến nghị), working directory là thư mục gốc repo.

**Install/build:** `python -m pip install -r requirements.txt`

**Start:** `python -u bot.py`

Chỉ chạy **một instance** bot. GitHub Actions chạy kiểm thử mỗi lần push/PR; auto-sync của host có thể cập nhật trước khi CI xong, nên nếu host hỗ trợ thì đặt deploy sau khi CI thành công. Kiểm tra host có tự install dependencies và restart hay chỉ đồng bộ file. Cần cấu hình giữ `.env`/secret qua các lần cập nhật.

Nếu VPS dùng Docker thay vì panel Python:

```bash
docker compose up -d --build
docker compose logs --tail=100 -f bot
```

Compose tự restart khi tiến trình dừng. Chưa thay thế giám sát độc lập; lỗi tác vụ nền cần xem log. Mẫu Caddy HTTPS trong `Caddyfile.example`; cần tên miền thật trỏ tới VPS. Không cài thêm reverse proxy nếu host đã cấp HTTPS/public URL sẵn.

## 4. payOS

1. Tạo/xác minh tài khoản, liên kết ngân hàng và tạo kênh thanh toán trên https://my.payos.vn.
2. Điền ba credential payOS vào host, đặt `PAYMENT_PROVIDER=payos` và `PAYMENT_PUBLIC_URL=https://TEN_MIEN_THAT`.
3. Cho HTTPS public URL chuyển tiếp vào PAYMENT_HOST:PAYMENT_PORT; khởi động bot.
4. Trong kênh thanh toán payOS, đăng ký webhook **https://TEN_MIEN_THAT/webhooks/payos**. Endpoint cần truy cập trực tiếp được, không có trang đăng nhập của host chặn phía trước.
5. Tạo sản phẩm, mua một đơn nhỏ để kiểm tra QR, tiền và nội dung. QR lấy từ dữ liệu payOS trả, không tự chế thông tin tài khoản.
6. Dùng `/store order code: ...` để xem/đối soát. Cần xác nhận webhook thực nhận được trước khi giao hàng thật. Return URL chỉ hướng dẫn quay lại Discord, không dùng làm bằng chứng nhận tiền.

Bot kiểm tra chữ ký webhook qua SDK, mã đơn, paymentLinkId, VND và tổng các giao dịch. Webhook trùng không cộng hai lần. Chuyển thiếu giữ review; chuyển đủ thành paid; chuyển thừa giữ review. API đối soát không tự bỏ qua khoản thừa đã được ghi nhận.

Đơn hết hạn mà tạo link lỗi sẽ chuyển review. Admin đối chiếu dashboard payOS và ngân hàng, bảo đảm không có link đang có thể thanh toán/không có tiền về, rồi mới dùng `/store resolve code: ... note: ...` để đóng đơn và trả kho. Lệnh này chỉ cho đơn chưa gắn link, không có giao dịch đã ghi nhận. Đơn có link phải hủy/đối soát qua payOS. Tiền đến muộn sau khi trả kho luôn cần nhân viên xử lý. Hoàn tiền vẫn làm ngoài bot.

## 5. Kiểm tra và cập nhật

```bash
python -m unittest discover -s tests -v
```

GitHub Actions dùng PostgreSQL 17 tạm để chạy cùng bộ kiểm thử transaction/tồn kho như SQLite, kiểm thử SDK webhook và đăng ký slash command không cần Discord token. Test PostgreSQL chỉ cho phép localhost và xóa dữ liệu fixture trong database CI, không chạy với Supabase thật.

Adapter PostgreSQL tái sử dụng logic nghiệp vụ, chuyển placeholder SQL qua psycopg. Mỗi thao tác ghi có transaction-level advisory lock riêng cho storebot: an toàn kho nhưng giới hạn throughput cho cửa hàng nhỏ. Khi quy mô lớn cần chuyển sang khóa theo sản phẩm/đơn. Khóa này không được dùng để phối hợp các bot game khác.

Token và mật khẩu không được commit. Khi cập nhật code thay đổi schema trong tương lai, phải có SQL nâng cấp riêng; auto-sync GitHub không tự đổi cấu trúc database.
