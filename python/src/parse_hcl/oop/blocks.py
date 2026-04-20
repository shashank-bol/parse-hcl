"""
OOP wrappers for Terraform blocks.

Class hierarchy:

::

    TerraformElement (base)
    └── Block (abstract)
        ├── AttributeBlock           (carries a ``properties`` map)
        │   ├── TerraformSettingsBlock
        │   ├── ProviderBlock
        │   ├── ModuleCallBlock
        │   ├── ResourceBlock        (also has ``meta`` / nested ``blocks`` / ``dynamic_blocks``)
        │   └── DataBlock            (also has nested ``blocks``)
        ├── VariableBlock
        ├── OutputBlock
        ├── LocalValue
        ├── NestedBlock              (block inside a resource / data body)
        ├── DynamicBlock             (HCL ``dynamic "..." { ... }``)
        └── GenericBlock             (moved / import / check / unknown ...)

The classes are designed for two-way conversion: each subclass implements
``from_dict`` / ``to_dict`` against the parser's ``TerraformDocument`` shape and
exposes update helpers (e.g. ``set_property``, ``add_block``) so callers can
mutate the tree without poking at raw dicts.

``raw`` and ``source`` fields are accepted on input (for traceability) but
``raw`` is never re-emitted; ``source`` is preserved when present so users can
keep file provenance through a round-trip if they choose.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any, Dict, List, Optional

from .base import TerraformElement
from .values import ArrayValue, LiteralValue, ObjectValue, Value


class Block(TerraformElement):
    """
    Abstract base for every HCL block.

    Concrete subclasses set :attr:`KIND` to the document section key
    (``"resource"``, ``"variable"``, ...) and implement ``from_dict`` /
    ``to_dict``.
    """

    #: Section key under which instances live in :class:`TerraformModule`.
    KIND: str = ""

    def __init__(self, source: Optional[str] = None) -> None:
        super().__init__()
        self.source: Optional[str] = source

    @classmethod
    @abstractmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Block":
        """Hydrate a typed block from its parser dict representation."""

    @abstractmethod
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to the parser dict shape (no ``raw``)."""

    # ----- shared helpers ------------------------------------------------

    def _attach_source(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Append ``source`` to ``payload`` when known and return ``payload``."""
        if self.source is not None:
            payload["source"] = self.source
        return payload


# ---------------------------------------------------------------------------
# Attribute-bearing blocks
# ---------------------------------------------------------------------------


class AttributeBlock(Block):
    """
    Mid-tier base for blocks that expose a ``properties`` map.

    Subclasses inherit the ``get_property`` / ``set_property`` /
    ``remove_property`` API and only need to handle their additional fields
    (labels, meta, nested blocks, etc.).
    """

    def __init__(
        self,
        properties: Optional[Dict[str, Value]] = None,
        source: Optional[str] = None,
    ) -> None:
        super().__init__(source=source)
        self.properties: Dict[str, Value] = {}
        for key, val in (properties or {}).items():
            self.set_property(key, val)

    # ----- property API --------------------------------------------------

    def get_property(self, key: str) -> Optional[Value]:
        return self.properties.get(key)

    def set_property(self, key: str, value: Any) -> Value:
        wrapped = value if isinstance(value, Value) else Value.from_dict(value)
        if wrapped is None:
            wrapped = LiteralValue(None)
        self.properties[key] = wrapped
        self._adopt(wrapped)
        return wrapped

    def remove_property(self, key: str) -> Optional[Value]:
        return self.properties.pop(key, None)

    def update_properties(self, mapping: Dict[str, Any]) -> "AttributeBlock":
        for k, v in mapping.items():
            self.set_property(k, v)
        return self

    def children(self) -> List[TerraformElement]:
        return list(self.properties.values())

    @staticmethod
    def _hydrate_properties(raw: Any) -> Dict[str, Value]:
        out: Dict[str, Value] = {}
        if isinstance(raw, dict):
            for key, val in raw.items():
                v = Value.from_dict(val)
                if v is not None:
                    out[key] = v
        return out

    def _emit_properties(self) -> Dict[str, Any]:
        return {k: v.to_dict() for k, v in self.properties.items()}


class TerraformSettingsBlock(AttributeBlock):
    """The top-level ``terraform { ... }`` block."""

    KIND = "terraform"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TerraformSettingsBlock":
        return cls(
            properties=cls._hydrate_properties(data.get("properties")),
            source=data.get("source"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return self._attach_source({"properties": self._emit_properties()})


class ProviderBlock(AttributeBlock):
    """A ``provider "<name>" { ... }`` block (optionally with ``alias``)."""

    KIND = "provider"

    def __init__(
        self,
        name: str,
        properties: Optional[Dict[str, Value]] = None,
        alias: Optional[str] = None,
        source: Optional[str] = None,
    ) -> None:
        super().__init__(properties=properties, source=source)
        self.name: str = name
        self.alias: Optional[str] = alias

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProviderBlock":
        return cls(
            name=str(data.get("name", "")),
            alias=data.get("alias"),
            properties=cls._hydrate_properties(data.get("properties")),
            source=data.get("source"),
        )

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"name": self.name}
        if self.alias is not None:
            payload["alias"] = self.alias
        payload["properties"] = self._emit_properties()
        return self._attach_source(payload)


class ModuleCallBlock(AttributeBlock):
    """
    A module call: ``module "<name>" { source = "...", ... }``.

    Named ``ModuleCallBlock`` rather than ``ModuleBlock`` to avoid confusion
    with the top-level :class:`~parse_hcl.oop.module.TerraformModule` container.
    """

    KIND = "module"

    def __init__(
        self,
        name: str,
        properties: Optional[Dict[str, Value]] = None,
        source: Optional[str] = None,
        source_raw: Optional[str] = None,
        source_output_dir: Optional[str] = None,
    ) -> None:
        super().__init__(properties=properties, source=source)
        self.name: str = name
        self.source_raw: Optional[str] = source_raw
        self.source_output_dir: Optional[str] = source_output_dir

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ModuleCallBlock":
        return cls(
            name=str(data.get("name", "")),
            properties=cls._hydrate_properties(data.get("properties")),
            source=data.get("source"),
            source_raw=data.get("source_raw"),
            source_output_dir=data.get("source_output_dir"),
        )

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "name": self.name,
            "properties": self._emit_properties(),
        }
        if self.source_raw is not None:
            payload["source_raw"] = self.source_raw
        if self.source_output_dir is not None:
            payload["source_output_dir"] = self.source_output_dir
        return self._attach_source(payload)


class NestedBlock(AttributeBlock):
    """
    A block nested inside another block's body (e.g. ``filter { ... }`` inside
    a data source, or ``lifecycle { ... }`` inside a resource).
    """

    KIND = "nested"

    def __init__(
        self,
        type: str,
        labels: Optional[List[str]] = None,
        attributes: Optional[Dict[str, Value]] = None,
        blocks: Optional[List["NestedBlock"]] = None,
        source: Optional[str] = None,
    ) -> None:
        super().__init__(properties=attributes, source=source)
        self.type: str = type
        self.labels: List[str] = list(labels or [])
        self.blocks: List[NestedBlock] = []
        for nested in blocks or []:
            self.add_block(nested)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "NestedBlock":
        instance = cls(
            type=str(data.get("type", "")),
            labels=list(data.get("labels") or []),
            attributes=cls._hydrate_properties(data.get("attributes")),
            source=data.get("source"),
        )
        for child in data.get("blocks") or []:
            if isinstance(child, dict):
                instance.add_block(NestedBlock.from_dict(child))
        return instance

    # ----- nested block API ---------------------------------------------

    def add_block(self, block: "NestedBlock") -> "NestedBlock":
        self.blocks.append(block)
        self._adopt(block)
        return block

    def remove_block(self, predicate: Any) -> Optional["NestedBlock"]:
        """
        Remove the first nested block matching ``predicate``.

        ``predicate`` may be a string (matched against ``type``) or a callable
        accepting a :class:`NestedBlock`. Returns the removed block, or
        ``None``.
        """
        for idx, block in enumerate(self.blocks):
            if (callable(predicate) and predicate(block)) or block.type == predicate:
                return self.blocks.pop(idx)
        return None

    def find_blocks(self, type: str) -> List["NestedBlock"]:
        return [b for b in self.blocks if b.type == type]

    def children(self) -> List[TerraformElement]:
        return [*self.properties.values(), *self.blocks]

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"type": self.type}
        if self.labels:
            payload["labels"] = list(self.labels)
        payload["attributes"] = self._emit_properties()
        if self.blocks:
            payload["blocks"] = [b.to_dict() for b in self.blocks]
        return payload


class DynamicBlock(Block):
    """An HCL ``dynamic "<label>" { for_each = ..., content { ... } }`` block."""

    KIND = "dynamic"

    def __init__(
        self,
        label: str,
        for_each: Optional[Value] = None,
        iterator: Optional[str] = None,
        content: Optional[Dict[str, Value]] = None,
        source: Optional[str] = None,
    ) -> None:
        super().__init__(source=source)
        self.label: str = label
        self.for_each: Optional[Value] = None
        self.iterator: Optional[str] = iterator
        self.content: Dict[str, Value] = {}
        if for_each is not None:
            self.set_for_each(for_each)
        for key, val in (content or {}).items():
            self.set_content(key, val)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DynamicBlock":
        instance = cls(
            label=str(data.get("label", "")),
            iterator=data.get("iterator"),
            source=data.get("source"),
        )
        if data.get("for_each") is not None:
            instance.set_for_each(data.get("for_each"))
        content_raw = data.get("content")
        if isinstance(content_raw, dict):
            for key, val in content_raw.items():
                instance.set_content(key, val)
        return instance

    def set_for_each(self, value: Any) -> Optional[Value]:
        wrapped = value if isinstance(value, Value) else Value.from_dict(value)
        self.for_each = wrapped
        self._adopt(wrapped)
        return wrapped

    def set_iterator(self, name: Optional[str]) -> "DynamicBlock":
        self.iterator = name
        return self

    def set_content(self, key: str, value: Any) -> Value:
        wrapped = value if isinstance(value, Value) else Value.from_dict(value)
        if wrapped is None:
            wrapped = LiteralValue(None)
        self.content[key] = wrapped
        self._adopt(wrapped)
        return wrapped

    def remove_content(self, key: str) -> Optional[Value]:
        return self.content.pop(key, None)

    def children(self) -> List[TerraformElement]:
        kids: List[TerraformElement] = list(self.content.values())
        if self.for_each is not None:
            kids.append(self.for_each)
        return kids

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"label": self.label}
        if self.for_each is not None:
            payload["for_each"] = self.for_each.to_dict()
        if self.iterator is not None:
            payload["iterator"] = self.iterator
        if self.content:
            payload["content"] = {k: v.to_dict() for k, v in self.content.items()}
        return payload


class ResourceBlock(AttributeBlock):
    """A ``resource "<type>" "<name>" { ... }`` block."""

    KIND = "resource"

    def __init__(
        self,
        type: str,
        name: str,
        properties: Optional[Dict[str, Value]] = None,
        meta: Optional[Dict[str, Value]] = None,
        blocks: Optional[List[NestedBlock]] = None,
        dynamic_blocks: Optional[List[DynamicBlock]] = None,
        source: Optional[str] = None,
    ) -> None:
        super().__init__(properties=properties, source=source)
        self.type: str = type
        self.name: str = name
        self.meta: Dict[str, Value] = {}
        self.blocks: List[NestedBlock] = []
        self.dynamic_blocks: List[DynamicBlock] = []
        for key, val in (meta or {}).items():
            self.set_meta(key, val)
        for nb in blocks or []:
            self.add_block(nb)
        for db in dynamic_blocks or []:
            self.add_dynamic_block(db)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ResourceBlock":
        instance = cls(
            type=str(data.get("type", "")),
            name=str(data.get("name", "")),
            properties=cls._hydrate_properties(data.get("properties")),
            meta=cls._hydrate_properties(data.get("meta")),
            source=data.get("source"),
        )
        for nb in data.get("blocks") or []:
            if isinstance(nb, dict):
                instance.add_block(NestedBlock.from_dict(nb))
        for db in data.get("dynamic_blocks") or []:
            if isinstance(db, dict):
                instance.add_dynamic_block(DynamicBlock.from_dict(db))
        return instance

    # ----- meta API ------------------------------------------------------

    def get_meta(self, key: str) -> Optional[Value]:
        return self.meta.get(key)

    def set_meta(self, key: str, value: Any) -> Value:
        wrapped = value if isinstance(value, Value) else Value.from_dict(value)
        if wrapped is None:
            wrapped = LiteralValue(None)
        self.meta[key] = wrapped
        self._adopt(wrapped)
        return wrapped

    def remove_meta(self, key: str) -> Optional[Value]:
        return self.meta.pop(key, None)

    # ----- nested-block API ---------------------------------------------

    def add_block(self, block: NestedBlock) -> NestedBlock:
        self.blocks.append(block)
        self._adopt(block)
        return block

    def remove_block(self, predicate: Any) -> Optional[NestedBlock]:
        for idx, block in enumerate(self.blocks):
            if (callable(predicate) and predicate(block)) or block.type == predicate:
                return self.blocks.pop(idx)
        return None

    def find_blocks(self, type: str) -> List[NestedBlock]:
        return [b for b in self.blocks if b.type == type]

    def add_dynamic_block(self, block: DynamicBlock) -> DynamicBlock:
        self.dynamic_blocks.append(block)
        self._adopt(block)
        return block

    def remove_dynamic_block(self, label: str) -> Optional[DynamicBlock]:
        for idx, block in enumerate(self.dynamic_blocks):
            if block.label == label:
                return self.dynamic_blocks.pop(idx)
        return None

    def children(self) -> List[TerraformElement]:
        return [
            *self.properties.values(),
            *self.meta.values(),
            *self.blocks,
            *self.dynamic_blocks,
        ]

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "type": self.type,
            "name": self.name,
            "properties": self._emit_properties(),
        }
        if self.meta:
            payload["meta"] = {k: v.to_dict() for k, v in self.meta.items()}
        if self.blocks:
            payload["blocks"] = [b.to_dict() for b in self.blocks]
        if self.dynamic_blocks:
            payload["dynamic_blocks"] = [b.to_dict() for b in self.dynamic_blocks]
        return self._attach_source(payload)


class DataBlock(AttributeBlock):
    """A ``data "<dataType>" "<name>" { ... }`` block."""

    KIND = "data"

    def __init__(
        self,
        dataType: str,
        name: str,
        properties: Optional[Dict[str, Value]] = None,
        blocks: Optional[List[NestedBlock]] = None,
        source: Optional[str] = None,
    ) -> None:
        super().__init__(properties=properties, source=source)
        self.dataType: str = dataType
        self.name: str = name
        self.blocks: List[NestedBlock] = []
        for nb in blocks or []:
            self.add_block(nb)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DataBlock":
        instance = cls(
            dataType=str(data.get("dataType", "")),
            name=str(data.get("name", "")),
            properties=cls._hydrate_properties(data.get("properties")),
            source=data.get("source"),
        )
        for nb in data.get("blocks") or []:
            if isinstance(nb, dict):
                instance.add_block(NestedBlock.from_dict(nb))
        return instance

    def add_block(self, block: NestedBlock) -> NestedBlock:
        self.blocks.append(block)
        self._adopt(block)
        return block

    def remove_block(self, predicate: Any) -> Optional[NestedBlock]:
        for idx, block in enumerate(self.blocks):
            if (callable(predicate) and predicate(block)) or block.type == predicate:
                return self.blocks.pop(idx)
        return None

    def find_blocks(self, type: str) -> List[NestedBlock]:
        return [b for b in self.blocks if b.type == type]

    def children(self) -> List[TerraformElement]:
        return [*self.properties.values(), *self.blocks]

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "dataType": self.dataType,
            "name": self.name,
            "properties": self._emit_properties(),
        }
        if self.blocks:
            payload["blocks"] = [b.to_dict() for b in self.blocks]
        return self._attach_source(payload)


# ---------------------------------------------------------------------------
# Variable / output / locals
# ---------------------------------------------------------------------------


class VariableBlock(Block):
    """A ``variable "<name>" { ... }`` block.

    The ``default`` value is a :class:`Value` tree, so a ``map(object({...}))``
    default exposes nested :class:`ObjectValue` / :class:`ArrayValue` instances
    that satisfy the user requirement of "each variable can again recursively
    have multiple blocks".
    """

    KIND = "variable"

    def __init__(
        self,
        name: str,
        type: Optional[str] = None,
        type_constraint: Optional[Dict[str, Any]] = None,
        default: Optional[Value] = None,
        description: Optional[str] = None,
        sensitive: Optional[bool] = None,
        nullable: Optional[bool] = None,
        validation: Optional[Dict[str, Any]] = None,
        source: Optional[str] = None,
    ) -> None:
        super().__init__(source=source)
        self.name: str = name
        self.type: Optional[str] = type
        self.type_constraint: Optional[Dict[str, Any]] = type_constraint
        self.default: Optional[Value] = None
        self.description: Optional[str] = description
        self.sensitive: Optional[bool] = sensitive
        self.nullable: Optional[bool] = nullable
        self.validation: Optional[Dict[str, Any]] = None
        if default is not None:
            self.set_default(default)
        if validation is not None:
            self.set_validation(validation)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VariableBlock":
        instance = cls(
            name=str(data.get("name", "")),
            type=data.get("type"),
            type_constraint=data.get("typeConstraint"),
            description=data.get("description"),
            sensitive=data.get("sensitive"),
            nullable=data.get("nullable"),
            source=data.get("source"),
        )
        if data.get("default") is not None:
            instance.set_default(data.get("default"))
        if data.get("validation") is not None:
            instance.set_validation(data.get("validation"))
        return instance

    # ----- update API ---------------------------------------------------

    def set_default(self, value: Any) -> Optional[Value]:
        wrapped = value if isinstance(value, Value) else Value.from_dict(value)
        self.default = wrapped
        self._adopt(wrapped)
        return wrapped

    def clear_default(self) -> None:
        self.default = None

    def set_type(self, type: Optional[str]) -> "VariableBlock":
        self.type = type
        return self

    def set_description(self, description: Optional[str]) -> "VariableBlock":
        self.description = description
        return self

    def set_sensitive(self, value: Optional[bool]) -> "VariableBlock":
        self.sensitive = value
        return self

    def set_nullable(self, value: Optional[bool]) -> "VariableBlock":
        self.nullable = value
        return self

    def set_validation(self, validation: Optional[Dict[str, Any]]) -> "VariableBlock":
        """
        Attach a validation block.

        ``validation`` may carry ``condition`` / ``error_message`` either as
        plain dicts (parser shape) or already-wrapped :class:`Value` instances.
        """
        if validation is None:
            self.validation = None
            return self
        normalized: Dict[str, Any] = {}
        for key in ("condition", "error_message"):
            if key in validation:
                wrapped = Value.from_dict(validation[key])
                normalized[key] = wrapped.to_dict() if wrapped is not None else None
        self.validation = normalized
        return self

    def children(self) -> List[TerraformElement]:
        return [self.default] if self.default is not None else []

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"name": self.name}
        if self.description is not None:
            payload["description"] = self.description
        if self.type is not None:
            payload["type"] = self.type
        if self.type_constraint is not None:
            payload["typeConstraint"] = self.type_constraint
        if self.default is not None:
            payload["default"] = self.default.to_dict()
        if self.sensitive is not None:
            payload["sensitive"] = self.sensitive
        if self.nullable is not None:
            payload["nullable"] = self.nullable
        if self.validation is not None:
            payload["validation"] = self.validation
        return self._attach_source(payload)


class OutputBlock(Block):
    """An ``output "<name>" { ... }`` block."""

    KIND = "output"

    def __init__(
        self,
        name: str,
        value: Optional[Value] = None,
        description: Optional[str] = None,
        sensitive: Optional[bool] = None,
        source: Optional[str] = None,
    ) -> None:
        super().__init__(source=source)
        self.name: str = name
        self.value: Optional[Value] = None
        self.description: Optional[str] = description
        self.sensitive: Optional[bool] = sensitive
        if value is not None:
            self.set_value(value)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OutputBlock":
        instance = cls(
            name=str(data.get("name", "")),
            description=data.get("description"),
            sensitive=data.get("sensitive"),
            source=data.get("source"),
        )
        if data.get("value") is not None:
            instance.set_value(data.get("value"))
        return instance

    def set_value(self, value: Any) -> Optional[Value]:
        wrapped = value if isinstance(value, Value) else Value.from_dict(value)
        self.value = wrapped
        self._adopt(wrapped)
        return wrapped

    def set_description(self, description: Optional[str]) -> "OutputBlock":
        self.description = description
        return self

    def set_sensitive(self, value: Optional[bool]) -> "OutputBlock":
        self.sensitive = value
        return self

    def children(self) -> List[TerraformElement]:
        return [self.value] if self.value is not None else []

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"name": self.name}
        if self.value is not None:
            payload["value"] = self.value.to_dict()
        if self.description is not None:
            payload["description"] = self.description
        if self.sensitive is not None:
            payload["sensitive"] = self.sensitive
        return self._attach_source(payload)


class LocalValue(Block):
    """
    A single entry inside a ``locals { ... }`` block.

    The parser flattens ``locals`` into a list of one entry per name; we model
    it the same way for symmetry.
    """

    KIND = "locals"

    def __init__(
        self,
        name: str,
        value: Optional[Value] = None,
        source: Optional[str] = None,
    ) -> None:
        super().__init__(source=source)
        self.name: str = name
        self.value: Optional[Value] = None
        if value is not None:
            self.set_value(value)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LocalValue":
        instance = cls(name=str(data.get("name", "")), source=data.get("source"))
        if data.get("value") is not None:
            instance.set_value(data.get("value"))
        return instance

    def set_value(self, value: Any) -> Optional[Value]:
        wrapped = value if isinstance(value, Value) else Value.from_dict(value)
        self.value = wrapped
        self._adopt(wrapped)
        return wrapped

    def children(self) -> List[TerraformElement]:
        return [self.value] if self.value is not None else []

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"name": self.name}
        if self.value is not None:
            payload["type"] = self.value.KIND
            payload["value"] = self.value.to_dict()
        return self._attach_source(payload)


class GenericBlock(AttributeBlock):
    """
    Catch-all for less common block kinds (``moved``, ``import``, ``check``,
    ``terraform_data``, ``unknown``).

    Stores ``type`` + optional ``labels`` + ``properties`` + nested ``blocks``,
    matching the parser's :class:`~parse_hcl.types.GenericBlock` shape.
    """

    KIND = "generic"

    def __init__(
        self,
        type: str,
        labels: Optional[List[str]] = None,
        properties: Optional[Dict[str, Value]] = None,
        blocks: Optional[List[NestedBlock]] = None,
        source: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> None:
        super().__init__(properties=properties, source=source)
        self.type: str = type
        self.labels: List[str] = list(labels or [])
        self.blocks: List[NestedBlock] = []
        # ``kind`` records which document section (moved / import / ...) this
        # block came from, so the module can re-emit it under the right key.
        self.section_kind: str = kind or type
        for nb in blocks or []:
            self.add_block(nb)

    @classmethod
    def from_dict(cls, data: Dict[str, Any], *, kind: Optional[str] = None) -> "GenericBlock":
        instance = cls(
            type=str(data.get("type", kind or "unknown")),
            labels=list(data.get("labels") or []),
            properties=cls._hydrate_properties(data.get("properties")),
            source=data.get("source"),
            kind=kind,
        )
        for nb in data.get("blocks") or []:
            if isinstance(nb, dict):
                instance.add_block(NestedBlock.from_dict(nb))
        return instance

    def add_block(self, block: NestedBlock) -> NestedBlock:
        self.blocks.append(block)
        self._adopt(block)
        return block

    def remove_block(self, predicate: Any) -> Optional[NestedBlock]:
        for idx, block in enumerate(self.blocks):
            if (callable(predicate) and predicate(block)) or block.type == predicate:
                return self.blocks.pop(idx)
        return None

    def children(self) -> List[TerraformElement]:
        return [*self.properties.values(), *self.blocks]

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"type": self.type}
        if self.labels:
            payload["labels"] = list(self.labels)
        payload["properties"] = self._emit_properties()
        if self.blocks:
            payload["blocks"] = [b.to_dict() for b in self.blocks]
        return self._attach_source(payload)


__all__ = [
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
]
