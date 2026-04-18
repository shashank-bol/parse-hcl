"""
Value classifier for HCL expressions.

Classifies raw value strings into typed Value structures and extracts references.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any, Dict, List, Optional

from ..lexer.hcl_lexer import is_escaped, is_quote, split_array_elements, split_object_entries
from ...types import ExpressionKind, ReferenceDict, Value

FUNCTION_CALL_NAME_PATTERN = re.compile(r"^([\w:.-]+)\s*\(")
"""Pattern for capturing the callee name of a function call expression.

Supports dotted names (``foo.bar``) and namespaced names (``provider::aws::arn_parse``).
"""

TRAVERSAL_PATTERN = re.compile(r"[A-Za-z_][\w-]*(?:\[(?:[^[\]]*|\*)])?(?:\.[A-Za-z_][\w-]*(?:\[(?:[^[\]]*|\*)])?)+")
"""Pattern for matching traversal expressions (e.g., aws_instance.web.id)."""

SPLAT_PATTERN = re.compile(r"\[\*]")
"""Pattern for matching splat expressions (e.g., aws_instance.web[*].id)."""

# Set by TerraformParser.parse_file when verbose > 0; reset in finally.
_reference_verbose: int = 0


def set_reference_logging(verbose: int) -> None:
    """
    Enable reference-extraction logging to stderr (``[references:verbose]`` / ``trace``).

    Called by ``TerraformParser`` when ``-v`` / ``-vv`` is used. ``0`` disables.
    """
    global _reference_verbose
    _reference_verbose = max(0, int(verbose))


def _ref_log(message: str) -> None:
    if _reference_verbose >= 1:
        print("[references:verbose]", message, file=sys.stderr)


def _ref_trace(message: str) -> None:
    if _reference_verbose >= 2:
        print("[references:trace]", message, file=sys.stderr)


def _preview_text(s: str, max_len: int = 160) -> str:
    s = s.replace("\n", "\\n")
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."


def classify_value(raw: str) -> Value:
    """
    Classifies a raw HCL value string into a typed Value structure.

    Supports literals, quoted strings, arrays, objects, and expressions.

    Args:
        raw: The raw value string to classify.

    Returns:
        The classified Value with type information and extracted references.

    Example:
        >>> classify_value('true')
        {'type': 'literal', 'value': True, 'raw': 'true'}

        >>> classify_value('"hello"')
        {'type': 'literal', 'value': 'hello', 'raw': '"hello"'}

        >>> classify_value('var.region')
        {'type': 'expression', 'kind': 'traversal', 'raw': 'var.region', ...}

        >>> classify_value('[1, 2, 3]')
        {'type': 'array', 'value': [...], 'raw': '[1, 2, 3]'}
    """
    trimmed = raw.strip()

    literal = _classify_literal(trimmed)
    if literal:
        return literal

    if _is_quoted_string(trimmed):
        inner = _unquote(trimmed)
        if "${" in inner:
            return _classify_expression(inner, "template")
        return {"type": "literal", "value": inner, "raw": trimmed}

    if trimmed.startswith("<<"):
        return _classify_expression(trimmed, "template")

    if trimmed.startswith("[") and trimmed.endswith("]"):
        return _classify_array(trimmed)

    if trimmed.startswith("{") and trimmed.endswith("}"):
        return _classify_object(trimmed)

    return _classify_expression(trimmed)


def _classify_literal(raw: str) -> Optional[Value]:
    """
    Classifies a raw value as a literal (boolean, number, or null).

    Args:
        raw: The trimmed raw value.

    Returns:
        LiteralValue if it's a literal, None otherwise.
    """
    if raw in ("true", "false"):
        return {"type": "literal", "value": raw == "true", "raw": raw}

    if re.match(r"^-?\d+(\.\d+)?([eE][+-]?\d+)?$", raw):
        return {"type": "literal", "value": float(raw) if "." in raw or "e" in raw or "E" in raw else int(raw), "raw": raw}

    if raw == "null":
        return {"type": "literal", "value": None, "raw": raw}

    return None


def _classify_array(raw: str) -> Value:
    """
    Classifies and parses an array value with recursive element parsing.

    Args:
        raw: The raw array string including brackets.

    Returns:
        ArrayValue with parsed elements and extracted references.
    """
    elements = split_array_elements(raw)
    parsed_elements = [classify_value(elem) for elem in elements]
    references = _collect_references(parsed_elements)

    return {
        "type": "array",
        "value": parsed_elements or None,
        "raw": raw,
        "references": references or None,
    }


def _classify_object(raw: str) -> Value:
    """
    Classifies and parses an object value with recursive entry parsing.

    Args:
        raw: The raw object string including braces.

    Returns:
        ObjectValue with parsed entries and extracted references.
    """
    entries = split_object_entries(raw)
    parsed_entries: Dict[str, Value] = {key: classify_value(value) for key, value in entries}
    references = _collect_references(list(parsed_entries.values()))

    return {
        "type": "object",
        "value": parsed_entries or None,
        "raw": raw,
        "references": references or None,
    }


def _collect_references(values: List[Value]) -> List[ReferenceDict]:
    """
    Collects all references from an array of values.

    Args:
        values: Array of Value objects.

    Returns:
        Deduplicated array of references.
    """
    refs: List[ReferenceDict] = []

    for value in values:
        if value.get("references"):
            refs.extend(value["references"])  # type: ignore[arg-type]

        if value["type"] == "array" and isinstance(value.get("value"), list):
            refs.extend(_collect_references(value["value"]))  # type: ignore[arg-type]
        if value["type"] == "object" and isinstance(value.get("value"), dict):
            refs.extend(_collect_references(list(value["value"].values())))  # type: ignore[arg-type]

    return _unique_references(refs)


def _classify_expression(raw: str, forced_kind: Optional[ExpressionKind] = None) -> Value:
    """
    Classifies an expression and extracts its references.

    For ``function_call`` expressions, the callee name and recursively-classified
    arguments are attached as ``name`` and ``attributes`` so downstream consumers
    can introspect each argument (strings, lists, maps, nested calls) without
    re-parsing the raw text.

    Args:
        raw: The raw expression string.
        forced_kind: Optional forced expression kind.

    Returns:
        ExpressionValue with kind and references.
    """
    kind = forced_kind or _detect_expression_kind(raw)
    references = _extract_expression_references(raw, kind)
    value: Value = {"type": "expression", "kind": kind, "raw": raw, "references": references or None}

    if kind == "function_call":
        parsed = _parse_function_call(raw)
        if parsed is not None:
            value["name"] = parsed["name"]  # type: ignore[typeddict-unknown-key]
            value["attributes"] = parsed["attributes"]  # type: ignore[typeddict-unknown-key]
            if parsed.get("trailer"):
                value["trailer"] = parsed["trailer"]  # type: ignore[typeddict-unknown-key]

    return value


def _parse_function_call(raw: str) -> Optional[Dict[str, Any]]:
    """
    Parses a function call expression into a callee name, classified arguments,
    and an optional trailing accessor chain (``[i]``, ``["k"]``, ``.attr``).

    Splits the argument list on top-level commas, respecting strings, brackets,
    braces, and nested parentheses (so nested function calls stay intact), then
    recursively classifies each argument via :func:`classify_value`.

    HCL permits chained access on a function-call result, e.g.
    ``jsondecode(local.x)[0]["environment"]``. That suffix is preserved as
    ``trailer`` (raw text, including leading ``[`` / ``.``) so serializers can
    reconstruct it verbatim while still using the structured ``name`` +
    ``attributes`` for the call itself.

    Args:
        raw: Raw expression text (e.g. ``func("a", [1, 2], nested(x))``).

    Returns:
        A dict with ``name`` (str), ``attributes`` (List[Value]), and optionally
        ``trailer`` (str), or ``None`` if the expression is not a well-formed
        function call.

    Example:
        >>> result = _parse_function_call('concat(["a"], var.other)')
        >>> result["name"]
        'concat'
        >>> len(result["attributes"])
        2
        >>> _parse_function_call('jsondecode(local.x)[0]["environment"]')["trailer"]
        '[0]["environment"]'
    """
    match = FUNCTION_CALL_NAME_PATTERN.match(raw)
    if not match:
        _ref_trace(f"function_call parse: no callee match in {_preview_text(raw)!r}")
        return None

    name = match.group(1)
    open_paren = raw.find("(", match.end() - 1)
    close_paren = _find_matching_paren(raw, open_paren)
    if open_paren < 0 or close_paren < 0:
        _ref_trace(f"function_call parse: unbalanced parens in {_preview_text(raw)!r}")
        return None

    inner = raw[open_paren + 1 : close_paren]
    arg_chunks = _split_function_call_args(inner)
    trailer = raw[close_paren + 1 :].strip()
    _ref_trace(
        f"function_call name={name!r} arg_chunks({len(arg_chunks)})={arg_chunks!r} trailer={trailer!r}"
    )

    attributes: List[Value] = [classify_value(chunk) for chunk in arg_chunks]
    result: Dict[str, Any] = {"name": name, "attributes": attributes}
    if trailer:
        result["trailer"] = trailer
    return result


def _find_matching_paren(text: str, open_index: int) -> int:
    """
    Returns the index of the ``)`` matching the ``(`` at ``open_index``.

    Respects string literals and nested brackets. Returns ``-1`` if unbalanced
    or if ``open_index`` does not point at a ``(``.
    """
    if open_index < 0 or open_index >= len(text) or text[open_index] != "(":
        return -1

    depth = 0
    in_string = False
    string_char: Optional[str] = None

    for i in range(open_index, len(text)):
        char = text[i]
        if in_string:
            if char == string_char and not is_escaped(text, i):
                in_string = False
                string_char = None
            continue
        if is_quote(char):
            in_string = True
            string_char = char
            continue
        if char in "([{":
            depth += 1
            continue
        if char in ")]}":
            depth -= 1
            if depth == 0:
                return i if char == ")" else -1
    return -1


def _split_function_call_args(inner: str) -> List[str]:
    """
    Splits a function-call argument list on top-level commas.

    Respects string literals (single/double quoted), arrays (``[]``), objects
    (``{}``), and nested parenthesized expressions / function calls. Strips and
    drops empty chunks (so trailing commas are tolerated).

    Args:
        inner: Text between the opening ``(`` and closing ``)`` (exclusive).

    Returns:
        List of raw argument strings in order.

    Example:
        >>> _split_function_call_args('"a", [1, 2], {x = 1}, nested(a, b)')
        ['"a"', '[1, 2]', '{x = 1}', 'nested(a, b)']
    """
    if not inner.strip():
        return []

    chunks: List[str] = []
    current: List[str] = []
    depth = 0
    in_string = False
    string_char: Optional[str] = None

    for i, char in enumerate(inner):
        if in_string:
            current.append(char)
            if char == string_char and not is_escaped(inner, i):
                in_string = False
                string_char = None
            continue

        if is_quote(char):
            in_string = True
            string_char = char
            current.append(char)
            continue

        if char in "([{":
            depth += 1
            current.append(char)
            continue

        if char in ")]}":
            depth -= 1
            current.append(char)
            continue

        if char == "," and depth == 0:
            chunk = "".join(current).strip()
            if chunk:
                chunks.append(chunk)
            current = []
            continue

        current.append(char)

    tail = "".join(current).strip()
    if tail:
        chunks.append(tail)

    return chunks


def _detect_expression_kind(raw: str) -> ExpressionKind:
    """
    Detects the kind of an expression based on its syntax.

    Grouping parens (``(expr)``) are peeled off for classification so that
    paren-wrapped expressions like ``(cond ? a : b)`` are recognized as
    ``conditional`` rather than falling through to the ``${`` template fallback
    (which would cause the emitter to re-quote the whole thing as a string).

    ``conditional`` is checked before the ``${`` template fallback because
    templates only appear inside quoted strings / heredocs at the top level;
    callers have already routed those via ``classify_value`` before we get here,
    so any ``${`` seen now is an interpolation embedded inside a string literal
    that is part of a larger expression.

    Args:
        raw: The raw expression string.

    Returns:
        The detected ExpressionKind.
    """
    probe = _strip_outer_parens(raw)

    if FUNCTION_CALL_NAME_PATTERN.match(probe):
        return "function_call"
    if _has_conditional_operator(probe):
        return "conditional"
    if re.match(r"^\[\s*for\s+.+\s+in\s+.+:\s+", probe) or re.match(r"^\{\s*for\s+.+\s+in\s+.+:\s+", probe):
        return "for_expr"
    if SPLAT_PATTERN.search(probe):
        return "splat"
    if re.match(r"^[\w.-]+(\[[^\]]*])?$", probe):
        return "traversal"
    if "${" in probe:
        return "template"
    return "unknown"


def _strip_outer_parens(raw: str) -> str:
    """
    Return ``raw`` with a single pair of balanced outer parentheses removed.

    Returns the original ``raw`` (after ``strip``) if there is no fully-enclosing
    pair (e.g. ``(a) + (b)`` — the leading ``(`` closes before end-of-string).
    """
    s = raw.strip()
    if len(s) < 2 or s[0] != "(" or s[-1] != ")":
        return raw
    close = _find_matching_paren(s, 0)
    if close != len(s) - 1:
        return raw
    return s[1:-1].strip()


def _has_conditional_operator(raw: str) -> bool:
    """
    Checks if an expression contains a conditional (ternary) operator.

    Handles nested expressions and strings correctly.

    Args:
        raw: The raw expression string.

    Returns:
        True if the expression is a conditional.
    """
    depth = 0
    in_string = False
    string_char: Optional[str] = None
    question_found = False
    question_depth = -1

    for i, char in enumerate(raw):
        if not in_string:
            if char in ('"', "'"):
                in_string = True
                string_char = char
                continue
            if char in "([{":
                depth += 1
                continue
            if char in ")]}":
                depth -= 1
                continue
            if char == "?" and depth == 0:
                question_found = True
                question_depth = depth
                continue
            if char == ":" and question_found and depth == question_depth:
                return True
        else:
            if char == string_char and not is_escaped(raw, i):
                in_string = False
                string_char = None
    return False


def _extract_expression_references(raw: str, kind: ExpressionKind) -> List[ReferenceDict]:
    """
    Extracts references from an expression.

    Args:
        raw: The raw expression string.
        kind: The expression kind.

    Returns:
        Array of extracted references.
    """
    _ref_log(f"expression kind={kind!r} raw={_preview_text(raw)!r}")
    base_refs = _extract_references_from_text(raw, context="expression")

    if kind == "template":
        matches = re.findall(r"\${([^}]+)}", raw)
        _ref_trace(f"template interpolations count={len(matches)}: {matches!r}")
        inner_refs: List[ReferenceDict] = []
        for expr in matches:
            inner_refs.extend(_extract_references_from_text(expr, context=f"template(${expr[:40]}...)"))
        merged = _unique_references(base_refs + inner_refs)
        _ref_log(f"expression -> {len(merged)} ref(s): {json.dumps(merged, default=str)}")
        return merged

    _ref_log(f"expression -> {len(base_refs)} ref(s): {json.dumps(base_refs, default=str)}")
    return base_refs


def _extract_references_from_text(raw: str, *, context: str = "text") -> List[ReferenceDict]:
    """
    Extracts all references from a text string.

    Supports: var.*, local.*, module.*, data.*, resource references,
    path.*, each.*, count.*, self.*

    Args:
        raw: The raw text to extract references from.
        context: Label for verbose logging (caller context).

    Returns:
        Array of extracted references.
    """
    _ref_trace(f"extract_refs context={context!r} raw={_preview_text(raw)!r}")
    refs: List[ReferenceDict] = []
    special = _extract_special_references(raw)
    if special:
        _ref_trace(f"  special refs: {json.dumps(special, default=str)}")
    refs.extend(special)

    trav_matches = TRAVERSAL_PATTERN.findall(raw)
    _ref_trace(f"  traversal pattern matches ({len(trav_matches)}): {trav_matches!r}")

    for match in trav_matches:
        has_splat = "[*]" in match
        parts = [re.sub(r"\[.*?]", "", part) for part in match.split(".")]
        _ref_trace(f"  match={match!r} parts={parts!r} splat={has_splat}")

        if parts[0] == "var" and len(parts) > 1:
            refs.append({"kind": "variable", "name": parts[1]})
            _ref_trace("    -> variable")
            continue

        if parts[0] == "local" and len(parts) > 1:
            refs.append({"kind": "local", "name": parts[1]})
            _ref_trace("    -> local")
            continue

        if parts[0] == "module" and len(parts) > 1:
            attribute = ".".join(parts[2:]) or parts[1]
            refs.append({"kind": "module_output", "module": parts[1], "name": attribute})
            _ref_trace("    -> module_output")
            continue

        if parts[0] == "data" and len(parts) > 2:
            attribute = ".".join(parts[3:]) or None
            ref: ReferenceDict = {
                "kind": "data",
                "data_type": parts[1],
                "name": parts[2],
                "attribute": attribute,
            }
            if has_splat:
                ref["splat"] = True
            refs.append(ref)
            _ref_trace("    -> data")
            continue

        if parts[0] == "path" and len(parts) > 1:
            refs.append({"kind": "path", "name": parts[1]})
            _ref_trace("    -> path")
            continue

        if parts[0] in ("each", "count", "self"):
            _ref_trace(f"    -> skip reserved prefix {parts[0]!r}")
            continue

        if len(parts) >= 2:
            attribute = ".".join(parts[2:]) or None
            ref: ReferenceDict = {
                "kind": "resource",
                "resource_type": parts[0],
                "name": parts[1],
                "attribute": attribute,
            }
            if has_splat:
                ref["splat"] = True
            refs.append(ref)
            _ref_trace("    -> resource")

    return _unique_references(refs)


def _extract_special_references(raw: str) -> List[ReferenceDict]:
    """
    Extracts special references: each.key, each.value, count.index, self.*

    Args:
        raw: The raw text to extract from.

    Returns:
        Array of special references.
    """
    refs: List[ReferenceDict] = []
    for match in re.findall(r"\beach\.(key|value)\b", raw):
        refs.append({"kind": "each", "property": match})  # type: ignore[typeddict-item]
        _ref_trace(f"  special each.{match}")
    if re.search(r"\bcount\.index\b", raw):
        refs.append({"kind": "count", "property": "index"})  # type: ignore[typeddict-item]
        _ref_trace("  special count.index")
    for match in re.findall(r"\bself\.([\w-]+)", raw):
        refs.append({"kind": "self", "attribute": match})
        _ref_trace(f"  special self.{match}")
    return refs


def _unique_references(refs: List[ReferenceDict]) -> List[ReferenceDict]:
    """
    Removes duplicate references based on their JSON representation.

    Args:
        refs: Array of references (may contain duplicates).

    Returns:
        Deduplicated array of references.
    """
    seen = set()
    unique: List[ReferenceDict] = []
    for ref in refs:
        key = json.dumps(ref, sort_keys=True)
        if key in seen:
            _ref_trace(f"  dedupe drop {key}")
            continue
        seen.add(key)
        unique.append(ref)
    return unique


def _is_quoted_string(value: str) -> bool:
    """
    Checks if a value is a quoted string (single or double quotes).

    Args:
        value: The value to check.

    Returns:
        True if the value is a quoted string.
    """
    return (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'"))


def _unquote(value: str) -> str:
    """
    Removes quotes from a quoted string and handles escape sequences.

    Args:
        value: The quoted string.

    Returns:
        The unquoted string with escape sequences processed.
    """
    quote = value[0]
    inner = value[1:-1]
    result = []
    i = 0
    while i < len(inner):
        if inner[i] == "\\" and i + 1 < len(inner):
            nxt = inner[i + 1]
            if nxt == "n":
                result.append("\n")
                i += 2
                continue
            if nxt == "t":
                result.append("\t")
                i += 2
                continue
            if nxt == "r":
                result.append("\r")
                i += 2
                continue
            if nxt == "\\":
                result.append("\\")
                i += 2
                continue
            if nxt == quote:
                result.append(quote)
                i += 2
                continue
        result.append(inner[i])
        i += 1
    return "".join(result)


__all__ = [
    "classify_value",
    "set_reference_logging",
]
