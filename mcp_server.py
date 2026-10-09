#!/usr/bin/env python3
"""Local, dependency-free MCP stdio tools for Event-scoped menu imports."""

import argparse
import base64
import binascii
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import app


PROTOCOL_VERSION = "2025-06-18"


def event_row(connection, event_id):
    app.require_event_id(event_id)
    row = connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if not row:
        raise app.InputError("Event not found")
    return row


def list_events(_arguments):
    with closing(app.db()) as connection:
        selected = app.selected_event_id(connection)
        return {"events": [app.event_record(row, selected) for row in connection.execute("SELECT * FROM events ORDER BY event_date DESC, id DESC")]}


def get_event(arguments):
    with closing(app.db()) as connection:
        event_id = app.require_event_id(arguments.get("eventId"))
        return app.event_record(event_row(connection, event_id), app.selected_event_id(connection))


def create_event(arguments):
    name = app.require_string(arguments.get("name"), "Event name", 100)
    date = app.event_date(arguments.get("date"))
    with closing(app.db()) as connection, app.DB_LOCK:
        connection.execute("BEGIN IMMEDIATE")
        try:
            event_id = connection.execute("INSERT INTO events (name, event_date) VALUES (?, ?)", (name, date)).lastrowid
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        return app.event_record(event_row(connection, event_id), app.selected_event_id(connection))


def select_event(arguments):
    event_id = app.require_event_id(arguments.get("eventId"))
    with closing(app.db()) as connection, app.DB_LOCK:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = event_row(connection, event_id)
            if not row["enabled"]:
                raise app.InputError("Disabled Event cannot be selected")
            connection.execute("UPDATE app_state SET selected_event_id = ? WHERE id = 1", (event_id,))
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        return app.event_record(event_row(connection, event_id), event_id)


def list_menu(arguments):
    event_id = app.require_event_id(arguments.get("eventId"))
    with closing(app.db()) as connection:
        event = app.event_record(event_row(connection, event_id), app.selected_event_id(connection))
        categories = connection.execute(app.CATEGORY_SELECT + " WHERE menu_categories.event_id = ? ORDER BY name COLLATE NOCASE, id", (event_id,)).fetchall()
        items = connection.execute("SELECT menu_items.*, EXISTS(SELECT 1 FROM menu_images WHERE menu_item_id = menu_items.id) AS has_image FROM menu_items WHERE event_id = ? ORDER BY name COLLATE NOCASE, id", (event_id,)).fetchall()
        return {"event": event, "categories": [app.category_record(row) for row in categories], "items": [app.menu_record(row) for row in items]}


