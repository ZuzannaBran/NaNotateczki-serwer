"""Deterministic text CRDT with per-attribute Quill formatting.

Inserts use immutable atom IDs and predecessor links. text.format updates
independent attribute keys by operation precedence; deletion keeps anchors.
"""
from __future__ import annotations

import math
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
MAX_ATTRIBUTES = 32


class TextSequenceError(ValueError):
    pass


def _atom_id(value: object) -> bool:
    return isinstance(value, str) and _ATOM_ID.fullmatch(value) is not None


def validate_attributes(value: object, *, allow_null: bool = False) -> dict:
    if not isinstance(value, dict) or len(value) > MAX_ATTRIBUTES:
        raise TextSequenceError("Invalid Quill attributes.")
    for key, item in value.items():
        if not isinstance(key, str) or not 1 <= len(key) <= 64:
            raise TextSequenceError("Invalid Quill attribute name.")
        if item is None and allow_null:
            continue
        if type(item) not in (str, bool, int, float):
            raise TextSequenceError("Invalid Quill attribute value.")
        if isinstance(item, str) and len(item) > 2048:
            raise TextSequenceError("Quill attribute is too long.")
        if isinstance(item, (float, int)) and not math.isfinite(item):
            raise TextSequenceError("Non-finite Quill attribute.")
    return value


def _embed_asset_id(value: object, text: object) -> str:
    """Require canonical Quill image/PDF references to attached assets."""
    if text != "\ufffc" or not isinstance(value, dict) or len(value) != 1:
        raise TextSequenceError("Invalid Quill embed atom.")
    kind, reference = next(iter(value.items()))
    if kind not in ("image", "pdf") or not isinstance(reference, str):
        raise TextSequenceError("Unsupported Quill embed.")
    if not reference.startswith("asset:"):
        raise TextSequenceError("Quill embed requires an asset reference.")
    asset_id = reference[6:]
    if (not 1 <= len(asset_id) <= 128
            or any(ord(char) < 32 or ord(char) == 127 for char in asset_id)):
        raise TextSequenceError("Invalid embedded asset ID.")
    return asset_id


def _valid_rank(rank: object, *, length: int) -> bool:
    return (
        isinstance(rank, list)
        and len(rank) == length
        and all(type(rank[i]) is int for i in range(0, length, 2))
        and all(isinstance(rank[i], str) for i in range(1, length, 2))
    )


def _visible_ids(atoms: dict[str, dict]) -> list[str]:
    children: dict[str | None, list[str]] = {}
    for atom_id, atom in atoms.items():
        if not _atom_id(atom_id) or not isinstance(atom, dict):
            raise TextSequenceError("Invalid text graph node.")
        parent = atom.get("after")
        if parent is not None and (
            not _atom_id(parent) or parent not in atoms
        ):
            raise TextSequenceError("Dangling text atom anchor.")
        if parent == atom_id:
            raise TextSequenceError("Self-referential text atom.")
        rank = atom.get("order")
        if not _valid_rank(rank, length=5):
            raise TextSequenceError("Invalid atom rank.")
        if type(atom.get("deleted")) is not bool or not isinstance(
            atom.get("text"), str
        ):
            raise TextSequenceError("Invalid atom fields.")
        validate_attributes(atom.get("attributes", {}))
        versions = atom.get("attributeVersions", {})
        if not isinstance(versions, dict) or any(
            not isinstance(key, str) or not _valid_rank(version, length=4)
            for key, version in versions.items()
        ):
            raise TextSequenceError("Invalid text attribute precedence.")
        if "embed" in atom:
            _embed_asset_id(atom["embed"], atom["text"])
        elif "\ufffc" in atom["text"]:
            raise TextSequenceError("Unlinked Quill object replacement.")
        children.setdefault(parent, []).append(atom_id)
    for siblings in children.values():
        siblings.sort(
            key=lambda item: (atoms[item]["order"], item), reverse=True
        )
    result: list[str] = []
    seen: set[str] = set()
    stack = list(reversed(children.get(None, [])))
    while stack:
        atom_id = stack.pop()
        if atom_id in seen:
            raise TextSequenceError("Cycle in text atom graph.")
        seen.add(atom_id)
        if not atoms[atom_id]["deleted"]:
            result.append(atom_id)
        stack.extend(reversed(children.get(atom_id, [])))
    if len(seen) != len(atoms):
        raise TextSequenceError("Unreachable text atom graph.")
    return result


def render_text(atoms: dict[str, dict]) -> str:
    return "".join(atoms[item]["text"] for item in _visible_ids(atoms))


