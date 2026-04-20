"""
Tests for the OOP terraform model in :mod:`parse_hcl.oop`.

Coverage:

- ``TerraformModule.from_tf_file`` round-trips through JSON and ``.tf``.
- ``Value`` subclass tree construction and mutation (literal / object / array /
  expression) with arbitrary nesting depth.
- Block update API (resource properties / meta, variable defaults, output
  values, dynamic blocks, nested blocks, locals).
- ``raw`` is dropped on serialization.
- File IO (``save_json`` / ``save_tf``).
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from parse_hcl import TerraformParser  # noqa: E402
from parse_hcl.oop import (  # noqa: E402
    ArrayValue,
    DataBlock,
    DynamicBlock,
    ExpressionValue,
    LiteralValue,
    LocalValue,
    ModuleCallBlock,
    NestedBlock,
    ObjectValue,
    OutputBlock,
    ProviderBlock,
    ResourceBlock,
    TerraformModule,
    TerraformSettingsBlock,
    Value,
    VariableBlock,
)


_DROPPED_KEYS = {"raw", "source"}


def _normalize(obj):
    """
    Return a comparable shape: drop ``raw`` / ``source``, ``None`` values, and
    empty containers. Lets us check structural equality between parser output
    (which carries echoes like ``raw`` and explicit ``alias: None``) and the
    OOP serializer (which omits both).
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _DROPPED_KEYS:
                continue
            cleaned = _normalize(v)
            if cleaned is None or cleaned == [] or cleaned == {}:
                continue
            out[k] = cleaned
        return out
    if isinstance(obj, list):
        return [_normalize(x) for x in obj]
    return obj


class ValueTreeTest(unittest.TestCase):
    """Direct unit tests on the :class:`Value` class hierarchy."""

    def test_literal_round_trip(self) -> None:
        v = LiteralValue(42)
        self.assertEqual(v.to_dict(), {"type": "literal", "value": 42})
        self.assertEqual(v.to_python(), 42)
        v.set("hello")
        self.assertEqual(v.value, "hello")

    def test_object_value_supports_nested_mutations(self) -> None:
        obj = ObjectValue({"name": "demo"})
        obj.set("count", 3)
        obj.set("tags", ObjectValue({"env": "dev"}))
        nested = obj.get("tags")
        self.assertIsInstance(nested, ObjectValue)
        nested.set("owner", "team-a")

        self.assertEqual(
            obj.to_dict(),
            {
                "type": "object",
                "value": {
                    "name": {"type": "literal", "value": "demo"},
                    "count": {"type": "literal", "value": 3},
                    "tags": {
                        "type": "object",
                        "value": {
                            "env": {"type": "literal", "value": "dev"},
                            "owner": {"type": "literal", "value": "team-a"},
                        },
                    },
                },
            },
        )
        # Removal works.
        obj.remove("count")
        self.assertNotIn("count", obj)

    def test_array_value_holds_typed_children(self) -> None:
        arr = ArrayValue([1, 2, 3])
        arr.append("four")
        arr.insert(0, ObjectValue({"k": "v"}))
        arr.set_at(2, ArrayValue([10, 20]))
        self.assertEqual(len(arr), 5)
        self.assertIsInstance(arr.get_at(0), ObjectValue)
        self.assertIsInstance(arr.get_at(2), ArrayValue)
        # Walk descends into children.
        types = sorted({type(x).__name__ for x in arr.walk()})
        self.assertIn("ArrayValue", types)
        self.assertIn("ObjectValue", types)
        self.assertIn("LiteralValue", types)

    def test_value_from_dict_dispatches_on_discriminator(self) -> None:
        cases = {
            "literal": (LiteralValue, {"type": "literal", "value": 1, "raw": "1"}),
            "array": (ArrayValue, {"type": "array", "value": [1, 2]}),
            "object": (ObjectValue, {"type": "object", "value": {"a": 1}}),
            "expression": (
                ExpressionValue,
                {"type": "expression", "kind": "traversal", "raw": "var.x"},
            ),
        }
        for label, (cls, payload) in cases.items():
            v = Value.from_dict(payload)
            self.assertIsInstance(v, cls, f"discriminator {label} should give {cls.__name__}")

    def test_serialization_drops_raw_for_non_expression(self) -> None:
        v = Value.from_dict({"type": "literal", "value": "hi", "raw": '"hi"'})
        self.assertNotIn("raw", v.to_dict())
        # Expressions keep raw because the writer needs the source text.
        e = Value.from_dict({"type": "expression", "kind": "traversal", "raw": "var.x"})
        self.assertEqual(e.to_dict()["raw"], "var.x")


