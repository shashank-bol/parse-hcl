# `parse_hcl.oop` — OOP Terraform Update API

An object-oriented layer on top of `parse_hcl` for **updating** Terraform
configurations programmatically. The pipeline is:

```
.tf  ──parse──▶  dict (TerraformDocument)
                     │
                     │  TerraformModule.from_dict / from_tf_file / from_json_file
                     ▼
            TerraformModule (root)
                ├── terraform: List[TerraformSettingsBlock]
                ├── provider:  List[ProviderBlock]
                ├── variable:  List[VariableBlock]
                ├── output:    List[OutputBlock]
                ├── module:    List[ModuleCallBlock]
                ├── resource:  List[ResourceBlock]
                ├── data:      List[DataBlock]
                ├── locals:    List[LocalValue]
                └── generic:   {moved | import | check | terraform_data | unknown}
```

Every node descends from `TerraformElement` and exposes:

- `to_dict()` — serialize back to the parser dict shape (no `raw`).
- `children()` / `walk()` — depth-first traversal.
- `parent` — back-pointer set automatically by container classes.

## Class Hierarchy

```
TerraformElement (abstract)
├── Block (abstract)
│   ├── AttributeBlock         (carries a `properties` map)
│   │   ├── TerraformSettingsBlock
│   │   ├── ProviderBlock
│   │   ├── ModuleCallBlock
│   │   ├── ResourceBlock      (+ meta, blocks, dynamic_blocks)
│   │   ├── DataBlock          (+ blocks)
│   │   ├── NestedBlock        (block inside a resource/data body)
│   │   └── GenericBlock       (moved / import / check / ...)
│   ├── DynamicBlock           (HCL `dynamic "..." { ... }`)
│   ├── VariableBlock
│   ├── OutputBlock
│   └── LocalValue
└── Value (abstract)
    ├── LiteralValue           (string / number / bool / null)
    ├── ObjectValue            (recursive `{ key = value }`)
    ├── ArrayValue             (recursive `[ item, ... ]`)
    └── ExpressionValue        (traversal / template / function_call / ...)
```

`ObjectValue` and `ArrayValue` recursively own child `Value` instances, so a
deeply nested `map(object({...}))` default materializes as a navigable Python
tree.

## Quick Start

```python
from parse_hcl.oop import (
    TerraformModule,
    LiteralValue,
    ObjectValue,
    ArrayValue,
    ExpressionValue,
    ResourceBlock,
    VariableBlock,
)

module = TerraformModule.from_tf_file("infra/main.tf")

# Update a resource property
bucket = module.find_resource("aws_s3_bucket", "demo")
bucket.set_property("bucket", LiteralValue("renamed-bucket"))
bucket.set_meta("count", LiteralValue(3))

# Update a variable's default (recursively)
config = module.find_variable("instance_config")
config.default.get("tags").set("env", LiteralValue("prod"))
config.default.get("ports").append(8443)

# Add a brand-new resource
module.add_resource(
    ResourceBlock(
        type="null_resource",
        name="trigger",
        properties={"triggers": ObjectValue({"ts": LiteralValue("now")})},
    )
)

# Persist
module.save_json("out/main.json")     # JSON without `raw` echoes
module.save_tf("out/main.tf")         # Regenerated HCL
```

## Loading

| Source           | Method                                |
| ---------------- | ------------------------------------- |
| `.tf` file       | `TerraformModule.from_tf_file(path)`  |
| Directory of TFs | `TerraformModule.from_tf_directory(path)` |
| Parser dict      | `TerraformModule.from_dict(doc)`      |
| JSON string      | `TerraformModule.from_json(text)`     |
| JSON file        | `TerraformModule.from_json_file(path)`|

Both single parser documents and full export payloads
(`{"version", "document", "graph"}`) are accepted.

## Saving

| Target | Method |
| ------ | ------ |
| Dict   | `module.to_dict(prune=False)` |
| JSON   | `module.to_json()` / `module.save_json(path)` |
| HCL    | `module.to_tf()` / `module.save_tf(path)` |

`save_tf` runs `terraform fmt` on the written file when the `terraform` CLI is
on `PATH` (pass `run_terraform_fmt=False` to skip). `to_tf()` is unchanged and
does not invoke Terraform.

`raw` block fields are not emitted; the writer rebuilds HCL from structured
fields. Expression values keep `raw` because the writer needs the source text
to faithfully re-emit them.

## Updating Values

| Class            | Mutation API                                              |
| ---------------- | --------------------------------------------------------- |
| `LiteralValue`   | `.set(value)`                                             |
| `ObjectValue`    | `.set(key, value)`, `.get`, `.remove`, `.update`, `.keys` |
| `ArrayValue`     | `.append`, `.extend`, `.insert`, `.set_at`, `.remove_at` |
| `ExpressionValue`| `.set_expression(text, kind=None)`, `.add_argument`       |

Plain Python values passed to setters are auto-wrapped via `Value.from_dict`,
so `obj.set("count", 3)` and `obj.set("count", LiteralValue(3))` are
equivalent.

## Updating Blocks

All `AttributeBlock` subclasses (provider, resource, data, module, terraform,
generic, nested) share:

```python
block.get_property(key)
block.set_property(key, value)        # value may be Value, dict, or primitive
block.remove_property(key)
block.update_properties({...})
```

Plus block-specific helpers, e.g.:

- `ResourceBlock.get_meta / set_meta / add_block / remove_block /
  add_dynamic_block / find_blocks`
- `VariableBlock.set_default / set_type / set_description / set_validation`
- `OutputBlock.set_value / set_sensitive`
- `LocalValue.set_value`
- `DynamicBlock.set_for_each / set_iterator / set_content`

## Querying

```python
module.find_resource("aws_s3_bucket", "demo")
module.find_resources(type="aws_s3_bucket")   # all of one type, or all if omitted
module.find_variable("region")
module.find_output("bucket_name")
module.find_local("name_prefix")
module.find_module_call("vpc")
module.find_provider("aws", alias="west")
module.find_data("aws_ami", "ubuntu")
module.filter_blocks(lambda b: hasattr(b, "type") and b.type.startswith("aws_"))

for node in module.walk():            # full recursive traversal
    ...
```

## Notes

- The `raw` field carried by parser dicts is intentionally ignored when
  serializing back. Block-level `raw` is dropped entirely; expression
  `raw` is preserved because the writer needs it to re-emit the textual form.
- `source` (origin file path) is preserved through round-trips when known.
- `TerraformModule` is itself a `TerraformElement`, so the entire
  configuration is a single navigable tree.
