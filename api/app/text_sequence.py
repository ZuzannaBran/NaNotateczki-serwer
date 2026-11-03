"""Deterministic sequence CRDT for v2 text objects.

Atoms have immutable IDs and anchors. Concurrent siblings are ordered by the
causal rank of their insertion, independent of HTTP arrival order.
Tombstones remain so late inserts anchored to deleted atoms can be placed.
"""
from __future__ import annotations

import re
from typing import Any

from .operation_ordering import precedence
from .operation_protocol import OperationEnvelope

_ATOM_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
MAX_ATOMS_PER_OPERATION = 256
MAX_ATOM_TEXT = 1024


class TextSequenceError(ValueError):
    pass


def _atom_id(value: object) -> bool:
    return isinstance(value, str) and _ATOM_ID.fullmatch(value) is not None


def apply_text_operation(node: dict[str, Any], op: OperationEnvelope) -> None:
    """Mutate a copied projection; never modify the live ORM JSON directly."""
    payload = op.to_dict()["payload"]
    properties = node.setdefault("properties", {})
    if "textAtoms" not in node:
        if properties.get("text", "") not in ("", None):
            raise TextSequenceError("Legacy text needs explicit migration.")
    original = node.get("textAtoms", {})
    if not isinstance(original, dict):
        raise TextSequenceError("Corrupt text atom map.")
    atoms = dict(original)

    if op.kind == "text.insert":
        if set(payload) != {"afterAtomId", "atoms"}:
            raise TextSequenceError("Invalid text.insert payload.")
        anchor = payload["afterAtomId"]
        values = payload["atoms"]
        if (
            anchor is not None and not _atom_id(anchor)
            or not isinstance(values, list)
            or not 1 <= len(values) <= MAX_ATOMS_PER_OPERATION
        ):
            raise TextSequenceError("Invalid text insertion.")
        if anchor is not None and anchor not in atoms:
            raise TextSequenceError("Unknown text insertion anchor.")
        ids = [item.get("id") if isinstance(item, dict) else None
               for item in values]
        if any(not _atom_id(item) for item in ids):
            raise TextSequenceError("Invalid text atom ID.")
        if len(set(ids)) != len(ids) or any(item in atoms for item in ids):
            raise TextSequenceError("Duplicate or reused text atom ID.")
        for item in values:
            if (
                not isinstance(item, dict)
                or set(item) != {"id", "text"}
                or not isinstance(item["text"], str)
                or not 1 <= len(item["text"]) <= MAX_ATOM_TEXT
                or any(0xD800 <= ord(char) <= 0xDFFF for char in item["text"])
            ):
                raise TextSequenceError("Invalid text atom.")
        rank = list(precedence(op))
        for index, item in enumerate(values):
            atoms[item["id"]] = {
                "after": anchor, "text": item["text"],
                "deleted": False, "order": [*rank, index],
            }
            anchor = item["id"]
    elif op.kind == "text.delete":
        if set(payload) != {"atomIds"}:
            raise TextSequenceError("Invalid text.delete payload.")
        ids = payload["atomIds"]
        if (
            not isinstance(ids, list)
            or not 1 <= len(ids) <= MAX_ATOMS_PER_OPERATION
            or any(not _atom_id(item) for item in ids)
            or len(set(ids)) != len(ids)
            or any(item not in atoms for item in ids)
        ):
            raise TextSequenceError("Invalid or missing text atom ID.")
        for item in ids:
            atoms[item] = {**atoms[item], "deleted": True}
    else:
        raise TextSequenceError("Unsupported text operation.")

    visible = render_text(atoms)
    node["textAtoms"] = atoms
    properties["text"] = visible


def render_text(atoms: dict[str, dict]) -> str:
    """Iterative RGA traversal; deleted nodes still anchor their descendants."""
    children: dict[str | None, list[str]] = {}
    for atom_id, atom in atoms.items():
        if not _atom_id(atom_id) or not isinstance(atom, dict):
            raise TextSequenceError("Invalid text graph node.")
        parent = atom.get("after")
        if parent is not None and parent not in atoms:
            raise TextSequenceError("Dangling text atom anchor.")
        if parent == atom_id:
            raise TextSequenceError("Self-referential text atom.")
        children.setdefault(parent, []).append(atom_id)
    for siblings in children.values():
        siblings.sort(key=lambda item: (atoms[item]["order"], item), reverse=True)

    output: list[str] = []
    visited: set[str] = set()
    stack = list(reversed(children.get(None, [])))
    while stack:
        atom_id = stack.pop()
        if atom_id in visited:
            raise TextSequenceError("Cycle in text atom graph.")
        visited.add(atom_id)
        atom = atoms[atom_id]
        if not atom["deleted"]:
            output.append(atom["text"])
        stack.extend(reversed(children.get(atom_id, [])))
    if len(visited) != len(atoms):
        raise TextSequenceError("Unreachable text atom graph.")
    return "".join(output)
