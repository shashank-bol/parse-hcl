import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from parse_hcl import TerraformParser, to_tf  # noqa: E402


def _without_source(obj):  # type: ignore[no-untyped-def]
    """Drop per-block ``source`` paths so round-trips are comparable across temp files."""
    if isinstance(obj, dict):
        return {k: _without_source(v) for k, v in obj.items() if k != "source"}
    if isinstance(obj, list):
        return [_without_source(x) for x in obj]
    return obj


_EC2_BASIC_TF = '''\
resource "aws_instance" "web" {
  ami           = "ami-0c55b159cbfafe1f0"
  instance_type = "t3.micro"

  tags = {
    Name = "web-server"
  }
}
'''


class TfWriterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixtures = ROOT / "tests" / "fixtures"
        self.parser = TerraformParser()

    def test_to_tf_parse_round_trip_aws_ec2_instance(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".tf", delete=False, encoding="utf-8") as f:
            f.write(_EC2_BASIC_TF)
            src = f.name
        try:
            doc = self.parser.parse_file(src)
            self.assertEqual(len(doc["resource"]), 1)
            r = doc["resource"][0]
            self.assertEqual(r["type"], "aws_instance")
            self.assertEqual(r["name"], "web")
            self.assertEqual(r["properties"]["instance_type"].get("raw"), '"t3.micro"')

            out = to_tf(doc, prefer_raw=True)
            self.assertIn('resource "aws_instance" "web"', out)
            self.assertIn("instance_type", out)
            self.assertIn("tags", out)

            with tempfile.NamedTemporaryFile(mode="w", suffix=".tf", delete=False, encoding="utf-8") as g:
                g.write(out)
                tmp = g.name
            try:
                doc2 = self.parser.parse_file(tmp)
                self.assertEqual(_without_source(doc), _without_source(doc2))
            finally:
                Path(tmp).unlink(missing_ok=True)
        finally:
            Path(src).unlink(missing_ok=True)

    def test_to_tf_parse_round_trip_main(self) -> None:
        path = self.fixtures / "main.tf"
        doc = self.parser.parse_file(str(path))
        out = to_tf(doc, prefer_raw=True)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".tf", delete=False, encoding="utf-8") as f:
            f.write(out)
            tmp = f.name
        try:
            doc2 = self.parser.parse_file(tmp)
            self.assertEqual(_without_source(doc), _without_source(doc2))
        finally:
            Path(tmp).unlink(missing_ok=True)

    def test_to_tf_export_payload_uses_nested_document(self) -> None:
        from parse_hcl import to_export

        doc = self.parser.parse_file(str(self.fixtures / "main.tf"))
        export = to_export(doc, prune_empty=False)
        out = to_tf(export, prefer_raw=True)
        self.assertIn('variable "region"', out)
        self.assertIn("resource \"aws_s3_bucket\" \"demo\"", out)

    def test_to_tf_synthesizes_minimal_dict_without_raw(self) -> None:
        doc = {
            "terraform": [],
            "provider": [],
            "variable": [
                {
                    "name": "region",
                    "type": "string",
                    "default": {"type": "literal", "value": "us-east-1", "raw": '"us-east-1"'},
                }
            ],
            "output": [],
            "module": [],
            "resource": [],
            "data": [],
            "locals": [],
            "moved": [],
            "import": [],
            "check": [],
            "terraform_data": [],
            "unknown": [],
        }
        out = to_tf(doc, prefer_raw=False)
        self.assertIn('variable "region"', out)
        self.assertIn("default = \"us-east-1\"", out)

    def test_emit_value_uses_raw_when_present(self) -> None:
        from parse_hcl.utils.serialization.tf_writer import emit_value

        v = {"type": "expression", "kind": "traversal", "raw": "var.region"}
        self.assertEqual(emit_value(v), "var.region")

    def test_emit_value_literal_prefers_value_over_stale_raw(self) -> None:
        from parse_hcl.utils.serialization.tf_writer import emit_value

        v = {"type": "literal", "value": "new", "raw": '"old"'}
        self.assertEqual(emit_value(v), '"new"')

    def test_emit_value_template_rewraps_inner_body_as_quoted_hcl_string(self) -> None:
        """Parser stores template body without quotes; emit must add them for valid .tf."""
        from parse_hcl.utils.parser.value_classifier import classify_value
        from parse_hcl.utils.serialization.tf_writer import emit_value

        hcl = (
            '"975910769154.dkr.ecr.${var.region}.amazonaws.com/devops-automations:lambda-1.10"'
        )
        v = classify_value(hcl)
        self.assertEqual(v.get("type"), "expression")
        self.assertEqual(v.get("kind"), "template")
        self.assertIn("${var.region}", v.get("raw", ""))

        out = emit_value(v)
        self.assertTrue(out.startswith('"') and out.endswith('"'))
        self.assertIn("${var.region}", out)

    def test_emit_value_object_is_line_separated_not_comma_separated(self) -> None:
        from parse_hcl.utils.serialization.tf_writer import emit_value

        obj = {
            "type": "object",
            "value": {
                "Name": {"type": "literal", "value": "web", "raw": '"web"'},
                "Env": {"type": "literal", "value": "prod", "raw": '"prod"'},
            },
        }
        out = emit_value(obj, _close_indent=2)
        self.assertTrue(out.startswith("{\n"))
        self.assertIn("\n    Name = ", out)
        self.assertIn("\n    Env = ", out)
        self.assertTrue(out.rstrip().endswith("}"))
        self.assertNotIn(", Env =", out)
        self.assertNotRegex(out, r"web\"[\s]*,")


class TfWriterIntegrationTest(unittest.TestCase):
    """
    End-to-end flows: parse HCL → mutate document dict → serialize with ``to_tf``.

    Default emission uses structured fields, so edits to ``properties`` / value dicts are
    reflected without clearing ``raw``. Use ``prefer_raw=True`` only to paste original
    block text (may ignore structured edits).
    """

    def setUp(self) -> None:
        self.parser = TerraformParser()

    def test_parse_dict_add_tag_convert_to_tf(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".tf", delete=False, encoding="utf-8") as f:
            f.write(_EC2_BASIC_TF)
            src = f.name
        try:
            doc = self.parser.parse_file(src)
            resource = doc["resource"][0]
            tags_value = resource["properties"]["tags"]
            self.assertEqual(tags_value["type"], "object")
            inner = tags_value.get("value") or {}
            self.assertIn("Name", inner)

            inner["Environment"] = {
                "type": "literal",
                "value": "prod",
                "raw": '"prod"',
            }

            tf_text = to_tf(doc)
            self.assertIn("Environment", tf_text)
            self.assertIn("prod", tf_text)
            self.assertIn("Name", tf_text)
            self.assertIn("web-server", tf_text)

            with tempfile.NamedTemporaryFile(mode="w", suffix=".tf", delete=False, encoding="utf-8") as g:
                g.write(tf_text)
                tmp = g.name
            try:
                doc2 = self.parser.parse_file(tmp)
                tags2 = doc2["resource"][0]["properties"]["tags"]
                env = (tags2.get("value") or {}).get("Environment")
                self.assertIsInstance(env, dict)
                self.assertEqual(env.get("value"), "prod")
            finally:
                Path(tmp).unlink(missing_ok=True)
        finally:
            Path(src).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
