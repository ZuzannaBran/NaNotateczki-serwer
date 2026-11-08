"""Deterministic, JSON-safe v2 projection with permanent entity tombstones.

Text atoms use deterministic RGA ordering. Inline Quill image/PDF embeds
reference only separately uploaded, verified and attached assets.
"""
from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from .operation_ordering import precedence
from .operation_protocol import LIBRARY_DOCUMENT_ID, OperationEnvelope
from .text_sequence import (
    TextSequenceError, apply_text_operation, render_delta,
)


class ProjectionError(ValueError):
    pass


def _properties(operation: OperationEnvelope, *, create: bool) -> dict:
    payload = operation.to_dict()["payload"]
    name = "value" if create else "properties"
    fields = payload.get(name)
    if set(payload) != {name} or not isinstance(fields, dict) or (
        not create and not fields
    ):
        raise ProjectionError(f"Expected exactly one {name} object.")
    if not create and (
        "id" in fields or "uid" in fields or (
            operation.kind.startswith("object.") and (
                "objectType" in fields or "textAtoms" in fields
            )
        )
    ):
        raise ProjectionError("Identity and CRDT internals cannot be changed.")
    if any(not isinstance(key, str) or not 1 <= len(key) <= 128 for key in fields):
        raise ProjectionError("Invalid property name.")
    if create and "id" in fields:
        expected = operation.object_id or operation.document_id
        if fields["id"] != expected:
            raise ProjectionError("Create payload ID does not match target.")
    if create and "uid" in fields and fields["uid"] != operation.document_id:
        raise ProjectionError("Create payload UID does not match document.")
    return fields


def _new_node() -> dict[str, Any]:
    return {"properties": {}, "versions": {}}


def _merge(node: dict, fields: Mapping, op: OperationEnvelope) -> None:
    rank = list(precedence(op))
    versions = node.setdefault("versions", {})
    properties = node.setdefault("properties", {})
    for key, value in fields.items():
        old = versions.get(key)
        if old is None or rank > old:
            versions[key] = rank
            properties[key] = value


def _validate_folder_graph(folders: dict) -> None:
    """Reject orphan parents and cycles after deterministic property merges."""
    checked: set[str] = set()
    for folder_id in folders:
        current = folder_id
        visiting: set[str] = set()
        while current is not None and current not in checked:
            if current in visiting:
                raise ProjectionError("Folder move creates a cycle.")
            visiting.add(current)
            node = folders.get(current)
            if node is None:
                raise ProjectionError("Folder parent does not exist.")
            properties = node.get("properties", {})
            parent = properties.get("parentId")
            if parent is not None and (
                not isinstance(parent, str) or not parent
            ):
                raise ProjectionError("Invalid folder parent ID.")
            current = parent
        checked.update(visiting)


def _remember_asset_receipt(
    current: dict, pages: dict, asset_id: str, receipt: dict,
) -> None:
    """Preserve the document-wide asset binding after detach and delete."""
    receipts = current.setdefault("assetReceipts", {})
    if not isinstance(receipts, dict):
        raise ProjectionError("Corrupt persisted asset receipts.")
    known = receipts.get(asset_id)
    if known is not None and known != receipt:
        raise ProjectionError("Asset ID was already bound to other bytes.")
    if known is not None:
        return

    # Earlier snapshots lack the receipt ledger. Existing attachments
    # can establish their identity, but detached old IDs cannot be trusted.
    for page in pages.values():
        for node in page.get("objects", {}).values():
            previous = node.get("assets", {}).get(asset_id)
            if previous is not None and previous != receipt:
                raise ProjectionError("Asset ID was already bound to other bytes.")
            if previous is None and asset_id in node.get("assetVersions", {}):
                raise ProjectionError("Asset ID has no durable receipt.")
    receipts[asset_id] = dict(receipt)