def menu_image_update(arguments):
    sources = [key for key in ("imagePath", "imageBase64") if key in arguments]
    removing = arguments.get("removeImage", False)
    if type(removing) is not bool or len(sources) + int(removing) > 1:
        raise app.InputError("Choose one imagePath, imageBase64, or removeImage=true")
    if "imageMime" in arguments and "imageBase64" not in arguments:
        raise app.InputError("imageMime is only used with imageBase64")
    if removing:
        return "remove", None, None
    if not sources:
        return "keep", None, None
    if sources[0] == "imagePath":
        path_value = arguments["imagePath"]
        if not isinstance(path_value, str) or not Path(path_value).is_absolute():
            raise app.InputError("imagePath must be an absolute local file path")
        try:
            with Path(path_value).open("rb") as source:
                data = source.read(app.MAX_IMAGE + 1)
        except (OSError, ValueError) as error:
            raise app.InputError(f"Cannot read imagePath: {error}") from error
    else:
        encoded = arguments["imageBase64"]
        if not isinstance(encoded, str) or len(encoded) > 4 * ((app.MAX_IMAGE + 2) // 3) + 4:
            raise app.InputError("Image must be smaller than 3 MB")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise app.InputError("imageBase64 must be valid base64") from error
    if not 0 < len(data) <= app.MAX_IMAGE:
        raise app.InputError("Image must be smaller than 3 MB")
    if data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        raise app.InputError("Use a valid JPEG, PNG, or WebP image")
    if "imageMime" in arguments and arguments["imageMime"] != mime:
        raise app.InputError("imageMime does not match the image data")
    return "replace", mime, data


def update_menu_item(arguments):
    event_id = app.require_event_id(arguments.get("eventId"))
    item_id = arguments.get("itemId")
    if type(item_id) is not int or item_id < 1:
        raise app.InputError("An explicit itemId is required")
    dry_run = arguments.get("dryRun")
    if type(dry_run) is not bool:
        raise app.InputError("dryRun must be true or false")
    fields = ("name", "price", "station", "categoryId", "enabled", "imagePath", "imageBase64", "removeImage")
    if not any(field in arguments for field in fields):
        raise app.InputError("Provide at least one field to update")
    image_action, image_mime, image_data = menu_image_update(arguments)
    with closing(app.db()) as connection, app.DB_LOCK:
        connection.execute("BEGIN" if dry_run else "BEGIN IMMEDIATE")
        try:
            event = app.event_record(event_row(connection, event_id), app.selected_event_id(connection))
            row = connection.execute("SELECT menu_items.*, EXISTS(SELECT 1 FROM menu_images WHERE menu_item_id = menu_items.id) AS has_image FROM menu_items WHERE id = ? AND event_id = ?", (item_id, event_id)).fetchone()
            if not row:
                raise app.InputError("Menu item not found in this Event")
            old = app.menu_record(row)
            proposed = dict(old)
            if "name" in arguments:
                proposed["name"] = app.require_string(arguments["name"], "Item name", 100)
            if "price" in arguments:
                proposed["price_cents"] = app.price_to_cents(arguments["price"])
            if "station" in arguments:
                proposed["station"] = app.require_station(arguments["station"])
            if "categoryId" in arguments:
                category_id = arguments["categoryId"]
                if category_id is not None:
                    if type(category_id) is not int or category_id < 1:
                        raise app.InputError("categoryId must be a positive integer or null")
                    if not connection.execute("SELECT 1 FROM menu_categories WHERE id = ? AND event_id = ?", (category_id, event_id)).fetchone():
                        raise app.InputError("Category not found in this Event")
                proposed["category_id"] = category_id
            if "enabled" in arguments:
                if type(arguments["enabled"]) is not bool:
                    raise app.InputError("enabled must be true or false")
                proposed["enabled"] = arguments["enabled"]
            existing_image = connection.execute("SELECT mime, data FROM menu_images WHERE menu_item_id = ?", (item_id,)).fetchone()
            old_digest = hashlib.sha256(existing_image["data"]).hexdigest() if existing_image else None
            new_digest = hashlib.sha256(image_data).hexdigest() if image_data is not None else None
            if image_action == "replace":
                proposed["has_image"] = True
            elif image_action == "remove":
                proposed["has_image"] = False
            image_change = "unchanged"
            if image_action == "remove" and existing_image:
                image_change = "remove"
            elif image_action == "replace" and (not existing_image or existing_image["mime"] != image_mime or old_digest != new_digest):
                image_change = "replace" if existing_image else "add"
            fingerprint = {"eventId": event_id, "itemId": item_id, "before": old, "oldImageSha256": old_digest,
                           "after": proposed, "imageAction": image_action, "imageMime": image_mime, "newImageSha256": new_digest}
            token = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
            preview = {"event": event, "itemId": item_id, "dryRun": dry_run, "before": old, "after": proposed,
                       "imageChange": image_change, "imageMime": image_mime if image_action == "replace" else None,
                       "previewToken": token}
            if dry_run:
                connection.execute("COMMIT")
                return preview
            if arguments.get("confirmed") is not True:
                raise app.InputError("Explicit user confirmation is required before updating a menu item")
            if arguments.get("previewToken") != token:
                raise app.ConflictError("Preview is missing or stale. Run dryRun again and confirm the new preview.")
            if proposed != old:
                connection.execute("UPDATE menu_items SET name = ?, price_cents = ?, station = ?, category_id = ?, enabled = ? WHERE id = ? AND event_id = ?",
                                   (proposed["name"], proposed["price_cents"], proposed["station"], proposed["category_id"], int(proposed["enabled"]), item_id, event_id))
            if image_change in ("add", "replace"):
                connection.execute("INSERT OR REPLACE INTO menu_images (menu_item_id, mime, data) VALUES (?, ?, ?)", (item_id, image_mime, image_data))
            elif image_change == "remove":
                connection.execute("DELETE FROM menu_images WHERE menu_item_id = ?", (item_id,))
            connection.execute("COMMIT")
            preview["dryRun"] = False
            preview["updated"] = proposed != old or image_change != "unchanged"
            return preview
        except Exception:
            connection.execute("ROLLBACK")
            raise


def normalize_items(raw_items):
    if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= 500:
        raise app.InputError("items must contain 1 to 500 menu positions")
    normalized = []
    seen = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise app.InputError("Each item must be an object")
        name = app.require_string(raw.get("name"), "Item name", 100)
        price_cents = app.price_to_cents(raw.get("price"))
        station = app.require_station(raw.get("station"))
        enabled = raw.get("enabled", True)
        if type(enabled) is not bool:
            raise app.InputError("Item enabled must be true or false")
        path = raw.get("categoryPath", [])
        if not isinstance(path, list) or len(path) > 8:
            raise app.InputError("categoryPath must be a list of up to 8 names")
        path = [app.require_string(part, "Category name", 100) for part in path]
        key = (tuple(part.casefold() for part in path), name.casefold())
        if key in seen:
            raise app.InputError(f"Repeated item in import: {name}")
        seen.add(key)
        normalized.append({"name": name, "price_cents": price_cents, "station": station,
                           "enabled": enabled, "categoryPath": path})
    return normalized


def menu_state(connection, event_id):
    categories = connection.execute("SELECT id, name, parent_id FROM menu_categories WHERE event_id = ? ORDER BY id", (event_id,)).fetchall()
    items = connection.execute("SELECT id, name, category_id, price_cents, station, enabled FROM menu_items WHERE event_id = ? ORDER BY id", (event_id,)).fetchall()
    return [dict(row) for row in categories], [dict(row) for row in items]


def plan_import(event_id, normalized, categories, items):
    paths = {None: ()}
    by_id = {category["id"]: category for category in categories}

    def category_path(category_id):
        if category_id in paths:
            return paths[category_id]
        category = by_id[category_id]
        path = category_path(category["parent_id"]) + (category["name"].casefold(),)
        paths[category_id] = path
        return path

    existing_categories = {category_path(category["id"]): category["id"] for category in categories}
    existing_items = {}
    for item in items:
        key = (category_path(item["category_id"]), item["name"].casefold())
        if key in existing_items:
            raise app.InputError(f"Existing Event menu has repeated item: {item['name']}")
        existing_items[key] = item
    missing_categories = {}
    actions = []
    for item in normalized:
        path = tuple(part.casefold() for part in item["categoryPath"])
        for depth in range(1, len(path) + 1):
            prefix = path[:depth]
            if prefix not in existing_categories:
                missing_categories[prefix] = item["categoryPath"][:depth]
        old = existing_items.get((path, item["name"].casefold()))
        if old is None:
            action = "create"
        elif (old["name"], old["price_cents"], old["station"], bool(old["enabled"])) == (item["name"], item["price_cents"], item["station"], item["enabled"]):
            action = "unchanged"
        else:
            action = "update"
        actions.append({"action": action, "id": old["id"] if old else None, **item})
    fingerprint = json.dumps({"eventId": event_id, "items": normalized, "categories": categories, "currentItems": items}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    token = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    return actions, list(missing_categories.values()), token


def bulk_upsert_menu(arguments):
    event_id = app.require_event_id(arguments.get("eventId"))
    dry_run = arguments.get("dryRun")
    if type(dry_run) is not bool:
        raise app.InputError("dryRun must be true or false")
    normalized = normalize_items(arguments.get("items"))
    with closing(app.db()) as connection, app.DB_LOCK:
        connection.execute("BEGIN IMMEDIATE" if not dry_run else "BEGIN")
        try:
            event = app.event_record(event_row(connection, event_id), app.selected_event_id(connection))
            categories, items = menu_state(connection, event_id)
            actions, missing_categories, token = plan_import(event_id, normalized, categories, items)
            counts = {kind: sum(action["action"] == kind for action in actions) for kind in ("create", "update", "unchanged")}
            preview = {"event": event, "dryRun": dry_run, "counts": counts,
                       "categoriesToCreate": missing_categories, "items": actions, "previewToken": token}
            if dry_run:
                connection.execute("COMMIT")
                return preview
            if arguments.get("confirmed") is not True:
                raise app.InputError("Explicit user confirmation is required before import")
            if arguments.get("previewToken") != token:
                raise app.ConflictError("Preview is missing or stale. Run dryRun again and confirm the new preview.")
            category_ids = {None: None}
            by_id = {category["id"]: category for category in categories}

            def existing_path(category_id):
                if category_id is None:
                    return ()
                category = by_id[category_id]
                return existing_path(category["parent_id"]) + (category["name"].casefold(),)

            for category in categories:
                category_ids[existing_path(category["id"])] = category["id"]
            for path in missing_categories:
                key = tuple(part.casefold() for part in path)
                category_ids[key] = connection.execute("INSERT INTO menu_categories (name, parent_id, event_id) VALUES (?, ?, ?)", (path[-1], category_ids[key[:-1]] if len(key) > 1 else None, event_id)).lastrowid
            for action in actions:
                if action["action"] == "unchanged":
                    continue
                if action["action"] == "create":
                    key = tuple(part.casefold() for part in action["categoryPath"])
                    connection.execute("INSERT INTO menu_items (name, category_id, event_id, price_cents, station, enabled) VALUES (?, ?, ?, ?, ?, ?)",
                                       (action["name"], category_ids.get(key), event_id, action["price_cents"], action["station"], int(action["enabled"])))
                else:
                    connection.execute("UPDATE menu_items SET name = ?, price_cents = ?, station = ?, enabled = ? WHERE id = ? AND event_id = ?",
                                       (action["name"], action["price_cents"], action["station"], int(action["enabled"]), action["id"], event_id))
            connection.execute("COMMIT")
            preview["dryRun"] = False
            preview["imported"] = True
            return preview
        except Exception:
            connection.execute("ROLLBACK")
            raise


TOOLS = [
    {"name": "list_events", "description": "List Events and identify the globally selected Event.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_event", "description": "Read one Event by explicit eventId.", "inputSchema": {"type": "object", "properties": {"eventId": {"type": "integer", "minimum": 1}}, "required": ["eventId"], "additionalProperties": False}},
    {"name": "create_event", "description": "Create an Event. This does not select it or import a menu.", "inputSchema": {"type": "object", "properties": {"name": {"type": "string", "minLength": 1}, "date": {"type": ["string", "null"], "description": "Optional YYYY-MM-DD"}}, "required": ["name"], "additionalProperties": False}},
    {"name": "select_event", "description": "Select an enabled Event as the one shared working context for all operator views. Requires explicit eventId.", "inputSchema": {"type": "object", "properties": {"eventId": {"type": "integer", "minimum": 1}}, "required": ["eventId"], "additionalProperties": False}},
    {"name": "list_menu", "description": "Read all categories and menu items for exactly the supplied eventId. Never infer the Event.", "inputSchema": {"type": "object", "properties": {"eventId": {"type": "integer", "minimum": 1}}, "required": ["eventId"], "additionalProperties": False}},
    {"name": "update_menu_item", "description": "Update one item in exactly eventId by itemId, including an optional JPEG/PNG/WebP image. First preview with dryRun=true, show the result to the user, then apply with dryRun=false, confirmed=true, and the previewToken. imagePath is an absolute path on the MCP host; imageBase64 is raw base64. Omitted fields and image are preserved.",
     "inputSchema": {"type": "object", "properties": {"eventId": {"type": "integer", "minimum": 1}, "itemId": {"type": "integer", "minimum": 1}, "name": {"type": "string"}, "price": {"type": ["string", "number"]}, "station": {"type": "string", "enum": ["KITCHEN", "BAR"]}, "categoryId": {"type": ["integer", "null"]}, "enabled": {"type": "boolean"}, "imagePath": {"type": "string"}, "imageBase64": {"type": "string"}, "imageMime": {"type": "string", "enum": ["image/jpeg", "image/png", "image/webp"]}, "removeImage": {"type": "boolean"}, "dryRun": {"type": "boolean"}, "previewToken": {"type": "string"}, "confirmed": {"type": "boolean"}}, "required": ["eventId", "itemId", "dryRun"], "additionalProperties": False}},
    {"name": "bulk_upsert_menu", "description": "Import into exactly eventId. First call dryRun=true, show the preview to the user, obtain confirmation, then call dryRun=false with the returned previewToken and confirmed=true. Never guess eventId. Existing items are matched by categoryPath and name; omitted items are preserved.",
     "inputSchema": {"type": "object", "properties": {"eventId": {"type": "integer", "minimum": 1}, "items": {"type": "array", "minItems": 1, "maxItems": 500, "items": {"type": "object", "properties": {"name": {"type": "string"}, "price": {"type": ["string", "number"]}, "station": {"type": "string", "enum": ["KITCHEN", "BAR"]}, "categoryPath": {"type": "array", "items": {"type": "string"}}, "enabled": {"type": "boolean"}}, "required": ["name", "price", "station"], "additionalProperties": False}}, "dryRun": {"type": "boolean"}, "previewToken": {"type": "string"}, "confirmed": {"type": "boolean"}}, "required": ["eventId", "items", "dryRun"], "additionalProperties": False}},
]

HANDLERS = {"list_events": list_events, "get_event": get_event, "create_event": create_event,
            "select_event": select_event, "list_menu": list_menu, "update_menu_item": update_menu_item,
            "bulk_upsert_menu": bulk_upsert_menu}


def handle_message(message):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None,
                "error": {"code": -32600, "message": "Invalid request"}}
    if "id" not in message:
        return None
    request_id = message["id"]
    method = message.get("method")
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "tabern-pos-events", "version": "1.0.0"},
                  "instructions": "Always list or get Events and pass an explicit eventId. Preview menu imports and item updates, show the result to the user, and obtain confirmation before applying."}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        if name not in HANDLERS:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "Unknown tool"}}
        try:
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise app.InputError("Tool arguments must be an object")
            value = HANDLERS[name](arguments)
            result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "structuredContent": value}
        except (app.InputError, app.ConflictError, ValueError, sqlite3.Error) as error:
            result = {"content": [{"type": "text", "text": str(error)}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main():
    parser = argparse.ArgumentParser(description="Event-scoped local menu MCP server")
    parser.add_argument("--data", type=Path, default=app.ROOT / "data" / "orders.sqlite3")
    args = parser.parse_args()
    app.DB_PATH = args.data.resolve()
    app.init_db()
    for line in sys.stdin:
        try:
            response = handle_message(json.loads(line))
        except (json.JSONDecodeError, UnicodeDecodeError):
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