def render_delta(atoms: dict[str, dict]) -> list[dict]:
    """Coalesce adjacent spans carrying identical Quill attributes."""
    output: list[dict] = []
    for atom_id in _visible_ids(atoms):
        atom = atoms[atom_id]
        attributes = atom.get("attributes", {})
        if "embed" in atom:
            entry = {"insert": dict(atom["embed"])}
            if attributes:
                entry["attributes"] = dict(attributes)
            output.append(entry)
        elif (
            output and isinstance(output[-1]["insert"], str)
            and output[-1].get("attributes", {}) == attributes
        ):
            output[-1]["insert"] += atom["text"]
        else:
            entry = {"insert": atom["text"]}
            if attributes:
                entry["attributes"] = dict(attributes)
            output.append(entry)
    return output


def apply_text_operation(node: dict[str, Any], op: OperationEnvelope) -> None:
    payload = op.to_dict()["payload"]
    properties = node.setdefault("properties", {})
    if "textAtoms" not in node and properties.get("text", "") not in ("", None):
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
            (anchor is not None and not _atom_id(anchor))
            or (anchor is not None and anchor not in atoms)
            or not isinstance(values, list)
            or not 1 <= len(values) <= MAX_ATOMS_PER_OPERATION
        ):
            raise TextSequenceError("Invalid text insertion.")
        ids = [item.get("id") if isinstance(item, dict) else None
               for item in values]
        if any(not _atom_id(item) for item in ids):
            raise TextSequenceError("Invalid text atom ID.")
        if len(set(ids)) != len(ids) or any(item in atoms for item in ids):
            raise TextSequenceError("Duplicate or reused text atom ID.")
        rank = list(precedence(op))
        for index, item in enumerate(values):
            if (
                not isinstance(item, dict)
                or not {"id", "text"} <= set(item)
                or set(item) - {"id", "text", "attributes", "embed"}
                or not isinstance(item["text"], str)
                or not 1 <= len(item["text"]) <= MAX_ATOM_TEXT
                or any(0xD800 <= ord(char) <= 0xDFFF for char in item["text"])
            ):
                raise TextSequenceError("Invalid text atom.")
            attributes = validate_attributes(item.get("attributes", {}))
            if "embed" in item:
                asset_id = _embed_asset_id(item["embed"], item["text"])
                attachment = node.get("assets", {}).get(asset_id)
                expected_mime = (
                    "application/pdf" if "pdf" in item["embed"] else None
                )
                if expected_mime is None:
                    allowed_mimes = {"image/png", "image/jpeg", "image/webp"}
                else:
                    allowed_mimes = {expected_mime}
                if (
                    not isinstance(attachment, dict)
                    or attachment.get("mediaType") not in allowed_mimes
                ):
                    raise TextSequenceError(
                        "Quill embed requires a matching attached asset."
                    )
            elif "\ufffc" in item["text"]:
                raise TextSequenceError("Unlinked Quill object replacement.")
            atoms[item["id"]] = {
                "after": anchor, "text": item["text"],
                "deleted": False, "order": [*rank, index],
                "attributes": dict(attributes),
                "attributeVersions": {key: rank for key in attributes},
                **({"embed": dict(item["embed"])} if "embed" in item else {}),
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
    elif op.kind == "text.format":
        if set(payload) != {"atomIds", "attributes"}:
            raise TextSequenceError("Invalid text.format payload.")
        ids = payload["atomIds"]
        changes = validate_attributes(
            payload["attributes"], allow_null=True
        )
        if (
            not changes or not isinstance(ids, list)
            or not 1 <= len(ids) <= MAX_ATOMS_PER_OPERATION
            or any(not _atom_id(item) for item in ids)
            or len(set(ids)) != len(ids)
            or any(item not in atoms for item in ids)
        ):
            raise TextSequenceError("Invalid text.format targets.")
        rank = list(precedence(op))
        for atom_id in ids:
            atom = dict(atoms[atom_id])
            attributes = dict(atom.get("attributes", {}))
            stored_versions = atom.get("attributeVersions", {})
            if not isinstance(stored_versions, dict) or any(
                not isinstance(key, str)
                or not _valid_rank(version, length=4)
                for key, version in stored_versions.items()
            ):
                raise TextSequenceError("Invalid text attribute precedence.")
            versions = dict(stored_versions)
            for key, value in changes.items():
                if key not in versions or rank > versions[key]:
                    if value is None:
                        attributes.pop(key, None)
                    else:
                        attributes[key] = value
                    versions[key] = rank
            atom["attributes"] = attributes
            atom["attributeVersions"] = versions
            atoms[atom_id] = atom
    else:
        raise TextSequenceError("Unsupported text operation.")

    properties["text"] = render_text(atoms)
    properties["delta"] = render_delta(atoms)
    node["textAtoms"] = atoms
