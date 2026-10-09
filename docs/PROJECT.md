# Project state and architecture

This document describes the current implementation of the LAN-first pop-up bar/cafe MVP. Read [the README](../README.md) first for the event runbook. The repository itself is the source of truth if this document and code diverge.

## Scope and current behavior

- One host runs the authoritative HTTP server and SQLite database. Other devices need only a browser on the same local Wi-Fi. The server binds to all local interfaces; no Internet, CDN, cloud account, remote API, or package download is needed at runtime.
- One selected Event is the shared working context. Admin lists, creates, edits, enables/disables, and selects Events. Each Event owns its menu and orders in the same SQLite database; all operator views display the selected Event and switch datasets when it changes. Existing installations migrate their menu and orders into a default **Поточна подія** Event.
- `/` and `/reception` have **Чек** and **Замовлення** tabs. Selecting the first menu item creates a numbered check automatically. Further item, quantity, and comment changes save automatically. At 680 px and below, equal-width **+ Новий чек** and **До чека** buttons sit side by side above the menu tiles; **До чека** displays the current total and scrolls to the receipt. **↑ Вгору** at the end of the receipt returns to the top. The mobile menu heading is hidden and the menu-item count is absent at every width. On wider screens, **+ Новий чек** stays in the receipt area. Reception shows issued and payment state and can mark the whole order paid. Issued lines cannot be changed or removed. A paid check is read-only until payment is cancelled.
- `/kitchen` and `/bar` show whole order cards, with all lines and their issue state. Each view classifies an order by its own station's lines: if any kitchen line is unissued, Kitchen places the card under **Не видано**; otherwise it moves to **Виконано**. Bar uses its bar lines the same way and moves them to **Видано**. The other station's pending lines do not hold a completed station's card in **Не видано**. Kitchen shows orders with kitchen lines and can issue only those lines. Station cards start collapsed at tile width even on large screens: order number and each item as `name хquantity` are on the left, action buttons on the right. Issued names are struck through; pending names have a distinct color. Tapping a collapsed card expands it; its expanded state survives polling rerenders. **➜ Видати все** atomically issues every unissued line assigned to that station and has a single Undo action. Bar cards show **💵 Оплатити** directly when unpaid, and **↶ Оплачено** reverses payment when paid; Kitchen has no payment controls. Bar has two tabs, **Створити замовлення** and **Видача**. Choosing the creation tab prepares a blank check; tapping an expanded unpaid Bar card opens that specific order in the entry tab. The Bar entry check can issue and return BAR lines and toggle whole-order payment, using the same authoritative API as the fulfillment tab; KITCHEN line state is visible there without issue controls. The mobile **Меню** caption above Bar's menu tiles is hidden. The fulfillment tab shows all orders so it can settle or reverse payment even when an order has no bar items. Unpaid cards sort before paid cards in Bar's **Видано** list. Repeating an unissued item increases its quantity; repeating an already issued item creates a new unissued line. All views share one database.
- Payment and fulfillment are independent. `paid` and a comment of at most 40 characters belong to the order; `issued` belongs to each line. The comment is visible in Reception's check and list, Bar's entry check, and Kitchen/Bar cards even while collapsed. `completed` is derived as paid and every line issued. Issue and payment toggles are reversible. A seven-second Undo snackbar follows each action, and permanent reverse controls remain available on issued lines and paid checks. Neither a paid order nor an issued line is a separate order status.
- `/admin` manages Events and the selected Event's tree of menu sections and items. The tree starts with sections collapsed; a section's arrow reveals or hides its contents. Items can live at the root or at any depth. Each item requires an explicit `KITCHEN` or `BAR` station, independent of category. Admin can upload, replace, or remove an item or section image (JPEG/PNG/WebP, up to 3 MB). Without an item photo, reception and bar order entry use a local default illustration; sections without an image use a folder icon. Operators can add, rename, move, and delete **empty** sections; add, edit, move, disable, or re-enable items. The `DELETE` menu API soft-disables an item. Editing an item scrolls to its form rather than to the Event panel.
- `mcp_server.py` provides local stdio tools for Event discovery, selection, reading one Event's menu, and previewed/confirmed bulk menu imports. Every menu tool takes an explicit `eventId`; see [the MCP guide](MCP.md).
- The interface and admin page are in Ukrainian. **Адмінка** is linked from Reception, Kitchen, and Bar headers. The connected indicator reads **Підключено**; the disconnected indicator reads **Немає з’єднання · повторюємо**. Prices display in UAH. No customer, inventory, payment processing, reporting, staff, or cloud features are implemented.

