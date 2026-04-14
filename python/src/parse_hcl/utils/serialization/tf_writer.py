"""
Serialize parsed Terraform documents (dict / TerraformDocument) back to HCL (.tf) text.

By default, emission uses structured fields (block attributes, ``type`` / ``value`` on
values) so edits to the dict are reflected in output. Lexer ``raw`` strings are *not*
preferred over structured data except where unavoidable (expressions).

Pass ``prefer_raw=True`` to ``to_tf`` to paste per-block ``raw`` text when present (best
effort lossless round-trip of original spacing; may ignore structured edits).
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Mapping, Sequence

# Order matches merge order in terraform_parser.combine and typical Terraform style.
_DOCUMENT_SECTIONS: Sequence[str] = (
    "terraform",
    "provider",
    "variable",
    "locals",
    "module",
    "data",
    "resource",
    "output",
    "moved",
    "import",
    "check",
    "terraform_data",
    "unknown",
)

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


def to_tf(document: Any, *, prefer_raw: bool = False) -> str:
    """
    Convert a parsed Terraform document dictionary to HCL source text.

    If ``document`` is a full export payload (with ``version`` and ``document`` keys),
    the nested ``document`` is used.

    Args:
        document: TerraformDocument-like mapping, or an export dict containing
            ``document``.
        prefer_raw: When True, use per-block ``raw`` strings when present (original
            formatting; structured edits may be ignored). When False (default), blocks
            are built from structured fields. Value-level ``raw`` is never preferred over
            ``type`` / ``value`` except for expressions (see ``emit_value``).

    Returns:
        Terraform configuration as a string (suitable for a ``.tf`` file).

    Example:
        >>> from parse_hcl import TerraformParser, to_tf
        >>> doc = TerraformParser().parse_file("main.tf")
        >>> tf = to_tf(doc)
    """
    if isinstance(document, dict) and "document" in document and isinstance(document["document"], dict):
        doc = document["document"]
    else:
        doc = document

    if not isinstance(doc, Mapping):
        raise TypeError("document must be a mapping")

    parts: List[str] = []
    for key in _DOCUMENT_SECTIONS:
        if key not in doc:
            continue
        section = doc[key]
        if section is None:
            continue
        if key == "locals":
            text = _emit_locals_section(section)
        else:
            if not isinstance(section, list):
                continue
            text = _emit_block_list(key, section, prefer_raw=prefer_raw)
        if text:
            parts.append(text)

    return "\n\n".join(parts).rstrip() + ("\n" if parts else "")


def _emit_block_list(kind: str, blocks: List[Any], *, prefer_raw: bool) -> str:
    out: List[str] = []
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        emitted = _emit_typed_block(kind, block, prefer_raw=prefer_raw)
        if emitted:
            out.append(emitted)
    return "\n\n".join(out)


def _emit_typed_block(kind: str, block: Mapping[str, Any], *, prefer_raw: bool) -> str:
    if prefer_raw and isinstance(block.get("raw"), str) and block["raw"].strip():
        return block["raw"].strip()

    if kind == "terraform":
        return _emit_terraform_settings(block)
    if kind == "provider":
        return _emit_provider_block(block)
    if kind == "variable":
        return _emit_variable_block(block)
    if kind == "module":
        return _emit_module_block(block)
    if kind == "data":
        return _emit_data_block(block)
    if kind == "resource":
        return _emit_resource_block(block)
    if kind == "output":
        return _emit_output_block(block)
    if kind in ("moved", "import", "check", "terraform_data", "unknown"):
        return _emit_generic_block(block)
    return ""


def _emit_locals_section(locals_list: Any) -> str:
    if not isinstance(locals_list, list) or not locals_list:
        return ""
    body_lines: List[str] = []
    for item in locals_list:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        if not isinstance(name, str):
            continue
        key = name if _IDENT_RE.match(name) else _hcl_quote_identifier(name)
        val = item.get("value")
        body_lines.append(f"  {key} = {emit_value(val, _close_indent=2)}")

    if not body_lines:
        return ""

    inner = "\n".join(body_lines)
    return f"locals {{\n{inner}\n}}"


def _emit_terraform_settings(block: Mapping[str, Any]) -> str:
    props = block.get("properties")
    if not isinstance(props, dict):
        return "terraform {\n}"
    lines = ["terraform {"]
    for attr, val in props.items():
        lines.append(f"  {attr} = {emit_value(val, _close_indent=2)}")
    lines.append("}")
    return "\n".join(lines)


def _emit_provider_block(block: Mapping[str, Any]) -> str:
    name = block.get("name", "default")
    if not isinstance(name, str):
        name = "default"
    props = block.get("properties")
    if not isinstance(props, dict):
        props = {}
    header = _hcl_block_header("provider", [name])
    body = _emit_attribute_lines(props, indent=1)
    return _finish_block(header, body)


def _emit_module_block(block: Mapping[str, Any]) -> str:
    name = block.get("name", "unnamed")
    if not isinstance(name, str):
        name = "unnamed"
    props = block.get("properties")
    if not isinstance(props, dict):
        props = {}
    header = _hcl_block_header("module", [name])
    body = _emit_attribute_lines(props, indent=1)
    return _finish_block(header, body)


def _emit_variable_block(block: Mapping[str, Any]) -> str:
    name = block.get("name", "unknown")
    if not isinstance(name, str):
        name = "unknown"
    header = _hcl_block_header("variable", [name])
    lines: List[str] = []

    if block.get("description") is not None and isinstance(block["description"], str):
        lines.append(f'  description = {_hcl_quote_string(block["description"])}')
    if block.get("type") is not None and isinstance(block["type"], str):
        lines.append(f'  type = {block["type"]}')
    if "default" in block:
        lines.append(f"  default = {emit_value(block.get('default'), _close_indent=2)}")
    if block.get("sensitive") is not None:
        lines.append(f"  sensitive = {_format_bool(block['sensitive'])}")
    if block.get("nullable") is not None:
        lines.append(f"  nullable = {_format_bool(block['nullable'])}")

    validation = block.get("validation")
    if isinstance(validation, dict):
        vlines = ["  validation {"]
        if "condition" in validation:
            vlines.append(f'    condition = {emit_value(validation.get("condition"), _close_indent=4)}')
        if "error_message" in validation:
            vlines.append(f'    error_message = {emit_value(validation.get("error_message"), _close_indent=4)}')
        vlines.append("  }")
        if len(vlines) > 2:
            lines.extend(vlines)

    inner = "\n".join(lines) if lines else ""
    return _finish_block(header, inner)


def _emit_output_block(block: Mapping[str, Any]) -> str:
    name = block.get("name", "unknown")
    if not isinstance(name, str):
        name = "unknown"
    header = _hcl_block_header("output", [name])
    lines: List[str] = []
    if "value" in block:
        lines.append(f"  value = {emit_value(block.get('value'), _close_indent=2)}")
    if block.get("description") is not None and isinstance(block["description"], str):
        lines.append(f'  description = {_hcl_quote_string(block["description"])}')
    if block.get("sensitive") is not None:
        lines.append(f"  sensitive = {_format_bool(block['sensitive'])}")
    inner = "\n".join(lines)
    return _finish_block(header, inner)


def _emit_data_block(block: Mapping[str, Any]) -> str:
    dtype = block.get("dataType", "unknown")
    name = block.get("name", "unnamed")
    if not isinstance(dtype, str):
        dtype = "unknown"
    if not isinstance(name, str):
        name = "unnamed"
    header = _hcl_block_header("data", [dtype, name])
    props = block.get("properties")
    if not isinstance(props, dict):
        props = {}
    inner_parts = [_emit_attribute_lines(props, indent=1)]
    blocks = block.get("blocks")
    if isinstance(blocks, list) and blocks:
        inner_parts.append(_emit_nested_blocks(blocks, base_indent=1))
    inner = "\n".join(p for p in inner_parts if p)
    return _finish_block(header, inner)


def _emit_resource_block(block: Mapping[str, Any]) -> str:
    rtype = block.get("type", "unknown")
    name = block.get("name", "unnamed")
    if not isinstance(rtype, str):
        rtype = "unknown"
    if not isinstance(name, str):
        name = "unnamed"
    header = _hcl_block_header("resource", [rtype, name])
    meta = block.get("meta")
    if not isinstance(meta, dict):
        meta = {}
    props = block.get("properties")
    if not isinstance(props, dict):
        props = {}

    inner_parts: List[str] = []
    if meta:
        inner_parts.append(_emit_attribute_lines(meta, indent=1))
    if props:
        inner_parts.append(_emit_attribute_lines(props, indent=1))

    blocks = block.get("blocks")
    if isinstance(blocks, list) and blocks:
        inner_parts.append(_emit_nested_blocks(blocks, base_indent=1))

    dynamic_blocks = block.get("dynamic_blocks")
    if isinstance(dynamic_blocks, list) and dynamic_blocks:
        for db in dynamic_blocks:
            if isinstance(db, Mapping):
                inner_parts.append(_emit_dynamic_block(db, indent=1))

    inner = "\n".join(p for p in inner_parts if p)
    return _finish_block(header, inner)


def _emit_dynamic_block(db: Mapping[str, Any], *, indent: int) -> str:
    pad = "  " * indent
    label = db.get("label", "dynamic")
    if not isinstance(label, str):
        label = "dynamic"
    lines: List[str] = [f'{pad}dynamic "{label}" {{']
    if "for_each" in db:
        lines.append(f"{pad}  for_each = {emit_value(db.get('for_each'), _close_indent=len(pad) + 2)}")
    if db.get("iterator"):
        lines.append(f"{pad}  iterator = {db['iterator']}")
    content = db.get("content")
    if isinstance(content, dict) and content:
        lines.append(f"{pad}  content {{")
        lines.append(_emit_attribute_lines(content, indent=indent + 2))
        lines.append(f"{pad}  }}")
    lines.append(f"{pad}}}")
    return "\n".join(lines)


def _emit_nested_blocks(blocks: Sequence[Any], *, base_indent: int) -> str:
    parts: List[str] = []
    for nb in blocks:
        if isinstance(nb, Mapping):
            parts.append(_emit_nested_block(nb, base_indent=base_indent))
    return "\n".join(p for p in parts if p)


def _emit_nested_block(nb: Mapping[str, Any], *, base_indent: int) -> str:
    pad = "  " * base_indent
    btype = nb.get("type", "block")
    if not isinstance(btype, str):
        btype = "block"
    labels = nb.get("labels")
    if not isinstance(labels, list):
        labels = []
    lbls = [str(x) for x in labels]
    header = _hcl_block_header(btype, lbls)
    attrs = nb.get("attributes")
    if not isinstance(attrs, dict):
        attrs = {}
    inner_parts: List[str] = []
    if attrs:
        inner_parts.append(_emit_attribute_lines(attrs, indent=base_indent + 1))
    children = nb.get("blocks")
    if isinstance(children, list) and children:
        inner_parts.append(_emit_nested_blocks(children, base_indent=base_indent + 1))
    inner = "\n".join(p for p in inner_parts if p)
    lines = [f"{pad}{header} {{"]
    if inner:
        lines.extend(inner.splitlines())
    lines.append(f"{pad}}}")
    return "\n".join(lines)


def _emit_generic_block(block: Mapping[str, Any]) -> str:
    btype = block.get("type", "unknown")
    if not isinstance(btype, str):
        btype = "unknown"
    labels = block.get("labels")
    if not isinstance(labels, list):
        labels = []
    lbls = [str(x) for x in labels]
    header = _hcl_block_header(btype, lbls)
    props = block.get("properties")
    if not isinstance(props, dict):
        props = {}
    inner_parts: List[str] = []
    if props:
        inner_parts.append(_emit_attribute_lines(props, indent=1))
    blocks = block.get("blocks")
    if isinstance(blocks, list) and blocks:
        inner_parts.append(_emit_nested_blocks(blocks, base_indent=1))
    inner = "\n".join(p for p in inner_parts if p)
    return _finish_block(header, inner)


def _emit_attribute_lines(attrs: Mapping[str, Any], *, indent: int) -> str:
    pad = "  " * indent
    lines: List[str] = []
    for key, val in attrs.items():
        lhs = key if _IDENT_RE.match(str(key)) else _hcl_quote_identifier(str(key))
        lines.append(f"{pad}{lhs} = {emit_value(val, _close_indent=len(pad))}")
    return "\n".join(lines)


def _finish_block(header: str, inner: str) -> str:
    inner_stripped = inner.strip() if inner else ""
    if inner_stripped:
        return f"{header} {{\n{inner_stripped}\n}}"
    return f"{header} {{\n}}"


def _hcl_block_header(keyword: str, labels: Sequence[str]) -> str:
    parts: List[str] = [keyword]
    for lab in labels:
        parts.append(f'"{lab}"')
    return " ".join(parts)


def _format_bool(v: Any) -> str:
    return "true" if v else "false"


def _hcl_quote_string(s: str) -> str:
    j = json.dumps(s)
    return j if isinstance(j, str) else f'"{s}"'


def _hcl_quote_identifier(s: str) -> str:
    return json.dumps(s)


def _emit_expression_value(val: Any) -> str:
    """
    Emit ``type: expression`` values.

    Quoted interpolations in source HCL (``\"...${}...\"``) are classified as
    ``kind: template`` with ``raw`` equal to the *inner* string (no surrounding
    quotes). Those must be emitted as a double-quoted HCL string or Terraform
    rejects the result. Heredocs (``raw`` starting with ``<<``) are left as-is.
    """
    raw = val.get("raw") if isinstance(val, dict) else None
    if raw is None:
        return ""
    kind = val.get("kind") if isinstance(val, dict) else None
    raw_str = str(raw)
    if kind == "template":
        if raw_str.lstrip().startswith("<<"):
            return raw_str
        return _hcl_quote_string(raw_str)
    return raw_str


def emit_value(val: Any, *, _depth: int = 0, _close_indent: int = 0) -> str:
    """
    Emit an HCL expression or literal from a parse-hcl Value dict (or plain Python value).

    Synthesis follows structured ``type`` / ``value`` (and nested value dicts) first.
    For ``expression`` values, ``raw`` is used with kind-specific rules: ``template``
    (quoted-string interpolations) is re-quoted; other kinds use ``raw`` as HCL text.
    Non-expression types fall back to ``raw`` only when structured data is missing.

    Object/map values (``type: object``) are written as line-separated blocks
    ``{`` / ``}`` with one ``key = value`` per line (Terraform style), not comma-separated.

    ``_close_indent`` is the character width for the closing ``}`` of the current
    object body, used only when synthesizing multiline maps (internal / advanced use).
    """
    if _depth > 64:
        raise ValueError("Value nesting too deep for emit_value")

    if val is None:
        return "null"

    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        if isinstance(val, float) and val.is_integer():
            return str(int(val))
        return str(val)
    if isinstance(val, str):
        return _hcl_quote_string(val)

    if isinstance(val, dict):
        vtype = val.get("type")

        if vtype == "literal":
            if "value" in val:
                return _emit_literal_python(val.get("value"))
            if val.get("raw") is not None and str(val.get("raw")).strip() != "":
                return str(val["raw"])
            return ""

        if vtype == "array":
            elems = val.get("value")
            if not isinstance(elems, list):
                elems = [] if elems is None else []
            if elems or "value" in val:
                inner = ", ".join(
                    emit_value(x, _depth=_depth + 1, _close_indent=0) for x in elems
                )
                return f"[{inner}]"
            if val.get("raw") is not None and str(val.get("raw")).strip() != "":
                return str(val["raw"])
            return "[]"

        if vtype == "object":
            entries = val.get("value")
            if isinstance(entries, dict) and entries:
                return _emit_hcl_object(entries, _depth=_depth + 1, _close_indent=_close_indent)
            if isinstance(entries, dict) and not entries:
                return "{}"
            if val.get("raw") is not None and str(val.get("raw")).strip() != "":
                return str(val["raw"])
            return "{}"

        if vtype == "expression":
            return _emit_expression_value(val)

        if val.get("raw") is not None and str(val.get("raw")).strip() != "":
            return str(val["raw"])
        return str(val.get("value", ""))

    if isinstance(val, list):
        inner = ", ".join(emit_value(x, _depth=_depth + 1, _close_indent=0) for x in val)
        return f"[{inner}]"

    return str(val)


def _emit_literal_python(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    if isinstance(value, str):
        return _hcl_quote_string(value)
    return str(value)


def _emit_hcl_object(
    entries: Mapping[str, Any],
    *,
    _depth: int,
    _close_indent: int,
) -> str:
    """Emit a Terraform object/map as a line-separated block (not comma-separated)."""
    if not entries:
        return "{}"
    key_indent = _close_indent + 2
    key_prefix = " " * key_indent
    close_prefix = " " * _close_indent
    lines: List[str] = []
    for k, v in entries.items():
        key = str(k)
        lhs = key if _IDENT_RE.match(key) else _hcl_quote_identifier(key)
        rhs = emit_value(v, _depth=_depth + 1, _close_indent=key_indent)
        lines.append(f"{key_prefix}{lhs} = {rhs}")
    inner = "\n".join(lines)
    return "{\n" + inner + "\n" + close_prefix + "}"


__all__ = ["to_tf", "emit_value"]
