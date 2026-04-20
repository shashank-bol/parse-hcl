"""
``parse_hcl.oop``: object-oriented Terraform model.

This subpackage layers a tree of typed Python classes on top of the dict-based
parser output. Use it when you want to:

- Load a ``.tf`` (or its parser JSON) into navigable Python objects.
- Update individual blocks / attributes / nested values via class methods.
- Serialize the result back to JSON and/or regenerated ``.tf``.

Round-trip example:

::

    from parse_hcl.oop import TerraformModule, LiteralValue

    module = TerraformModule.from_tf_file("main.tf")

    bucket = module.find_resource("aws_s3_bucket", "demo")
    bucket.set_property("bucket", LiteralValue("renamed-bucket"))
    bucket.set_meta("count", LiteralValue(3))

    module.save_json("out/main.json")
    module.save_tf("out/main.tf")

The ``raw`` field carried by parser dicts is intentionally dropped on
serialization; ``.tf`` is rebuilt from structured fields.
"""

from .base import TerraformElement, drop_raw, prune_empty
from .blocks import (
    AttributeBlock,
    Block,
    DataBlock,
    DynamicBlock,
    GenericBlock,
    LocalValue,
    ModuleCallBlock,
    NestedBlock,
    OutputBlock,
    ProviderBlock,
    ResourceBlock,
    TerraformSettingsBlock,
    VariableBlock,
)
from .module import TerraformModule
from .values import (
    ArrayValue,
    ExpressionValue,
    LiteralValue,
    ObjectValue,
    PrimitiveValue,
    Value,
    coerce_value,
)

__all__ = [
    # Base
    "TerraformElement",
    "drop_raw",
    "prune_empty",
    # Values
    "Value",
    "LiteralValue",
    "ObjectValue",
    "ArrayValue",
    "ExpressionValue",
    "PrimitiveValue",
    "coerce_value",
    # Blocks
    "Block",
    "AttributeBlock",
    "TerraformSettingsBlock",
    "ProviderBlock",
    "ModuleCallBlock",
    "ResourceBlock",
    "DataBlock",
    "NestedBlock",
    "DynamicBlock",
    "VariableBlock",
    "OutputBlock",
    "LocalValue",
    "GenericBlock",
    # Module
    "TerraformModule",
]
