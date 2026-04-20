"""
Abstract base classes for the parse-hcl OOP model.

The OOP layer wraps the dictionary-based ``TerraformDocument`` produced by the
parser in a tree of strongly-typed Python objects (modules, blocks, values).
Every node descends from :class:`TerraformElement` and exposes a small uniform
interface for traversal (``children``/``walk``), mutation (``parent``), and
(de)serialization to/from the dict representation.

The ``raw`` field carried by parsed dicts is intentionally dropped: it is a
lossless echo of the original HCL text and is not needed when reverse-engineering
``.tf`` from JSON. Any data needed by ``to_tf`` is reconstructed from structured
fields (``type`` / ``value`` / ``properties`` / ...).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Iterator, List, Optional


class TerraformElement(ABC):
    """
    Abstract base class for every node in the OOP terraform tree.

    Each element keeps a back-pointer to its ``parent`` (set by container
    classes when the element is added) and provides:

    - :meth:`to_dict` to serialize back to the dict shape consumed by the
      JSON / TF writers.
    - :meth:`children` and :meth:`walk` for tree traversal.

    The ``from_dict`` factory is implemented by each concrete subclass.
    """

    parent: Optional["TerraformElement"]

    def __init__(self) -> None:
        self.parent = None

    @abstractmethod
    def to_dict(self) -> Any:
        """Serialize this element back to the parser dict shape (no ``raw``)."""

    def children(self) -> List["TerraformElement"]:
        """
        Return direct children of this element.

        Default implementation returns an empty list. Container subclasses
        (modules, blocks with attributes, ``ObjectValue`` / ``ArrayValue``)
        override this to expose their structural children.
        """
        return []

    def walk(self) -> Iterator["TerraformElement"]:
        """Depth-first traversal of this element and all descendants."""
        yield self
        for child in self.children():
            yield from child.walk()

    def _adopt(self, child: Optional["TerraformElement"]) -> None:
        """Attach ``child`` to ``self`` as its parent (no-op for ``None``)."""
        if isinstance(child, TerraformElement):
            child.parent = self


def drop_raw(data: Any) -> Any:
    """
    Recursively drop ``raw`` keys from a dict / list structure.

    Used by callers that want a clean payload (no echoed source text). The OOP
    serializers already exclude ``raw`` themselves; this helper is exposed for
    convenience when working with externally-produced dicts.
    """
    if isinstance(data, dict):
        return {k: drop_raw(v) for k, v in data.items() if k != "raw"}
    if isinstance(data, list):
        return [drop_raw(item) for item in data]
    return data


def prune_empty(data: Any) -> Any:
    """
    Recursively drop empty containers (``None``, ``[]``, ``{}``) from ``data``.

    Mirrors the behaviour of ``parse_hcl.utils.serialization.serializer._prune_value``
    so OOP-produced dicts compare cleanly with parser output.
    """
    if data is None:
        return None
    if isinstance(data, list):
        items = [item for item in (prune_empty(x) for x in data) if item not in (None, [], {}, ())]
        return items or None
    if isinstance(data, dict):
        out: Dict[str, Any] = {}
        for key, val in data.items():
            cleaned = prune_empty(val)
            if cleaned is None or cleaned == [] or cleaned == {}:
                continue
            out[key] = cleaned
        return out or None
    return data


__all__ = ["TerraformElement", "drop_raw", "prune_empty"]
