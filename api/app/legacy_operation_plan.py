"""Deterministic, loss-detecting conversion of v1 notebook snapshots."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Iterator

from .asset_store import content_type
from .operation_protocol import LIBRARY_DOCUMENT_ID, OperationEnvelope
from .text_sequence import MAX_ATOMS_PER_OPERATION, validate_attributes

_NAMESPACE = uuid.UUID("7ca8e05b-3920-5db8-b9de-9b0dfa885c34")
_OBJECT_LISTS = ("inkStrokes", "textBlocks", "imageBlocks")


class LegacyConversionError(ValueError):
    """Unrepresentable v1 content: abort without changing either protocol."""


def stable_id(*parts: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, json.dumps(parts, ensure_ascii=True)))


def mapping(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise LegacyConversionError(f"Invalid {label}.")
    return value


def sequence(value: object, label: str) -> list:
    if not isinstance(value, list):
        raise LegacyConversionError(f"Invalid {label}.")
    return value


def string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise LegacyConversionError(f"Invalid {label}.")
    return value


def identity(value: object, label: str) -> str:
    value = string(value, label)
    if not 1 <= len(value) <= 128 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise LegacyConversionError(f"Invalid {label} identity.")
    return value


def finite(value: object, label: str, *, minimum=None, maximum=None) -> float | int:
    if (type(value) not in (int, float) or not math.isfinite(value) or
            (minimum is not None and value < minimum) or
            (maximum is not None and value > maximum)):
        raise LegacyConversionError(f"Invalid {label}.")
    return value



def positive(value: object, label: str) -> float | int:
    result = finite(value, label)
    if result <= 0:
        raise LegacyConversionError(f"Invalid {label}.")
    return result

def flag(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise LegacyConversionError(f"Invalid {label}.")
    return value


def color(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 0xffffffff:
        raise LegacyConversionError("Invalid ARGB color.")
    return value


def timestamp(value: object, label: str) -> str:
    value = string(value, label)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise LegacyConversionError(f"Invalid {label}.") from error
    if parsed.tzinfo is None:
        raise LegacyConversionError(f"{label} has no timezone.")
    return value


@dataclass(frozen=True)
class Asset:
    asset_id: str
    data: bytes
    media_type: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    def payload(self) -> dict:
        return {
            "assetId": self.asset_id, "sha256": self.digest,
            "sizeBytes": len(self.data), "mediaType": self.media_type,
        }


def decode_asset(asset_id: str, encoded: object, mime: object = None) -> Asset:
    if not isinstance(encoded, str) or not encoded:
        raise LegacyConversionError("Legacy asset has no inline bytes.")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise LegacyConversionError("Invalid legacy asset encoding.") from error
    actual = content_type(data[:16])
    if len(data) > 64 * 1024 * 1024:
        raise LegacyConversionError("Legacy asset exceeds the protocol size limit.")
    if mime is not None and mime != actual:
        raise LegacyConversionError("Legacy asset MIME differs from its bytes.")
    return Asset(asset_id, data, actual)


class Journal:
    def __init__(self, owner_id: uuid.UUID, document_id: str):
        self.document_id = identity(document_id, "document")
        self.actor = "v1-cutover:" + stable_id(str(owner_id), document_id)
        self.seq = 0
        self.previous = None

    def next(self, kind: str, payload=None, *, page=None, obj=None):
        self.seq += 1
        op_id = stable_id(self.actor, str(self.seq))
        raw = {
            "schemaVersion": 1, "operationId": op_id, "actorId": self.actor,
            "actorSeq": self.seq, "documentId": self.document_id,
            "type": kind, "payload": payload or {},
            "dependencies": [self.previous] if self.previous else [],
            "logicalClock": self.seq,
        }
        if page is not None:
            raw["pageId"] = page
        if obj is not None:
            raw["objectId"] = obj
        operation = OperationEnvelope.from_dict(raw)
        self.previous = op_id
        return operation


def collect_ids(notebook: dict) -> tuple[set[str], dict[str, str]]:
    pages: set[str] = set()
    objects: dict[str, str] = {}
    for raw in sequence(notebook.get("pages"), "legacy pages"):
        page = mapping(raw, "legacy page")
        page_id = identity(page.get("id"), "page")
        if page_id in pages:
            raise LegacyConversionError("Duplicate legacy page ID.")
        pages.add(page_id)
        for category in _OBJECT_LISTS:
            for raw_object in sequence(page.get(category, []), category):
                obj = identity(mapping(raw_object, category).get("id"), "object")
                if obj in objects:
                    raise LegacyConversionError("Duplicate legacy object ID.")
                objects[obj] = page_id
    return pages, objects


def text_steps(journal: Journal, page: str, obj: str, block: dict):
    plain = string(block.get("text"), "text")
    raw = block.get("deltaJson")
    if raw is None:
        delta = [{"insert": plain}]
    else:
        try:
            delta = sequence(json.loads(string(raw, "Quill Delta")), "Quill Delta")
        except json.JSONDecodeError as error:
            raise LegacyConversionError("Invalid Quill Delta JSON.") from error
    atoms: list[dict] = []
    assets: dict[str, Asset] = {}
    for entry in delta:
        item = mapping(entry, "Quill insertion")
        if "insert" not in item or set(item) - {"insert", "attributes"}:
            raise LegacyConversionError("Quill Delta is not a snapshot.")
        attrs = validate_attributes(
            {} if item.get("attributes") is None else item["attributes"]
        )
        insert = item["insert"]
        if isinstance(insert, str):
            for character in insert:
                atoms.append({"text": character, **({"attributes": attrs} if attrs else {})})
            continue
        embed = mapping(insert, "Quill embed")
        if len(embed) != 1:
            raise LegacyConversionError("Invalid Quill embed.")
        kind, value = next(iter(embed.items()))
        if kind not in ("image", "pdf") or not isinstance(value, str):
            raise LegacyConversionError("Unsupported Quill embed.")
        # Never fetch a URL or read a path found in an untrusted v1 document.
        if value.startswith("asset:"):
            ref = identity(value.removeprefix("asset:"), "embed asset")
        elif value.startswith("data:") and ";base64," in value:
            mime, data = value[5:].split(";base64,", 1)
            ref = stable_id(journal.document_id, obj, kind, hashlib.sha256(data.encode()).hexdigest())
            assets[ref] = decode_asset(ref, data, mime)
            if (kind == "pdf") != (mime == "application/pdf"):
                raise LegacyConversionError("Quill embed type differs from its bytes.")
        else:
            raise LegacyConversionError("Quill embed has no verifiable asset source.")
        atoms.append({
            "text": "\ufffc", "embed": {kind: "asset:" + ref},
            **({"attributes": attrs} if attrs else {}),
        })
    rendered = "".join(atom["text"] for atom in atoms)
    if rendered != plain and (raw is None or rendered != plain + "\n"):
        raise LegacyConversionError("Quill Delta does not match legacy plain text.")
    if len(atoms) > 50000:
        raise LegacyConversionError("Text exceeds the v2 projection limit.")
    seen = set()
    for atom in atoms:
        if "embed" not in atom:
            continue
        ref = next(iter(atom["embed"].values())).removeprefix("asset:")
        if ref in seen:
            continue
        seen.add(ref)
        if ref not in assets:
            raise LegacyConversionError("Existing Quill asset needs a verified migration source.")
        asset = assets[ref]
        yield asset
        yield journal.next("asset.attach", asset.payload(), page=page, obj=obj)
    anchor = None
    for start in range(0, len(atoms), MAX_ATOMS_PER_OPERATION):
        batch = [
            {"id": stable_id(journal.actor, obj, str(index)), **atoms[index]}
            for index in range(start, min(start + MAX_ATOMS_PER_OPERATION, len(atoms)))
        ]
        yield journal.next(
            "text.insert", {"afterAtomId": anchor, "atoms": batch}, page=page, obj=obj,
        )
        anchor = batch[-1]["id"]


def notebook_plan(
    owner_id: uuid.UUID, uid: str, snapshot: dict | None,
    *, historical_pages: Iterable[str] = (), historical_objects: dict[str, str] | None = None,
) -> Iterator[OperationEnvelope | Asset]:
    if uid == LIBRARY_DOCUMENT_ID:
        raise LegacyConversionError("Legacy notebook uses the reserved library UID.")
    journal = Journal(owner_id, uid)
    if snapshot is None:
        yield journal.next("document.delete")
        return
    if snapshot.get("uid") != uid:
        raise LegacyConversionError("Legacy notebook UID differs from its row.")
    live_pages, live_objects = collect_ids(snapshot)
    if not live_pages:
        raise LegacyConversionError("Legacy notebook has no pages.")
    title = string(snapshot.get("title"), "title")
    folder = string("Notes" if snapshot.get("folder") is None else snapshot["folder"], "folder")
    kind = snapshot.get("kind", 0)
    if not title or not folder or type(kind) is not int or kind not in (0, 1, 2):
        raise LegacyConversionError("Notebook title, folder or kind is unsupported.")
    yield journal.next("document.create", {"value": {
        "title": title, "folder": folder, "kindIndex": kind,
        "createdAt": timestamp(snapshot.get("createdAt"), "creation time"),
        "updatedAt": timestamp(snapshot.get("updatedAt"), "modification time"),
    }})
    for page in snapshot["pages"]:
        page_id = page["id"]
        yield journal.next("page.create", {"value": {
            "title": string(page.get("title"), "page title"),
            "isBookmarked": flag(page.get("isBookmarked", False), "bookmark"),
        }}, obj=page_id)
        for category in _OBJECT_LISTS:
            for index, block in enumerate(page.get(category, [])):
                obj = block["id"]
                if index >= 1000000:
                    raise LegacyConversionError("Object order exceeds the v2 limit.")
                value = {"sortIndex": index}
                if category == "inkStrokes":
                    tool = block.get("tool")
                    if type(tool) is not int or tool not in {0, 1, 4, 5, 6, 7, 8, 9, 10, 13}:
                        raise LegacyConversionError("Unsupported legacy ink tool.")
                    points = sequence(block.get("points"), "ink points")
                    if not 1 <= len(points) <= 10000:
                        raise LegacyConversionError("Ink point count exceeds v2 bounds.")
                    value.update(
                        objectType="stroke", colorValue=color(block.get("color")),
                        width=positive(block.get("width"), "ink width"),
                        toolIndex=tool,
                        points=[
                            [finite(point.get(key), key) for key in ("dx", "dy", "pressure")]
                            for raw in points
                            for point in (mapping(raw, "ink point"),)
                        ],
                    )
                    yield journal.next("stroke.create", {"value": value}, page=page_id, obj=obj)
                    continue
                for key in ("dx", "dy"):
                    value[key] = finite(block.get(key), key)
                value["width"] = positive(block.get("width"), "width")
                value["rotation"] = finite(block.get("rotation", 0), "rotation")
                if category == "textBlocks":
                    value.update(
                        objectType="text", colorValue=color(block.get("color")),
                        fontSize=positive(block.get("fontSize"), "font size"),
                    )
                    yield journal.next("object.create", {"value": value}, page=page_id, obj=obj)
                    yield from text_steps(journal, page_id, obj, block)
                    continue
                asset = decode_asset(stable_id(uid, obj, "asset"), block.get("bytes"), block.get("imageMime"))
                value.update(objectType="image", assetId=asset.asset_id,
                             height=positive(block.get("height"), "image height"))
                for key, default in (("cropLeft", 0), ("cropTop", 0),
                                     ("cropRight", 1), ("cropBottom", 1)):
                    value[key] = finite(block.get(key, default), key, minimum=0, maximum=1)
                yield asset
                yield journal.next("object.create", {"value": value}, page=page_id, obj=obj)
                yield journal.next("asset.attach", asset.payload(), page=page_id, obj=obj)
    # IDs of moved live objects must not receive a tombstone on their old page.
    for obj, page_id in sorted((historical_objects or {}).items()):
        if obj not in live_objects:
            yield journal.next("object.delete", page=page_id, obj=obj)
    for page_id in sorted(set(historical_pages) - live_pages):
        yield journal.next("page.delete", obj=page_id)


def library_plan(owner_id: uuid.UUID, value: dict) -> Iterator[OperationEnvelope]:
    colors = sequence(value.get("recentCoverColors", []), "recent folder colors")
    for item in colors:
        color(item)
    journal = Journal(owner_id, LIBRARY_DOCUMENT_ID)
    paths = {string(path, "folder path") for path in sequence(value.get("folders", []), "folders")}
    covers = mapping(value.get("covers", {}), "folder covers")
    paths.update(covers)
    for path in list(paths):
        parts = path.split("/")
        if any(not part or part.strip() != part for part in parts):
            raise LegacyConversionError("Unsupported legacy folder path.")
        paths.update("/".join(parts[:index]) for index in range(1, len(parts)))
    for path in sorted(paths, key=lambda item: (item.count("/"), item)):
        parent, _, name = path.rpartition("/")
        properties = {"name": name, "parentId": stable_id(str(owner_id), parent) if parent else None}
        if path in covers:
            properties["cover"] = mapping(covers[path], "folder cover")
        yield journal.next("folder.create", {"value": properties}, obj=stable_id(str(owner_id), path))
    used: set[str] = set()
    for planner, tasks in sorted(mapping(value.get("plannerTasks", {}), "planner tasks").items()):
        identity(planner, "planner")
        for index, task in enumerate(sequence(tasks, "tasks")):
            task = mapping(task, "task")
            obj = identity(task.get("id"), "task")
            if obj in used:
                raise LegacyConversionError("Duplicate legacy task ID.")
            used.add(obj)
            deadline = task.get("deadline")
            if deadline is not None:
                try:
                    datetime.fromisoformat(string(deadline, "task deadline").replace("Z", "+00:00"))
                except ValueError as error:
                    raise LegacyConversionError("Invalid task deadline.") from error
            yield journal.next("task.create", {"value": {
                "plannerUid": planner, "title": string(task.get("title"), "task title"),
                "completed": flag(task.get("completed", False), "task completion"),
                "deadline": deadline,
                "deadlineHasTime": flag(task.get("deadlineHasTime", False), "task deadline mode"),
                "sortIndex": index,
            }}, obj=obj)
