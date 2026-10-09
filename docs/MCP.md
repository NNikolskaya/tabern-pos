# Event menu import through MCP

The local MCP server in `mcp_server.py` lets an AI agent prepare a menu for one explicit Event. It uses the same SQLite database as the POS. It is a stdio process started by the MCP client on the host laptop; it does not need the Internet or expose another network port. Python 3.9+ is sufficient.

For step-by-step setup in **Claude Desktop, Claude Code, Hermes Agent, or ChatGPT**, see [MCP client connections](MCP_CLIENTS.md). ChatGPT requires a separate Secure MCP Tunnel and Internet access for AI tool calls.

Configure an MCP client to start the server with absolute paths. Replace the example path with the checkout on the host laptop:

```json
{
  "mcpServers": {
    "tabern-pos-menu": {
      "command": "python3",
      "args": [
        "/absolute/path/to/tabern-pos/mcp_server.py",
        "--data",
        "/absolute/path/to/tabern-pos/data/orders.sqlite3"
      ]
    }
  }
}
```

The `--data` path **must match** the `--data` path of the running POS. If the POS uses its default database, use the path above. Keep the database on the laptop; the client may connect from another machine only if that client can launch the MCP process on the laptop. The agent and the POS can access this one SQLite database at the same time.

## Saturday import

1. Create Saturday's Event in `/admin`, or call `create_event` with a name and optional `YYYY-MM-DD` date. Use `list_events` and `get_event` to identify its **ID**. Select it with **Обрати** in Admin or `select_event(eventId)`.
2. Give the source menu to the AI agent. Instruct it to map each product to a price, an explicit `KITCHEN` or `BAR` station, and a `categoryPath` list such as `["Алко", "Пиво"]`. A root item uses `[]`.
3. The agent calls `list_menu(eventId)` for this exact Event and compares the existing menu.
4. The agent calls `bulk_upsert_menu` with the exact `eventId`, parsed `items`, and `dryRun:true`. This returns categories to create, each item action (`create`, `update`, `unchanged`), totals, and a `previewToken`. No data is written.
5. Review the preview, especially Event name/ID, station, price, category paths, and changes to existing items. Tell the agent to import only after you approve that concrete preview.
6. The agent calls `bulk_upsert_menu` with the **same** `eventId` and `items`, `dryRun:false`, `confirmed:true`, and the returned `previewToken`. The import is one transaction. A changed menu or changed import invalidates the token and requires a new preview.
7. Open Reception, Bar, and Kitchen; their Event banner must show Saturday's Event. Create a test order with kitchen and bar items and verify both queues.

Example import item:

```json
{
  "name": "Пиво",
  "price": "80.00",
  "station": "BAR",
  "categoryPath": ["Алко", "Пиво"],
  "enabled": true
}
```

`price` is in UAH, not cents. Omit `enabled` to leave an item enabled. Existing products are matched within the Event by case-insensitive category path and item name; the import updates their price, station, enabled flag, and name spelling. Items absent from the import are retained. Bulk import leaves photos unchanged. The import cannot target an unspecified Event: `list_menu` and `bulk_upsert_menu` require `eventId`, and a real import additionally requires confirmation and the current preview token.

## Update one menu position or its photo

Call `list_menu(eventId)` to find the item's `id`. Then call `update_menu_item` with that `eventId`, `itemId`, the fields to change, and `dryRun:true`. Review the returned Event, before/after values, and `imageChange`. After user confirmation, repeat the same arguments with `dryRun:false`, `confirmed:true`, and the returned `previewToken`. A changed item or image invalidates the token. Omitted fields and the existing photo are preserved.

```json
{"eventId": 2, "itemId": 17, "name": "Пиво світле", "price": "90.00", "imagePath": "/absolute/path/to/beer.png", "dryRun": true}
```

`imagePath` must point to a file on the computer running the MCP server. Alternatively, supply raw `imageBase64` and optionally `imageMime` (`image/jpeg`, `image/png`, or `image/webp`). Use `removeImage:true` to remove a photo. Choose only one of `imagePath`, `imageBase64`, or `removeImage:true`. Photos must be valid JPEG, PNG, or WebP and at most 3 MB. The tool can also change `station`, `enabled`, and `categoryId`; use `categoryId:null` to move an item to the menu root. The category must belong to the same Event. Photo bytes are stored in the same local SQLite database as Admin uploads. No Internet fetch is performed by this tool.

## Tools

| Tool | Action |
| --- | --- |
| `list_events` | List all Events and show which is selected |
| `get_event(eventId)` | Read one Event |
| `create_event(name, date?)` | Create an Event without selecting it |
| `select_event(eventId)` | Select one enabled Event for every POS view |
| `list_menu(eventId)` | Read only that Event's menu |
| `update_menu_item(eventId, itemId, fields, dryRun, previewToken?, confirmed?)` | Preview or update one position and optionally add, replace, or remove its photo |
| `bulk_upsert_menu(eventId, items, dryRun, previewToken?, confirmed?)` | Preview or apply one Event's menu import |

The MCP server is part of the trusted local environment and has direct database access. Give its configuration only to agents/operators who may manage the menu and choose the working Event.
