# Thanh toán QR, người bán xác nhận

Đây là luồng mặc định của bot. Chỉ điền trong `.env`/dashboard host:

```dotenv
PAYMENT_PROVIDER=manual
BANK_BIN=MA_BIN_6_CHU_SO
BANK_ACCOUNT=SO_TAI_KHOAN
BANK_ACCOUNT_NAME=TEN_CHU_TAI_KHOAN
```

Lấy mã BIN chính xác của ngân hàng từ danh sách VietQR/ngân hàng. Giữ nguyên các số 0 đầu số tài khoản. Thử một QR để kiểm tra app ngân hàng hiển thị đúng người nhận. QR dùng Quick Link ảnh VietQR, không cần payOS, payment API key, tên miền webhook hoặc mở cổng HTTP. Thông tin nhận tiền/số tiền/mã đơn được gửi tới dịch vụ tạo ảnh VietQR; QR chỉ điền sẵn thông tin, không xác nhận tiền về.

## Khách mua

`/store buy sku: TEST quantity: 1` → bot hiện QR, tên người nhận, tài khoản, tiền và nội dung chuyển khoản 6 ký tự, ví dụ `K7RM4X`. Khách giữ đúng nội dung và số tiền. Mỗi đơn có mã riêng, không đổi sau restart hoặc khi xem lại QR. `/store payment code: K7RM4X` để xem lại.

Mã chỉ dùng chữ in hoa và số, bỏ ký tự dễ nhầm; bot kiểm tra trùng và không tái sử dụng mã đã lưu của đơn hủy/hết hạn. Mã số dài của đơn vẫn được giữ nội bộ và có thể dùng như trước.

## Người bán

1. `/store configure` chọn role nhân viên và kênh log riêng tư. Đơn mới được đưa vào hàng đợi thông báo để nhân viên biết mã chuyển khoản.
2. Kiểm tra **lịch sử nhận tiền thật trong ngân hàng**, đối chiếu số tiền và mã 6 ký tự với đơn. Screenshot khách gửi không thay thế việc này.
3. `/store paid code: K7RM4X` → đã thanh toán, có audit người xác nhận.
4. `/store process code: K7RM4X` → đang xử lý.
5. `/store complete code: K7RM4X` → hoàn thành.

Các lệnh `order`, `payment`, `paid`, `process`, `complete`, `cancel` nhận cả mã 6 ký tự hoặc mã số dài. Người mua chỉ xem/xử lý đơn của mình; người bán cần role nhân viên hoặc Administrator.

Đơn manual giữ kho **24 giờ** từ lúc đặt. Khách đã chuyển không nên hủy hoặc chuyển lại. Nếu khách chuyển sau hạn, sai mã hoặc thừa/thiếu tiền, liên hệ người bán để kiểm tra và giải quyết; bot không tự nhận biết những giao dịch đó. QR hết hạn không ngăn ứng dụng ngân hàng chuyển tiền. Khi đổi tài khoản nhận tiền, đơn cũ vẫn giữ QR/tài khoản đã lưu lúc tạo: kiểm tra cả tài khoản cũ đối với đơn đang mở.

Nếu ảnh QR không tải được, dùng số tài khoản, số tiền và nội dung được ghi trong đơn; nút Mở QR cũng dùng cùng dịch vụ ảnh. Cần Internet để dịch vụ ảnh/Discord tải ảnh.

Hướng dẫn Supabase và khởi động host vẫn ở DEPLOY.md. Các phần payOS trong tài liệu cũ chỉ là lựa chọn khác, không cần cấu hình khi PAYMENT_PROVIDER=manual.

Nguồn Quick Link: https://www.vietqr.io/qr-dong
