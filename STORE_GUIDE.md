# Store và thanh toán payOS

**Cập nhật:** cấu hình Supabase/GitHub/host mới nhất nằm trong [DEPLOY.md](DEPLOY.md). Backend mặc định hiện là PostgreSQL/Supabase; nội dung SQLite bên dưới chỉ áp dụng khi đặt STORE_BACKEND=sqlite. Admin có thêm `/store resolve code note` cho đơn lỗi tạo link chưa nhận tiền đã quá hạn; xem điều kiện trong DEPLOY.md.

Mã nguồn nằm trực tiếp trong folder manageandstore. `store.py` là Discord Cog, `store_db.py` quản lý dữ liệu SQLite, `payments.py` tích hợp SDK payOS chính thức. Bot tải Cog trước khi sync slash command.

## Chạy bản đầu

Khuyến nghị Python 3.11–3.13 với đầy đủ thư viện theo `requirements.txt`.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
# Điền .env rồi chạy:
.\.venv\Scripts\python.exe bot.py
```

Không copy đè `.env` nếu đã cấu hình. Bot cần Send Messages, Embed Links, Attach Files và View Channels ngoài quyền setup đã mô tả trong README. Bật Message Content Intent để dùng lệnh `!store`.

## Lệnh cửa hàng

Lệnh đều có cả slash `/store ...` và prefix `!store ...`. Tham số trong bảng là tên tham số slash; với prefix, nhập theo thứ tự cột tham số.

| Lệnh | Tham số | Quyền / tác dụng |
| --- | --- | --- |
| configure | staff_role, log_channel | Admin chọn role nhân viên và kênh log riêng tư |
| add | sku, price, stock, name, description (tùy chọn) | Nhân viên thêm sản phẩm; giá là số nguyên VND |
| edit | sku, price (tùy chọn), stock_delta=0, active (tùy chọn) | Nhân viên đổi giá, tăng/giảm tồn, ẩn/hiện sản phẩm |
| products | page=1 | Xem danh sách hàng đang bán |
| panel | page=1 | Nhân viên đăng bảng bán hàng công khai, có nút mua |
| buy | sku, quantity=1 | Tạo đơn, giữ kho, trả QR/link riêng tư |
| orders | page=1 | Khách xem đơn của mình |
| allorders | page=1 | Nhân viên xem đơn của server |
| order | code | Khách xem đơn của mình; nhân viên xem đơn của server; đối soát payOS |
| payment | code | Lấy lại link/QR cho cùng đơn, không tạo mã đơn mới |
| cancel | code | Hủy đơn chờ, trả kho sau khi xác minh an toàn |
| paid | code | Nhân viên xác nhận đã nhận tiền, chỉ với đơn manual |
| process | code | Nhân viên nhận xử lý đơn đã thanh toán |
| complete | code | Nhân viên xác nhận đã giao hàng/xong dịch vụ |
| revenue | không có | Số đơn và tổng tiền ghi nhận, chưa trừ phí/hoàn tiền |
| help | không có | Hướng dẫn nhanh |

Ví dụ:

```text
/store add sku: DESIGN price: 50000 stock: 10 name: Thiết kế banner
/store buy sku: DESIGN quantity: 1
/store edit sku: DESIGN stock_delta: 5
/store edit sku: DESIGN active: false
!store add DESIGN 50000 10 "Thiết kế banner" "Một banner theo yêu cầu"
```

Administrator luôn có quyền nhân viên; chưa cấu hình role thì chỉ Administrator quản lý được hàng. Kênh log phải chặn View Channel của mọi role ngoài nhân viên, Administrator và bot, kể cả ghi đè thành viên riêng lẻ. Bot kiểm tra lại quyền kênh log khi gửi.

Bảng panel là ảnh chụp giá/tồn tại thời điểm đăng, dùng `products` để xem mới nhất. Nút mua vẫn hoạt động sau restart. Khi mua, bot kiểm tra lại tồn và lưu cố định giá vào đơn. Thông tin đơn chỉ gửi ephemeral qua slash hoặc DM qua prefix. Nếu không nhận DM, dùng slash command. Cửa hàng giao hàng thủ công qua nhân viên; chưa có giao key tự động hoặc vận chuyển.

## Chế độ thanh toán thủ công

`PAYMENT_PROVIDER=manual` là mặc định. Nhân viên đưa thông tin chuyển khoản, kiểm tra tiền về rồi dùng `/store paid code: ...`. Sau đó `/store process`, `/store complete`.

Đơn chờ giữ kho trong 15 phút. Bot kiểm tra hạn mỗi 30 giây và trả kho khi hết hạn. Không xác nhận đơn manual đã quá hạn; nếu khách chuyển muộn, nhân viên cần kiểm tra và xử lý bên ngoài bot.

## Bật payOS: payment request → QR → webhook

1. Tạo tài khoản tại https://my.payos.vn, hoàn tất xác minh cá nhân/doanh nghiệp và tạo kênh thanh toán liên kết ngân hàng theo hướng dẫn payOS.
2. Điền `.env`:

```dotenv
PAYMENT_PROVIDER=payos
PAYOS_CLIENT_ID=client-id-cua-ban
PAYOS_API_KEY=api-key-cua-ban
PAYOS_CHECKSUM_KEY=checksum-key-cua-ban
PAYMENT_PUBLIC_URL=https://pay.example.com
PAYMENT_HOST=127.0.0.1
PAYMENT_PORT=8080
```

3. Cho địa chỉ HTTPS công khai trên trỏ về HTTP `127.0.0.1:8080` bằng reverse proxy trên máy chủ hoặc tunnel khi phát triển. HTTPS phải có chứng chỉ hợp lệ. Nếu proxy ở container khác, có thể cần `PAYMENT_HOST=0.0.0.0` và cấu hình mạng phù hợp.
4. Chạy bot rồi đăng ký webhook của kênh payOS là `https://pay.example.com/webhooks/payos`. Khi payOS gửi mẫu kiểm tra đã ký nhưng không có đơn tương ứng, bot trả thành công và không sửa đơn.
5. Tạo sản phẩm và đặt một đơn thử. Bot gọi API tạo payment request với mã đơn duy nhất, tiền VND, nội dung ngắn `DH` + 7 chữ số, hạn 15 phút, return/cancel URL. payOS trả link checkout và dữ liệu VietQR; bot render dữ liệu QR thành PNG và gửi kèm nút mở trang thanh toán. Luôn dùng đúng QR/nội dung payOS trả về.
6. Webhook được xác minh chữ ký bằng SDK. Bot đối chiếu mã đơn, paymentLinkId, VND, số tiền; lưu mã tham chiếu chống xử lý trùng. Chuyển `pending → paid` rồi lưu thông báo để gửi khách/kênh log. Việc mở return URL hoặc bấm nút không làm đơn thành đã trả tiền.

