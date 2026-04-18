"""
Block scanner for HCL source code.

Scans HCL content and extracts top-level blocks with their metadata,
including block type, labels, body content, and source information.
"""

from __future__ import annotations

import re
import sys
from typing import List

from ..common.errors import ParseError, offset_to_location
from ..common.logger import warn
from ...types import BlockKind, HclBlock
from .hcl_lexer import (
    find_matching_brace,
    is_quote,
    read_identifier,
    read_quoted_string,
    skip_string,
    skip_whitespace_and_comments,
)

KNOWN_BLOCKS = {
    "terraform",
    "locals",
    "provider",
    "variable",
    "output",
    "module",
    "resource",
    "data",
    "moved",
    "import",
    "check",
    "terraform_data",
}
"""Set of known Terraform block types."""


def _scan_log(verbose: int, message: str) -> None:
    if verbose >= 1:
        print("[scanner:verbose]", message, file=sys.stderr)


def _scan_trace(verbose: int, message: str) -> None:
    if verbose >= 2:
        print("[scanner:trace]", message, file=sys.stderr)


class BlockScanner:
    """
    Scanner for extracting HCL blocks from source content.

    Parses HCL source and returns a list of blocks with their type,
    labels, body content, and source location information.

    Example:
        >>> scanner = BlockScanner()
        >>> blocks = scanner.scan('resource "aws_instance" "main" {}', 'main.tf')
        >>> print(blocks[0]['kind'])
        'resource'
    """

    def scan(self, content: str, source: str, strict: bool = False, *, verbose: int = 0) -> List[HclBlock]:
        """
        Scans HCL content and extracts all top-level blocks.

        Args:
            content: The HCL source content to scan.
            source: The source file path (for error messages).
            strict: If True, raises ParseError on syntax errors.
                   If False, logs warnings and continues.
            verbose: If ``>= 1``, log each token (keyword, labels, ``{``, body span) for
                every top-level block to stderr as ``[scanner:verbose]``. If ``>= 2``,
                also log byte offsets and skipped regions as ``[scanner:trace]``.

        Returns:
            List of HclBlock dictionaries containing:
            - kind: The block type ('resource', 'variable', etc.)
            - keyword: The original keyword as written
            - labels: List of quoted string labels
            - body: The block body content (between braces)
            - raw: The normalized raw block text
            - source: The source file path

        Raises:
            ParseError: If strict=True and a syntax error is encountered.

        Example:
            >>> scanner = BlockScanner()
            >>> content = '''
            ... variable "name" {
            ...   type = string
            ... }
            ... '''
            >>> blocks = scanner.scan(content, 'variables.tf')
            >>> print(blocks[0]['labels'])
            ['name']
        """
        blocks: List[HclBlock] = []
        length = len(content)
        index = 0

        _scan_log(verbose, f"start scan source={source!r} bytes={length}")
        block_seq = 0

        while index < length:
            index = skip_whitespace_and_comments(content, index)

            if index >= length:
                break

            if is_quote(content[index]):
                _scan_trace(verbose, f"skip top-level string literal at offset={index}")
                index = skip_string(content, index)
                continue

            identifier_start = index
            keyword = read_identifier(content, index)

            if not keyword:
                _scan_trace(verbose, f"skip non-identifier byte at offset={index!r} char={content[index]!r}")
                index += 1
                continue

            index += len(keyword)
            index = skip_whitespace_and_comments(content, index)

            _scan_log(verbose, f"  token IDENT keyword={keyword!r} offset={identifier_start}")

            labels: List[str] = []
            label_idx = 0
            while index < length and is_quote(content[index]):
                q_start = index
                text, end = read_quoted_string(content, index)
                labels.append(text)
                _scan_log(
                    verbose,
                    f"  token STRING[{label_idx}] decoded={text!r} (quoted span offset {q_start}..{end})",
                )
                label_idx += 1
                index = skip_whitespace_and_comments(content, end)

            if index >= length or content[index] != "{":
                _scan_trace(
                    verbose,
                    f"reject: keyword={keyword!r} at offset={identifier_start} is not a block "
                    f"(next char at {index}: {repr(content[index]) if index < length else 'EOF'})",
                )
                index = identifier_start + len(keyword)
                continue
            brace_index = index
            _scan_log(
                verbose,
                f"  token LBRACE '{{' at offset={brace_index} (after keyword={keyword!r})",
            )
            end_index = find_matching_brace(content, brace_index)

            if end_index == -1:
                location = offset_to_location(content, brace_index)
                message = f"Unclosed block '{keyword}': missing closing '}}'"
                if strict:
                    raise ParseError(message, source, location)
                warn(f"{message} in {source}:{location.line}:{location.column}")
                break

            raw = _normalize_raw(content[identifier_start : end_index + 1])
            body = content[brace_index + 1 : end_index]
            kind: BlockKind = keyword if keyword in KNOWN_BLOCKS else "unknown"  # type: ignore[assignment]

            block_seq += 1
            body_chars = len(body)
            _scan_log(
                verbose,
                f"block #{block_seq}: accept kind={kind!r} labels={labels!r} "
                f"span={identifier_start}..{end_index} body_chars={body_chars}",
            )
            _scan_trace(
                verbose,
                f"  body preview (first 200 chars): {body[:200]!r}"
                + ("..." if len(body) > 200 else ""),
            )

            blocks.append(
                {
                    "kind": kind,
                    "keyword": keyword,
                    "labels": labels,
                    "body": body.strip(),
                    "raw": raw,
                    "source": source,
                }
            )

            index = end_index + 1
            _scan_trace(verbose, f"resume scan after block #{block_seq} at offset={index}")

        _scan_log(verbose, f"end scan source={source!r} blocks={len(blocks)}")
        return blocks


def _normalize_raw(raw: str) -> str:
    """
    Normalizes the raw block text for consistent output.

    Removes leading indentation and normalizes alignment around
    equals signs for cleaner output.

    Args:
        raw: The raw block text to normalize.

    Returns:
        Normalized block text with consistent formatting.

    Example:
        >>> raw = '''
        ...     variable "name" {
        ...         type     =    string
        ...     }
        ... '''
        >>> normalized = _normalize_raw(raw)
        >>> print(normalized)
        variable "name" {
            type = string
        }
    """
    trimmed = raw.strip()
    lines = trimmed.splitlines()

    if len(lines) == 1:
        return lines[0]

    indents = [
        len(line[: len(line) - len(line.lstrip())])
        for line in lines[1:]
        if line.strip()
    ]
    min_indent = min(indents) if indents else 0

    def normalize_alignment(line: str) -> str:
        """Normalizes spacing around equals signs."""
        line = re.sub(r"\s{2,}=\s*", " = ", line)
        line = re.sub(r"\s*=\s{2,}", " = ", line)
        return line.rstrip()

    normalized_lines = []
    for idx, line in enumerate(lines):
        without_indent = line.lstrip() if idx == 0 else line[min(min_indent, len(line)) :]
        normalized_lines.append(normalize_alignment(without_indent))

    return "\n".join(normalized_lines)
