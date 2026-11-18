# Debug bot

Bot ghi cùng log ra console VPS và file UTF-8 `logs/bot.log`. File tự xoay khi đạt 5 MB, giữ thêm 5 bản (`bot.log.1` đến `.5`). Docker Compose lưu thư mục này trong volume `bot_logs`; host chạy Python lưu trong thư mục project. Nếu thư mục không ghi được, bot báo lỗi và tiếp tục ghi console.

Biến môi trường tùy chọn, không bắt buộc sửa để chạy:

```dotenv
LOG_LEVEL=INFO
LOG_FILE=logs/bot.log
```

Đổi `LOG_LEVEL=DEBUG` và restart khi cần theo dõi truy vấn đọc, thời gian thao tác database và thử gửi thông báo. Bot không ghi SQL/parameters, prompt hoặc toàn bộ phản hồi AI, webhook payload hay nội dung chat. Không bật debug HTTP của thư viện bên ngoài: dữ liệu đó có thể chứa headers hoặc nội dung nhạy cảm.

Mỗi lệnh ghi `command.started`, `command.finished`, thời gian chạy và `req`, `guild`, `channel`, `user`. `req` là ID interaction hoặc tin nhắn Discord. Lỗi bất ngờ trả **Mã tra log** cho người gọi; tìm cùng `req` trong log. Lỗi nghiệp vụ như hết hàng/thiếu quyền ghi WARNING; lỗi bất ngờ có traceback. Một thao tác thất bại có thể xuất hiện ở nhiều tầng (database, rồi command) để giữ cả nguyên nhân và ngữ cảnh.

Các nhóm sự kiện:

| Nhóm | Nội dung |
| --- | --- |
| `startup`, `shutdown`, `gateway` | Phiên bản Python/discord.py, load store, sync lệnh, mất/kết nối lại |
| `panel` | Bắt đầu/kết thúc đăng panel, kênh, số sản phẩm, quyền thiếu |
| `purchase`, `payment` | Giữ kho, mã đơn, số tiền, tạo QR, đối soát, webhook, lỗi dịch vụ |
| `db` | Thao tác thành công/thất bại và thời gian; giao dịch chậm từ 1 giây ghi WARNING |
| `ticket` | Tạo/tái sử dụng/đóng kênh, báo chuyển tiền, kiểm tra riêng tư, rollback |
| `notification`, `maintenance` | DM bị chặn, gửi log/ticket lỗi, số lần thử, đối soát nền |
| `ai`, `setup` | Model/HTTP status/thời gian/số lần thử, xác thực plan, tạo tài nguyên và rollback |

Đọc console trên panel host; với Docker: `docker compose logs --tail=200 -f bot`.

Đọc file trên Linux: `tail -n 200 logs/bot.log`; tìm một yêu cầu: `grep 'req=ID_CAN_TIM' logs/bot.log*`.

Trên PowerShell: `Get-Content logs/bot.log -Tail 200`; tìm mã: `Select-String -Path logs/bot.log* -Pattern 'req=ID_CAN_TIM'`.

Khi báo lỗi, gửi mã tra log và đoạn log quanh thao tác có traceback, không cần gửi `.env`. Formatter che giá trị credential đang cấu hình, URL PostgreSQL, Bearer token và API key trong query, kể cả trong traceback. Log vẫn chứa ID Discord và thông tin vận hành đơn hàng: chỉ chia sẻ đoạn liên quan cho người xử lý. Log và bản xoay không được đưa vào Git/Docker image.

## DNS trên host

Bản `2026.11.18-dns-retry-1` hiện trong `startup.begin build=...` sau khi cập nhật code và restart. Định dạng `INFO:__main__:Bot online` / `ERROR:store:Store command failed` thuộc logging cũ; nếu chỉ thấy các dòng đó thì kiểm tra thời điểm log và tiến trình đang chạy.

Nếu cả Discord Gateway và Supabase cùng báo `Temporary failure in name resolution`, đó là dấu hiệu lỗi phân giải DNS/mạng tại container/host. Không thể xác nhận DNS host đã ổn từ máy phát triển. Kiểm tra trong chính container đang chạy bot và nhờ nhà cung cấp host kiểm tra DNS/upstream/mạng nếu lỗi kéo dài. Không thay pooler hostname bằng IP đoán và không đổi token/mật khẩu để xử lý lỗi này.

Bot thử mở kết nối database tối đa 3 lần cho lỗi DNS tạm thời, chờ 1 rồi 2 giây. Không retry lỗi sai mật khẩu/tên miền không tồn tại, và không chạy lại transaction/commit đã bắt đầu. Retry bị giới hạn; code không sửa được DNS của hạ tầng host. Discord.py giữ cơ chế reconnect Gateway sẵn có.

Maintenance và notification tăng khoảng chờ theo cấp số nhân khi DNS tiếp tục lỗi, tối đa 5 phút; quay về 30/15 giây khi chạy thành công. Notification tạm bỏ lượt gửi khi Gateway chưa ready. Các sự kiện `db.dns_retry`, `db.dns_unavailable`, `db.dns_recovered`, `maintenance.dns_backoff`, `notification.dns_backoff` và `*.recovered` giúp theo dõi lỗi/phục hồi.