Bot cũng đối soát payOS nền: tối đa 10 đơn mỗi đợt 30 giây, mỗi đơn cách ít nhất khoảng 60 giây. `/store order` cho phép đối soát chủ động. Đơn hủy/hết hạn chỉ được trả kho sau khi payOS xác nhận không nhận tiền. Không dựa riêng vào đồng hồ máy để hủy đơn tự động.

Một bộ credential payOS được dùng chung cho toàn bộ bot; sản phẩm và đơn tách theo server nhưng cùng tài khoản nhận tiền. Không hỗ trợ mỗi server một tài khoản ngân hàng trong bản này.

## Khi có lỗi hoặc giao dịch lệch

- Tạo link bị timeout: đơn đã được lưu, kho tiếp tục được giữ. Dùng `payment` hoặc `order` với cùng mã đơn để phục hồi. Không tạo mã đơn khác để thử lại.
- Chưa có link mà đã qua hạn tạo: kiểm tra đơn trên dashboard payOS trước. Bản này không tự trả kho khi không biết request tạo link đã thành công hay chưa; không tự suy đoán từ lỗi mạng.
- Tiền thiếu/thừa, thanh toán muộn sau khi trả kho: trạng thái `review`, không được chuyển sang giao hàng. Nhân viên đối chiếu dashboard/ngân hàng; nếu payOS sau đó xác nhận đã thu đủ cho đơn còn giữ kho, đối soát sẽ chuyển sang `paid`.
- Giao dịch cần hoàn tiền hoặc chấp nhận thanh toán muộn sau trả kho phải xử lý thủ công ngoài bot trong bản này. Bot chưa có lệnh hoàn tiền hoặc lệnh bỏ qua trạng thái review.
- Không cần tin nhắn Discord gửi thành công mới lưu thanh toán. Thông báo được lưu và thử gửi tối đa 6 lần khi lỗi tạm thời; DM bị chặn không làm mất trạng thái đơn. Trường hợp retry có thể có thông báo lặp, không tạo giao dịch lặp.
- SQLite là dữ liệu chính: backup thư mục `data` khi bot đã tắt, hoặc dùng SQLite backup API khi đang chạy. Không commit `.env` hoặc file cơ sở dữ liệu lên git. Một tiến trình bot trên một file DB là cách triển khai được hỗ trợ.

Không mặc định dịch vụ miễn phí: phí, điều kiện liên kết ngân hàng và hạn mức cần kiểm tra với payOS. Tạo QR đơn thuần không chứng minh khách đã chuyển tiền; cần webhook có xác thực hoặc đối soát API.

## Kiểm thử

```powershell
python -m unittest discover -s tests -v
python -m compileall -q bot.py plan.py store.py store_db.py payments.py tests
```

Các kiểm thử DB không gọi mạng: chống bán vượt kho, xử lý webhook trùng, giá đơn không đổi khi sửa giá sản phẩm, cách ly server, hoàn kho đúng một lần, hạn đơn và thanh toán đến muộn. Các kiểm thử HTTP/SDK và command smoke test cần cài đủ `requirements.txt`; kiểm thử SDK dùng khóa giả và payload tự ký, không gọi thanh toán thật.

Kết quả tại môi trường triển khai mã: 22 kiểm thử schema/SQLite đạt, 31 kiểm thử HTTP/SDK/Discord/PostgreSQL local bị bỏ qua vì thiếu thư viện hoặc database test; kiểm tra cú pháp Python đạt. Cài thư viện bằng pip tại môi trường này không thành công, nên chưa xác nhận import/runtime Discord hoặc giao dịch payOS thực tế.

Tài liệu chính thức đã dùng:

- https://payos.vn/docs/api/
- https://payos.vn/docs/sdks/back-end/python/
- https://payos.vn/docs/tich-hop-webhook/kiem-tra-du-lieu-voi-signature/
- https://payos.vn/docs/du-lieu-tra-ve/webhook/
- https://developer.sepay.vn/vi/sepay-webhooks/tao-qr-va-form-thanh-toan
