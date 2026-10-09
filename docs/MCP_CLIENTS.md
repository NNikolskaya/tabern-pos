# Підключення MCP до Claude, ChatGPT і Hermes

Це інструкція для локального checkout на Mac. Замініть `/absolute/path/to/tabern-pos` на абсолютний шлях до проєкту на вашому комп'ютері:

```text
Проєкт: /absolute/path/to/tabern-pos
Сервер: /absolute/path/to/tabern-pos/mcp_server.py
Робоча база: /absolute/path/to/tabern-pos/data/orders.sqlite3
Python: /usr/bin/python3
```

MCP сервер запускається **клієнтом** як окремий процес. Команда `python3 app.py` запускає POS для телефонів; вона не запускає MCP. У всіх конфігураціях `--data` має вказувати на ту саму базу, що й POS. Для першої перевірки підключення можна тимчасово замінити шлях бази на `/private/tmp/tabern-pos-mcp-test.sqlite3`: сервер створить там окрему тестову базу, яку POS не показуватиме.

## Claude Desktop на Mac

1. Відкрий **Claude → Settings → Developer → Edit Config**. Це відкриє `~/Library/Application Support/Claude/claude_desktop_config.json`.
2. Додай `tabern-pos-menu` до наявного об’єкта `mcpServers`. Якщо файл порожній, використай увесь приклад:

   ```json
   {
     "mcpServers": {
       "tabern-pos-menu": {
         "command": "/usr/bin/python3",
         "args": [
           "/absolute/path/to/tabern-pos/mcp_server.py",
           "--data",
           "/absolute/path/to/tabern-pos/data/orders.sqlite3"
         ]
       }
     }
   }
   ```

3. Повністю закрий і знову відкрий Claude Desktop. У полі нового чату відкрий **Add files, connectors, and more → Connectors → Manage connectors** і перевір, що `tabern-pos-menu` має інструменти `list_events` і `list_menu`.
4. Попроси: `Виклич list_events через tabern-pos-menu. Нічого не змінюй.` Якщо сервер не підключився, перевір синтаксис JSON і `~/Library/Logs/Claude/mcp.log` та `mcp-server-tabern-pos-menu.log`.

Офіційна інструкція: [Connect to local MCP servers](https://modelcontextprotocol.io/docs/develop/connect-local-servers).

## Claude Code

У терміналі, **з каталогу цього проєкту**, виконай:

```sh
claude mcp add --transport stdio --scope local tabern-pos-menu -- /usr/bin/python3 /absolute/path/to/tabern-pos/mcp_server.py --data /absolute/path/to/tabern-pos/data/orders.sqlite3
claude mcp list
claude mcp get tabern-pos-menu
```

`--scope local` зберігає підключення для цього проєкту. У сесії Claude Code команда `/mcp` показує стан сервера. Після цього попроси `Виклич list_events через tabern-pos-menu. Нічого не змінюй.` Не передавай `--data` перед `--`: усе після подвійного дефіса є командою запуску MCP сервера.

Офіційна інструкція: [Claude Code MCP](https://code.claude.com/docs/en/mcp).

## Hermes Agent

Встановлений тут Hermes підтримує локальний stdio MCP. Підключи сервер:

```sh
hermes mcp add tabern-pos-menu --command /usr/bin/python3 --args /absolute/path/to/tabern-pos/mcp_server.py --data /absolute/path/to/tabern-pos/data/orders.sqlite3
hermes mcp test tabern-pos-menu
hermes mcp list
```

`--args` має бути останньою опцією команди Hermes. Під час `mcp add` Hermes може запропонувати ввімкнути знайдені інструменти — вибери потрібні. Відкрий `hermes chat` і попроси `Виклич list_events через tabern-pos-menu. Нічого не змінюй.` Якщо чат уже відкритий, введи `/reload-mcp`.

Альтернатива через `~/.hermes/config.yaml` (додай до наявного `mcp_servers`, не стирай інші сервери):

```yaml
mcp_servers:
  tabern-pos-menu:
    command: /usr/bin/python3
    args:
      - /absolute/path/to/tabern-pos/mcp_server.py
      - --data
      - /absolute/path/to/tabern-pos/data/orders.sqlite3
```

Офіційна інструкція: [Hermes MCP](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/mcp.md).

## ChatGPT у браузері

**Пряме підключення цього stdio сервера в ChatGPT неможливе.** ChatGPT підключається до віддалених MCP endpoint; для локального сервера потрібен [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels). Тунель може запускати наш stdio процес на Mac, але потребує вихідного HTTPS до OpenAI. Отже, AI імпорт через ChatGPT не працює без Інтернету; сам POS після імпорту продовжує працювати в локальній мережі без нього.

Для **створення Events та імпорту меню** потрібен доступ до MCP write actions. За [поточними правилами ChatGPT](https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt), повний MCP із записом доступний у Business та Enterprise/Edu; Pro в developer mode має read/fetch, чого для імпорту недостатньо. Якщо потрібного доступу або Tunnel у твоєму workspace немає, використовуй Claude Desktop/Code чи Hermes для цього локального MCP.

Якщо доступ є:

1. У [Platform tunnel settings](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) створи тунель і зв’яжи його з потрібним ChatGPT workspace. Завантаж `tunnel-client` за посиланням у налаштуваннях. Для цього потрібні відповідні права в Platform/workspace.
2. На Mac налаштуй `tunnel-client` із ID цього тунелю. Значення `YOUR_TUNNEL_ID` і ключ керування заміни даними з власного Platform; не вставляй ключ у документацію проєкту:

   ```sh
   export CONTROL_PLANE_API_KEY="<ключ із Platform>"
   tunnel-client init --sample sample_mcp_stdio_local --profile tabern-pos --tunnel-id YOUR_TUNNEL_ID --mcp-command "/usr/bin/python3 /absolute/path/to/tabern-pos/mcp_server.py --data /absolute/path/to/tabern-pos/data/orders.sqlite3"
   tunnel-client doctor --profile tabern-pos --explain
   tunnel-client run --profile tabern-pos
   ```

3. У ChatGPT web відкрий **Plugins → + → Create custom MCP server**, вибери **Tunnel** та потрібний tunnel ID. Перевір виявлені інструменти й створи приватне тестове підключення. Назви меню та інші дані, які агент читає через інструменти, проходять через це з’єднання з ChatGPT.
4. У новому чаті вибери це підключення та попроси лише `list_events`. Потім перевір `list_menu(eventId)` і `bulk_upsert_menu(..., dryRun:true)` для тестової події. Імпортуй тільки після перегляду preview.

Офіційні кроки: [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) і [Create custom MCP server](https://developers.openai.com/api/docs/guides/custom-mcp-server). Якщо Platform не показує можливість створити Tunnel або ChatGPT не пропонує custom MCP із записом, цей варіант зараз недоступний для твого акаунта.

## Перевірка імпорту після підключення

Для будь-якого клієнта використовуй [безпечну послідовність імпорту](MCP.md#saturday-import): `list_events` → конкретний `eventId` → `list_menu(eventId)` → `bulk_upsert_menu` з `dryRun:true` → перегляд preview → явне підтвердження → імпорт із тим самим `eventId` і `previewToken`. Без `eventId` меню не імпортується. Для тесту наскрізного зв’язку з POS створи окрему подію **MCP TEST** у робочій базі, вибери її в Admin і переконайся, що імпортовані позиції видно в Reception/Bar; потім поверни вибір на робочу подію.
