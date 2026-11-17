import json
import unittest

from ai_setup import InvalidPlan, parse_plan, wire_schema, build_request


def plan():
    return {"summary": "Study", "staff_role": "Teacher", "categories": [
        {"name": "Study", "channels": [{"name": "general", "kind": "text", "topic": "", "access": "public"}]}]}


def response(value, finish_reason="stop"):
    return {"choices": [{"finish_reason": finish_reason, "message": {"content": value}}]}


class AIPlanTests(unittest.TestCase):
    def test_native_gemini_response_and_thought_filter(self):
        result = parse_plan({"candidates": [{"finishReason": "STOP", "content": {"parts": [
            {"text": "PRIVATE THOUGHT", "thought": True}, {"text": json.dumps(plan())}]}}]})
        self.assertEqual(result.staff_role, "Teacher")

    def test_gemini_truncated_or_blocked(self):
        for data in ({"candidates": [{"finishReason": "MAX_TOKENS"}]},
                     {"promptFeedback": {"blockReason": "SAFETY"}},
                     {"candidates": [{"finishReason": "SAFETY"}]}):
            with self.assertRaises(InvalidPlan):
                parse_plan(data)

    def test_gemini_request_supports_old_compatibility_base_without_leaking_key_to_url(self):
        messages = [{"role": "system", "content": "Rules"}, {"role": "user", "content": "Build store"}]
        for base in ("https://generativelanguage.googleapis.com/v1beta", "https://generativelanguage.googleapis.com/v1beta/openai"):
            url, headers, payload = build_request(base, "models/gemini-3.8-flash", "TEST_KEY", messages, wire_schema(), 6000)
            self.assertTrue(url.endswith("/v1beta/models/gemini-3.8-flash:generateContent"))
            self.assertNotIn("TEST_KEY", url)
            self.assertEqual(headers, {"x-goog-api-key": "TEST_KEY"})
            self.assertEqual(payload["generationConfig"]["responseMimeType"], "application/json")
            self.assertIn("responseJsonSchema", payload["generationConfig"])
            self.assertNotIn("reasoning_effort", payload)

    def test_bad_gemini_model_is_configuration_error(self):
        with self.assertRaises(ValueError):
            build_request("https://generativelanguage.googleapis.com/v1beta", "model?key=bad", "TEST", [], {}, 100)

    def test_fenced_json(self):
        result = parse_plan(response("```json\n" + json.dumps(plan()) + "\n```"))
        self.assertEqual(result.staff_role, "Teacher")

    def test_reports_truncation_separately(self):
        with self.assertRaisesRegex(InvalidPlan, "token"):
            parse_plan(response('{"summary":', "length"))

    def test_invalid_field_is_diagnosed_without_content(self):
        data = plan()
        data["categories"][0]["channels"][0]["kind"] = "PRIVATE-SENSITIVE-CONTENT"
        with self.assertRaises(InvalidPlan) as caught:
            parse_plan(response(json.dumps(data)))
        self.assertIn("categories.0.channels.0.kind", str(caught.exception))
        self.assertNotIn("PRIVATE-SENSITIVE", str(caught.exception))

    def test_unsafe_extra_permission_still_rejected(self):
        data = plan()
        data["permissions"] = "administrator"
        with self.assertRaises(InvalidPlan):
            parse_plan(response(json.dumps(data)))

    def test_bad_envelope_and_empty_content(self):
        for data in ({}, {"choices": []}, {"choices": None}, {"choices": [None]},
                     {"choices": [{"message": "invalid"}]}, response(None), response("")):
            with self.assertRaises(InvalidPlan):
                parse_plan(data)

    def test_strict_wire_schema_requires_defaulted_fields(self):
        schema = wire_schema()
        channel = schema["$defs"]["Channel"]
        self.assertIn("access", channel["required"])
        self.assertIn("topic", channel["required"])
        self.assertFalse(channel["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