## Operational UX requirement

**The UI must remain reliably operable under fatigue, noise, interruptions, and reduced attention.** This is a standing requirement for every Reception, Kitchen, and Bar change. Keep the next action and current payment/fulfillment state visible, use clear action verbs, make common actions one tap, and provide a direct way to correct mistakes. Avoid flows that depend on remembering hidden state or completing a sequence without interruption. Check new interactions on a small screen and after a reload or reconnect.

## Repository map

| Path | Responsibility |
| --- | --- |
| `app.py` | Python standard-library HTTP server, REST API, SQLite schema/migration, address discovery, startup output |
| `mcp_server.py` | Local stdio MCP server for explicit Event-scoped menu imports |
| `static/event_context.js` | Shared selected-Event polling, banner, and scoped request checks |
| `static/index.html`, `static/pos.js` | Reception check/order tabs, menu-tree navigation, auto-save, polling and recovery |
| `static/station.html`, `static/station.js`, `static/bar_entry.js` | Whole-order Kitchen and Bar queues; Bar payment and order entry |
| `static/default-dish.svg` | Local fallback image for menu tiles |
| `static/admin.html`, `static/admin.js` | Event selection/editing and selected Event's menu editing |
| `static/style.css` | Local responsive styles; no external assets |
| `terminal_qr.py`, `vendor/qrcodegen.py` | Terminal QR containing the first detected LAN URL; vendored generator attribution is in `vendor/README.md` |
| `tests/test_app.py`, `tests/test_mcp.py`, `tests/test_qr.py` | HTTP/database, MCP import/protocol, and terminal QR tests |
| `data/orders.sqlite3` | Default operational database, created at first start; `data/` is gitignored |

There is no frontend build step, package manager, framework, or service worker. Python 3.9+ is required. The optional MCP stdio process starts only when an AI agent connects; the POS itself has one server process. Node is useful only for optional JavaScript syntax checks.

## Design decisions and data flow

1. **Single authority:** clients make HTTP requests to the host. No peer-to-peer sync, CRDT, broker, or WAN dependency. This keeps order totals and paid status authoritative in one place.
2. **SQLite with WAL:** each HTTP request opens a short-lived connection. Order creation, edits, payment, and issuance use `BEGIN IMMEDIATE` transactions plus a process-local write lock. Amounts are integer cents. A line stores its item name, price, and station at order entry so later menu edits do not reprice or reroute existing work.
3. **Polling:** All views poll the selected Event every 2.5 seconds and on focus/visibility/online recovery. Reception fetches that Event's menu, categories, and orders. Kitchen and Bar fetch its orders, show whole order cards, and classify them by their own station's lines; Bar also shows unpaid orders for payment. Bar order entry keeps a session draft and reconciles server updates. Admin polls Events and the selected menu. An Event change clears the previous Event's displayed state and draft. Polling avoids a persistent connection that may drop during phone sleep. Expected scale is a small event; the API currently returns the selected Event's full order list, with no pagination.
4. **Auto-save:** `static/pos.js` waits 150 ms after a change and sends one write at a time. More taps are queued locally while a write is in flight. A new order uses a unique `request_id`; retrying a POST cannot create a duplicate. Existing orders use `expected_revision`; a stale PUT gets HTTP 409 rather than overwriting another device's edit.
5. **Recovery:** Reception retries failed requests and polls the server again. An unconfirmed draft and pending POST ID are kept in that tab's `sessionStorage` for reload recovery. If a PUT may have succeeded but its response was lost, the client reads the order and reconciles matching lines. If another device made different line changes, the UI stops auto-save and offers **Завантажити поточну версію**; it does not merge competing edits. Kitchen and Bar issue/pay actions are idempotent; a lost response is resolved by the next poll.
6. **Menu tree:** `menu_categories.parent_id` points to another section or is `NULL` for a root section. `menu_items.category_id` points to a section or is `NULL` for a root item. Section moves reject cycles; siblings cannot share the same name ignoring case. A section can be deleted only when it has no child sections or items, including disabled items.
7. **Event ownership:** `app_state` holds one selected Event ID. `event_id` on sections, menu items, and orders makes ownership explicit. SQLite foreign keys and triggers reject cross-Event section/item/order-line references. Scoped REST reads use the selected Event. Scoped writes require an `X-Event-Id` header matching it, so an old tab cannot silently save into a newly selected Event. Order-line snapshots preserve the original menu item and Event association through their order.

