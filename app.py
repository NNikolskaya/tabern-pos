#!/usr/bin/env python3
"""Single-machine, LAN-first order server. Python standard library only."""

import argparse
import datetime
import json
import socket
import sqlite3
import sys
import threading
import re
import subprocess
import platform
from contextlib import closing
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from terminal_qr import render_terminal_qr


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
MAX_BODY = 64 * 1024
MAX_IMAGE = 3 * 1024 * 1024
DB_PATH = None
DB_LOCK = threading.RLock()


def db():
    connection = sqlite3.connect(str(DB_PATH), timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(db()) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                event_date TEXT,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))
            );
            CREATE TABLE IF NOT EXISTS app_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                selected_event_id INTEGER NOT NULL REFERENCES events(id)
            );
            CREATE TABLE IF NOT EXISTS menu_categories (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                parent_id INTEGER REFERENCES menu_categories(id),
                event_id INTEGER REFERENCES events(id)
            );
            CREATE TABLE IF NOT EXISTS category_images (
                category_id INTEGER PRIMARY KEY REFERENCES menu_categories(id) ON DELETE CASCADE,
                mime TEXT NOT NULL,
                data BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS menu_items (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                price_cents INTEGER NOT NULL CHECK (price_cents >= 0),
                category TEXT NOT NULL DEFAULT '',
                category_id INTEGER REFERENCES menu_categories(id),
                event_id INTEGER REFERENCES events(id),
                station TEXT NOT NULL DEFAULT 'BAR',
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            CREATE TABLE IF NOT EXISTS menu_images (
                menu_item_id INTEGER PRIMARY KEY REFERENCES menu_items(id),
                mime TEXT NOT NULL,
                data BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                event_id INTEGER REFERENCES events(id),
                status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
                paid INTEGER NOT NULL DEFAULT 0 CHECK (paid IN (0, 1)),
                comment TEXT NOT NULL DEFAULT '' CHECK (length(comment) <= 40),
                revision INTEGER NOT NULL DEFAULT 0,
                total_cents INTEGER NOT NULL CHECK (total_cents >= 0),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            CREATE TABLE IF NOT EXISTS order_lines (
                id INTEGER PRIMARY KEY,
                order_id INTEGER NOT NULL REFERENCES orders(id),
                menu_item_id INTEGER REFERENCES menu_items(id),
                station TEXT NOT NULL DEFAULT 'BAR',
                issued INTEGER NOT NULL DEFAULT 0 CHECK (issued IN (0, 1)),
                item_name TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                price_cents INTEGER NOT NULL CHECK (price_cents >= 0),
                line_total_cents INTEGER NOT NULL CHECK (line_total_cents >= 0)
            );
        """)
        # Existing event databases created before editable checks need one column.
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(orders)")}
        if "revision" not in columns:
            connection.execute("ALTER TABLE orders ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
        if "paid" not in columns:
            connection.execute("ALTER TABLE orders ADD COLUMN paid INTEGER NOT NULL DEFAULT 0")
        if "comment" not in columns:
            connection.execute("ALTER TABLE orders ADD COLUMN comment TEXT NOT NULL DEFAULT '' CHECK (length(comment) <= 40)")
        if "event_id" not in columns:
            connection.execute("ALTER TABLE orders ADD COLUMN event_id INTEGER REFERENCES events(id)")
        connection.execute("UPDATE orders SET paid = 1 WHERE status = 'done' AND paid = 0")
        item_columns = {row["name"] for row in connection.execute("PRAGMA table_info(menu_items)")}
        if "category_id" not in item_columns:
            connection.execute("ALTER TABLE menu_items ADD COLUMN category_id INTEGER REFERENCES menu_categories(id)")
        if "event_id" not in item_columns:
            connection.execute("ALTER TABLE menu_items ADD COLUMN event_id INTEGER REFERENCES events(id)")
        if "station" not in item_columns:
            connection.execute("ALTER TABLE menu_items ADD COLUMN station TEXT NOT NULL DEFAULT 'BAR'")
        line_columns = {row["name"] for row in connection.execute("PRAGMA table_info(order_lines)")}
        if "station" not in line_columns:
            connection.execute("ALTER TABLE order_lines ADD COLUMN station TEXT NOT NULL DEFAULT 'BAR'")
        if "issued" not in line_columns:
            connection.execute("ALTER TABLE order_lines ADD COLUMN issued INTEGER NOT NULL DEFAULT 0")
        category_columns = {row["name"] for row in connection.execute("PRAGMA table_info(menu_categories)")}
        if "event_id" not in category_columns:
            connection.execute("ALTER TABLE menu_categories ADD COLUMN event_id INTEGER REFERENCES events(id)")
        connection.execute("BEGIN IMMEDIATE")
        try:
            default_event = connection.execute("SELECT id FROM events ORDER BY id LIMIT 1").fetchone()
            if not default_event:
                default_event_id = connection.execute("INSERT INTO events (name) VALUES ('Поточна подія')").lastrowid
            else:
                default_event_id = default_event["id"]
            for table in ("menu_categories", "menu_items", "orders"):
                connection.execute(f"UPDATE {table} SET event_id = ? WHERE event_id IS NULL", (default_event_id,))
            connection.execute("INSERT OR IGNORE INTO app_state (id, selected_event_id) VALUES (1, ?)", (default_event_id,))
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        # This transaction also resumes safely if a previous migration stopped after ALTER TABLE.
        legacy = connection.execute("SELECT DISTINCT category FROM menu_items WHERE category != '' ORDER BY category").fetchall()
        if legacy:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for row in legacy:
                    for owner in connection.execute("SELECT DISTINCT event_id FROM menu_items WHERE category = ?", (row["category"],)).fetchall():
                        category = connection.execute("SELECT id FROM menu_categories WHERE event_id = ? AND parent_id IS NULL AND name = ? COLLATE NOCASE", (owner["event_id"], row["category"])).fetchone()
                        category_id = category["id"] if category else connection.execute("INSERT INTO menu_categories (name, event_id) VALUES (?, ?)", (row["category"], owner["event_id"])).lastrowid
                        connection.execute("UPDATE menu_items SET category_id = ? WHERE event_id = ? AND category = ? AND category_id IS NULL", (category_id, owner["event_id"], row["category"]))
                connection.execute("UPDATE menu_items SET category = '' WHERE category != ''")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        connection.executescript("""
            CREATE INDEX IF NOT EXISTS idx_categories_event ON menu_categories(event_id);
            CREATE INDEX IF NOT EXISTS idx_items_event ON menu_items(event_id);
            CREATE INDEX IF NOT EXISTS idx_orders_event ON orders(event_id);
            CREATE TRIGGER IF NOT EXISTS categories_owner_insert BEFORE INSERT ON menu_categories
              WHEN NEW.event_id IS NULL OR (NEW.parent_id IS NOT NULL AND
                (SELECT event_id FROM menu_categories WHERE id = NEW.parent_id) IS NOT NEW.event_id)
              BEGIN SELECT RAISE(ABORT, 'Category event mismatch'); END;
            CREATE TRIGGER IF NOT EXISTS categories_owner_update BEFORE UPDATE ON menu_categories
              WHEN NEW.event_id IS NULL OR NEW.event_id IS NOT OLD.event_id OR
                (NEW.parent_id IS NOT NULL AND (SELECT event_id FROM menu_categories WHERE id = NEW.parent_id) IS NOT NEW.event_id)
              BEGIN SELECT RAISE(ABORT, 'Category event mismatch'); END;
            CREATE TRIGGER IF NOT EXISTS items_owner_insert BEFORE INSERT ON menu_items
              WHEN NEW.event_id IS NULL OR (NEW.category_id IS NOT NULL AND
                (SELECT event_id FROM menu_categories WHERE id = NEW.category_id) IS NOT NEW.event_id)
              BEGIN SELECT RAISE(ABORT, 'Menu item event mismatch'); END;
            CREATE TRIGGER IF NOT EXISTS items_owner_update BEFORE UPDATE ON menu_items
              WHEN NEW.event_id IS NULL OR NEW.event_id IS NOT OLD.event_id OR
                (NEW.category_id IS NOT NULL AND (SELECT event_id FROM menu_categories WHERE id = NEW.category_id) IS NOT NEW.event_id)
              BEGIN SELECT RAISE(ABORT, 'Menu item event mismatch'); END;
            CREATE TRIGGER IF NOT EXISTS orders_owner_insert BEFORE INSERT ON orders
              WHEN NEW.event_id IS NULL
              BEGIN SELECT RAISE(ABORT, 'Order event is required'); END;
            CREATE TRIGGER IF NOT EXISTS orders_owner_update BEFORE UPDATE OF event_id ON orders
              WHEN NEW.event_id IS NOT OLD.event_id
              BEGIN SELECT RAISE(ABORT, 'Order event cannot change'); END;
            CREATE TRIGGER IF NOT EXISTS lines_owner_insert BEFORE INSERT ON order_lines
              WHEN NEW.menu_item_id IS NOT NULL AND
                (SELECT event_id FROM menu_items WHERE id = NEW.menu_item_id) IS NOT
                (SELECT event_id FROM orders WHERE id = NEW.order_id)
              BEGIN SELECT RAISE(ABORT, 'Order line event mismatch'); END;
            CREATE TRIGGER IF NOT EXISTS lines_owner_update BEFORE UPDATE OF order_id, menu_item_id ON order_lines
              WHEN NEW.menu_item_id IS NOT NULL AND
                (SELECT event_id FROM menu_items WHERE id = NEW.menu_item_id) IS NOT
                (SELECT event_id FROM orders WHERE id = NEW.order_id)
              BEGIN SELECT RAISE(ABORT, 'Order line event mismatch'); END;
        """)


class InputError(Exception):
    pass


class ConflictError(Exception):
    pass


def require_string(value, label, limit, optional=False):
    if not isinstance(value, str):
        raise InputError(f"{label} must be text")
    value = value.strip()
    if (not optional and not value) or len(value) > limit:
        raise InputError(f"{label} must be {'1 to ' if not optional else 'at most '}{limit} characters")
    return value


def price_to_cents(value):
    try:
        price = Decimal(str(value))
    except InvalidOperation:
        raise InputError("Price must be a number")
    if not price.is_finite() or price < 0 or price > 1000000 or price.as_tuple().exponent < -2:
        raise InputError("Price must be between 0 and 1,000,000 with at most two decimals")
    return int(price * 100)


def event_date(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise InputError("Event date must be YYYY-MM-DD or null")
    try:
        parsed = datetime.date.fromisoformat(value)
    except ValueError:
        raise InputError("Event date must be YYYY-MM-DD or null")
    if parsed.isoformat() != value:
        raise InputError("Event date must be YYYY-MM-DD or null")
    return value


def event_record(row, selected_id=None):
    return {"id": row["id"], "name": row["name"], "date": row["event_date"],
            "enabled": bool(row["enabled"]), "selected": row["id"] == selected_id}


def selected_event_id(connection):
    row = connection.execute("SELECT selected_event_id FROM app_state WHERE id = 1").fetchone()
    if not row:
        raise InputError("No Event is selected")
    return row["selected_event_id"]


def require_event_id(value):
    if type(value) is not int or value < 1:
        raise InputError("An explicit eventId is required")
    return value


def require_selected_event(connection, header):
    try:
        event_id = int(header)
    except (TypeError, ValueError):
        raise InputError("X-Event-Id is required")
    if str(event_id) != header or event_id < 1:
        raise InputError("X-Event-Id must be a positive integer")
    if event_id != selected_event_id(connection):
        raise ConflictError("The selected Event changed. Reload the current Event before editing.")
    return event_id


def menu_record(row):
    return {"id": row["id"], "name": row["name"], "price_cents": row["price_cents"],
            "category_id": row["category_id"], "event_id": row["event_id"], "station": row["station"], "enabled": bool(row["enabled"]),
            "has_image": bool(row["has_image"]) if "has_image" in row.keys() else False}


def require_station(value):
    if value not in ("KITCHEN", "BAR"):
        raise InputError("Station must be KITCHEN or BAR")
    return value


def category_record(row):
    return {"id": row["id"], "name": row["name"], "parent_id": row["parent_id"], "event_id": row["event_id"],
            "has_image": bool(row["has_image"]) if "has_image" in row.keys() else False}


CATEGORY_SELECT = "SELECT menu_categories.*, EXISTS(SELECT 1 FROM category_images WHERE category_id = menu_categories.id) AS has_image FROM menu_categories"


def validate_category_parent(connection, parent_id, category_id=None, event_id=None):
    if parent_id is None:
        return
    if type(parent_id) is not int or parent_id < 1:
        raise InputError("Invalid parent category")
    seen = {category_id} if category_id is not None else set()
    while parent_id is not None:
        if parent_id in seen:
            raise InputError("A category cannot contain itself")
        seen.add(parent_id)
        row = connection.execute("SELECT parent_id, event_id FROM menu_categories WHERE id = ?", (parent_id,)).fetchone()
        if not row:
            raise InputError("Parent category not found")
        if event_id is not None and row["event_id"] != event_id:
            raise InputError("Parent category belongs to another Event")
        parent_id = row["parent_id"]


def validate_sibling_name(connection, name, parent_id, category_id=None, event_id=None):
    row = connection.execute("SELECT id FROM menu_categories WHERE event_id = ? AND parent_id IS ? AND name = ? COLLATE NOCASE", (event_id, parent_id, name)).fetchone()
    if row and row["id"] != category_id:
        raise InputError("A category with this name already exists at this level")


def order_record(connection, row):
    lines = connection.execute("SELECT id, menu_item_id, item_name, quantity, price_cents, line_total_cents, station, issued FROM order_lines WHERE order_id = ? ORDER BY id", (row["id"],)).fetchall()
    paid = bool(row["paid"])
    return {"id": row["id"], "event_id": row["event_id"], "request_id": row["request_id"], "number": row["id"], "paid": paid, "comment": row["comment"],
            "completed": paid and all(line["issued"] for line in lines),
            "revision": row["revision"],
            "total_cents": row["total_cents"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], "lines": [{**dict(line), "issued": bool(line["issued"])} for line in lines]}


def parse_order_lines(raw_lines, allow_empty=False):
    if not isinstance(raw_lines, list) or not (0 if allow_empty else 1) <= len(raw_lines) <= 100:
        raise InputError("Order needs 1 to 100 items" if not allow_empty else "Order allows up to 100 items")
    parsed = []
    seen = set()
    for line in raw_lines:
        if not isinstance(line, dict) or type(line.get("menu_item_id")) is not int or type(line.get("quantity")) is not int:
            raise InputError("Each line needs a menu item and whole-number quantity")
        item_id, quantity = line["menu_item_id"], line["quantity"]
        if item_id in seen or not 1 <= quantity <= 999:
            raise InputError("Items must be unique and quantity must be 1 to 999")
        seen.add(item_id)
        parsed.append((item_id, quantity))
    return parsed


def resolve_order_lines(connection, parsed, event_id, existing=None):
    existing = existing or {}
    lines = []
    for item_id, quantity in parsed:
        if item_id in existing:
            name, price_cents, station, issued = existing[item_id]
        else:
            item = connection.execute("SELECT * FROM menu_items WHERE id = ? AND event_id = ? AND enabled = 1", (item_id, event_id)).fetchone()
            if not item:
                raise InputError("A menu item is no longer available. Refresh the menu and try again.")
            name, price_cents, station, issued = item["name"], item["price_cents"], item["station"], False
        lines.append((item_id, name, quantity, price_cents, quantity * price_cents, station, int(issued)))
    return lines


def insert_order_lines(connection, order_id, lines):
    connection.executemany("INSERT INTO order_lines (order_id, menu_item_id, item_name, quantity, price_cents, line_total_cents, station, issued) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           [(order_id, *line) for line in lines])


def create_order(connection, payload, event_id):
    request_id = require_string(payload.get("request_id"), "Request ID", 100)
    comment = order_comment(payload.get("comment", ""))
    parsed = parse_order_lines(payload.get("lines"))
    with DB_LOCK:
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing = connection.execute("SELECT * FROM orders WHERE request_id = ?", (request_id,)).fetchone()
            if existing:
                if existing["event_id"] != event_id:
                    raise ConflictError("Request ID belongs to another Event")
                connection.execute("COMMIT")
                return order_record(connection, existing)
            lines = resolve_order_lines(connection, parsed, event_id)
            total = sum(line[4] for line in lines)
            cursor = connection.execute("INSERT INTO orders (request_id, event_id, comment, total_cents) VALUES (?, ?, ?, ?)", (request_id, event_id, comment, total))
            order_id = cursor.lastrowid
            insert_order_lines(connection, order_id, lines)
            connection.execute("COMMIT")
            return order_record(connection, connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())
        except Exception:
            connection.execute("ROLLBACK")
            raise


def require_revision(value):
    if type(value) is not int or value < 0:
        raise InputError("Expected order revision is required")
    return value


def order_comment(value):
    if not isinstance(value, str) or len(value) > 40:
        raise InputError("Comment must be at most 40 characters")
    return value


def update_order(connection, order_id, payload):
    revision = require_revision(payload.get("expected_revision"))
    comment = order_comment(payload["comment"]) if "comment" in payload else None
    if payload.get("line_mode") == "identified":
        return update_identified_order(connection, order_id, payload, revision, comment)
    parsed = parse_order_lines(payload.get("lines"), allow_empty=True)
    with DB_LOCK:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if not row:
                raise InputError("Order not found")
            if row["revision"] != revision:
                raise ConflictError("This order changed on another device. Reload it before editing.")
            if row["paid"]:
                raise ConflictError("Cancel payment before editing this order.")
            old_lines = connection.execute("SELECT menu_item_id, item_name, price_cents, station, issued, quantity FROM order_lines WHERE order_id = ?", (order_id,)).fetchall()
            if len({line["menu_item_id"] for line in old_lines}) != len(old_lines):
                raise ConflictError("This order has repeated menu items. Reload with the current app before editing.")
            requested = dict(parsed)
            if any(line["issued"] and requested.get(line["menu_item_id"]) != line["quantity"] for line in old_lines):
                raise ConflictError("Issued items cannot be changed or removed")
            snapshots = {line["menu_item_id"]: (line["item_name"], line["price_cents"], line["station"], line["issued"]) for line in old_lines}
            lines = resolve_order_lines(connection, parsed, row["event_id"], snapshots)
            connection.execute("DELETE FROM order_lines WHERE order_id = ?", (order_id,))
            insert_order_lines(connection, order_id, lines)
            connection.execute("UPDATE orders SET total_cents = ?, comment = ?, revision = revision + 1, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                               (sum(line[4] for line in lines), row["comment"] if comment is None else comment, order_id))
            connection.execute("COMMIT")
            return order_record(connection, connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())
        except Exception:
            connection.execute("ROLLBACK")
            raise


def update_identified_order(connection, order_id, payload, revision, comment):
    raw_lines = payload.get("lines")
    if not isinstance(raw_lines, list) or len(raw_lines) > 100:
        raise InputError("Order allows up to 100 items")
    with DB_LOCK:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if not row:
                raise InputError("Order not found")
            if row["revision"] != revision:
                raise ConflictError("This order changed on another device. Reload it before editing.")
            if row["paid"]:
                raise ConflictError("Cancel payment before editing this order.")
            existing = {line["id"]: line for line in connection.execute("SELECT * FROM order_lines WHERE order_id = ?", (order_id,))}
            seen = set()
            unissued_items = set()
            changes = []
            for raw in raw_lines:
                if not isinstance(raw, dict) or type(raw.get("menu_item_id")) is not int or type(raw.get("quantity")) is not int or not 1 <= raw["quantity"] <= 999:
                    raise InputError("Each line needs a menu item and quantity from 1 to 999")
                line_id = raw.get("line_id")
                if line_id is not None:
                    if type(line_id) is not int or line_id not in existing or line_id in seen or existing[line_id]["menu_item_id"] != raw["menu_item_id"]:
                        raise InputError("Unknown or repeated order line")
                    old = existing[line_id]
                    if old["issued"] and old["quantity"] != raw["quantity"]:
                        raise ConflictError("Issued items cannot be changed or removed")
                    if not old["issued"]:
                        unissued_items.add(old["menu_item_id"])
                    seen.add(line_id)
                    changes.append((old, raw["quantity"]))
                else:
                    item = connection.execute("SELECT * FROM menu_items WHERE id = ? AND event_id = ? AND enabled = 1", (raw["menu_item_id"], row["event_id"])).fetchone()
                    if not item:
                        raise InputError("A menu item is no longer available. Refresh the menu and try again.")
                    if item["id"] in unissued_items:
                        raise InputError("Only one unissued line per menu item is allowed")
                    unissued_items.add(item["id"])
                    changes.append((item, raw["quantity"]))
            if any(line["issued"] and line_id not in seen for line_id, line in existing.items()):
                raise ConflictError("Issued items cannot be changed or removed")
            for line_id, old in existing.items():
                if line_id not in seen:
                    connection.execute("DELETE FROM order_lines WHERE id = ?", (line_id,))
            for item, quantity in changes:
                if "menu_item_id" in item.keys():
                    connection.execute("UPDATE order_lines SET quantity = ?, line_total_cents = price_cents * ? WHERE id = ?", (quantity, quantity, item["id"]))
                else:
                    connection.execute("INSERT INTO order_lines (order_id, menu_item_id, item_name, quantity, price_cents, line_total_cents, station) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                       (order_id, item["id"], item["name"], quantity, item["price_cents"], quantity * item["price_cents"], item["station"]))
            total = connection.execute("SELECT COALESCE(SUM(line_total_cents), 0) FROM order_lines WHERE order_id = ?", (order_id,)).fetchone()[0]
            connection.execute("UPDATE orders SET total_cents = ?, comment = ?, revision = revision + 1, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?", (total, row["comment"] if comment is None else comment, order_id))
            connection.execute("COMMIT")
            return order_record(connection, connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())
        except Exception:
            connection.execute("ROLLBACK")
            raise


def set_order_paid(connection, order_id, paid):
    with DB_LOCK:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if not row:
                raise InputError("Order not found")
            if bool(row["paid"]) == paid:
                connection.execute("COMMIT")
                return order_record(connection, row)
            if paid and not connection.execute("SELECT 1 FROM order_lines WHERE order_id = ? LIMIT 1", (order_id,)).fetchone():
                raise InputError("An empty order cannot be marked paid")
            connection.execute("UPDATE orders SET paid = ?, revision = revision + 1, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?", (int(paid), order_id))
            connection.execute("COMMIT")
            return order_record(connection, connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())
        except Exception:
            connection.execute("ROLLBACK")
            raise


def set_line_issued(connection, order_id, item_id, issued, by_line_id=False):
    with DB_LOCK:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if not row:
                raise InputError("Order not found")
            column = "id" if by_line_id else "menu_item_id"
            matches = connection.execute(f"SELECT id, issued FROM order_lines WHERE order_id = ? AND {column} = ?", (order_id, item_id)).fetchall()
            if not matches:
                raise InputError("Order item not found")
            if len(matches) != 1:
                raise ConflictError("Multiple lines have this menu item. Use the order line ID.")
            line = matches[0]
            if bool(line["issued"]) != issued:
                connection.execute("UPDATE order_lines SET issued = ? WHERE id = ?", (int(issued), line["id"]))
                connection.execute("UPDATE orders SET revision = revision + 1, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?", (order_id,))
            connection.execute("COMMIT")
            return order_record(connection, connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())
        except Exception:
            connection.execute("ROLLBACK")
            raise


def set_station_lines_issued(connection, order_id, station, line_ids, issued):
    require_station(station)
    if not isinstance(line_ids, list) or not line_ids or len(line_ids) > 100 or any(type(line_id) is not int or line_id < 1 for line_id in line_ids) or len(set(line_ids)) != len(line_ids):
        raise InputError("Line IDs must be a nonempty list of unique positive integers")
    with DB_LOCK:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if not row:
                raise InputError("Order not found")
            placeholders = ",".join("?" for _ in line_ids)
            lines = connection.execute(f"SELECT id, station, issued FROM order_lines WHERE order_id = ? AND id IN ({placeholders})", (order_id, *line_ids)).fetchall()
            if len(lines) != len(line_ids) or any(line["station"] != station for line in lines):
                raise InputError("Lines must belong to this order and station")
            changed = [line["id"] for line in lines if bool(line["issued"]) != issued]
            if changed:
                connection.executemany("UPDATE order_lines SET issued = ? WHERE id = ?", [(int(issued), line_id) for line_id in changed])
                connection.execute("UPDATE orders SET revision = revision + 1, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?", (order_id,))
            connection.execute("COMMIT")
            return order_record(connection, connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())
        except Exception:
            connection.execute("ROLLBACK")
            raise


class Handler(BaseHTTPRequestHandler):
    server_version = "PopUpOrders/1.0"

    def log_message(self, format, *args):
        # Polling from several devices should not bury the startup address.
        if self.command != "GET":
            super().log_message(format, *args)

    def json_response(self, status, value):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise InputError("Invalid Content-Length")
        if not 0 < length <= MAX_BODY:
            raise InputError("Request body is empty or too large")
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise InputError("Content-Type must be application/json")
        try:
            value = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            raise InputError("Invalid JSON")
        if not isinstance(value, dict):
            raise InputError("Expected a JSON object")
        return value

    def read_image(self):
        mime = self.headers.get("Content-Type", "").split(";")[0]
        if mime not in ("image/jpeg", "image/png", "image/webp"):
            raise InputError("Use a JPEG, PNG, or WebP image")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise InputError("Invalid Content-Length")
        if not 0 < length <= MAX_IMAGE:
            raise InputError("Image must be smaller than 3 MB")
        data = self.rfile.read(length)
        valid = ((mime == "image/jpeg" and data.startswith(b"\xff\xd8\xff")) or
                 (mime == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n")) or
                 (mime == "image/webp" and data.startswith(b"RIFF") and data[8:12] == b"WEBP"))
        if not valid:
            raise InputError("Invalid image data")
        return mime, data

    def image_response(self, mime, data):
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def handle_api(self, method, path, query):
        with closing(db()) as connection:
            parts = path.strip("/").split("/")
            if path == "/api/health" and method == "GET":
                return 200, {"ok": True}
            if path == "/api/info" and method == "GET":
                return 200, {"addresses": [f"http://{address}:{self.server.server_port}/" for address in local_addresses()]}
            if path == "/api/events" and method == "GET":
                selected_id = selected_event_id(connection)
                rows = connection.execute("SELECT * FROM events ORDER BY event_date DESC, id DESC").fetchall()
                return 200, {"events": [event_record(row, selected_id) for row in rows], "selected_event_id": selected_id}
            if path == "/api/events" and method == "POST":
                body = self.read_json()
                name = require_string(body.get("name"), "Event name", 100)
                date = event_date(body.get("date"))
                enabled = body.get("enabled", True)
                if type(enabled) is not bool:
                    raise InputError("Enabled must be true or false")
                with DB_LOCK:
                    cursor = connection.execute("INSERT INTO events (name, event_date, enabled) VALUES (?, ?, ?)", (name, date, int(enabled)))
                return 201, event_record(connection.execute("SELECT * FROM events WHERE id = ?", (cursor.lastrowid,)).fetchone(), selected_event_id(connection))
            if len(parts) >= 3 and parts[:2] == ["api", "events"] and parts[2].isdigit():
                event_id = int(parts[2])
                row = connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
                if not row:
                    return 404, {"error": "Event not found"}
                if len(parts) == 3 and method == "GET":
                    return 200, event_record(row, selected_event_id(connection))
                if len(parts) == 3 and method == "PUT":
                    body = self.read_json()
                    name = require_string(body.get("name"), "Event name", 100)
                    date = event_date(body.get("date"))
                    enabled = body.get("enabled")
                    if type(enabled) is not bool:
                        raise InputError("Enabled must be true or false")
                    with DB_LOCK:
                        connection.execute("BEGIN IMMEDIATE")
                        try:
                            if not enabled and event_id == selected_event_id(connection):
                                raise InputError("Select another Event before disabling this one")
                            connection.execute("UPDATE events SET name = ?, event_date = ?, enabled = ? WHERE id = ?", (name, date, int(enabled), event_id))
                            connection.execute("COMMIT")
                        except Exception:
                            connection.execute("ROLLBACK")
                            raise
                    return 200, event_record(connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone(), selected_event_id(connection))
                if len(parts) == 4 and parts[3] == "select" and method == "PUT":
                    with DB_LOCK:
                        connection.execute("BEGIN IMMEDIATE")
                        try:
                            current = connection.execute("SELECT enabled FROM events WHERE id = ?", (event_id,)).fetchone()
                            if not current["enabled"]:
                                raise InputError("Disabled Event cannot be selected")
                            connection.execute("UPDATE app_state SET selected_event_id = ? WHERE id = 1", (event_id,))
                            connection.execute("COMMIT")
                        except Exception:
                            connection.execute("ROLLBACK")
                            raise
                    return 200, event_record(connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone(), event_id)
            if path == "/api/context" and method == "GET":
                selected_id = selected_event_id(connection)
                return 200, {"event": event_record(connection.execute("SELECT * FROM events WHERE id = ?", (selected_id,)).fetchone(), selected_id)}
            event_id = selected_event_id(connection)
            if method in ("POST", "PUT", "DELETE") and parts[:2] in (["api", "categories"], ["api", "menu"], ["api", "orders"]):
                event_id = require_selected_event(connection, self.headers.get("X-Event-Id"))
            if path == "/api/categories" and method == "GET":
                rows = connection.execute(CATEGORY_SELECT + " WHERE menu_categories.event_id = ? ORDER BY name COLLATE NOCASE, id", (event_id,)).fetchall()
                return 200, {"event_id": event_id, "categories": [category_record(row) for row in rows]}
            if path == "/api/categories" and method == "POST":
                body = self.read_json()
                name = require_string(body.get("name"), "Category name", 100)
                parent_id = body.get("parent_id")
                with DB_LOCK:
                    validate_category_parent(connection, parent_id, event_id=event_id)
                    validate_sibling_name(connection, name, parent_id, event_id=event_id)
                    cursor = connection.execute("INSERT INTO menu_categories (name, parent_id, event_id) VALUES (?, ?, ?)", (name, parent_id, event_id))
                return 201, category_record(connection.execute(CATEGORY_SELECT + " WHERE menu_categories.id = ?", (cursor.lastrowid,)).fetchone())
            if len(parts) == 3 and parts[:2] == ["api", "categories"] and parts[2].isdigit() and method in ("PUT", "DELETE"):
                category_id = int(parts[2])
                with DB_LOCK:
                    category = connection.execute("SELECT * FROM menu_categories WHERE id = ? AND event_id = ?", (category_id, event_id)).fetchone()
                    if not category:
                        return 404, {"error": "Category not found"}
                    if method == "DELETE":
                        children = connection.execute("SELECT 1 FROM menu_categories WHERE parent_id = ? LIMIT 1", (category_id,)).fetchone()
                        items = connection.execute("SELECT 1 FROM menu_items WHERE category_id = ? LIMIT 1", (category_id,)).fetchone()
                        if children or items:
                            raise InputError("Move or remove items and subcategories before deleting this category")
                        connection.execute("DELETE FROM menu_categories WHERE id = ?", (category_id,))
                        return 200, {"deleted": True}
                    body = self.read_json()
                    name = require_string(body.get("name"), "Category name", 100)
                    parent_id = body.get("parent_id")
                    validate_category_parent(connection, parent_id, category_id, event_id)
                    validate_sibling_name(connection, name, parent_id, category_id, event_id)
                    connection.execute("UPDATE menu_categories SET name = ?, parent_id = ? WHERE id = ?", (name, parent_id, category_id))
                return 200, category_record(connection.execute(CATEGORY_SELECT + " WHERE menu_categories.id = ?", (category_id,)).fetchone())
            if len(parts) == 4 and parts[:2] == ["api", "categories"] and parts[2].isdigit() and parts[3] == "image" and method in ("PUT", "DELETE"):
                category_id = int(parts[2])
                if not connection.execute("SELECT 1 FROM menu_categories WHERE id = ? AND event_id = ?", (category_id, event_id)).fetchone():
                    return 404, {"error": "Category not found"}
                with DB_LOCK:
                    if method == "PUT":
                        mime, data = self.read_image()
                        connection.execute("INSERT OR REPLACE INTO category_images (category_id, mime, data) VALUES (?, ?, ?)", (category_id, mime, data))
                    else:
                        connection.execute("DELETE FROM category_images WHERE category_id = ?", (category_id,))
                return 200, {"has_image": method == "PUT"}
            if path == "/api/menu" and method == "GET":
                all_items = query.get("all") == ["1"]
                rows = connection.execute("SELECT menu_items.*, EXISTS(SELECT 1 FROM menu_images WHERE menu_item_id = menu_items.id) AS has_image FROM menu_items WHERE event_id = ? " + ("" if all_items else "AND enabled = 1 ") + "ORDER BY name COLLATE NOCASE, id", (event_id,)).fetchall()
                return 200, {"event_id": event_id, "items": [menu_record(row) for row in rows]}
            if path == "/api/menu" and method == "POST":
                body = self.read_json()
                name = require_string(body.get("name"), "Name", 100)
                category_id = body.get("category_id")
                cents = price_to_cents(body.get("price"))
                station = require_station(body.get("station"))
                enabled = body.get("enabled", True)
                if type(enabled) is not bool:
                    raise InputError("Enabled must be true or false")
                with DB_LOCK:
                    validate_category_parent(connection, category_id, event_id=event_id)
                    cursor = connection.execute("INSERT INTO menu_items (name, category_id, event_id, price_cents, station, enabled) VALUES (?, ?, ?, ?, ?, ?)", (name, category_id, event_id, cents, station, int(enabled)))
                return 201, menu_record(connection.execute("SELECT * FROM menu_items WHERE id = ?", (cursor.lastrowid,)).fetchone())
            if len(parts) == 3 and parts[:2] == ["api", "menu"] and parts[2].isdigit() and method in ("PUT", "DELETE"):
                item_id = int(parts[2])
                item = connection.execute("SELECT * FROM menu_items WHERE id = ? AND event_id = ?", (item_id, event_id)).fetchone()
                if not item:
                    return 404, {"error": "Menu item not found"}
                if method == "DELETE":
                    with DB_LOCK:
                        connection.execute("UPDATE menu_items SET enabled = 0 WHERE id = ?", (item_id,))
                else:
                    body = self.read_json()
                    name = require_string(body.get("name"), "Name", 100)
                    category_id = body.get("category_id")
                    cents = price_to_cents(body.get("price"))
                    station = require_station(body.get("station"))
                    enabled = body.get("enabled")
                    if type(enabled) is not bool:
                        raise InputError("Enabled must be true or false")
                    with DB_LOCK:
                        validate_category_parent(connection, category_id, event_id=event_id)
                        connection.execute("UPDATE menu_items SET name = ?, category_id = ?, price_cents = ?, station = ?, enabled = ? WHERE id = ?",
                                           (name, category_id, cents, station, int(enabled), item_id))
                return 200, menu_record(connection.execute("SELECT * FROM menu_items WHERE id = ?", (item_id,)).fetchone())
            if len(parts) == 4 and parts[:2] == ["api", "menu"] and parts[2].isdigit() and parts[3] == "image" and method in ("PUT", "DELETE"):
                item_id = int(parts[2])
                if not connection.execute("SELECT 1 FROM menu_items WHERE id = ? AND event_id = ?", (item_id, event_id)).fetchone():
                    return 404, {"error": "Menu item not found"}
                with DB_LOCK:
                    if method == "PUT":
                        mime, data = self.read_image()
                        connection.execute("INSERT OR REPLACE INTO menu_images (menu_item_id, mime, data) VALUES (?, ?, ?)", (item_id, mime, data))
                    else:
                        connection.execute("DELETE FROM menu_images WHERE menu_item_id = ?", (item_id,))
                return 200, {"has_image": method == "PUT"}
            if path == "/api/orders" and method == "GET":
                rows = connection.execute("SELECT * FROM orders WHERE event_id = ? ORDER BY id DESC", (event_id,)).fetchall()
                return 200, {"event_id": event_id, "orders": [order_record(connection, row) for row in rows]}
            if path == "/api/orders" and method == "POST":
                return 201, create_order(connection, self.read_json(), event_id)
            if len(parts) >= 3 and parts[:2] == ["api", "orders"] and parts[2].isdigit():
                order_id = int(parts[2])
                row = connection.execute("SELECT * FROM orders WHERE id = ? AND event_id = ?", (order_id, event_id)).fetchone()
                if not row:
                    return 404, {"error": "Order not found"}
                if len(parts) == 3 and method == "GET":
                    return 200, order_record(connection, row)
                if len(parts) == 3 and method == "PUT":
                    return 200, update_order(connection, order_id, self.read_json())
                if len(parts) == 4 and parts[3] == "status" and method == "PUT":
                    body = self.read_json()
                    if body.get("status") not in ("paid", "unpaid"):
                        raise InputError("Status must be paid or unpaid")
                    return 200, set_order_paid(connection, order_id, body["status"] == "paid")
                if len(parts) == 4 and parts[3] == "station-lines" and method == "PUT":
                    body = self.read_json()
                    if type(body.get("issued")) is not bool:
                        raise InputError("Issued must be true or false")
                    return 200, set_station_lines_issued(connection, order_id, body.get("station"), body.get("line_ids"), body["issued"])
                if len(parts) == 5 and parts[3] == "lines" and parts[4].isdigit() and method == "PUT":
                    body = self.read_json()
                    if type(body.get("issued")) is not bool:
                        raise InputError("Issued must be true or false")
                    return 200, set_line_issued(connection, order_id, int(parts[4]), body["issued"])
                if len(parts) == 5 and parts[3] == "order-lines" and parts[4].isdigit() and method == "PUT":
                    body = self.read_json()
                    if type(body.get("issued")) is not bool:
                        raise InputError("Issued must be true or false")
                    return 200, set_line_issued(connection, order_id, int(parts[4]), body["issued"], by_line_id=True)
            return 404, {"error": "Not found"}

    def handle_request(self, method):
        parsed = urlsplit(self.path)
        parts = parsed.path.strip("/").split("/")
        if method == "GET" and len(parts) == 4 and parts[:2] == ["api", "categories"] and parts[2].isdigit() and parts[3] == "image":
            with closing(db()) as connection:
                row = connection.execute("SELECT category_images.mime, category_images.data FROM category_images JOIN menu_categories ON menu_categories.id = category_images.category_id WHERE category_id = ? AND menu_categories.event_id = ?", (int(parts[2]), selected_event_id(connection))).fetchone()
            if not row:
                self.json_response(404, {"error": "Image not found"})
            else:
                self.image_response(row["mime"], row["data"])
            return
        if method == "GET" and len(parts) == 4 and parts[:2] == ["api", "menu"] and parts[2].isdigit() and parts[3] == "image":
            with closing(db()) as connection:
                row = connection.execute("SELECT menu_images.mime, menu_images.data FROM menu_images JOIN menu_items ON menu_items.id = menu_images.menu_item_id WHERE menu_item_id = ? AND menu_items.event_id = ?", (int(parts[2]), selected_event_id(connection))).fetchone()
            if not row:
                self.json_response(404, {"error": "Image not found"})
            else:
                self.image_response(row["mime"], row["data"])
            return
        if parsed.path.startswith("/api/"):
            try:
                status, value = self.handle_api(method, parsed.path, parse_qs(parsed.query))
            except InputError as exc:
                status, value = 400, {"error": str(exc)}
            except ConflictError as exc:
                status, value = 409, {"error": str(exc)}
            except sqlite3.Error:
                self.log_error("Database operation failed")
                status, value = 500, {"error": "Server database error"}
            self.json_response(status, value)
            return
        if method != "GET":
            self.json_response(405, {"error": "Method not allowed"})
            return
        files = {"/": ("index.html", "text/html"), "/admin": ("admin.html", "text/html"),
                 "/reception": ("index.html", "text/html"), "/kitchen": ("station.html", "text/html"), "/bar": ("station.html", "text/html"),
                 "/admin.js": ("admin.js", "text/javascript"), "/pos.js": ("pos.js", "text/javascript"),
                 "/station.js": ("station.js", "text/javascript"),
                 "/bar_entry.js": ("bar_entry.js", "text/javascript"),
                 "/event_context.js": ("event_context.js", "text/javascript"),
                 "/undo.js": ("undo.js", "text/javascript"),
                 "/default-dish.svg": ("default-dish.svg", "image/svg+xml"),
                 "/style.css": ("style.css", "text/css")}
        if parsed.path not in files:
            self.json_response(404, {"error": "Not found"})
            return
        filename, mime = files[parsed.path]
        data = (STATIC / filename).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.handle_request("GET")

    def do_POST(self):
        self.handle_request("POST")

    def do_PUT(self):
        self.handle_request("PUT")

    def do_DELETE(self):
        self.handle_request("DELETE")


def local_addresses():
    addresses = set()
    preferred = None
    if platform.system() == "Darwin":
        try:
            ports = subprocess.run(["networksetup", "-listallhardwareports"], capture_output=True, text=True, timeout=3, check=False).stdout
            wifi = re.search(r"Hardware Port: Wi-Fi\s+Device: (\w+)", ports)
            if wifi:
                details = subprocess.run(["ifconfig", wifi.group(1)], capture_output=True, text=True, timeout=2, check=False).stdout
                match = re.search(r"\binet (\d{1,3}(?:\.\d{1,3}){3})\b", details)
                if match:
                    preferred = match.group(1)
                    addresses.add(preferred)
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        for entry in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(entry[4][0])
    except socket.gaierror:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("192.0.2.1", 80))  # Route lookup only; sends no packet.
        routed = probe.getsockname()[0]
        addresses.add(routed)
        if preferred is None:
            preferred = routed
        probe.close()
    except OSError:
        pass
    # Some hotspot setups have no default route. Read interface addresses locally as well.
    for command in (["ifconfig"], ["ip", "-4", "addr"], ["ipconfig"]):
        try:
            output = subprocess.run(command, capture_output=True, text=True, timeout=2, check=False).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        addresses.update(re.findall(r"\b(?:inet(?: addr:)?|IPv4 Address[^:]*:)\s*(\d{1,3}(?:\.\d{1,3}){3})", output))
    found = sorted(ip for ip in addresses if not ip.startswith(("127.", "169.254.")) and ip != "0.0.0.0")
    if preferred in found:
        found.remove(preferred)
        found.insert(0, preferred)
    return found


def main():
    global DB_PATH
    parser = argparse.ArgumentParser(description="LAN-first pop-up bar order server")
    parser.add_argument("--port", type=int, default=8080, help="listening port (default: 8080)")
    parser.add_argument("--data", type=Path, default=ROOT / "data" / "orders.sqlite3", help="SQLite database path")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1 to 65535")
    DB_PATH = args.data.resolve()
    init_db()
    try:
        server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    except OSError as exc:
        sys.exit(f"Cannot start server on port {args.port}: {exc}")
    print(f"\nPop-up orders ready. Data: {DB_PATH}")
    print(f"On this laptop: http://localhost:{args.port}/")
    addresses = local_addresses()
    if addresses:
        print("On other devices connected to the same Wi-Fi, try:")
        client_url = f"http://{addresses[0]}:{args.port}/"
        print(f"  {client_url}")
        print("Scan this QR code from a device on the same Wi-Fi:")
        print(render_terminal_qr(client_url))
        if len(addresses) > 1:
            print("Other detected local addresses (if the first does not work):")
            for address in addresses[1:]:
                print(f"  http://{address}:{args.port}/")
    else:
        print("No Wi-Fi/LAN address found yet. Connect to local Wi-Fi, then restart the server.")
    print("Views: /reception, /kitchen, /bar, /admin. Press Ctrl+C to stop.\n", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopping server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
