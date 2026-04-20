"""
:class:`TerraformModule`: the OOP container for an entire parsed configuration.

A :class:`TerraformModule` owns lists of typed blocks (variables, resources,
outputs, locals, ...) and is itself a :class:`TerraformElement` so the whole
configuration is a single navigable tree.

Round-trip pipeline supported by this class:

::

    .tf  ──parse──▶  dict  ──from_dict──▶  TerraformModule
                                                │
                                       (mutate via OOP API)
                                                │
                          ◀──to_tf────  dict  ◀──to_dict
                          ◀──to_json──

``raw`` fields are dropped on serialization (the writer rebuilds HCL from
structured fields), satisfying the requirement that ``raw`` is not used for
reverse-engineering ``.tf``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Union

from ..services.terraform_parser import TerraformParser
from ..utils.common.logger import warn
from ..utils.serialization.tf_writer import to_tf as _to_tf
from .base import TerraformElement, drop_raw, prune_empty
from .blocks import (
    Block,
    DataBlock,
    GenericBlock,
    LocalValue,
    ModuleCallBlock,
    OutputBlock,
    ProviderBlock,
    ResourceBlock,
    TerraformSettingsBlock,
    VariableBlock,
)

# Document section name -> block class. Generic sections are handled separately.
_TYPED_SECTIONS: Dict[str, type] = {
    "terraform": TerraformSettingsBlock,
    "provider": ProviderBlock,
    "variable": VariableBlock,
    "output": OutputBlock,
    "module": ModuleCallBlock,
    "resource": ResourceBlock,
    "data": DataBlock,
    "locals": LocalValue,
}

_GENERIC_SECTIONS: tuple = (
    "moved",
    "import",
    "check",
    "terraform_data",
    "unknown",
)


class TerraformModule(TerraformElement):
    """
    Tree root for an OOP terraform configuration.

    Attributes mirror the document sections produced by
    :class:`~parse_hcl.services.terraform_parser.TerraformParser`. Use
    :meth:`from_tf_file` / :meth:`from_json_file` / :meth:`from_dict` to load,
    update via the helper methods (``add_resource`` / ``find_resource`` / ...)
    or by reaching into the typed children, then persist with
    :meth:`save_json` / :meth:`save_tf`.
    """

    def __init__(self) -> None:
        super().__init__()
        self.terraform: List[TerraformSettingsBlock] = []
        self.provider: List[ProviderBlock] = []
        self.variable: List[VariableBlock] = []
        self.output: List[OutputBlock] = []
        self.module: List[ModuleCallBlock] = []
        self.resource: List[ResourceBlock] = []
        self.data: List[DataBlock] = []
        self.locals: List[LocalValue] = []
        # Generic sections keyed by document key (``moved``, ``import`` -> stored as
        # ``import`` even though the dict uses ``import_`` to avoid Python's reserved word).
        self.generic: Dict[str, List[GenericBlock]] = {key: [] for key in _GENERIC_SECTIONS}

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, document: Dict[str, Any]) -> "TerraformModule":
        """
        Build a :class:`TerraformModule` from a parser ``TerraformDocument`` dict.

        If ``document`` looks like an export payload (``{"version", "document",
        "graph"}``), the nested ``document`` is used.
        """
        if isinstance(document, dict) and "document" in document and isinstance(document["document"], dict):
            document = document["document"]
        if not isinstance(document, dict):
            raise TypeError("document must be a mapping")

        instance = cls()
        for key, klass in _TYPED_SECTIONS.items():
            for entry in document.get(key) or []:
                if isinstance(entry, dict):
                    instance._append(key, klass.from_dict(entry))
        # Generic sections may be stored under ``import_`` due to Python keyword.
        for key in _GENERIC_SECTIONS:
            entries = document.get(key)
            if entries is None and key == "import":
                entries = document.get("import_")
            for entry in entries or []:
                if isinstance(entry, dict):
                    block = GenericBlock.from_dict(entry, kind=key)
                    instance._append(key, block)
        return instance

    @classmethod
    def from_json(cls, payload: Union[str, bytes]) -> "TerraformModule":
        """Build from a JSON string (parser-shaped document or export payload)."""
        return cls.from_dict(json.loads(payload))

    @classmethod
    def from_json_file(cls, path: Union[str, Path]) -> "TerraformModule":
        """Build from a JSON file produced by :meth:`save_json` / the CLI."""
        text = Path(path).read_text(encoding="utf-8")
        return cls.from_json(text)

    @classmethod
    def from_tf_file(cls, path: Union[str, Path]) -> "TerraformModule":
        """Parse a ``.tf`` (or ``.tf.json``) file and wrap the result."""
        document = TerraformParser().parse_file(str(path))
        return cls.from_dict(dict(document))

    @classmethod
    def from_tf_directory(cls, path: Union[str, Path]) -> "TerraformModule":
        """Parse every ``.tf`` file under ``path`` (recursively) and wrap the merged document."""
        result = TerraformParser().parse_directory(str(path), aggregate=True, include_per_file=False)
        combined = result.get("combined") or {}
        return cls.from_dict(dict(combined))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _append(self, key: str, block: Block) -> None:
        self._adopt(block)
        if key in _TYPED_SECTIONS:
            getattr(self, key).append(block)
        else:
            self.generic[key].append(block)

    def _typed_lists(self) -> Iterator[tuple]:
        """Yield ``(section_key, list_of_blocks)`` for every typed section."""
        yield ("terraform", self.terraform)
        yield ("provider", self.provider)
        yield ("variable", self.variable)
        yield ("output", self.output)
        yield ("module", self.module)
        yield ("resource", self.resource)
        yield ("data", self.data)
        yield ("locals", self.locals)

    # ------------------------------------------------------------------
    # Tree API
    # ------------------------------------------------------------------

    def children(self) -> List[TerraformElement]:
        kids: List[TerraformElement] = []
        for _, blocks in self._typed_lists():
            kids.extend(blocks)
        for blocks in self.generic.values():
            kids.extend(blocks)
        return kids

    def all_blocks(self) -> List[Block]:
        """All top-level blocks, in document order (typed sections first)."""
        return [b for b in self.children() if isinstance(b, Block)]

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def find_resource(self, type: str, name: str) -> Optional[ResourceBlock]:
        for r in self.resource:
            if r.type == type and r.name == name:
                return r
        return None

    def find_resources(self, type: Optional[str] = None) -> List[ResourceBlock]:
        if type is None:
            return list(self.resource)
        return [r for r in self.resource if r.type == type]

    def find_variable(self, name: str) -> Optional[VariableBlock]:
        for v in self.variable:
            if v.name == name:
                return v
        return None

    def find_output(self, name: str) -> Optional[OutputBlock]:
        for o in self.output:
            if o.name == name:
                return o
        return None

    def find_local(self, name: str) -> Optional[LocalValue]:
        for lv in self.locals:
            if lv.name == name:
                return lv
        return None

    def find_module_call(self, name: str) -> Optional[ModuleCallBlock]:
        for m in self.module:
            if m.name == name:
                return m
        return None

    def find_provider(self, name: str, alias: Optional[str] = None) -> Optional[ProviderBlock]:
        for p in self.provider:
            if p.name == name and p.alias == alias:
                return p
        return None

    def find_data(self, dataType: str, name: str) -> Optional[DataBlock]:
        for d in self.data:
            if d.dataType == dataType and d.name == name:
                return d
        return None

    # ------------------------------------------------------------------
    # Mutation API
    # ------------------------------------------------------------------

    def add_resource(self, block: ResourceBlock) -> ResourceBlock:
        self._append("resource", block)
        return block

    def add_variable(self, block: VariableBlock) -> VariableBlock:
        self._append("variable", block)
        return block

    def add_output(self, block: OutputBlock) -> OutputBlock:
        self._append("output", block)
        return block

    def add_local(self, block: LocalValue) -> LocalValue:
        self._append("locals", block)
        return block

    def add_module_call(self, block: ModuleCallBlock) -> ModuleCallBlock:
        self._append("module", block)
        return block

    def add_provider(self, block: ProviderBlock) -> ProviderBlock:
        self._append("provider", block)
        return block

    def add_data(self, block: DataBlock) -> DataBlock:
        self._append("data", block)
        return block

    def add_generic(self, kind: str, block: GenericBlock) -> GenericBlock:
        if kind not in _GENERIC_SECTIONS:
            raise ValueError(f"unknown generic section: {kind!r}")
        block.section_kind = kind
        self._append(kind, block)
        return block

    def remove_resource(self, type: str, name: str) -> Optional[ResourceBlock]:
        for idx, r in enumerate(self.resource):
            if r.type == type and r.name == name:
                return self.resource.pop(idx)
        return None

    def remove_variable(self, name: str) -> Optional[VariableBlock]:
        for idx, v in enumerate(self.variable):
            if v.name == name:
                return self.variable.pop(idx)
        return None

    def remove_output(self, name: str) -> Optional[OutputBlock]:
        for idx, o in enumerate(self.output):
            if o.name == name:
                return self.output.pop(idx)
        return None

    def remove_local(self, name: str) -> Optional[LocalValue]:
        for idx, lv in enumerate(self.locals):
            if lv.name == name:
                return self.locals.pop(idx)
        return None

    def filter_blocks(self, predicate: Callable[[Block], bool]) -> List[Block]:
        return [b for b in self.all_blocks() if predicate(b)]

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self, *, prune: bool = False) -> Dict[str, Any]:
        """
        Serialize the whole tree back to a parser-shaped document dict.

        With ``prune=True`` empty containers (``[]`` / ``{}`` / ``None``) are
        stripped to match the default output of the JSON / YAML serializers.
        """
        payload: Dict[str, Any] = {}
        for key, blocks in self._typed_lists():
            payload[key] = [b.to_dict() for b in blocks]
        for key, blocks in self.generic.items():
            payload[key] = [b.to_dict() for b in blocks]
        if prune:
            return prune_empty(payload) or {}
        return payload

    def to_json(self, *, indent: int = 2, prune: bool = False) -> str:
        """Serialize to a JSON string (without ``raw`` fields)."""
        return json.dumps(self.to_dict(prune=prune), indent=indent)

    def to_tf(self) -> str:
        """Render the tree as HCL (``.tf``) text using the existing writer."""
        return _to_tf(self.to_dict())

    def save_json(self, path: Union[str, Path], *, indent: int = 2, prune: bool = False) -> Path:
        """Write the JSON representation to ``path`` and return the path."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_json(indent=indent, prune=prune), encoding="utf-8")
        return target

    def save_tf(
        self,
        path: Union[str, Path],
        *,
        run_terraform_fmt: bool = True,
        terraform_bin: str = "terraform",
    ) -> Path:
        """
        Write the HCL representation to ``path`` and return the path.

        When ``run_terraform_fmt`` is True and a ``terraform`` executable is on
        ``PATH``, runs ``terraform fmt`` on the saved file so layout matches
        Terraform's canonical style. If the binary is missing, formatting is
        skipped silently. If ``terraform fmt`` exits non-zero, a warning is
        printed to stderr and the file contents from the writer are left as-is.
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_tf(), encoding="utf-8")

        if run_terraform_fmt:
            exe = shutil.which(terraform_bin)
            if exe is not None:
                try:
                    result = subprocess.run(
                        [exe, "fmt", str(target.resolve())],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if result.returncode != 0:
                        msg = (result.stderr or result.stdout or "").strip()
                        warn(
                            f"terraform fmt failed for {target} (exit {result.returncode})",
                            msg,
                        )
                except OSError as exc:
                    warn(f"terraform fmt could not run for {target}: {exc}")

        return target

    # ------------------------------------------------------------------
    # Convenience constructors / helpers
    # ------------------------------------------------------------------

    @staticmethod
    def strip_raw(document: Dict[str, Any]) -> Dict[str, Any]:
        """Drop ``raw`` keys from an externally-supplied parser document."""
        cleaned = drop_raw(document)
        return cleaned if isinstance(cleaned, dict) else {}

    def merge(self, other: "TerraformModule") -> "TerraformModule":
        """Append every top-level block from ``other`` into ``self`` (in place)."""
        for key, blocks in other._typed_lists():
            for block in blocks:
                self._append(key, block)
        for key, blocks in other.generic.items():
            for block in blocks:
                self._append(key, block)
        return self

    def __iter__(self) -> Iterator[Block]:
        return iter(self.all_blocks())

    def __len__(self) -> int:
        return sum(1 for _ in self.all_blocks())

    def __repr__(self) -> str:
        counts = {
            key: len(getattr(self, key))
            for key, _ in self._typed_lists()
        }
        for key, blocks in self.generic.items():
            if blocks:
                counts[key] = len(blocks)
        summary = ", ".join(f"{k}={v}" for k, v in counts.items() if v)
        return f"TerraformModule({summary})"


__all__ = ["TerraformModule"]
