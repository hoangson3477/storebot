# AI setup với Gemini

Trong `.env` hoặc biến môi trường host:

```dotenv
AI_BASE_URL=https://generativelanguage.googleapis.com/v1beta
AI_API_KEY=KEY_GEMINI_CUA_BAN
AI_MODEL=gemini-3.8-flash
```

Có thể giữ model Gemini khác đang được tài khoản của bạn cấp quyền. Không ghi API key vào Git. Bản cập nhật giữ nguyên `.env` hiện có.

Bot tự nhận diện hostname Google và gọi REST `models/{model}:generateContent`, dùng header `x-goog-api-key`, `systemInstruction`, `contents`, JSON MIME type và JSON Schema trong generationConfig. Không cần cài SDK Google thêm. Cấu hình cũ kết thúc bằng `/v1beta/openai` cũng được nhận diện và chuyển sang đường gọi Gemini native, không cần đổi key.

Phản hồi được đọc từ `candidates[].content.parts`, bỏ phần thought nếu có; MAX_TOKENS được báo riêng, nội dung bị chặn không bị coi là JSON hợp lệ. Bot kiểm tra lại giới hạn kênh/role bằng Pydantic và thử sửa tối đa 3 lượt. Không ghi key hoặc nội dung phản hồi thô vào log.

Sau khi cập nhật `ai_setup.py`, restart bot, dùng `/setup describe: ...` rồi xem bản đề xuất. Không đổi cấu trúc server cho tới khi người dùng xác nhận.

Nếu báo HTTP 400, cần kiểm tra model/schema; 401/403 là key/quyền, 404 là endpoint/model, 429 là hạn mức. Việc thay model không tự tăng hạn mức API.

Tài liệu: https://ai.google.dev/api/generate-content và https://ai.google.dev/gemini-api/docs/structured-output
