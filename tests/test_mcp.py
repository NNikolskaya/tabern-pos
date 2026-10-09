import base64
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import app
import mcp_server


class McpTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        app.DB_PATH = Path(self.temp.name) / "orders.sqlite3"
        app.init_db()
        self.first_id = mcp_server.list_events({})["events"][0]["id"]

    def tearDown(self):
        self.temp.cleanup()

    def test_event_scoped_preview_and_confirmed_import(self):
        second = mcp_server.create_event({"name": "Kabak", "date": "2026-10-10"})
        self.assertEqual(self.first_id, mcp_server.list_events({})["events"][-1]["id"])
        items = [{"name": "Beer", "price": "80.00", "station": "BAR", "categoryPath": ["Drinks", "Alcohol"]},
                 {"name": "Soup", "price": "125", "station": "KITCHEN", "categoryPath": ["Food"]}]
        args = {"eventId": second["id"], "items": items, "dryRun": True}
        preview = mcp_server.bulk_upsert_menu(args)
        self.assertEqual({"create": 2, "update": 0, "unchanged": 0}, preview["counts"])
        self.assertEqual([], mcp_server.list_menu({"eventId": second["id"]})["items"])
        with self.assertRaises(app.InputError):
            mcp_server.bulk_upsert_menu({**args, "dryRun": False, "previewToken": preview["previewToken"]})
        with self.assertRaises(app.InputError):
            mcp_server.list_menu({})
        with self.assertRaises(app.InputError):
            mcp_server.bulk_upsert_menu({"items": items, "dryRun": True})

        applied = mcp_server.bulk_upsert_menu({**args, "dryRun": False, "confirmed": True, "previewToken": preview["previewToken"]})
        self.assertTrue(applied["imported"])
        listed = mcp_server.list_menu({"eventId": second["id"]})
        self.assertEqual({"Beer", "Soup"}, {item["name"] for item in listed["items"]})
        self.assertEqual(3, len(listed["categories"]))
        self.assertEqual([], mcp_server.list_menu({"eventId": self.first_id})["items"])
        self.assertEqual({"create": 0, "update": 0, "unchanged": 2}, mcp_server.bulk_upsert_menu(args)["counts"])

        changed = [{**items[0], "price": "90.00"}, items[1]]
        changed_preview = mcp_server.bulk_upsert_menu({**args, "items": changed})
        self.assertEqual(1, changed_preview["counts"]["update"])
        with self.assertRaises(app.ConflictError):
            mcp_server.bulk_upsert_menu({**args, "items": changed, "dryRun": False, "confirmed": True,
                                         "previewToken": preview["previewToken"]})
        self.assertEqual(8000, next(item for item in mcp_server.list_menu({"eventId": second["id"]})["items"] if item["name"] == "Beer")["price_cents"])
        mcp_server.bulk_upsert_menu({**args, "items": changed, "dryRun": False, "confirmed": True,
                                     "previewToken": changed_preview["previewToken"]})
        self.assertEqual(9000, next(item for item in mcp_server.list_menu({"eventId": second["id"]})["items"] if item["name"] == "Beer")["price_cents"])

    def test_stdio_protocol_and_explicit_event_id(self):
        process = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "mcp_server.py"),
                                  "--data", str(app.DB_PATH)],
                                 input="\n".join(json.dumps(request) for request in [
                                     {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                                     {"jsonrpc": "2.0", "method": "notifications/initialized"},
                                     {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                                     {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_menu", "arguments": {}}},
                                 ]) + "\n", text=True, capture_output=True, timeout=10)
        self.assertEqual(0, process.returncode, process.stderr)
        replies = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([1, 2, 3], [reply["id"] for reply in replies])
        self.assertIn("tools", replies[1]["result"])
        self.assertIn("update_menu_item", {tool["name"] for tool in replies[1]["result"]["tools"]})
        self.assertTrue(replies[2]["result"]["isError"])

    def test_update_item_and_image_with_preview_and_event_scope(self):
        other_event = mcp_server.create_event({"name": "Other"})["id"]
        source = {"eventId": self.first_id, "items": [{"name": "Beer", "price": "80", "station": "BAR"}], "dryRun": True}
        token = mcp_server.bulk_upsert_menu(source)["previewToken"]
        mcp_server.bulk_upsert_menu({**source, "dryRun": False, "confirmed": True, "previewToken": token})
        item_id = mcp_server.list_menu({"eventId": self.first_id})["items"][0]["id"]
        image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLytQAAAABJRU5ErkJggg==")
        image_path = Path(self.temp.name) / "beer.png"
        image_path.write_bytes(image)
        change = {"eventId": self.first_id, "itemId": item_id, "name": "Lager", "price": "95.50", "imagePath": str(image_path), "dryRun": True}
        preview = mcp_server.update_menu_item(change)
        self.assertEqual("add", preview["imageChange"])
        self.assertEqual(9550, preview["after"]["price_cents"])
        self.assertFalse(mcp_server.list_menu({"eventId": self.first_id})["items"][0]["has_image"])
        with self.assertRaises(app.InputError):
            mcp_server.update_menu_item({**change, "dryRun": False, "previewToken": preview["previewToken"]})
        with self.assertRaises(app.InputError):
            mcp_server.update_menu_item({**change, "eventId": other_event})
        with self.assertRaises(app.InputError):
            mcp_server.update_menu_item({**change, "eventId": None})
        image_path.write_bytes(image + b"changed")
        with self.assertRaises(app.ConflictError):
            mcp_server.update_menu_item({**change, "dryRun": False, "confirmed": True, "previewToken": preview["previewToken"]})
        image_path.write_bytes(image)
        applied = mcp_server.update_menu_item({**change, "dryRun": False, "confirmed": True, "previewToken": preview["previewToken"]})
        self.assertTrue(applied["updated"])
        self.assertEqual([], mcp_server.list_menu({"eventId": other_event})["items"])
        item = mcp_server.list_menu({"eventId": self.first_id})["items"][0]
        self.assertEqual(("Lager", 9550, True), (item["name"], item["price_cents"], item["has_image"]))
        with app.db() as connection:
            stored = connection.execute("SELECT mime, data FROM menu_images WHERE menu_item_id = ?", (item_id,)).fetchone()
            self.assertEqual(("image/png", image), (stored["mime"], stored["data"]))

        remove = {"eventId": self.first_id, "itemId": item_id, "removeImage": True, "dryRun": True}
        remove_token = mcp_server.update_menu_item(remove)["previewToken"]
        mcp_server.update_menu_item({**remove, "dryRun": False, "confirmed": True, "previewToken": remove_token})
        self.assertFalse(mcp_server.list_menu({"eventId": self.first_id})["items"][0]["has_image"])
        encoded = {"eventId": self.first_id, "itemId": item_id, "imageBase64": base64.b64encode(image).decode(), "imageMime": "image/png", "dryRun": True}
        encoded_token = mcp_server.update_menu_item(encoded)["previewToken"]
        mcp_server.update_menu_item({**encoded, "dryRun": False, "confirmed": True, "previewToken": encoded_token})
        self.assertTrue(mcp_server.list_menu({"eventId": self.first_id})["items"][0]["has_image"])
        with self.assertRaises(app.InputError):
            mcp_server.update_menu_item({**encoded, "imageMime": "image/jpeg"})


if __name__ == "__main__":
    unittest.main()