def apply_operation(
    current: dict | None,
    op: OperationEnvelope,
    tombstones: set[tuple[str, str, str]],
    *,
    verified_assets: frozenset[str] = frozenset(),
) -> tuple[dict | None, tuple[str, str, str] | None]:
    """Return updated projection and optional newly permanent tombstone.

    The caller serializes writers, persists the projection and tombstone,
    and appends the operation in ONE PostgreSQL transaction.
    """
    kind = op.kind
    if kind == "asset.attach":
        payload = op.to_dict()["payload"]
        if (
            set(payload) != {"assetId", "sha256", "sizeBytes", "mediaType"}
            or not isinstance(payload["assetId"], str)
            or not 1 <= len(payload["assetId"]) <= 128
            or any(ord(c) < 32 or ord(c) == 127 for c in payload["assetId"])
            or not isinstance(payload["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", payload["sha256"])
            or type(payload["sizeBytes"]) is not int
            or not 1 <= payload["sizeBytes"] <= 64 * 1024 * 1024
            or payload["mediaType"] not in {
                "image/png", "image/jpeg", "image/webp", "application/pdf"
            }
        ):
            raise ProjectionError("Invalid asset.attach payload.")
        if payload["sha256"] not in verified_assets:
            raise ProjectionError("Asset is not verified.")
    elif kind == "asset.detach":
        payload = op.to_dict()["payload"]
        if (
            set(payload) != {"assetId"}
            or not isinstance(payload["assetId"], str)
            or not 1 <= len(payload["assetId"]) <= 128
            or any(ord(c) < 32 or ord(c) == 127 for c in payload["assetId"])
        ):
            raise ProjectionError("Invalid asset.detach payload.")
    if kind == "stroke.create":
        is_create = True
    else:
        is_create = kind.endswith(".create")
    is_delete = kind.endswith(".delete") and not kind.startswith("text.")
    if kind.startswith(("text.", "asset.")) or kind == "object.move":
        fields = None
    elif is_delete:
        if op.to_dict()["payload"]:
            raise ProjectionError("Delete operations require empty payload.")
    elif is_create:
        fields = _properties(op, create=True)
    else:
        fields = _properties(op, create=False)

    document_key = ("document", "", "")
    page_key = ("page", "", op.page_id or op.object_id or "")
    object_key = ("object", op.page_id or "", op.object_id or "")
    folder_key = ("folder", "", op.object_id or "")
    task_key = ("task", "", op.object_id or "")

    if kind.startswith("document."):
        if kind == "document.create":
            if current is not None or document_key in tombstones:
                raise ProjectionError("Document ID already used.")
            result = _new_node()
            result["pages"] = {}
            _merge(result, fields, op)
            return result, None
        if kind == "document.delete":
            return None, document_key if document_key not in tombstones else None
        if document_key in tombstones:
            return None, None
        if current is None:
            raise ProjectionError("Document has not been created.")
        _merge(current, fields, op)
        return current, None

    if document_key in tombstones:
        return None, None

    if kind.startswith("folder."):
        if op.document_id != LIBRARY_DOCUMENT_ID:
            raise ProjectionError("Folder outside library scope.")
        if current is None:
            if kind != "folder.create" and not is_delete:
                raise ProjectionError("Library has not been created.")
            current = {"folders": {}}
        folders = current.setdefault("folders", {})
        if kind == "folder.delete":
            for folder_id, child in folders.items():
                if (folder_id != op.object_id and
                        child.get("properties", {}).get("parentId")
                        == op.object_id):
                    raise ProjectionError(
                        "Folder has children; delete or move them first."
                    )
            folders.pop(op.object_id, None)
            return current, folder_key if folder_key not in tombstones else None
        if folder_key in tombstones:
            if is_create:
                raise ProjectionError("Folder ID was deleted.")
            return current, None
        node = folders.get(op.object_id)
        if is_create:
            if node is not None:
                raise ProjectionError("Folder ID already used.")
            node = _new_node()
            folders[op.object_id] = node
        if node is None:
            raise ProjectionError("Folder has not been created.")
        _merge(node, fields, op)
        _validate_folder_graph(folders)
        return current, None

    if kind.startswith("task."):
        if op.document_id != LIBRARY_DOCUMENT_ID:
            raise ProjectionError("Task outside library scope.")
        if current is None:
            if kind != "task.create" and not is_delete:
                raise ProjectionError("Library has not been created.")
            current = {"folders": {}, "tasks": {}}
        tasks = current.setdefault("tasks", {})
        if kind == "task.delete":
            tasks.pop(op.object_id, None)
            return current, task_key if task_key not in tombstones else None
        if task_key in tombstones:
            if is_create:
                raise ProjectionError("Task ID was deleted.")
            return current, None
        node = tasks.get(op.object_id)
        if is_create:
            if node is not None:
                raise ProjectionError("Task ID already used.")
            node = _new_node()
            tasks[op.object_id] = node
        if node is None:
            raise ProjectionError("Task has not been created.")
        if is_create and (
            not isinstance(fields.get("plannerUid"), str)
            or not fields["plannerUid"]
        ):
            raise ProjectionError("Task needs a plannerUid.")
        if "plannerUid" in fields and (
            not isinstance(fields["plannerUid"], str)
            or not fields["plannerUid"]
        ):
            raise ProjectionError("Invalid task plannerUid.")
        _merge(node, fields, op)
        return current, None

    if current is None:
        if is_delete:
            # Deletes of previously unknown IDs still protect against replay.
            return None, (page_key if kind.startswith("page.") else object_key)
        raise ProjectionError("Document has not been created.")

    pages = current.setdefault("pages", {})
    if kind.startswith("page."):
        key = ("page", "", op.object_id or "")
        if is_delete:
            pages.pop(op.object_id, None)
            return current, key if key not in tombstones else None
        if key in tombstones:
            if is_create:
                raise ProjectionError("Page ID was deleted.")
            return current, None
        node = pages.get(op.object_id)
        if is_create:
            if node is not None:
                raise ProjectionError("Page ID already used.")
            node = _new_node()
            node["objects"] = {}
            pages[op.object_id] = node
        if node is None:
            raise ProjectionError("Page has not been created.")
        _merge(node, fields, op)
        return current, None

    # A moved object retains its globally unique ID. Historical page-scoped
    # tombstones must also prevent reuse on other pages.
    was_deleted = any(
        kind == "object" and object_id == op.object_id
        for kind, _, object_id in tombstones
    )
    if was_deleted:
        if is_create:
            raise ProjectionError("Object ID was deleted.")
        return current, None
    if kind == "object.delete":
        owners = [
            page_node["objects"]
            for page_node in pages.values()
            if op.object_id in page_node.get("objects", {})
        ]
        if len(owners) > 1:
            raise ProjectionError("Object delete target is ambiguous.")
        if owners:
            owners[0].pop(op.object_id)
        return current, object_key
    if ("page", "", op.page_id or "") in tombstones:
        if is_create:
            raise ProjectionError("Parent page was deleted.")
        return current, None
    page = pages.get(op.page_id)
    if page is None:
        if is_delete:
            return current, object_key if object_key not in tombstones else None
        raise ProjectionError("Parent page has not been created.")
    objects = page.setdefault("objects", {})
    if kind == "object.move":
        payload = op.to_dict()["payload"]
        if set(payload) != {"toPageId", "sortIndex"}:
            raise ProjectionError("Invalid object.move payload.")
        destination_id, sort_index = payload["toPageId"], payload["sortIndex"]
        if (
            not isinstance(destination_id, str)
            or not 1 <= len(destination_id) <= 128
            or type(sort_index) is not int
            or not 0 <= sort_index < 1000000
            or destination_id == op.page_id
        ):
            raise ProjectionError("Invalid object.move destination.")
        # A competing actor may have moved the object away already.
        # Resolve all locations before trusting the stale source page.
        owners = [
            (page_id, page_node["objects"][op.object_id])
            for page_id, page_node in pages.items()
            if op.object_id in page_node.get("objects", {})
        ]
        if len(owners) != 1:
            raise ProjectionError("Move target is missing or ambiguous.")
        actual_source, moved = owners[0]
        previous = moved.get("lastMove")
        rank = list(precedence(op))
        if previous is not None:
            if not isinstance(previous, dict) or set(previous) != {
                "rank", "actorId", "operationId"
            }:
                raise ProjectionError("Corrupt persisted move version.")
            if rank <= previous["rank"]:
                # A losing operation must not resurrect a removed destination.
                return current, None
        destination_page = pages.get(destination_id)
        if destination_page is None or (
            "page", "", destination_id
        ) in tombstones:
            raise ProjectionError("Destination page does not exist.")
        if actual_source != op.page_id:
            if (
                previous is None
                or previous["actorId"] == op.actor_id
                or previous["operationId"] in op.dependencies
            ):
                raise ProjectionError("Causal move source is inconsistent.")
        if ("object", destination_id, op.object_id or "") in tombstones:
            raise ProjectionError("Destination identity was deleted.")
        destination_objects = destination_page.setdefault("objects", {})
        if (op.object_id in destination_objects
                and actual_source != destination_id):
            raise ProjectionError("Destination identity is occupied.")
        if actual_source != destination_id:
            pages[actual_source]["objects"].pop(op.object_id)
            destination_objects[op.object_id] = moved
        _merge(moved, {"sortIndex": sort_index}, op)
        moved["lastMove"] = {
            "rank": rank,
            "actorId": op.actor_id,
            "operationId": op.operation_id,
        }
        return current, None
    node = objects.get(op.object_id)
    if is_create:
        if any(
            op.object_id in candidate.get("objects", {})
            for candidate in pages.values()
        ):
            raise ProjectionError("Object ID already used.")
        node = _new_node()
        node["type"] = kind
        objects[op.object_id] = node
    if node is None:
        raise ProjectionError("Object has not been created.")
    if kind.startswith("asset."):
        asset_id = payload["assetId"]
        rank = list(precedence(op))
        versions = node.setdefault("assetVersions", {})
        if kind == "asset.attach":
            receipt = {
                key: payload[key]
                for key in ("sha256", "sizeBytes", "mediaType")
            }
            _remember_asset_receipt(current, pages, asset_id, receipt)
        if asset_id not in versions or rank > versions[asset_id]:
            assets = node.setdefault("assets", {})
            if kind == "asset.detach" or (
                kind == "asset.attach" and asset_id in assets
                and assets[asset_id] != {
                    "sha256": payload["sha256"],
                    "sizeBytes": payload["sizeBytes"],
                    "mediaType": payload["mediaType"],
                }
            ):
                try:
                    visible = render_delta(node.get("textAtoms", {}))
                except TextSequenceError as exc:
                    raise ProjectionError(str(exc)) from exc
                if any(
                    isinstance(item["insert"], dict)
                    and f"asset:{asset_id}" in item["insert"].values()
                    for item in visible
                ):
                    raise ProjectionError(
                        "Cannot detach or replace an embedded asset."
                    )
            if kind == "asset.detach" and (
                node.get("properties", {}).get("assetId") == asset_id
            ):
                raise ProjectionError("Cannot detach the active image asset.")
            if kind == "asset.attach":
                assets[asset_id] = {
                    "sha256": payload["sha256"],
                    "sizeBytes": payload["sizeBytes"],
                    "mediaType": payload["mediaType"],
                }
            else:
                assets.pop(asset_id, None)
            versions[asset_id] = rank
        return current, None
    if kind.startswith("text."):
        if (node.get("type") != "object.create" or
                node.get("properties", {}).get("objectType") != "text"):
            raise ProjectionError("Text operations require a text object.")
        try:
            apply_text_operation(node, op)
        except TextSequenceError as exc:
            raise ProjectionError(str(exc)) from exc
        return current, None
    if "textAtoms" in node and ({"text", "delta"} & fields.keys()):
        raise ProjectionError("CRDT text cannot be replaced by object.set.")
    if "assetId" in fields:
        if (
            node.get("type") != "object.create"
            or node.get("properties", {}).get("objectType") != "image"
            or not isinstance(fields["assetId"], str)
            or fields["assetId"] not in node.get("assets", {})
        ):
            raise ProjectionError(
                "Image asset replacement needs a verified attachment."
            )
    _merge(node, fields, op)
    return current, None


def ordered_objects(page: dict) -> list[tuple[str, dict]]:
    """Return the z-order independently of JSONB object-key order."""
    objects = page.get("objects", {})
    if not isinstance(objects, dict):
        raise ProjectionError("Invalid object collection.")
    def order(entry: tuple[str, dict]) -> tuple[int, str]:
        object_id, node = entry
        index = node.get("properties", {}).get("sortIndex", 0)
        if type(index) is not int or not 0 <= index < 1000000:
            raise ProjectionError("Invalid object z-order.")
        return index, object_id
    return sorted(objects.items(), key=order)