class BlockApiTest(unittest.TestCase):
    """Class-method update API on individual block types."""

    def test_resource_block_update_property_and_meta(self) -> None:
        r = ResourceBlock(type="aws_s3_bucket", name="demo")
        r.set_property("bucket", LiteralValue("my-bucket"))
        r.set_meta("count", LiteralValue(3))
        self.assertEqual(r.get_property("bucket").to_python(), "my-bucket")
        self.assertEqual(r.get_meta("count").to_python(), 3)
        self.assertEqual(
            r.to_dict()["properties"]["bucket"], {"type": "literal", "value": "my-bucket"}
        )

    def test_resource_block_nested_and_dynamic_blocks(self) -> None:
        r = ResourceBlock(type="aws_security_group", name="web")
        nested = NestedBlock(type="lifecycle", attributes={"create_before_destroy": LiteralValue(True)})
        r.add_block(nested)
        dyn = DynamicBlock(label="ingress", iterator="port")
        dyn.set_for_each(ExpressionValue("var.ports", kind="traversal"))
        dyn.set_content("from_port", ExpressionValue("port.value", kind="traversal"))
        dyn.set_content("protocol", LiteralValue("tcp"))
        r.add_dynamic_block(dyn)

        self.assertEqual(len(r.find_blocks("lifecycle")), 1)
        self.assertEqual(r.dynamic_blocks[0].label, "ingress")
        # Round-trip the dict through the writer to confirm it produces TF.
        from parse_hcl import to_tf

        out = to_tf({"resource": [r.to_dict()]})
        self.assertIn('dynamic "ingress"', out)
        self.assertIn("lifecycle", out)

    def test_variable_block_recursive_default(self) -> None:
        """A variable's default may itself be a deeply nested object/array tree."""
        var = VariableBlock(name="config", type="object")
        var.set_default(
            ObjectValue(
                {
                    "name": LiteralValue("demo"),
                    "tags": ObjectValue({"env": LiteralValue("dev")}),
                    "ports": ArrayValue([80, 443]),
                }
            )
        )
        # Walk through children: 1 default (ObjectValue) + 3 entries +
        # the nested object (1 entry) + the array (2 entries).
        nested_object = var.default.get("tags")
        nested_object.set("owner", LiteralValue("team-a"))
        var.default.get("ports").append(8443)

        emitted = var.to_dict()
        self.assertEqual(
            emitted["default"]["value"]["tags"]["value"]["owner"],
            {"type": "literal", "value": "team-a"},
        )
        self.assertEqual(
            [v["value"] for v in emitted["default"]["value"]["ports"]["value"]],
            [80, 443, 8443],
        )

    def test_output_block_sets_value_and_sensitive(self) -> None:
        o = OutputBlock(name="endpoint")
        o.set_value(ExpressionValue("aws_lb.app.dns_name", kind="traversal"))
        o.set_sensitive(True)
        d = o.to_dict()
        self.assertEqual(d["value"]["raw"], "aws_lb.app.dns_name")
        self.assertEqual(d["sensitive"], True)

    def test_local_value_holds_arbitrary_value_tree(self) -> None:
        lv = LocalValue("common_tags", value=ObjectValue({"env": LiteralValue("dev")}))
        self.assertEqual(
            lv.to_dict()["value"],
            {"type": "object", "value": {"env": {"type": "literal", "value": "dev"}}},
        )


