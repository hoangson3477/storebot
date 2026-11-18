"""Validated AI planning with bounded repair attempts and safe diagnostics."""
import json
import logging
import os
import re
import time
from urllib.parse import urlparse

import aiohttp
from pydantic import ValidationError

from plan import Plan, SYSTEM_PROMPT

log = logging.getLogger(__name__)


class InvalidPlan(ValueError):
    pass


def parse_plan(body):
    try:
        if isinstance(body, dict) and ("candidates" in body or "promptFeedback" in body):
            if body.get("promptFeedback", {}).get("blockReason"):
                raise InvalidPlan("Gemini từ chối tạo bản thiết kế này.")
            candidate = body.get("candidates", [])[0]
            finish = candidate.get("finishReason", "STOP")
            if finish == "MAX_TOKENS":
                raise InvalidPlan("Phản hồi Gemini bị cắt vì hết giới hạn token.")
            if finish != "STOP":
                raise InvalidPlan("Gemini không hoàn tất bản thiết kế (" + re.sub(r"[^A-Z_]", "", str(finish))[:40] + ").")
            content = "".join(part.get("text", "") for part in candidate.get("content", {}).get("parts", [])
                              if not part.get("thought"))
            body = {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
        choice = body["choices"][0]
        if choice.get("finish_reason") == "length":
            raise InvalidPlan("Phản hồi bị cắt vì hết giới hạn token.")
        if choice.get("finish_reason") == "content_filter" or choice["message"].get("refusal"):
            raise InvalidPlan("Model từ chối tạo bản thiết kế này.")
        content = choice["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise InvalidPlan("Model không trả nội dung JSON.")
        content = content.strip()
        # Some compatible endpoints wrap otherwise valid JSON in a Markdown code fence.
        fenced = re.fullmatch(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.DOTALL | re.IGNORECASE)
        if fenced:
            content = fenced.group(1).strip()
        return Plan.model_validate_json(content)
    except ValidationError as exc:
        issues = []
        for error in exc.errors(include_input=False, include_context=False, include_url=False)[:5]:
            path = ".".join(str(x) for x in error["loc"]) or "JSON"
            path = re.sub(r"[^a-zA-Z0-9_.]", "_", path)[:100]
            issues.append(f"{path}: {error['type']}")
        raise InvalidPlan("Sai cấu trúc: " + "; ".join(issues)) from None
    except (KeyError, IndexError, TypeError, AttributeError):
        raise InvalidPlan("Endpoint không trả đúng định dạng Chat Completions.") from None


def wire_schema():
    schema = Plan.model_json_schema()

    def clean(node):
        if isinstance(node, dict):
            for key in ("default", "title", "minLength", "maxLength", "pattern", "minItems", "maxItems"):
                node.pop(key, None)
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for value in node.values():
                clean(value)
        elif isinstance(node, list):
            for value in node:
                clean(value)
    clean(schema)
    return schema


def build_request(base, model, key, messages, schema, tokens):
    parsed = urlparse(base)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("AI_BASE_URL cần là HTTPS URL gốc, không chứa key hoặc query.")
    if parsed.hostname == "generativelanguage.googleapis.com":
        if parsed.path.rstrip("/") not in ("", "/v1beta", "/v1beta/openai", "/v1"):
            raise ValueError("Gemini AI_BASE_URL: dùng https://generativelanguage.googleapis.com/v1beta")
        model = model.removeprefix("models/")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", model):
            raise ValueError("AI_MODEL phải là tên model Gemini, không phải URL.")
        version = "v1" if parsed.path.rstrip("/") == "/v1" else "v1beta"
        payload = {
            "systemInstruction": {"parts": [{"text": messages[0]["content"]}]},
            "contents": [{"role": "model" if item["role"] == "assistant" else "user",
                          "parts": [{"text": item["content"]}]} for item in messages[1:]],
            "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": schema,
                                 "maxOutputTokens": tokens},
        }
        return (f"https://generativelanguage.googleapis.com/{version}/models/{model}:generateContent",
                {"x-goog-api-key": key}, payload)
    return (base.rstrip("/") + "/chat/completions", {"Authorization": f"Bearer {key}"},
            {"model": model, "messages": messages, "response_format": {"type": "json_object"}, "max_tokens": tokens})


async def propose(description: str) -> Plan:
    key, model = os.getenv("AI_API_KEY"), os.getenv("AI_MODEL")
    if not key or not model:
        raise ValueError("Chưa cấu hình AI_API_KEY và AI_MODEL trong .env.")
    base = os.getenv("AI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    if not base.startswith("https://"):
        raise ValueError("AI_BASE_URL phải dùng HTTPS.")
    schema = wire_schema()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + "\nJSON schema bắt buộc:\n" + json.dumps(schema, ensure_ascii=False)},
        {"role": "user", "content": description},
    ]
    last_error = ""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        for attempt in range(3):
            url, headers, payload = build_request(base, model, key, messages, schema, 6000 if attempt == 0 else 8192)
            started = time.monotonic()
            log.info("ai.request_started provider_host=%s model=%s attempt=%s", urlparse(base).hostname, model, attempt + 1)
            async with session.post(url, headers=headers, json=payload) as response:
                log.info("ai.response status=%s attempt=%s elapsed_ms=%.0f", response.status, attempt + 1,
                         (time.monotonic() - started) * 1000)
                if response.status != 200:
                    hints = {401: "API key không hợp lệ", 403: "không có quyền dùng model",
                             404: "sai endpoint hoặc model", 429: "hết hạn mức hoặc gọi quá nhanh"}
                    raise ValueError(f"AI HTTP {response.status}: {hints.get(response.status, 'kiểm tra cấu hình và dịch vụ AI')}.")
                try:
                    body = await response.json()
                except (ValueError, aiohttp.ContentTypeError):
                    raise ValueError("Endpoint AI không trả JSON. Kiểm tra AI_BASE_URL.") from None
            try:
                plan = parse_plan(body)
                log.info("ai.plan_validated attempt=%s categories=%s channels=%s", attempt + 1, len(plan.categories),
                         sum(len(category.channels) for category in plan.categories))
                return plan
            except InvalidPlan as exc:
                last_error = str(exc)
                log.warning("Setup response invalid (attempt %s): %s", attempt + 1, last_error)
                # No raw model output/user prompt/key is logged or included in error messages.
                messages.append({"role": "user", "content":
                    "Bản trước không hợp lệ: " + last_error +
                    ". Hãy tạo lại TOÀN BỘ JSON đúng schema; không Markdown; tên không có @/#; "
                    "tối đa 6 category, 30 kênh tổng, mỗi category tối đa 8 kênh. "
                    "Rút gọn mô tả và bỏ lời giải thích ngoài JSON."})
    raise ValueError("Chưa tạo được bản setup sau khi tự thử lại. " + last_error)
