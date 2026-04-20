"""
OOP wrappers for HCL values (literal / object / array / expression).

Every value is a tree node. ``ObjectValue`` and ``ArrayValue`` recursively own
child :class:`Value` instances, so a deeply nested ``map(object({...}))`` default
materializes as a navigable Python tree that supports in-place edits via the
class methods below.

Use :func:`Value.from_dict` (a polymorphic factory on the base) to deserialize
any value dict produced by the parser, and :meth:`Value.to_dict` to convert it
back. ``raw`` is dropped on serialization.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any, Dict, Iterable, Iterator, List, Optional, Union

from .base import TerraformElement

PrimitiveValue = Union[str, int, float, bool, None]
"""Python literal types accepted by :class:`LiteralValue`."""


class Value(TerraformElement):
    """
    Abstract base for all HCL value nodes.

    Concrete subclasses are :class:`LiteralValue`, :class:`ObjectValue`,
    :class:`ArrayValue`, and :class:`ExpressionValue`. Use :meth:`Value.from_dict`
    to dispatch on the ``type`` discriminator carried by parser output.
    """

    #: Discriminator written under the ``type`` key when serialized.
    KIND: str = ""

    @staticmethod
    def from_dict(data: Any) -> Optional["Value"]:
        """
        Build a typed :class:`Value` subclass from a parser dict.

        Returns ``None`` for ``None`` input. Plain Python primitives are wrapped
        in a :class:`LiteralValue`. Dicts without a recognized ``type`` field
        are interpreted as a literal carrying the dict itself.
        """
        if data is None:
            return None
        if isinstance(data, Value):
            return data
        if isinstance(data, (str, int, float, bool)):
            return LiteralValue(data)
        if isinstance(data, list):
            return ArrayValue.from_dict({"type": "array", "value": data})
        if isinstance(data, dict):
            kind = data.get("type")
            if kind == "literal":
                return LiteralValue.from_dict(data)
            if kind == "object":
                return ObjectValue.from_dict(data)
            if kind == "array":
                return ArrayValue.from_dict(data)
            if kind == "expression":
                return ExpressionValue.from_dict(data)
            # Fallback: dict without recognized discriminator -> wrap as literal.
            return LiteralValue(data)
        return LiteralValue(data)

    @abstractmethod
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to ``{type: ..., value/...}`` (no ``raw``)."""

    def to_python(self) -> Any:
        """
        Best-effort conversion to a plain Python value (for read-only use).

        Expression nodes can't always be reduced; they return the source text
        when available, otherwise ``None``.
        """
        return None  # overridden by concrete subclasses


class LiteralValue(Value):
    """A scalar value: string, number, bool, or null."""

    KIND = "literal"

    def __init__(self, value: PrimitiveValue) -> None:
        super().__init__()
        self.value: PrimitiveValue = value

    @classmethod
    def of(cls, value: PrimitiveValue) -> "LiteralValue":
        """Convenience constructor that mirrors ``Value.from_dict`` for primitives."""
        return cls(value)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LiteralValue":
        return cls(data.get("value"))

    def set(self, value: PrimitiveValue) -> "LiteralValue":
        """Replace the underlying scalar in place and return ``self``."""
        self.value = value
        return self

    def to_dict(self) -> Dict[str, Any]:
        return {"type": self.KIND, "value": self.value}

    def to_python(self) -> PrimitiveValue:
        return self.value

    def __repr__(self) -> str:
        return f"LiteralValue({self.value!r})"


class ObjectValue(Value):
    """
    A ``{ key = value, ... }`` HCL object with nested :class:`Value` entries.

    Supports dict-like ``get`` / ``set`` / ``remove`` / ``keys`` and can hold
    optional ``references`` for downstream graph use.
    """

    KIND = "object"

    def __init__(
        self,
        entries: Optional[Dict[str, Value]] = None,
        references: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        super().__init__()
        self.entries: Dict[str, Value] = {}
        self.references: List[Dict[str, Any]] = list(references or [])
        for key, val in (entries or {}).items():
            self.set(key, val)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ObjectValue":
        raw_entries = data.get("value") or {}
        entries: Dict[str, Value] = {}
        if isinstance(raw_entries, dict):
            for key, val in raw_entries.items():
                v = Value.from_dict(val)
                if v is not None:
                    entries[key] = v
        return cls(entries=entries, references=data.get("references"))

    def keys(self) -> List[str]:
        return list(self.entries.keys())

    def get(self, key: str) -> Optional[Value]:
        return self.entries.get(key)

    def set(self, key: str, value: Any) -> Value:
        """
        Insert or replace an entry. Plain Python values are auto-wrapped via
        :meth:`Value.from_dict`. Returns the stored :class:`Value` instance.
        """
        wrapped = value if isinstance(value, Value) else Value.from_dict(value)
        if wrapped is None:
            wrapped = LiteralValue(None)
        self.entries[key] = wrapped
        self._adopt(wrapped)
        return wrapped

    def remove(self, key: str) -> Optional[Value]:
        return self.entries.pop(key, None)

    def update(self, mapping: Dict[str, Any]) -> "ObjectValue":
        for k, v in mapping.items():
            self.set(k, v)
        return self

    def children(self) -> List[TerraformElement]:
        return list(self.entries.values())

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "type": self.KIND,
            "value": {k: v.to_dict() for k, v in self.entries.items()},
        }
        if self.references:
            out["references"] = list(self.references)
        return out

    def to_python(self) -> Dict[str, Any]:
        return {k: v.to_python() for k, v in self.entries.items()}

    def __contains__(self, key: object) -> bool:
        return key in self.entries

    def __iter__(self) -> Iterator[str]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def __repr__(self) -> str:
        return f"ObjectValue({list(self.entries)!r})"


