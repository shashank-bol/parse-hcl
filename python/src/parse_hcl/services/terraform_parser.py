"""
Main Terraform parser for HCL configuration files.

Parses .tf and .tf.json files into structured TerraformDocument objects.
"""

from __future__ import annotations

import json
import sys
from typing import Any, List

from ..parsers.generic_parser import (
    DataParser,
    GenericBlockParser,
    ModuleParser,
    ProviderParser,
    ResourceParser,
    TerraformSettingsParser,
)
from ..parsers.locals_parser import LocalsParser
from ..parsers.output_parser import OutputParser
from ..parsers.variable_parser import VariableParser
from ..types import DirectoryParseResult, FileParseResult, TerraformDocument, create_empty_document
from ..utils.common.fs import is_directory, list_terraform_files, path_exists, read_text_file
from ..utils.common.logger import info
from ..utils.lexer.block_scanner import BlockScanner
from ..utils.parser import value_classifier as _value_classifier
from .terraform_json_parser import TerraformJsonParser


def _summarize_parsed(kind: str, parsed: Any) -> str:
    """One-line summary for verbose logging."""
    if not isinstance(parsed, dict):
        return repr(parsed)[:120]
    if kind == "resource":
        return f'{parsed.get("type", "?")}.{parsed.get("name", "?")}'
    if kind == "data":
        return f'{parsed.get("dataType", "?")}.{parsed.get("name", "?")}'
    if kind in ("variable", "output", "module", "provider"):
        return str(parsed.get("name", "?"))
    if kind == "locals":
        return f'local:{parsed.get("name", "?")}'
    if kind == "terraform":
        return "terraform { ... }"
    if kind in ("moved", "import", "check", "terraform_data", "unknown"):
        labels = parsed.get("labels") or []
        return f'{parsed.get("type", kind)} {labels}'
    return kind