## SQLite schema and migrations

- `events(id, name, event_date, enabled)` identifies catering deployments; `app_state(id=1, selected_event_id)` stores the one selected Event.
- `menu_categories(id, name, parent_id, event_id)` is each Event's section tree.
- `category_images(category_id, mime, data)` stores optional section images in SQLite.
- `menu_items(id, name, price_cents, category_id, event_id, station, enabled, created_at)` stores configurable products. `station` is explicitly `KITCHEN` or `BAR`. The old `category` text column remains for compatibility but is cleared and unused after migration.
- `menu_images(menu_item_id, mime, data)` stores optional dish photos in the same SQLite database, so normal database backup includes them.
- `orders(id, request_id, event_id, paid, comment, revision, total_cents, created_at, updated_at)` stores checks. `comment` defaults to an empty string and is limited to 40 characters. The legacy `status` column remains only for old database compatibility; old `done` rows migrate to `paid=1`. `request_id` is unique across Events; `id` is the visible check number.
- `order_lines(id, order_id, menu_item_id, item_name, quantity, price_cents, line_total_cents, station, issued)` stores the price/name/station snapshot and whole-line fulfillment flag. Separate lines can refer to the same menu item after the earlier line was issued.

`init_db()` creates missing tables and adds missing revision, payment, comment, station, issuance, and Event ownership columns to older databases. Existing sections, items, and orders migrate into **Поточна подія**, selected initially. Existing flat category strings become root sections without changing item IDs. Old `done` checks migrate to `paid=1`. Existing orders receive an empty comment. Existing menu items and order lines receive `BAR` as a migration fallback because their former schema had no station field; review and correct existing menu item stations in `/admin` before the event. Existing order-line station snapshots stay at `BAR` unless migrated manually. Back up the existing database before event-day upgrades; do not replace or delete it.

An order must have 1–100 distinct lines at creation. It may later have zero lines after removals; an empty order cannot be marked paid. Each quantity is 1–999. Retained lines keep their original unit price and station during edits. Issued lines cannot be removed or have quantity changed until returned to unissued. An unissued line removed in a completed save and later added again is a new line at the then-current enabled menu price. Adding the same item before issue increases that line's quantity; adding it after issue creates a second line. Reversing issuance can temporarily leave two unissued lines for the same menu item; the UI keeps both visible and adds to the latest one. Reception and Bar can edit order contents and comments only while unpaid; payment cancellation unlocks editing again. Fulfillment actions remain independent of payment.

## HTTP API

JSON responses use `Cache-Control: no-store`. Mutating endpoints accept JSON request bodies. This is an internal LAN API, not a stable external integration contract.