class ArrayValue(Value):
    """A ``[a, b, ...]`` HCL list/tuple of nested :class:`Value` elements."""

    KIND = "array"

    def __init__(
        self,
        elements: Optional[Iterable[Any]] = None,
        references: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        super().__init__()
        self.elements: List[Value] = []
        self.references: List[Dict[str, Any]] = list(references or [])
        for item in elements or []:
            self.append(item)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ArrayValue":
        items = data.get("value") or []
        if not isinstance(items, list):
            items = []
        return cls(elements=items, references=data.get("references"))

    def append(self, item: Any) -> Value:
        wrapped = item if isinstance(item, Value) else Value.from_dict(item)
        if wrapped is None:
            wrapped = LiteralValue(None)
        self.elements.append(wrapped)
        self._adopt(wrapped)
        return wrapped

    def extend(self, items: Iterable[Any]) -> "ArrayValue":
        for item in items:
            self.append(item)
        return self

    def insert(self, index: int, item: Any) -> Value:
        wrapped = item if isinstance(item, Value) else Value.from_dict(item)
        if wrapped is None:
            wrapped = LiteralValue(None)
        self.elements.insert(index, wrapped)
        self._adopt(wrapped)
        return wrapped

    def remove_at(self, index: int) -> Value:
        return self.elements.pop(index)

    def get_at(self, index: int) -> Value:
        return self.elements[index]

    def set_at(self, index: int, item: Any) -> Value:
        wrapped = item if isinstance(item, Value) else Value.from_dict(item)
        if wrapped is None:
            wrapped = LiteralValue(None)
        self.elements[index] = wrapped
        self._adopt(wrapped)
        return wrapped

    def children(self) -> List[TerraformElement]:
        return list(self.elements)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "type": self.KIND,
            "value": [v.to_dict() for v in self.elements],
        }
        if self.references:
            out["references"] = list(self.references)
        return out

    def to_python(self) -> List[Any]:
        return [v.to_python() for v in self.elements]

    def __getitem__(self, index: int) -> Value:
        return self.elements[index]

    def __setitem__(self, index: int, item: Any) -> None:
        self.set_at(index, item)

    def __len__(self) -> int:
        return len(self.elements)

    def __iter__(self) -> Iterator[Value]:
        return iter(self.elements)

    def __repr__(self) -> str:
        return f"ArrayValue(len={len(self.elements)})"


class ExpressionValue(Value):
    """
    An HCL expression that the parser couldn't fully reduce (traversal,
    template, function call, conditional, splat, ...).

    The textual form is preserved in :attr:`expression` and emitted back
    verbatim by the writer. Use :meth:`set_expression` to replace it.
    """

    KIND = "expression"

    def __init__(
        self,
        expression: str = "",
        kind: str = "unknown",
        references: Optional[List[Dict[str, Any]]] = None,
        name: Optional[str] = None,
        attributes: Optional[List[Any]] = None,
        trailer: Optional[str] = None,
        parsed: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__()
        self.expression: str = expression
        self.kind: str = kind
        self.references: List[Dict[str, Any]] = list(references or [])
        self.name: Optional[str] = name
        self.attributes: List[Value] = []
        self.trailer: Optional[str] = trailer
        self.parsed: Optional[Dict[str, Any]] = parsed
        for arg in attributes or []:
            self.add_argument(arg)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExpressionValue":
        attrs_raw = data.get("attributes") or []
        instance = cls(
            expression=str(data.get("raw") or ""),
            kind=str(data.get("kind") or "unknown"),
            references=data.get("references"),
            name=data.get("name"),
            trailer=data.get("trailer"),
            parsed=data.get("parsed"),
        )
        for arg in attrs_raw:
            instance.add_argument(arg)
        return instance

    def set_expression(self, expression: str, kind: Optional[str] = None) -> "ExpressionValue":
        """Replace the raw expression text (and optionally the ``kind``)."""
        self.expression = expression
        if kind is not None:
            self.kind = kind
        return self

    def add_argument(self, arg: Any) -> Value:
        """Append a positional argument (only meaningful for ``function_call``)."""
        wrapped = arg if isinstance(arg, Value) else Value.from_dict(arg)
        if wrapped is None:
            wrapped = LiteralValue(None)
        self.attributes.append(wrapped)
        self._adopt(wrapped)
        return wrapped

    def children(self) -> List[TerraformElement]:
        return list(self.attributes)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "type": self.KIND,
            "kind": self.kind,
            # The TF writer relies on ``raw`` to re-emit expressions because the
            # classifier doesn't fully decompose them; we therefore keep the
            # textual form here even though raw is otherwise dropped.
            "raw": self.expression,
        }
        if self.references:
            out["references"] = list(self.references)
        if self.name is not None:
            out["name"] = self.name
        if self.attributes:
            out["attributes"] = [v.to_dict() for v in self.attributes]
        if self.trailer:
            out["trailer"] = self.trailer
        if self.parsed is not None:
            out["parsed"] = self.parsed
        return out

    def to_python(self) -> str:
        return self.expression

    def __repr__(self) -> str:
        return f"ExpressionValue({self.expression!r}, kind={self.kind!r})"


def coerce_value(value: Any) -> Optional[Value]:
    """
    Public alias for :meth:`Value.from_dict`.

    Useful as a free function when wiring user-supplied data into the OOP tree.
    """
    return Value.from_dict(value)


__all__ = [
    "Value",
    "LiteralValue",
    "ObjectValue",
    "ArrayValue",
    "ExpressionValue",
    "PrimitiveValue",
    "coerce_value",
]
