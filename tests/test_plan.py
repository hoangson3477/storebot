import unittest

from pydantic import ValidationError
from plan import Plan


def example():
    return {"summary": "Server học tập", "staff_role": "Giáo viên", "categories": [
        {"name": "Học tập", "channels": [{"name": "tài-liệu", "kind": "text", "access": "readonly"}]}
    ]}


class PlanTests(unittest.TestCase):
    def test_valid_plan(self):
        self.assertEqual(Plan.model_validate(example()).categories[0].channels[0].access, "readonly")

    def test_disallows_arbitrary_permissions(self):
        data = example()
        data["permissions"] = "administrator"
        with self.assertRaises(ValidationError):
            Plan.model_validate(data)

    def test_disallows_unknown_channel_and_access(self):
        for field, value in [("kind", "forum"), ("access", "admin")]:
            data = example()
            data["categories"][0]["channels"][0][field] = value
            with self.assertRaises(ValidationError):
                Plan.model_validate(data)

    def test_duplicate_category(self):
        data = example()
        data["categories"] *= 2
        with self.assertRaises(ValidationError):
            Plan.model_validate(data)

    def test_total_channels_limited(self):
        data = example()
        data["categories"] = [{"name": str(i), "channels": [
            {"name": str(j), "kind": "text"} for j in range(8)]} for i in range(4)]
        with self.assertRaises(ValidationError):
            Plan.model_validate(data)


if __name__ == "__main__":
    unittest.main()
