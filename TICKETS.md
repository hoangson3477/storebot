# Panel và ticket mua hàng

1. Admin chạy `/store configure staff_role: @Nhân viên log_channel: #log-store` (kênh log riêng).
2. Tạo category dành cho mua hàng. Cho bot Manage Channels, Manage Roles, View Channel, Send Messages, Embed Links, Attach Files và Read Message History. Chạy `/store ticketsetup category: <category>`.
3. Thêm sản phẩm bằng `/store add`, rồi chạy `/store panel` trong kênh bán hàng. Lệnh này đăng catalog công khai có nút **Mua sản phẩm**. Có thể chọn `page` nếu có nhiều sản phẩm.
4. Khách bấm mua, nhập SKU và số lượng. Bot tạo đơn và ticket riêng cho khách/nhân viên, gửi số tiền, nội dung chuyển khoản và QR. `/store buy` cũng dùng luồng này sau khi bật ticket.
5. Khách bấm **Tôi đã chuyển tiền** để báo nhân viên. Đây là lời báo của khách, không xác nhận tiền thật. Nhân viên kiểm tra ngân hàng rồi dùng `/store paid code: <mã 6 ký tự>`; tiếp theo `/store process`, giao hàng và `/store complete` với cùng mã đơn.
6. Nhân viên bấm **Đóng ticket** hoặc dùng `/store ticketclose` trong ticket. Chỉ đóng khi đơn đã hoàn thành, đã hủy hoặc hết hạn. Bot khóa gửi tin của khách và giữ lịch sử; không tự xóa kênh hay hoàn tiền.

`/store ticket code: <mã>` trả lại ticket đã có hoặc tạo ticket cho một đơn còn hoạt động. Không tạo đơn/QR chuyển khoản mới. Nếu kênh mở bị xóa ngoài bot, lệnh có thể tạo lại ticket. Ticket đã đóng không tự mở lại.

Các lệnh đều có dạng tin nhắn tương ứng: `!store panel`, `!store ticketsetup <category_id>`, `!store ticket K7RM4X`, `!store ticketclose`.

Ticket lưu trong database và các nút được đăng ký lại khi bot khởi động. Thông báo thay đổi trạng thái đơn được gửi thêm vào ticket. Admin Discord vẫn có quyền xem theo cơ chế Administrator. Bot kiểm tra lại quyền trước khi gửi thông tin vào ticket.

Nếu tạo ticket lỗi, đơn vẫn được giữ và khách nhận QR riêng như trước. Sửa quyền/cấu hình rồi dùng `/store ticket`, tránh đặt hoặc chuyển tiền lần nữa. Hạn thanh toán manual hiện tại là 24 giờ; nhân viên cần đối soát trước hạn.

## Database

Bot tự tạo thêm hai bảng `ticket_settings` và `tickets` khi load store, bằng tài khoản database đang chạy bot; với PostgreSQL bật RLS và thu hồi quyền public/anon/authenticated. Không sửa/xóa đơn và sản phẩm cũ. Nếu tài khoản không có quyền tạo bảng, quản trị database chạy `sql/tickets.sql` trước khi khởi động.

Chạy một instance bot, như cấu hình triển khai hiện tại. Panel cũ vẫn dùng được nút mua sau khi khởi động lại; gửi panel mới để khách đọc hướng dẫn mới.
