"""
The OpenAPI schema (docs/openapi.yaml) is generated from the views by
`make openapi` (drf-spectacular). These tests keep the committed file in
step with the code: regenerate it whenever views, serializers or filters change.
"""

import tempfile
from pathlib import Path

import yaml
from django.conf import settings
from django.core.management import call_command
from django.test import SimpleTestCase

from apps.common.tests.test_no_email_in_responses import api_view_names

SCHEMA_FILE = Path(settings.BASE_DIR) / "docs" / "openapi.yaml"


def generate() -> str:
    """What `make openapi` writes (fails on any schema warning, like the command)."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "openapi.yaml"
        call_command("spectacular", file=str(out), validate=True, fail_on_warn=True)
        return out.read_text(encoding="utf-8")


class OpenAPISchemaTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.generated = generate()
        cls.spec = yaml.safe_load(cls.generated)

    def test_committed_schema_is_up_to_date(self):
        self.assertTrue(SCHEMA_FILE.exists(), "docs/openapi.yaml is missing: run `make openapi`.")
        self.assertEqual(
            SCHEMA_FILE.read_text(encoding="utf-8"), self.generated,
            "docs/openapi.yaml is out of date: run `make openapi` and commit it.",
        )

    def test_every_endpoint_is_documented(self):
        operations = [op for item in self.spec["paths"].values() for method, op in item.items()
                      if method in ("get", "post", "put", "patch", "delete")]
        self.assertEqual(len(self.spec["paths"]), len(api_view_names()))
        for op in operations:
            with self.subTest(op["operationId"]):
                self.assertTrue(op.get("summary") and op.get("tags"), "summary and tag")
                if op.get("security"):  # needs a login (the account endpoints don't)
                    self.assertIn("403", op["responses"], "not logged in")
