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


_TEMPLATEFILE_LOCAL_TF = '''\
locals {
  backend_webapp_container_definitions = templatefile(
    "./.terraform/modules/tf-modules/templates/ecs/webapp-main.tmpl",
    {
      awslogs_group_name    = "/ecs/fargate-webapp-main-${lower(var.env_short_name)}"
      awslogs_stream_prefix = "ecs"
      env_short_name        = var.env_short_name
      region                = var.region
      ecr_region            = var.region
      env_name              = lower(var.env_short_name)

      }
      )
      }
module "fargate-django" {
  source               = "./.terraform/modules/tf-modules/ecs/django"
  env_short_name       = var.env_short_name
  backend_webapp = {
    container_definitions = local.backend_webapp_container_definitions_dynamic
    target_group_arn      = module.webapp_main_lb.target_group_id
    cpu                   = 2048
    memory                = 16384
  }
 }
'''


class FunctionCallStructuredRoundTripTest(unittest.TestCase):
    """``function_call`` emission is driven by ``name`` + ``attributes`` (not raw).

    Parsing the output must preserve every local, module, argument, and map key of
    the input — confirming the ``templatefile(...)`` call and its nested map survive
    both directions and that a second parse/emit cycle is idempotent.
    """

    def setUp(self) -> None:
        self.parser = TerraformParser()

    def _parse_tf_string(self, tf_text: str) -> dict:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".tf", delete=False, encoding="utf-8") as f:
            f.write(tf_text)
            path = f.name
        try:
            return self.parser.parse_file(path)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_templatefile_call_round_trip_preserves_all_fields(self) -> None:
        doc = self._parse_tf_string(_TEMPLATEFILE_LOCAL_TF)

        local_val = doc["locals"][0]["value"]
        self.assertEqual(local_val["kind"], "function_call")
        self.assertEqual(local_val["name"], "templatefile")
        self.assertEqual(len(local_val["attributes"]), 2)

        tf_out = to_tf(doc)

        # Output must not be driven by raw — it should look structured and
        # include the quoted template interpolation, not the raw inner body.
        self.assertIn('"/ecs/fargate-webapp-main-${lower(var.env_short_name)}"', tf_out)
        self.assertIn("templatefile(", tf_out)
        self.assertIn("lower(var.env_short_name)", tf_out)

        doc2 = self._parse_tf_string(tf_out)

        # Whitespace alignment may be reformatted, so we assert structural
        # equivalence via targeted checks rather than deep dict equality
        # (``raw`` text captures the original spacing and will differ).

        # Locals / modules retained.
        self.assertEqual(len(doc2["locals"]), 1)
        self.assertEqual(len(doc2["module"]), 1)

        # templatefile callee name + args preserved across round trip.
        v2 = doc2["locals"][0]["value"]
        self.assertEqual(v2["kind"], "function_call")
        self.assertEqual(v2["name"], "templatefile")
        self.assertEqual(len(v2["attributes"]), 2)

        # Every map key in the second arg preserved and in-order.
        map1 = local_val["attributes"][1]["value"]
        map2 = v2["attributes"][1]["value"]
        self.assertEqual(list(map1.keys()), list(map2.keys()))

        # Nested ``lower(...)`` call also keeps its structure.
        env_name_val = map2["env_name"]
        self.assertEqual(env_name_val["kind"], "function_call")
        self.assertEqual(env_name_val["name"], "lower")
        self.assertEqual(env_name_val["attributes"][0]["raw"], "var.env_short_name")

        # Second emit is byte-identical to first (idempotent).
        self.assertEqual(to_tf(doc2), tf_out)

    def test_templatefile_round_trip_survives_json_serialization(self) -> None:
        import json as _json

        doc = self._parse_tf_string(_TEMPLATEFILE_LOCAL_TF)
        as_json_roundtrip = _json.loads(_json.dumps(doc, default=str))

        tf_out = to_tf(as_json_roundtrip)
        doc2 = self._parse_tf_string(tf_out)

        self.assertEqual(
            list(doc["locals"][0]["value"]["attributes"][1]["value"].keys()),
            list(doc2["locals"][0]["value"]["attributes"][1]["value"].keys()),
        )
        self.assertEqual(
            sorted(doc["module"][0]["properties"].keys()),
            sorted(doc2["module"][0]["properties"].keys()),
        )


_CHAINED_CALL_TF = '''\
locals {
  backend_webapp_container_definitions_dynamic = jsonencode([merge(jsondecode(local.backend_webapp_container_definitions)[0],
    {
      environment = concat(
        jsondecode(local.backend_webapp_container_definitions)[0]["environment"],
        [
          {
            "name" : "ACCESS_LOGS_ENABLED",
            "value" : "true"
          }
        ]
      )
    }
  ), jsondecode(local.backend_webapp_container_definitions)[1], jsondecode(local.backend_webapp_container_definitions)[2]])
}
'''


class ChainedFunctionCallRoundTripTest(unittest.TestCase):
    """Round-trip for function calls with index / key accessor chains.

    HCL allows ``jsondecode(x)[0]["k"]`` — the accessor chain after the closing
    ``)`` was previously dropped on serialize, producing invalid / lossy output.
    These tests pin the classifier's ``trailer`` capture and the emitter's
    reconstruction of the full expression (plus idempotent re-emit).
    """

    def setUp(self) -> None:
        self.parser = TerraformParser()

    def _parse_tf_string(self, tf_text: str) -> dict:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".tf", delete=False, encoding="utf-8") as f:
            f.write(tf_text)
            path = f.name
        try:
            return self.parser.parse_file(path)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_chained_call_trailers_preserved_on_round_trip(self) -> None:
        doc = self._parse_tf_string(_CHAINED_CALL_TF)
        tf_out = to_tf(doc)

        # Every accessor on the jsondecode result must appear verbatim.
        self.assertIn("jsondecode(local.backend_webapp_container_definitions)[0]", tf_out)
        self.assertIn('jsondecode(local.backend_webapp_container_definitions)[0]["environment"]', tf_out)
        self.assertIn("jsondecode(local.backend_webapp_container_definitions)[1]", tf_out)
        self.assertIn("jsondecode(local.backend_webapp_container_definitions)[2]", tf_out)

        # Re-parse and assert structural invariants + idempotent re-emit.
        doc2 = self._parse_tf_string(tf_out)

        outer = doc2["locals"][0]["value"]
        self.assertEqual(outer["kind"], "function_call")
        self.assertEqual(outer["name"], "jsonencode")

        arr_elems = outer["attributes"][0]["value"]
        self.assertEqual(len(arr_elems), 3)

        # merge(jsondecode(...)[0], {...})
        merge_call = arr_elems[0]
        self.assertEqual(merge_call["name"], "merge")
        merge_arg0 = merge_call["attributes"][0]
        self.assertEqual(merge_arg0["name"], "jsondecode")
        self.assertEqual(merge_arg0["trailer"], "[0]")

        # inside merge's second arg, the environment = concat(...) call has
        # a jsondecode(...)[0]["environment"] first arg.
        env_obj = merge_call["attributes"][1]["value"]
        concat_call = env_obj["environment"]
        self.assertEqual(concat_call["name"], "concat")
        self.assertEqual(concat_call["attributes"][0]["trailer"], '[0]["environment"]')

        # sibling elements keep their trailers too
        self.assertEqual(arr_elems[1]["trailer"], "[1]")
        self.assertEqual(arr_elems[2]["trailer"], "[2]")

        # Second emit is byte-identical.
        self.assertEqual(to_tf(doc2), tf_out)


if __name__ == "__main__":
    unittest.main()
