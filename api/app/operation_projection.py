"""Deterministic, JSON-safe v2 projection with permanent entity tombstones.

Text atoms use deterministic RGA ordering. Assets still require a separate
verified transport before they can be enabled.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .operation_ordering import precedence
from .operation_protocol import LIBRARY_DOCUMENT_ID, OperationEnvelope
from .text_sequence import TextSequenceError, apply_text_operation


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


def apply_operation(
    current: dict | None,
    op: OperationEnvelope,
    tombstones: set[tuple[str, str, str]],
) -> tuple[dict | None, tuple[str, str, str] | None]:
    """Return updated projection and optional newly permanent tombstone.

    The caller serializes writers, persists the projection and tombstone,
    and appends the operation in ONE PostgreSQL transaction.
    """
    kind = op.kind
    if kind.startswith("asset."):
        raise ProjectionError("Asset reducers are not enabled in v2 yet.")
    if kind == "stroke.create":
        is_create = True
    else:
        is_create = kind.endswith(".create")
    is_delete = kind.endswith(".delete") and not kind.startswith("text.")
    if kind.startswith("text."):
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
    if kind == "object.delete":
        objects.pop(op.object_id, None)
        return current, object_key if object_key not in tombstones else None
    if object_key in tombstones:
        if is_create:
            raise ProjectionError("Object ID was deleted.")
        return current, None
    node = objects.get(op.object_id)
    if is_create:
        if node is not None:
            raise ProjectionError("Object ID already used.")
        node = _new_node()
        node["type"] = kind
        objects[op.object_id] = node
    if node is None:
        raise ProjectionError("Object has not been created.")
    if kind.startswith("text."):
        if (node.get("type") != "object.create" or
                node.get("properties", {}).get("objectType") != "text"):
            raise ProjectionError("Text operations require a text object.")
        try:
            apply_text_operation(node, op)
        except TextSequenceError as exc:
            raise ProjectionError(str(exc)) from exc
        return current, None
    if "textAtoms" in node and "text" in fields:
        raise ProjectionError("CRDT text cannot be replaced by object.set.")
    _merge(node, fields, op)
    return current, None
