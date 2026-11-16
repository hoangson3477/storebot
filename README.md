# Manage and Store — AI setup và cửa hàng

Bot có setup server bằng AI và cửa hàng: sản phẩm, tồn kho, đơn hàng, xác nhận thủ công hoặc payOS QR/webhook. Xem [STORE_GUIDE.md](STORE_GUIDE.md) để cấu hình cửa hàng và thanh toán.

**Triển khai mới: đọc [DEPLOY.md](DEPLOY.md) trước.** Backend mặc định là PostgreSQL/Supabase (`DATABASE_URL`); muốn thử SQLite local phải đặt `STORE_BACKEND=sqlite`. Mẫu cấu hình trong `.env.example`. Database wargame dùng schema storebot riêng. Không commit `.env`.

## Chạy bot

1. Cài Python 3.11 trở lên, chạy `python -m pip install -r requirements.txt`.
2. Copy `.env.example` thành `.env`, điền Discord token, API key, model.
3. AI_BASE_URL là URL gốc API Chat Completions tương thích OpenAI (kết thúc bằng /v1), không phải URL website chat. Model phải hỗ trợ JSON object. Không mặc định miễn phí; hạn mức tùy nhà cung cấp.
4. Trong Discord Developer Portal, bật Message Content Intent để dùng `!setup`.
5. Mời bot với scopes `bot`, `applications.commands`; cấp View Channels, Send Messages, Attach Files, Read Message History, Manage Channels, Manage Roles. Đặt role bot cao hơn các role bot cần quản lý.
6. Điền TEST_GUILD_ID để thử slash command trên server riêng; nếu bỏ trống, bot sync global và Discord có thể cần thời gian hiển thị.
7. Chạy `python bot.py`.

## Sử dụng

`/setup describe: Xây server bán dịch vụ thiết kế, có bảng giá, khu hỗ trợ, phòng nhân viên và nơi trò chuyện`

Hoặc `!setup Xây server học tiếng Anh cho 30 học sinh, có phòng luyện nói và kênh tài liệu`.

AI đọc mô tả và trả cấu trúc JSON. Bot kiểm tra giới hạn, tên trùng, kiểu kênh và quyền rồi đính kèm bản xem trước. Người yêu cầu có 3 phút để xác nhận hoặc hủy. Chỉ Administrator dùng được và quyền được kiểm tra lại khi xác nhận.

Tối đa 6 category, 30 kênh và một role nhân viên không có quyền quản trị. Mọi kênh có quyền riêng theo public/readonly/staff. Role khác có quyền ghi đè và Administrator vẫn tuân theo quy tắc quyền của Discord; cần rà soát quyền sẵn có trên server.

Bot chỉ tạo mới, từ chối category/role cùng tên, không sửa hoặc xóa tài nguyên có sẵn. Nếu tạo lỗi, bot thử xóa những tài nguyên vừa tạo và báo các ID chưa dọn được. Không có khả năng phục hồi giao dịch nếu tiến trình bị tắt đột ngột; hãy thử trên server riêng trước.

Mô tả được gửi đến nhà cung cấp AI đã cấu hình. Không nhập token, mật khẩu hoặc dữ liệu khách hàng vào mô tả.

## Kiểm tra

`python -m unittest discover -s tests`

Chưa cần token để kiểm tra schema. Kiểm thử Discord và AI thực cần tài khoản/API đã cấu hình.