The Event selected in `app_state` scopes `/api/categories`, `/api/menu`, and `/api/orders`. Each write to those routes must include `X-Event-Id: <selected Event ID>`; missing headers return 400 and stale Event IDs return 409. Successful list responses include `event_id`, as do individual menu, category, and order records. Event management endpoints do not use this header.

| Method and path | Purpose |
| --- | --- |
| `GET /api/health` | Server liveness |
| `GET /api/info` | Detected local addresses for the current port |
| `GET /api/context` | Current selected Event |
| `GET /api/events` | All Events and selected Event ID |
| `POST /api/events` | Create `{name, date?, enabled?}` without selecting it |
| `GET /api/events/{id}` | Read one Event |
| `PUT /api/events/{id}` | Edit `{name, date, enabled}`; cannot disable the selected Event |
| `PUT /api/events/{id}/select` | Select an enabled Event for all clients |
| `GET /api/categories` | Entire section tree as a flat list of `{id, name, parent_id, has_image}` |
| `POST /api/categories` | Create with `{name, parent_id}`; `parent_id` may be `null` |
| `PUT /api/categories/{id}` | Rename or move with `{name, parent_id}` |
| `DELETE /api/categories/{id}` | Delete an empty section |
| `GET /api/categories/{id}/image` | Return the stored section image, or 404 |
| `PUT /api/categories/{id}/image` | Replace section image with raw JPEG, PNG, or WebP bytes (up to 3 MB) |
| `DELETE /api/categories/{id}/image` | Remove the section image; UI shows its folder icon |
| `GET /api/menu` | Enabled menu items; `?all=1` includes disabled items |
| `POST /api/menu` | Create with `{name, price, category_id, station, enabled}`; `station` is required |
| `PUT /api/menu/{id}` | Edit with `{name, price, category_id, station, enabled}` |
| `DELETE /api/menu/{id}` | Soft-disable the item |
| `GET /api/menu/{id}/image` | Return the stored local photo, or 404 |
| `PUT /api/menu/{id}/image` | Replace photo with raw JPEG, PNG, or WebP bytes (up to 3 MB) |
| `DELETE /api/menu/{id}/image` | Remove the stored photo; UI shows its default illustration |
| `GET /api/orders` | Selected Event's orders, newest ID first, including lines |
| `GET /api/orders/{id}` | One order and its lines |
| `POST /api/orders` | Create with `{request_id, comment?, lines:[{menu_item_id, quantity}]}`; comment is at most 40 characters |
| `PUT /api/orders/{id}` | Edit an unpaid order with `{expected_revision, comment?, line_mode:"identified", lines:[{line_id?, menu_item_id, quantity}]}`; omit `line_id` for a new line. `lines:[]` is allowed unless issued lines exist. The older `{expected_revision, lines}` form remains for orders without repeated menu items. Omitted comment preserves its current value. |
| `PUT /api/orders/{id}/order-lines/{line_id}` | Set a specific line's issuance with `{issued:true/false}`; idempotent |
| `PUT /api/orders/{id}/station-lines` | Atomically issue or return selected lines of one station with `{station, line_ids, issued}`; idempotent and used by **Видати все** and its Undo |
| `PUT /api/orders/{id}/lines/{menu_item_id}` | Legacy issue route; rejects ambiguous repeated menu items |
| `PUT /api/orders/{id}/status` | Set the whole order's payment with `{status:"paid"/"unpaid"}`; idempotent |

Input errors return 400, missing resources 404, stale revisions, paid-order edits, or issued-line edits return 409, and database errors 500. The UI should handle a 409 by reloading the authoritative order, not blindly retrying an overwrite. Runtime static routes are `/`, `/reception`, `/kitchen`, `/bar`, `/admin`, and their local JS/CSS/SVG assets.

