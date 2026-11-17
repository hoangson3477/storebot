"""Shared in-Discord usage guide; no database or AI calls."""
import discord


SECTIONS = {
    "setup": ("Setup server bằng AI", (
        "`/setup describe: Xây server bán tài khoản game, có hỗ trợ và kênh nhân viên`\n"
        "Hoặc `!setup Xây server học tập có phòng Toán, Văn, Anh và phòng voice`\n"
        "Cần **Administrator**; bot cần **Manage Channels** và **Manage Roles**. "
        "Mô tả 10–2000 ký tự, mỗi server chờ 60 giây giữa các yêu cầu.\n"
        "AI Gemini gửi bản xem trước → đọc file → bấm **Xác nhận tạo** trong 3 phút. "
        "Bot tạo kênh/role mới; sau đó bạn gán role nhân viên cho người phù hợp."
    )),
    "customer": ("Khách mua hàng", (
        "`/store products [page]` — xem sản phẩm và mã SKU.\n"
        "`/store buy sku: KEY quantity: 1` — đặt hàng, nhận QR riêng.\n"
        "`/store orders [page]` — xem đơn của bạn.\n"
        "`/store order code: K7RM4X` — xem chi tiết đơn.\n"
        "`/store payment code: K7RM4X` — lấy lại QR của đơn.\n"
        "`/store cancel code: K7RM4X` — hủy đơn chưa thanh toán.\n"
        "**Chuyển khoản thủ công:** quét QR, giữ đúng số tiền và nội dung mã 6 ký tự. "
        "Người bán kiểm tra tiền về rồi xác nhận; bot không tự kiểm tra tài khoản ngân hàng. "
        "Nếu đã chuyển tiền, liên hệ người bán để kiểm tra trước khi hủy hoặc đặt lại."
    )),
    "staff": ("Nhân viên / chủ shop", (
        "Cần role nhân viên đã cấu hình hoặc **Administrator**.\n"
        "`/store add sku: KEY price: 50000 stock: 10 name: Key game` — thêm sản phẩm; giá VND.\n"
        "`/store edit sku: KEY stock_delta: 5` — thêm 5 vào tồn kho; số âm để giảm. "
        "Có thể dùng `price` đổi giá, `active` bật/tắt bán.\n"
        "`/store panel [page]` — đăng bảng hàng có nút mua tại kênh hiện tại.\n"
        "`/store allorders [page]` — xem đơn toàn shop.\n"
        "Sau khi kiểm tra tiền thật: `/store paid code: K7RM4X` → "
        "`/store process code: K7RM4X` → giao hàng → `/store complete code: K7RM4X`.\n"
        "`/store revenue` — xem tổng tiền và đơn hoàn thành."
    )),
    "admin": ("Cấu hình lần đầu", (
        "1. Tạo role nhân viên và kênh log riêng: tắt **View Channel** cho @everyone "
        "và các role ngoài nhân viên; cho bot xem kênh, gửi tin và **Embed Links**.\n"
        "2. `/store configure staff_role: @Nhân viên log_channel: #log-store`.\n"
        "3. Thêm sản phẩm bằng `/store add`, rồi đăng `/store panel` ở kênh bán hàng.\n"
        "Chế độ QR thủ công cần cấu hình `PAYMENT_PROVIDER=manual`, `BANK_BIN`, "
        "`BANK_ACCOUNT`, `BANK_ACCOUNT_NAME` trong môi trường chạy bot, rồi khởi động lại.\n"
        "`/store resolve code: <mã đơn> note: <lý do>` — admin xử lý đơn lỗi quá hạn "
        "sau khi kiểm tra chắc chắn chưa nhận tiền."
    )),
    "tickets": ("Panel và ticket mua hàng riêng", (
        "**Admin:** `/store configure` chọn role nhân viên/kênh log, rồi "
        "`/store ticketsetup category: <category mua hàng>`.\n"
        "**Nhân viên:** `/store panel` đăng bảng hàng công khai ở kênh hiện tại.\n"
        "**Khách:** bấm **Mua sản phẩm**, nhập SKU/số lượng → bot mở ticket riêng và gửi QR. "
        "`/store buy` cũng mở ticket khi đã cấu hình.\n"
        "`/store ticket code: K7RM4X` lấy lại ticket của đơn; không tạo đơn/thanh toán mới.\n"
        "Trong ticket: **Xem đơn / QR**, **Tôi đã chuyển tiền** (chỉ báo nhân viên). "
        "Nhân viên dùng các lệnh paid → process → complete.\n"
        "**Đóng ticket** hoặc `/store ticketclose`: nhân viên đóng khi đơn đã hoàn thành/hủy/hết hạn. "
        "Lịch sử được giữ lại; đóng ticket không hủy đơn hay hoàn tiền."
    )),
}


def build_help(topic="all"):
    embed = discord.Embed(title="Hướng dẫn sử dụng bot", color=discord.Color.blurple())
    embed.description = (
        "Dùng `/help topic: setup`, `store`, `staff` hoặc `admin` để xem từng phần.\n"
        "Lệnh `/` chọn tham số trong Discord; `[...]` là tùy chọn. "
        "Các lệnh store dùng trong server."
    )
    keys = {"all": tuple(SECTIONS), "store": ("customer", "staff", "admin", "tickets")}.get(topic, (topic,))
    for key in keys:
        title, content = SECTIONS[key]
        embed.add_field(name=title, value=content, inline=False)
    embed.add_field(name="Dùng lệnh tin nhắn !", value=(
        '`!store products` · `!store buy KEY 1` · `!store paid K7RM4X`\n'
        '`!store add KEY 50000 10 "Key game" "Mô tả sản phẩm"`\n'
        "Tham số có khoảng trắng đặt trong dấu nháy kép. Lệnh đơn hàng gửi qua DM; "
        "bật nhận DM từ server hoặc dùng `/` để nhận phản hồi riêng. "
        "Lệnh `!` cần bật Message Content Intent cho bot."
    ), inline=False)
    embed.set_footer(text="Mã trong ví dụ chỉ minh họa. Dùng SKU và mã đơn thực tế của bạn.")
    return embed