class TerraformModuleRoundTripTest(unittest.TestCase):
    """End-to-end: parse fixture → wrap → mutate → save → re-parse."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixtures = ROOT / "tests" / "fixtures"

    def test_main_fixture_round_trip_via_json_and_tf(self) -> None:
        src = self.fixtures / "main.tf"
        module = TerraformModule.from_tf_file(src)
        self.assertEqual(len(module.resource), 1)
        self.assertEqual(module.resource[0].type, "aws_s3_bucket")

        with tempfile.TemporaryDirectory() as td:
            json_out = Path(td) / "main.json"
            tf_out = Path(td) / "main.tf"
            module.save_json(json_out)
            module.save_tf(tf_out)

            self.assertTrue(json_out.exists())
            self.assertTrue(tf_out.exists())

            # Re-load from JSON and from TF; both must structurally match the
            # original document (modulo source paths and `raw` echoes).
            from_json = TerraformModule.from_json_file(json_out)
            from_tf = TerraformModule.from_tf_file(tf_out)

            original = TerraformParser().parse_file(str(src))
            self.maxDiff = None
            self.assertEqual(_normalize(dict(original)), _normalize(from_json.to_dict()))
            self.assertEqual(_normalize(dict(original)), _normalize(from_tf.to_dict()))

    def test_advanced_fixture_round_trip(self) -> None:
        """The advanced fixture exercises every block kind we model."""
        src = self.fixtures / "advanced.tf"
        module = TerraformModule.from_tf_file(src)
        # Verify each section was hydrated to the expected typed class.
        self.assertTrue(all(isinstance(b, TerraformSettingsBlock) for b in module.terraform))
        self.assertTrue(all(isinstance(b, ProviderBlock) for b in module.provider))
        self.assertTrue(all(isinstance(b, VariableBlock) for b in module.variable))
        self.assertTrue(all(isinstance(b, OutputBlock) for b in module.output))
        self.assertTrue(all(isinstance(b, ModuleCallBlock) for b in module.module))
        self.assertTrue(all(isinstance(b, ResourceBlock) for b in module.resource))
        self.assertTrue(all(isinstance(b, DataBlock) for b in module.data))
        self.assertTrue(all(isinstance(b, LocalValue) for b in module.locals))

        with tempfile.TemporaryDirectory() as td:
            tf_out = Path(td) / "out.tf"
            module.save_tf(tf_out)
            re_parsed = TerraformParser().parse_file(str(tf_out))
            self.maxDiff = None
            self.assertEqual(_normalize(dict(re_parsed)), _normalize(module.to_dict()))

    def test_mutations_reflect_in_emitted_tf(self) -> None:
        module = TerraformModule.from_tf_file(self.fixtures / "main.tf")
        bucket = module.find_resource("aws_s3_bucket", "demo")
        self.assertIsNotNone(bucket)
        bucket.set_property("bucket", LiteralValue("renamed-bucket"))
        bucket.set_meta("count", LiteralValue(5))

        var = module.find_variable("region")
        var.set_default("eu-west-1")
        var.set_description("Updated region")

        local = module.find_local("name_prefix")
        local.set_value("prod")

        emitted = module.to_tf()
        self.assertIn('bucket = "renamed-bucket"', emitted)
        self.assertIn("count = 5", emitted)
        self.assertIn('default = "eu-west-1"', emitted)
        self.assertIn('description = "Updated region"', emitted)
        self.assertIn('name_prefix = "prod"', emitted)

    def test_save_json_then_reload_then_save_tf(self) -> None:
        """The JSON saved by the OOP layer is itself a valid input for TerraformModule."""
        module = TerraformModule.from_tf_file(self.fixtures / "main.tf")
        with tempfile.TemporaryDirectory() as td:
            json_path = Path(td) / "out.json"
            tf_path = Path(td) / "out.tf"
            module.save_json(json_path, prune=True)

            payload = json.loads(json_path.read_text(encoding="utf-8"))
            # Block-level ``raw`` echoes are dropped (the OOP layer rebuilds TF
            # from structured fields). Expression values still carry ``raw``
            # because the writer needs the source text to re-emit them.
            for section in ("terraform", "provider", "variable", "resource", "output"):
                for block in payload.get(section, []):
                    self.assertNotIn("raw", block)

            reloaded = TerraformModule.from_json_file(json_path)
            reloaded.save_tf(tf_path)
            self.assertIn("resource", tf_path.read_text(encoding="utf-8"))

    def test_add_and_remove_blocks(self) -> None:
        module = TerraformModule()
        module.add_resource(
            ResourceBlock(
                type="null_resource",
                name="trigger",
                properties={"triggers": ObjectValue({"ts": LiteralValue("now")})},
            )
        )
        module.add_variable(VariableBlock(name="env", type="string", default=LiteralValue("dev")))
        module.add_output(OutputBlock(name="env_out", value=ExpressionValue("var.env", kind="traversal")))

        self.assertEqual(len(module.resource), 1)
        self.assertEqual(len(module.variable), 1)
        self.assertEqual(len(module.output), 1)

        removed = module.remove_resource("null_resource", "trigger")
        self.assertIsNotNone(removed)
        self.assertEqual(len(module.resource), 0)

        # Synthesizing a brand-new module to TF should still produce valid HCL
        # for the remaining blocks.
        out = module.to_tf()
        self.assertIn('variable "env"', out)
        self.assertIn('output "env_out"', out)

    def test_walk_yields_full_tree(self) -> None:
        module = TerraformModule.from_tf_file(self.fixtures / "advanced.tf")
        nodes = list(module.walk())
        self.assertGreater(len(nodes), 50)
        # Every walked node must inherit from TerraformElement.
        from parse_hcl.oop import TerraformElement

        for node in nodes:
            self.assertIsInstance(node, TerraformElement)


class ParentLinkTest(unittest.TestCase):
    """Container classes set ``parent`` on adopted children."""

    def test_object_value_sets_parent_on_entries(self) -> None:
        parent = ObjectValue()
        child = LiteralValue("x")
        parent.set("k", child)
        self.assertIs(child.parent, parent)

    def test_resource_block_sets_parent_on_properties(self) -> None:
        block = ResourceBlock(type="t", name="n")
        v = LiteralValue(1)
        block.set_property("k", v)
        self.assertIs(v.parent, block)

    def test_module_sets_parent_on_blocks(self) -> None:
        module = TerraformModule()
        block = ResourceBlock(type="t", name="n")
        module.add_resource(block)
        self.assertIs(block.parent, module)


if __name__ == "__main__":
    unittest.main()