The stdio MCP server exposes `list_events`, `get_event`, `create_event`, `select_event`, `list_menu(eventId)`, `update_menu_item(eventId, itemId, ..., dryRun, previewToken?, confirmed?)`, and `bulk_upsert_menu(eventId, items, dryRun, previewToken?, confirmed?)`. Individual updates may add, replace, or remove an item's JPEG/PNG/WebP photo through a local absolute path or base64 input, with the same 3 MB limit as Admin. Both individual updates and bulk imports require a preview, explicit confirmation, and matching token. The bulk preview produces counts, item actions, categories to create, and a token bound to the target Event, proposed items, and current menu snapshot. Imports write in one transaction; omitted items remain in the Event menu. The [MCP guide](MCP.md) documents client setup and the operator approval sequence.

## Operations and limits

- Start with `python3 app.py`; default port is 8080 and default DB is `data/orders.sqlite3`. `--port` and `--data` override them. Startup prints localhost and detected LAN addresses plus a terminal QR for the first LAN address. An IDE console may render the ANSI QR poorly; use a normal terminal or the printed URL.
- **Restart the Python process after changes to `app.py`.** Reloading a browser only reloads the static frontend. Running old backend code with new frontend files can break `/api/categories` and auto-save. If 8080 is occupied, identify whether the existing app is running before choosing another port. Do not intentionally run two event servers against the same database.
- The host must stay awake and reachable. If the host is down, other devices cannot see or save authoritative orders. A browser's session draft is only temporary recovery data, not a second database or a guarantee against device failure.
- Role separation is by operational view, not authentication. The trusted-LAN API does not enforce staff roles. Kitchen shows no payment controls, Reception shows no issue controls, and Bar shows both issue and payment controls.
- The app trusts the LAN: there is no login, TLS, role separation, or protection against a person on that Wi-Fi using `/admin` or the API. Use a private event network. A phone hotspot or guest Wi-Fi with client isolation may prevent client-to-host traffic even though all devices show the same network name.
- SQLite WAL can create `orders.sqlite3-wal` and `orders.sqlite3-shm`. Stop the server before copying the entire `data/` directory for a simple backup. There is no automatic backup, restore UI, print flow, or cloud sync.
- The address may change when switching router/hotspot. Restart the server and use its newly printed URL/QR. Multiple detected addresses can include interfaces that client devices cannot reach; test the first URL on the actual event Wi-Fi.

## Verification and remaining work

Run:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
node --check static/pos.js
node --check static/admin.js
node --check static/station.js
node --check static/bar_entry.js
node --check static/event_context.js
```

As of 2026-10-06, the automated suite includes Event isolation, stale-client write rejection, MCP preview/confirmation and stdio behavior, the 40-character order comment and paid edit lock, the extra-serving case (increase an unissued beer line, then add a separate beer line after issue), section image upload/removal, and atomic station-wide issue and reversal at the API. Earlier browser smoke checks covered Bar creation, issue, edit of an unpaid issued order, duplicate-line display at Reception, and return to Bar's active queue. Event switching, menu import, collapsed Admin sections, comments on collapsed station cards, and mobile Reception controls still need visual inspection on an actual device. The three-view workflow still needs a real device smoke test.

**Still required before the event:** run the [README pre-event check](../README.md#pre-event-check) on the actual laptop, actual Wi-Fi/hotspot, and three devices with WAN disconnected. Verify local firewall/client isolation, QR reachability, mixed-order propagation across views, Bar order creation, photo display/fallback, reconnect after phone sleep, whole-order queue movement, issued-line immutability, payment in either order relative to fulfillment, restart persistence, and a backup copy of the event database. Decide whether the current UAH currency and no-auth trusted-LAN assumption fit the event. Payment processing and broader POS features remain outside this MVP.

**Handoff snapshot, 2026-10-06:** the app was restarted on port 8080 with the updated backend. `/api/health` returned OK and the live `/api/categories` response included `has_image`. The operational database remains in ignored `data/` and is not part of the Git commit.
