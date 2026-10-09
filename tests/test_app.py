import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path

import app


class AppTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        app.DB_PATH = Path(self.temp.name) / "orders.sqlite3"
        app.init_db()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, method="GET", data=None, event_id=None):
        payload = json.dumps(data).encode() if data is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        if method != "GET" and path.startswith(("/api/menu", "/api/categories", "/api/orders")):
            if event_id is None:
                with app.db() as connection:
                    event_id = app.selected_event_id(connection)
            headers["X-Event-Id"] = str(event_id)
        req = urllib.request.Request(self.base + path, data=payload, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_menu_order_edit_paid_lock_and_price_snapshot(self):
        _, coffee = self.request("/api/menu", "POST", {"name": "Coffee", "price": "3.50", "category_id": None, "station": "BAR"})
        _, cake = self.request("/api/menu", "POST", {"name": "Cake", "price": "4.25", "category_id": None, "station": "KITCHEN"})
        _, menu = self.request("/api/menu")
        self.assertEqual(2, len(menu["items"]))

        order_input = {"request_id": "device-a-unique-1", "lines": [
            {"menu_item_id": coffee["id"], "quantity": 2},
            {"menu_item_id": cake["id"], "quantity": 3},
        ]}
        status, order = self.request("/api/orders", "POST", order_input)
        self.assertEqual(201, status)
        self.assertEqual(1975, order["total_cents"])
        self.assertEqual([700, 1275], [line["line_total_cents"] for line in order["lines"]])

        # A second client sees the authoritative order; repeated submits cannot duplicate it.
        _, second_client = self.request("/api/orders")
        self.assertEqual(order["id"], second_client["orders"][0]["id"])
        _, repeated = self.request("/api/orders", "POST", order_input)
        self.assertEqual(order["id"], repeated["id"])
        self.assertEqual(1, len(self.request("/api/orders")[1]["orders"]))

        self.request(f"/api/menu/{coffee['id']}", "PUT", {"name": "Large coffee", "price": "5.00", "category_id": None, "station": "BAR", "enabled": True})
        self.request(f"/api/menu/{cake['id']}", "DELETE")
        _, current_menu = self.request("/api/menu")
        self.assertEqual(["Large coffee"], [item["name"] for item in current_menu["items"]])
        _, historical = self.request(f"/api/orders/{order['id']}")
        self.assertEqual("Coffee", historical["lines"][0]["item_name"])
        self.assertEqual(350, historical["lines"][0]["price_cents"])
        self.assertEqual(400, self.request("/api/orders", "POST", {"request_id": "next", "lines": [{"menu_item_id": cake["id"], "quantity": 1}]})[0])

        # Editing keeps the original prices, including a now-disabled item.
        edit = {"expected_revision": 0, "lines": [
            {"menu_item_id": coffee["id"], "quantity": 3},
            {"menu_item_id": cake["id"], "quantity": 1},
        ]}
        _, edited = self.request(f"/api/orders/{order['id']}", "PUT", edit)
        self.assertEqual(1475, edited["total_cents"])
        self.assertEqual(1, edited["revision"])
        self.assertEqual([350, 425], [line["price_cents"] for line in edited["lines"]])
        self.assertEqual(409, self.request(f"/api/orders/{order['id']}", "PUT", edit)[0])

        # Auto-saving a removal of the last line is valid; an empty check stays open.
        _, emptied = self.request(f"/api/orders/{order['id']}", "PUT", {"expected_revision": 1, "lines": []})
        self.assertEqual(0, emptied["total_cents"])
        self.assertEqual(400, self.request(f"/api/orders/{order['id']}/status", "PUT", {"status": "paid", "expected_revision": 2})[0])
        _, edited = self.request(f"/api/orders/{order['id']}", "PUT", {"expected_revision": 2, "lines": [{"menu_item_id": coffee["id"], "quantity": 1}]})
        self.assertEqual(3, edited["revision"])

        _, paid = self.request(f"/api/orders/{order['id']}/status", "PUT", {"status": "paid", "expected_revision": 3})
        self.assertTrue(paid["paid"])
        self.assertEqual(4, paid["revision"])
        self.assertTrue(self.request("/api/orders")[1]["orders"][0]["paid"])
        self.assertEqual(409, self.request(f"/api/orders/{order['id']}", "PUT", {"expected_revision": 4, "lines": [{"menu_item_id": coffee["id"], "quantity": 2}]})[0])
        _, unpaid = self.request(f"/api/orders/{order['id']}/status", "PUT", {"status": "unpaid", "expected_revision": 4})
        self.assertFalse(unpaid["paid"])
        self.assertEqual(unpaid["revision"], self.request(f"/api/orders/{order['id']}/status", "PUT", {"status": "unpaid"})[1]["revision"])
        _, reopened_edit = self.request(f"/api/orders/{order['id']}", "PUT", {"expected_revision": unpaid["revision"], "lines": [{"menu_item_id": coffee["id"], "quantity": 2}]})
        self.assertEqual(2, reopened_edit["lines"][0]["quantity"])
        self.assertTrue(self.request(f"/api/orders/{order['id']}/status", "PUT", {"status": "paid", "expected_revision": 3})[1]["paid"])

        # A fresh SQLite connection sees menu and orders after server restart.
        with app.db() as connection:
            self.assertEqual(1, connection.execute("SELECT count(*) FROM orders").fetchone()[0])
            self.assertEqual(2, connection.execute("SELECT count(*) FROM menu_items").fetchone()[0])

    def test_concurrent_clients_and_validation(self):
        _, item = self.request("/api/menu", "POST", {"name": "Tea", "price": "2.00", "category": "", "station": "BAR"})
        def create(index):
            return self.request("/api/orders", "POST", {"request_id": f"client-{index}", "lines": [{"menu_item_id": item["id"], "quantity": 1}]})
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(create, range(20)))
        self.assertTrue(all(status == 201 for status, _ in results))
        self.assertEqual(20, len({order["id"] for _, order in results}))
        self.assertEqual(20, len(self.request("/api/orders")[1]["orders"]))
        self.assertEqual(400, self.request("/api/menu", "POST", {"name": "Bad", "price": "1.234"})[0])
        self.assertEqual(400, self.request("/api/orders", "POST", {"request_id": "bad", "lines": [{"menu_item_id": item["id"], "quantity": 0}]})[0])

    def test_order_comment_limit_persistence_and_paid_edit_lock(self):
        _, item = self.request("/api/menu", "POST", {"name": "Tea", "price": "2", "category_id": None, "station": "BAR"})
        forty = "Ї" * 40
        payload = {"request_id": "comment-order", "comment": forty, "lines": [{"menu_item_id": item["id"], "quantity": 1}]}
        self.assertEqual(400, self.request("/api/orders", "POST", {**payload, "request_id": "too-long", "comment": forty + "Ї"})[0])
        _, created = self.request("/api/orders", "POST", payload)
        self.assertEqual(forty, created["comment"])
        self.assertEqual(forty, self.request("/api/orders")[1]["orders"][0]["comment"])
        self.assertEqual(400, self.request(f"/api/orders/{created['id']}", "PUT", {"expected_revision": 0, "comment": forty + "Ї", "line_mode": "identified", "lines": [{"line_id": created["lines"][0]["id"], "menu_item_id": item["id"], "quantity": 1}]})[0])
        _, updated = self.request(f"/api/orders/{created['id']}", "PUT", {"expected_revision": 0, "comment": "Без льоду", "line_mode": "identified", "lines": [{"line_id": created["lines"][0]["id"], "menu_item_id": item["id"], "quantity": 1}]})
        self.assertEqual("Без льоду", updated["comment"])
        _, paid = self.request(f"/api/orders/{created['id']}/status", "PUT", {"status": "paid"})
        self.assertEqual(409, self.request(f"/api/orders/{created['id']}", "PUT", {"expected_revision": paid["revision"], "comment": "Інший", "line_mode": "identified", "lines": [{"line_id": created["lines"][0]["id"], "menu_item_id": item["id"], "quantity": 1}]})[0])
        _, unpaid = self.request(f"/api/orders/{created['id']}/status", "PUT", {"status": "unpaid"})
        _, updated = self.request(f"/api/orders/{created['id']}", "PUT", {"expected_revision": unpaid["revision"], "comment": "Інший", "lines": [{"menu_item_id": item["id"], "quantity": 2}]})
        self.assertEqual("Інший", updated["comment"])
        self.assertEqual(2, updated["lines"][0]["quantity"])
        app.init_db()
        self.assertEqual("Інший", self.request(f"/api/orders/{created['id']}")[1]["comment"])

    def test_events_isolate_menu_orders_and_stale_clients(self):
        _, initial = self.request("/api/events")
        first_id = initial["selected_event_id"]
        self.assertEqual(1, len(initial["events"]))
        _, first_item = self.request("/api/menu", "POST", {"name": "Beer", "price": "3", "category_id": None, "station": "BAR"})
        _, first_order = self.request("/api/orders", "POST", {"request_id": "first-event-order", "lines": [{"menu_item_id": first_item["id"], "quantity": 1}]})
        status, second = self.request("/api/events", "POST", {"name": "Kabak", "date": "2026-10-10"})
        self.assertEqual(201, status)
        self.assertFalse(second["selected"])
        self.assertEqual(400, self.request("/api/events", "POST", {"name": "Bad date", "date": "2026-13-10"})[0])
        _, selected = self.request(f"/api/events/{second['id']}/select", "PUT")
        self.assertTrue(selected["selected"])
        self.assertEqual(second["id"], self.request("/api/context")[1]["event"]["id"])
        self.assertEqual([], self.request("/api/menu")[1]["items"])
        self.assertEqual([], self.request("/api/orders")[1]["orders"])
        self.assertEqual(404, self.request(f"/api/orders/{first_order['id']}")[0])
        self.assertEqual(409, self.request("/api/menu", "POST", {"name": "Wrong", "price": "1", "category_id": None, "station": "BAR"}, event_id=first_id)[0])
        raw = urllib.request.Request(self.base + "/api/menu", data=json.dumps({"name": "Missing context", "price": "1", "category_id": None, "station": "BAR"}).encode(), headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(raw, timeout=5)
        self.assertEqual(400, missing.exception.code)
        self.assertEqual(400, self.request("/api/orders", "POST", {"request_id": "cross-event", "lines": [{"menu_item_id": first_item["id"], "quantity": 1}]})[0])
        _, second_item = self.request("/api/menu", "POST", {"name": "Beer", "price": "4", "category_id": None, "station": "BAR"})
        _, second_order = self.request("/api/orders", "POST", {"request_id": "second-event-order", "lines": [{"menu_item_id": second_item["id"], "quantity": 1}]})
        self.assertEqual(second["id"], second_order["event_id"])
        self.assertEqual(400, self.request(f"/api/events/{second['id']}", "PUT", {"name": "Kabak", "date": "2026-10-10", "enabled": False})[0])
        self.request(f"/api/events/{first_id}/select", "PUT")
        self.assertEqual([first_item["id"]], [item["id"] for item in self.request("/api/menu")[1]["items"]])
        self.assertEqual([first_order["id"]], [order["id"] for order in self.request("/api/orders")[1]["orders"]])
        _, edited = self.request(f"/api/events/{second['id']}", "PUT", {"name": "Kabak — Saturday", "date": "2026-10-10", "enabled": False})
        self.assertEqual("Kabak — Saturday", edited["name"])
        self.assertFalse(edited["enabled"])
        self.assertEqual(400, self.request(f"/api/events/{second['id']}/select", "PUT")[0])
        with app.db() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO order_lines (order_id, menu_item_id, item_name, quantity, price_cents, line_total_cents) VALUES (?, ?, 'Wrong', 1, 1, 1)", (first_order["id"], second_item["id"]))

    def test_independent_station_fulfillment_and_payment(self):
        _, kitchen = self.request("/api/menu", "POST", {"name": "Soup", "price": "5.00", "category_id": None, "station": "KITCHEN"})
        _, bar = self.request("/api/menu", "POST", {"name": "Juice", "price": "2.00", "category_id": None, "station": "BAR"})
        _, order = self.request("/api/orders", "POST", {"request_id": "mixed", "lines": [
            {"menu_item_id": kitchen["id"], "quantity": 3}, {"menu_item_id": bar["id"], "quantity": 1}]})
        self.assertFalse(order["paid"])
        self.assertFalse(order["completed"])
        self.assertEqual(["KITCHEN", "BAR"], [line["station"] for line in order["lines"]])
        _, issued = self.request(f"/api/orders/{order['id']}/lines/{kitchen['id']}", "PUT", {"issued": True})
        self.assertEqual([True, False], [line["issued"] for line in issued["lines"]])
        self.assertFalse(issued["paid"])
        self.assertEqual(issued["revision"], self.request(f"/api/orders/{order['id']}/lines/{kitchen['id']}", "PUT", {"issued": True})[1]["revision"])
        self.assertEqual(409, self.request(f"/api/orders/{order['id']}", "PUT", {"expected_revision": 1, "lines": [{"menu_item_id": kitchen["id"], "quantity": 2}]})[0])
        _, paid = self.request(f"/api/orders/{order['id']}/status", "PUT", {"status": "paid"})
        self.assertTrue(paid["paid"])
        self.assertFalse(paid["completed"])
        self.assertEqual(409, self.request(f"/api/orders/{order['id']}", "PUT", {"expected_revision": paid["revision"], "lines": [
            {"menu_item_id": kitchen["id"], "quantity": 3}, {"menu_item_id": bar["id"], "quantity": 2}]})[0])
        _, unpaid = self.request(f"/api/orders/{order['id']}/status", "PUT", {"status": "unpaid"})
        _, edited = self.request(f"/api/orders/{order['id']}", "PUT", {"expected_revision": unpaid["revision"], "lines": [
            {"menu_item_id": kitchen["id"], "quantity": 3}, {"menu_item_id": bar["id"], "quantity": 2}]})
        self.assertFalse(edited["paid"])
        self.assertEqual([True, False], [line["issued"] for line in edited["lines"]])
        self.request(f"/api/orders/{order['id']}/status", "PUT", {"status": "paid"})
        _, complete = self.request(f"/api/orders/{order['id']}/lines/{bar['id']}", "PUT", {"issued": True})
        self.assertTrue(complete["completed"])
        _, reopened = self.request(f"/api/orders/{order['id']}/order-lines/{complete['lines'][0]['id']}", "PUT", {"issued": False})
        self.assertEqual([False, True], [line["issued"] for line in reopened["lines"]])
        self.assertFalse(reopened["completed"])
        self.assertEqual(reopened["revision"], self.request(f"/api/orders/{order['id']}/order-lines/{complete['lines'][0]['id']}", "PUT", {"issued": False})[1]["revision"])
        self.assertEqual(400, self.request("/api/menu", "POST", {"name": "No station", "price": "1", "category_id": None})[0])
        _, disabled = self.request("/api/menu", "POST", {"name": "Later", "price": "1", "category_id": None, "station": "BAR", "enabled": False})
        self.assertFalse(disabled["enabled"])

    def test_issue_all_for_one_station_is_atomic_and_reversible(self):
        _, beer = self.request("/api/menu", "POST", {"name": "Beer", "price": "3", "category_id": None, "station": "BAR"})
        _, water = self.request("/api/menu", "POST", {"name": "Water", "price": "1", "category_id": None, "station": "BAR"})
        _, soup = self.request("/api/menu", "POST", {"name": "Soup", "price": "5", "category_id": None, "station": "KITCHEN"})
        _, order = self.request("/api/orders", "POST", {"request_id": "bulk-issue", "lines": [
            {"menu_item_id": beer["id"], "quantity": 2}, {"menu_item_id": water["id"], "quantity": 1}, {"menu_item_id": soup["id"], "quantity": 1}]})
        bar_ids = [line["id"] for line in order["lines"] if line["station"] == "BAR"]
        kitchen_id = next(line["id"] for line in order["lines"] if line["station"] == "KITCHEN")
        path = f"/api/orders/{order['id']}/station-lines"
        payload = {"station": "BAR", "line_ids": bar_ids, "issued": True}
        self.assertEqual(400, self.request(path, "PUT", {"station": "BAR", "line_ids": bar_ids + [kitchen_id], "issued": True})[0])
        self.assertFalse(any(line["issued"] for line in self.request(f"/api/orders/{order['id']}")[1]["lines"]))
        _, issued = self.request(path, "PUT", payload)
        self.assertEqual([True, True, False], [line["issued"] for line in issued["lines"]])
        self.assertEqual(order["revision"] + 1, issued["revision"])
        self.assertEqual(issued["revision"], self.request(path, "PUT", payload)[1]["revision"])
        _, reversed_order = self.request(path, "PUT", {"station": "BAR", "line_ids": bar_ids, "issued": False})
        self.assertEqual([False, False, False], [line["issued"] for line in reversed_order["lines"]])
        self.assertEqual(400, self.request(path, "PUT", {"station": "BAR", "line_ids": [bar_ids[0], bar_ids[0]], "issued": True})[0])

    def test_add_same_item_after_issued_without_reissuing_old_line(self):
        _, beer = self.request("/api/menu", "POST", {"name": "Beer", "price": "4.00", "category_id": None, "station": "BAR"})
        _, order = self.request("/api/orders", "POST", {"request_id": "extra-beer", "lines": [{"menu_item_id": beer["id"], "quantity": 2}]})
        first = order["lines"][0]
        _, increased = self.request(f"/api/orders/{order['id']}", "PUT", {"expected_revision": order["revision"], "line_mode": "identified", "lines": [
            {"line_id": first["id"], "menu_item_id": beer["id"], "quantity": 3}]})
        self.assertEqual(1, len(increased["lines"]))
        self.assertEqual(3, increased["lines"][0]["quantity"])
        _, issued = self.request(f"/api/orders/{order['id']}/order-lines/{first['id']}", "PUT", {"issued": True})
        edit = {"expected_revision": issued["revision"], "line_mode": "identified", "lines": [
            {"line_id": first["id"], "menu_item_id": beer["id"], "quantity": 3},
            {"menu_item_id": beer["id"], "quantity": 1},
        ]}
        _, extra = self.request(f"/api/orders/{order['id']}", "PUT", edit)
        self.assertEqual([True, False], [line["issued"] for line in extra["lines"]])
        self.assertEqual([first["id"], extra["lines"][1]["id"]], [line["id"] for line in extra["lines"]])
        self.assertEqual(1600, extra["total_cents"])
        self.assertEqual(409, self.request(f"/api/orders/{order['id']}", "PUT", edit)[0])
        self.assertEqual(409, self.request(f"/api/orders/{order['id']}/lines/{beer['id']}", "PUT", {"issued": True})[0])
        _, extra = self.request(f"/api/orders/{order['id']}", "PUT", {"expected_revision": extra["revision"], "line_mode": "identified", "lines": [
            {"line_id": first["id"], "menu_item_id": beer["id"], "quantity": 3},
            {"line_id": extra["lines"][1]["id"], "menu_item_id": beer["id"], "quantity": 2},
        ]})
        self.assertEqual([3, 2], [line["quantity"] for line in extra["lines"]])
        self.assertEqual(2000, extra["total_cents"])
        _, completed = self.request(f"/api/orders/{order['id']}/order-lines/{extra['lines'][1]['id']}", "PUT", {"issued": True})
        self.assertEqual([True, True], [line["issued"] for line in completed["lines"]])
        self.assertFalse(completed["completed"])
        self.assertEqual(409, self.request(f"/api/orders/{order['id']}", "PUT", {"expected_revision": completed["revision"], "line_mode": "identified", "lines": [
            {"line_id": first["id"], "menu_item_id": beer["id"], "quantity": 3}]})[0])
        _, paid = self.request(f"/api/orders/{order['id']}/status", "PUT", {"status": "paid"})
        self.assertEqual(409, self.request(f"/api/orders/{order['id']}", "PUT", {"expected_revision": paid["revision"], "line_mode": "identified", "require_unpaid": True, "lines": [
            {"line_id": line["id"], "menu_item_id": beer["id"], "quantity": line["quantity"]} for line in paid["lines"]]})[0])
        _, unpaid = self.request(f"/api/orders/{order['id']}/status", "PUT", {"status": "unpaid"})
        self.assertFalse(unpaid["paid"])
        _, reversed_second = self.request(f"/api/orders/{order['id']}/order-lines/{extra['lines'][1]['id']}", "PUT", {"issued": False})
        _, reversed_first = self.request(f"/api/orders/{order['id']}/order-lines/{first['id']}", "PUT", {"issued": False})
        self.assertEqual([False, False], [line["issued"] for line in reversed_first["lines"]])
        self.assertEqual(reversed_second["total_cents"], reversed_first["total_cents"])
        _, adjusted = self.request(f"/api/orders/{order['id']}", "PUT", {"expected_revision": reversed_first["revision"], "line_mode": "identified", "lines": [
            {"line_id": line["id"], "menu_item_id": beer["id"], "quantity": line["quantity"] + (1 if line["id"] == extra["lines"][1]["id"] else 0)}
            for line in reversed_first["lines"]]})
        self.assertEqual([3, 3], [line["quantity"] for line in adjusted["lines"]])

    def test_menu_photo_upload_and_fallback(self):
        _, item = self.request("/api/menu", "POST", {"name": "Soup", "price": "5", "category_id": None, "station": "KITCHEN"})
        self.assertFalse(self.request("/api/menu")[1]["items"][0]["has_image"])
        image_path = f"/api/menu/{item['id']}/image"
        self.assertEqual(404, self.request(image_path)[0])
        image = b"\x89PNG\r\n\x1a\n" + b"test-image-data"
        upload = urllib.request.Request(self.base + image_path, data=image, headers={"Content-Type": "image/png", "X-Event-Id": "1"}, method="PUT")
        with urllib.request.urlopen(upload, timeout=5) as response:
            self.assertEqual(200, response.status)
        self.assertTrue(self.request("/api/menu")[1]["items"][0]["has_image"])
        with urllib.request.urlopen(self.base + image_path, timeout=5) as response:
            self.assertEqual(image, response.read())
            self.assertEqual("image/png", response.headers["Content-Type"])
        self.assertEqual(200, self.request(image_path, "DELETE")[0])
        self.assertFalse(self.request("/api/menu")[1]["items"][0]["has_image"])
        with urllib.request.urlopen(self.base + "/default-dish.svg", timeout=5) as response:
            self.assertIn(b"<svg", response.read())

    def test_category_photo_upload_removal_and_migration(self):
        _, category = self.request("/api/categories", "POST", {"name": "Drinks", "parent_id": None})
        self.assertFalse(category["has_image"])
        image_path = f"/api/categories/{category['id']}/image"
        self.assertEqual(404, self.request(image_path)[0])
        image = b"\x89PNG\r\n\x1a\n" + b"category-image-data"
        upload = urllib.request.Request(self.base + image_path, data=image, headers={"Content-Type": "image/png", "X-Event-Id": "1"}, method="PUT")
        with urllib.request.urlopen(upload, timeout=5) as response:
            self.assertEqual(200, response.status)
        self.assertTrue(self.request("/api/categories")[1]["categories"][0]["has_image"])
        with urllib.request.urlopen(self.base + image_path, timeout=5) as response:
            self.assertEqual(image, response.read())
            self.assertEqual("image/png", response.headers["Content-Type"])
        _, renamed = self.request(f"/api/categories/{category['id']}", "PUT", {"name": "Alcohol", "parent_id": None})
        self.assertTrue(renamed["has_image"])
        self.assertEqual(200, self.request(image_path, "DELETE")[0])
        self.assertFalse(self.request("/api/categories")[1]["categories"][0]["has_image"])
        self.assertEqual(400, self.request(image_path, "PUT")[0])
        app.init_db()
        with app.db() as connection:
            self.assertEqual(1, connection.execute("SELECT count(*) FROM menu_categories").fetchone()[0])

    def test_existing_database_gains_revision_column(self):
        old_path = Path(self.temp.name) / "previous.sqlite3"
        with sqlite3.connect(str(old_path)) as connection:
            connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, request_id TEXT UNIQUE, status TEXT DEFAULT 'open' CHECK (status IN ('open', 'done')), total_cents INTEGER, created_at TEXT, updated_at TEXT)")
            connection.execute("INSERT INTO orders (request_id, status, total_cents, created_at, updated_at) VALUES ('old', 'done', 100, '2026-10-01', '2026-10-01')")
        app.DB_PATH = old_path
        app.init_db()
        with app.db() as connection:
            row = connection.execute("SELECT revision, status, paid, event_id, comment FROM orders WHERE request_id = 'old'").fetchone()
            self.assertEqual((0, "done", 1, app.selected_event_id(connection), ""), tuple(row))

    def test_nested_menu_and_legacy_category_migration(self):
        _, drinks = self.request("/api/categories", "POST", {"name": "Drinks", "parent_id": None})
        _, coffee = self.request("/api/categories", "POST", {"name": "Coffee", "parent_id": drinks["id"]})
        _, espresso = self.request("/api/categories", "POST", {"name": "Espresso", "parent_id": coffee["id"]})
        _, root_item = self.request("/api/menu", "POST", {"name": "Water", "price": "1.00", "category_id": None, "station": "BAR"})
        _, parent_item = self.request("/api/menu", "POST", {"name": "Tea", "price": "2.00", "category_id": drinks["id"], "station": "BAR"})
        _, leaf_item = self.request("/api/menu", "POST", {"name": "Double", "price": "3.00", "category_id": espresso["id"], "station": "KITCHEN"})
        self.assertEqual([None, drinks["id"], espresso["id"]], [root_item["category_id"], parent_item["category_id"], leaf_item["category_id"]])
        self.assertEqual(3, len(self.request("/api/categories")[1]["categories"]))
        self.assertEqual(400, self.request(f"/api/categories/{drinks['id']}", "PUT", {"name": "Drinks", "parent_id": espresso["id"]})[0])
        self.assertEqual(400, self.request("/api/categories", "POST", {"name": "coffee", "parent_id": drinks["id"]})[0])
        self.assertEqual(400, self.request(f"/api/categories/{drinks['id']}", "DELETE")[0])
        self.assertEqual(400, self.request("/api/menu", "POST", {"name": "Bad", "price": "1", "category_id": 99999})[0])
        _, moved = self.request(f"/api/menu/{leaf_item['id']}", "PUT", {"name": "Double", "price": "3.00", "category_id": coffee["id"], "station": "KITCHEN", "enabled": True})
        self.assertEqual(coffee["id"], moved["category_id"])
        self.assertEqual(200, self.request(f"/api/categories/{espresso['id']}", "DELETE")[0])

        legacy = Path(self.temp.name) / "legacy-menu.sqlite3"
        with sqlite3.connect(str(legacy)) as connection:
            connection.execute("CREATE TABLE menu_items (id INTEGER PRIMARY KEY, name TEXT NOT NULL, price_cents INTEGER NOT NULL, category TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT)")
            connection.execute("INSERT INTO menu_items (name, price_cents, category, created_at) VALUES ('Latte', 350, 'Coffee', '2026-10-01'), ('Cake', 400, 'Food', '2026-10-01')")
        app.DB_PATH = legacy
        app.init_db()
        app.init_db()
        with app.db() as connection:
            rows = connection.execute("SELECT menu_items.name, menu_categories.name FROM menu_items JOIN menu_categories ON menu_items.category_id = menu_categories.id ORDER BY menu_items.id").fetchall()
            self.assertEqual([("Latte", "Coffee"), ("Cake", "Food")], [tuple(row) for row in rows])
            self.assertEqual(2, connection.execute("SELECT count(*) FROM menu_categories").fetchone()[0])
            self.assertEqual({"BAR"}, {row[0] for row in connection.execute("SELECT station FROM menu_items")})
            selected = app.selected_event_id(connection)
            self.assertEqual({selected}, {row[0] for row in connection.execute("SELECT event_id FROM menu_items")})
            self.assertEqual({selected}, {row[0] for row in connection.execute("SELECT event_id FROM menu_categories")})

    def test_serves_both_tabs_and_admin_assets_locally(self):
        for path in ("/", "/reception", "/kitchen", "/bar", "/admin", "/pos.js", "/station.js", "/bar_entry.js", "/undo.js", "/admin.js", "/event_context.js", "/style.css"):
            with self.subTest(path=path), urllib.request.urlopen(self.base + path, timeout=5) as response:
                self.assertEqual(200, response.status)
                self.assertTrue(response.read())


if __name__ == "__main__":
    unittest.main()