class TerraformParser:
    """
    Parser for Terraform configuration files (.tf, .tf.json).

    Provides methods for parsing single files and directories of Terraform
    configurations into structured TerraformDocument objects.

    Example:
        >>> parser = TerraformParser()
        >>>
        >>> # Parse a single file
        >>> doc = parser.parse_file('main.tf')
        >>>
        >>> # Parse a directory
        >>> result = parser.parse_directory('./terraform')
        >>>
        >>> # Access parsed resources
        >>> for resource in doc['resource']:
        ...     print(f"{resource['type']}.{resource['name']}")
    """

    def __init__(self, *, verbose: int = 0) -> None:
        """
        Initializes the TerraformParser with all required sub-parsers.

        Args:
            verbose: Logging verbosity for parsing (stderr). ``0`` = off, ``1`` = steps
                (block scan, kind, labels, summaries), ``2+`` = also print each parsed
                block as JSON (can be large).
        """
        self._verbose = max(0, verbose)
        self.scanner = BlockScanner()
        self.variable_parser = VariableParser()
        self.output_parser = OutputParser()
        self.locals_parser = LocalsParser()
        self.module_parser = ModuleParser()
        self.provider_parser = ProviderParser()
        self.resource_parser = ResourceParser()
        self.data_parser = DataParser()
        self.terraform_settings_parser = TerraformSettingsParser()
        self.generic_block_parser = GenericBlockParser()
        self.json_parser = TerraformJsonParser()

    def _log_verbose(self, message: str) -> None:
        if self._verbose >= 1:
            print("[parser:verbose]", message, file=sys.stderr)

    def _log_parsed_block(
        self,
        kind: str,
        index: int,
        total: int,
        parsed: Any,
        *,
        sub: int | None = None,
        subs: int | None = None,
    ) -> None:
        scope = f"block {index + 1}/{total}"
        if sub is not None and subs is not None:
            scope = f"{scope} local {sub}/{subs}"
        if self._verbose >= 1:
            labels = ""
            if isinstance(parsed, dict) and parsed.get("labels") is not None:
                labels = f" labels={parsed.get('labels')}"
            self._log_verbose(f"{scope} kind={kind}{labels} -> {_summarize_parsed(kind, parsed)}")
        if self._verbose >= 2:
            title = f"parsed {scope} ({kind})"
            print(f"[parser:trace] === {title} ===", file=sys.stderr)
            print(json.dumps(parsed, indent=2, default=str), file=sys.stderr)

    def _log_document_summary(self, document: TerraformDocument) -> None:
        if self._verbose < 1:
            return
        parts = [f"{k}={len(document.get(k, []))}" for k in document if isinstance(document.get(k), list)]
        self._log_verbose("document summary: " + ", ".join(parts))

    def parse_file(self, file_path: str) -> TerraformDocument:
        """
        Parses a single Terraform configuration file.

        Supports both HCL (.tf) and JSON (.tf.json) formats.

        Args:
            file_path: Path to the Terraform configuration file.

        Returns:
            A TerraformDocument containing all parsed blocks.

        Raises:
            FileNotFoundError: If the file does not exist.
            ParseError: If the file contains invalid HCL syntax.

        Example:
            >>> parser = TerraformParser()
            >>> doc = parser.parse_file('main.tf')
            >>> print(len(doc['resource']))
            5
        """
        if file_path.endswith(".tf.json"):
            info(f"Parsing Terraform JSON file: {file_path}")
            self._log_verbose(f"scan: Terraform JSON (native) path={file_path}")
            _value_classifier.set_reference_logging(self._verbose)
            try:
                document = self.json_parser.parse_file(file_path)
            finally:
                _value_classifier.set_reference_logging(0)
            self._log_document_summary(document)
            if self._verbose >= 2:
                print("[parser:trace] === full document (.tf.json) ===", file=sys.stderr)
                print(json.dumps(document, indent=2, default=str), file=sys.stderr)
            return document

        info(f"Parsing Terraform file: {file_path}")
        content = read_text_file(file_path)
        _value_classifier.set_reference_logging(self._verbose)
        try:
            blocks = self.scanner.scan(content, file_path, verbose=self._verbose)
            self._log_verbose(f"scan: found {len(blocks)} top-level block(s) in {file_path}")
            document = create_empty_document()

            total = len(blocks)
            for block_index, block in enumerate(blocks):
                kind = block["kind"]

                if kind == "variable":
                    parsed = self.variable_parser.parse(block)
                    document["variable"].append(parsed)
                    self._log_parsed_block(kind, block_index, total, parsed)
                elif kind == "output":
                    parsed = self.output_parser.parse(block)
                    document["output"].append(parsed)
                    self._log_parsed_block(kind, block_index, total, parsed)
                elif kind == "locals":
                    locals_list = self.locals_parser.parse(block)
                    n_locals = len(locals_list)
                    for j, local in enumerate(locals_list):
                        document["locals"].append(local)
                        self._log_parsed_block(kind, block_index, total, local, sub=j + 1, subs=n_locals)
                elif kind == "module":
                    parsed = self.module_parser.parse(block)
                    document["module"].append(parsed)
                    self._log_parsed_block(kind, block_index, total, parsed)
                elif kind == "provider":
                    parsed = self.provider_parser.parse(block)
                    document["provider"].append(parsed)
                    self._log_parsed_block(kind, block_index, total, parsed)
                elif kind == "resource":
                    parsed = self.resource_parser.parse(block)
                    document["resource"].append(parsed)
                    self._log_parsed_block(kind, block_index, total, parsed)
                elif kind == "data":
                    parsed = self.data_parser.parse(block)
                    document["data"].append(parsed)
                    self._log_parsed_block(kind, block_index, total, parsed)
                elif kind == "terraform":
                    parsed = self.terraform_settings_parser.parse(block)
                    document["terraform"].append(parsed)
                    self._log_parsed_block(kind, block_index, total, parsed)
                elif kind == "moved":
                    parsed = self.generic_block_parser.parse(block)
                    document["moved"].append(parsed)
                    self._log_parsed_block(kind, block_index, total, parsed)
                elif kind == "import":
                    parsed = self.generic_block_parser.parse(block)
                    document["import"].append(parsed)
                    self._log_parsed_block(kind, block_index, total, parsed)
                elif kind == "check":
                    parsed = self.generic_block_parser.parse(block)
                    document["check"].append(parsed)
                    self._log_parsed_block(kind, block_index, total, parsed)
                elif kind == "terraform_data":
                    parsed = self.generic_block_parser.parse(block)
                    document["terraform_data"].append(parsed)
                    self._log_parsed_block(kind, block_index, total, parsed)
                else:
                    parsed = self.generic_block_parser.parse(block)
                    document["unknown"].append(parsed)
                    self._log_parsed_block(kind, block_index, total, parsed)

            self._log_document_summary(document)
            if self._verbose >= 2:
                print("[parser:trace] === full document (merged) ===", file=sys.stderr)
                print(json.dumps(document, indent=2, default=str), file=sys.stderr)

            return document
        finally:
            _value_classifier.set_reference_logging(0)

    def parse_directory(self, dir_path: str, aggregate: bool = True, include_per_file: bool = True) -> DirectoryParseResult:
        """
        Parses all Terraform configuration files in a directory.

        Recursively finds and parses all .tf and .tf.json files in the directory,
        excluding common non-Terraform directories (.terraform, .git, node_modules).

        Args:
            dir_path: Path to the directory to parse.
            aggregate: Whether to combine all files into a single document (default: True).
            include_per_file: Whether to include per-file results (default: True).

        Returns:
            A DirectoryParseResult containing:
            - combined: The aggregated TerraformDocument (if aggregate is True)
            - files: List of per-file parse results (if include_per_file is True)

        Raises:
            ValueError: If the directory path is invalid.

        Example:
            >>> parser = TerraformParser()
            >>> result = parser.parse_directory('./terraform')
            >>> print(len(result['combined']['resource']))
            10
            >>> print(len(result['files']))
            3
        """
        if not path_exists(dir_path) or not is_directory(dir_path):
            raise ValueError(f"Invalid directory path: {dir_path}")

        files = list_terraform_files(dir_path)
        self._log_verbose(f"directory scan: {len(files)} file(s) under {dir_path}")
        parsed_files: List[FileParseResult] = [{"path": file_path, "document": self.parse_file(file_path)} for file_path in files]

        combined = self.combine([item["document"] for item in parsed_files]) if aggregate else None
        if aggregate and combined is not None and self._verbose >= 1:
            self._log_verbose("--- combined document ---")
            self._log_document_summary(combined)
            if self._verbose >= 2:
                print("[parser:trace] === combined document (all files) ===", file=sys.stderr)
                print(json.dumps(combined, indent=2, default=str), file=sys.stderr)
        result: DirectoryParseResult = {"files": parsed_files if include_per_file else []}
        if combined is not None:
            result["combined"] = combined
        return result

    def combine(self, documents: List[TerraformDocument]) -> TerraformDocument:
        """
        Combines multiple TerraformDocument objects into a single document.

        Merges all blocks from each document into a single unified document.

        Args:
            documents: List of TerraformDocument objects to combine.

        Returns:
            A single TerraformDocument containing all blocks from all documents.

        Example:
            >>> parser = TerraformParser()
            >>> doc1 = parser.parse_file('main.tf')
            >>> doc2 = parser.parse_file('variables.tf')
            >>> combined = parser.combine([doc1, doc2])
        """
        combined = create_empty_document()
        for doc in documents:
            combined["terraform"].extend(doc.get("terraform", []))
            combined["provider"].extend(doc.get("provider", []))
            combined["variable"].extend(doc.get("variable", []))
            combined["output"].extend(doc.get("output", []))
            combined["module"].extend(doc.get("module", []))
            combined["resource"].extend(doc.get("resource", []))
            combined["data"].extend(doc.get("data", []))
            combined["locals"].extend(doc.get("locals", []))
            combined["moved"].extend(doc.get("moved", []))
            combined["import"].extend(doc.get("import", []))
            combined["check"].extend(doc.get("check", []))
            combined["terraform_data"].extend(doc.get("terraform_data", []))
            combined["unknown"].extend(doc.get("unknown", []))
        return combined
