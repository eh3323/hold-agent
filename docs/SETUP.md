# Setup Guide — Hold Agent × Hermes × WeChat

End-to-end walkthrough for turning a fresh machine into a daily NBA rookie-card portfolio reporter in WeChat.

Pipeline:

```
  cron (09:00) ──► Hermes Agent ──► Hold Agent MCP tools ──► eBay scrape + PNG render
                        │
                        └──► WeChat (iLink Bot)  ◄─── MEDIA:<path> attachment
```

You will:

1. Install & configure **Hermes Agent** (the LLM gateway + cron scheduler).
2. Connect Hermes to **WeChat** via QR-code pairing.
3. Install **Hold Agent** (this repo) as an MCP server.
4. Seed a SQLite portfolio and smoke-test the eBay scraper.
5. Wire Hold Agent into Hermes and schedule the daily cron.

Total wall-clock time: ~20 minutes once dependencies are installed.

---

## 0. Prerequisites

- **macOS / Linux / WSL2** (native Windows is not supported by Hermes)
- **Python 3.11+**
- **Git**
- A **personal WeChat account** (for the iLink Bot bridge)
- An **Anthropic API key** (or any other LLM provider supported by Hermes)

Quick sanity check:

```bash
python3 --version    # ≥ 3.11
git --version
```

---

## 1. Install Hermes Agent

One-liner from the [Hermes repo](https://github.com/NousResearch/hermes-agent):

```bash
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
```

Then restart your shell (or `source ~/.zshrc` / `~/.bashrc`) so `hermes` is on `PATH`.

Verify:

```bash
hermes --version
hermes doctor          # diagnose any missing bits
```

### 1a. Run the setup wizard

```bash
hermes setup
```

This is an interactive wizard — it will:

- Ask which **LLM provider** to use (pick Anthropic for Claude Sonnet 4)
- Store your API key in `~/.hermes/.env`
- Write a starter `~/.hermes/config.yaml`
- (If it finds `~/.openclaw`) offer to migrate — skip unless you're a legacy user

Alternative granular commands if you prefer:

```bash
hermes model           # pick provider + model
hermes config set model anthropic/claude-sonnet-4-5
```

### 1b. Smoke-test the CLI

```bash
hermes
> hello
```

If Hermes replies with text, the LLM side is working. Exit with `/quit`.

---

## 2. Connect Hermes to WeChat

Hermes speaks to personal WeChat accounts through Tencent's **iLink Bot API** — long-polling, no public webhook needed.

### 2a. Install the WeChat adapter dependencies

```bash
pip install aiohttp cryptography qrcode
```

`cryptography` is required (WeChat media is AES-128-ECB encrypted); `qrcode` is optional but lets the setup wizard print the QR in your terminal.

### 2b. Pair your WeChat account

```bash
hermes gateway setup
```

Pick **Weixin** when prompted. The wizard:

1. Requests a QR code from the iLink API
2. Displays it in-terminal (or gives you a URL)
3. Scan with the **WeChat mobile app**, tap **confirm login**
4. Saves credentials to `~/.hermes/weixin/accounts/<account_id>.json`

You'll see `微信连接成功，account_id=<id>` on success.

### 2c. Configure environment

Edit `~/.hermes/.env`:

```bash
# Required (auto-filled by the wizard, verify they exist)
WEIXIN_ACCOUNT_ID=<your-account-id>
WEIXIN_TOKEN=<your-ilink-token>

# Optional: only allow yourself to DM the bot
WEIXIN_DM_POLICY=allowlist
WEIXIN_ALLOWED_USERS=<your-wechat-user-id>

# Optional: where cron output lands (your own WeChat ID = DM yourself)
WEIXIN_HOME_CHANNEL=<your-wechat-user-id>
WEIXIN_HOME_CHANNEL_NAME=Portfolio
```

To find your own WeChat user ID: send any message to the bot after the gateway is running — the ID is logged.

### 2d. Start the gateway

```bash
hermes gateway start
# or foreground:
hermes gateway
```

Send the bot a WeChat message (e.g. `ping`). If it replies, the bridge is live.

> **Migrating from OpenClaw / Clawdbot?** Use `hermes claw migrate` — see the [migration guide](https://hermes-agent.nousresearch.com/docs/guides/migrate-from-openclaw) for details.

---

## 3. Install Hold Agent

```bash
cd ~   # or wherever you keep projects
git clone https://github.com/eh3323/hold-agent.git
cd hold-agent

python -m venv .venv
source .venv/bin/activate

pip install -e .
playwright install chromium
```

The `playwright install chromium` step is mandatory — the PNG report renderer uses Playwright to screenshot styled HTML.

### 3a. Environment file

```bash
cp .env.example .env
```

Defaults are fine for a first run. Interesting knobs:

| Var | Default | What it does |
|---|---|---|
| `EBAY_HOLDINGS_DAYS` | `14` | Lookback window for the daily refresh |
| `EBAY_DELAY` | `3.0` | Seconds between eBay requests (be polite) |
| `EBAY_USE_PLAYWRIGHT_FALLBACK` | `true` | Fall back to stealth browser if `curl_cffi` hits a CAPTCHA |
| `EBAY_PROXY_URL` | _(unset)_ | Residential proxy if you hit rate limits |

`ANTHROPIC_API_KEY` is **only** needed if you plan to use the standalone `HoldAgent` Python wrapper. When driven from Hermes it's unused.

### 3b. Initialize the database

```bash
python -m rookiecard.db.migrate
```

Creates `data/rookiecard.db` with the `players`, `card_sales`, `pop_reports`, `portfolio`, and `trade_journal` tables.

### 3c. Seed your first holding

Use the SQLite CLI or any client. Example — a Cooper Flagg raw base rookie:

```bash
sqlite3 data/rookiecard.db <<'SQL'
INSERT INTO players (name, draft_year, draft_position)
VALUES ('Cooper Flagg', 2025, 1);

INSERT INTO portfolio
  (player_id, series, parallel, grade, buy_price, buy_date, search_query)
VALUES
  (1,
   '2025-26 Topps Flagship No Limit',
   'Base',
   'Raw',
   10.00,
   '2026-04-06',
   'Cooper Flagg No Limit NL-1 RC -rainbow -foilboard -purple -PSA -BGS -SGC -graded');
SQL
```

**Why the messy `search_query`?** That string is pasted directly into eBay's sold-listings search. eBay negative-keyword syntax (`-foilboard`, `-PSA`, ...) strips parallels and graded copies so prices stay comparable. Tune it per card.

### 3d. Smoke-test the scraper

```bash
python -m rookiecard.scrapers.ebay \
  --player "Cooper Flagg" --series "Topps" --days 14
```

You should see ~30–60 sold records printed. If you get `0 records`, check:

- The player name exists in `players`
- `search_query` isn't too restrictive (try it at `ebay.com/sch/` first)
- eBay isn't CAPTCHA-walling you — set `EBAY_USE_PLAYWRIGHT_FALLBACK=true`

### 3e. Generate a report PNG manually

```bash
python -c "
from rookiecard.mcp.tools.portfolio import refresh_portfolio_prices, export_portfolio_report
refresh_portfolio_prices()
print(export_portfolio_report())
"
```

Output: `data/reports/portfolio_<YYYY-MM-DD>.png`. Open it — this is the same image Hermes will send to WeChat.

---

## 4. Wire Hold Agent into Hermes

### 4a. Register as an MCP server

Edit `~/.hermes/config.yaml` and add under `mcp_servers:`:

```yaml
mcp_servers:
  rookiecard:
    command: /ABS/PATH/TO/hold-agent/.venv/bin/python
    args: ["-m", "rookiecard.mcp.server"]
    env:
      PYTHONPATH: /ABS/PATH/TO/hold-agent
      DB_PATH: /ABS/PATH/TO/hold-agent/data/rookiecard.db
    tools:
      include:
        - get_portfolio
        - add_to_portfolio
        - get_card_prices
        - get_pricing_analysis
        - refresh_portfolio_prices
        - export_portfolio_report
    enabled: true
```

Replace `/ABS/PATH/TO/hold-agent` with your actual absolute path (e.g. `/Users/you/hold-agent`). Relative paths will not work — Hermes spawns this as a subprocess.

### 4b. Restart the gateway

```bash
hermes gateway restart
```

(Or stop the foreground process and re-run `hermes gateway`.)

### 4c. Verify the tools are loaded

DM the bot in WeChat:

```
List your available tools from the rookiecard MCP server.
```

You should see all six tool names echoed back.

### 4d. Trigger an ad-hoc run

```
Refresh my portfolio prices and send me the daily report.
```

If the bot replies with an image of a portfolio table + a Chinese P&L summary, the pipeline is working end-to-end.

---

## 5. Schedule the daily cron

Hermes has a built-in scheduler that invokes any prompt on a crontab expression, with platform delivery baked in.

```bash
hermes cron create "0 9 * * *" \
  "Do a daily portfolio check:
   1. Call refresh_portfolio_prices (14 days)
   2. Call export_portfolio_report, note the returned file_path
   3. Call get_portfolio for the overall P&L
   4. For any card with pnl_pct <= -15% or non-healthy status, call get_card_prices

   Reply with:
     MEDIA:<file_path>

     <2-3 line Chinese summary: holdings count, overall P&L %, any risk cards + one suggestion>" \
  --deliver weixin \
  --name "Portfolio Daily"
```

Two things to notice:

- **`MEDIA:<file_path>`** — Hermes extracts this marker from the reply and uploads the file as a native WeChat image. Keep it on its own line.
- **`--deliver weixin`** — routes output to the WeChat channel configured in step 2. For multi-recipient delivery, also set `WEIXIN_HOME_CHANNEL` in `.env`.

Inspect / edit:

```bash
hermes cron list
hermes cron delete <job-id>
```

At 09:00 the next day, expect a PNG + Chinese summary in WeChat.

---

## 6. Adding more cards

For each new holding, insert a row into `portfolio` with a **handcrafted `search_query`**. That's the single most important field — it decides what eBay considers "the same card."

Rules of thumb:

- Include the **set + subset/insert name** (`No Limit`, `Prizm`, `Instant Impact`, ...)
- Include the **card number** when it's specific enough (`NL-1`, `#201`)
- Strip **parallels** you don't own with `-rainbow -foilboard -silver -gold -purple` etc.
- Strip **graded** versions if you hold raw: `-PSA -BGS -SGC -CGC -graded`
- Do the opposite if you hold a graded card — include `PSA 10`, strip raw

Preview the query at `https://www.ebay.com/sch/i.html?_nkw=<your-query>&LH_Sold=1&LH_Complete=1` before saving it.

---

## 7. Common issues

| Symptom | Likely cause | Fix |
|---|---|---|
| `hermes: command not found` | PATH not reloaded | `source ~/.zshrc` or open a new shell |
| Gateway starts but WeChat silent | Bot session expired (`errcode=-14`) | Re-run `hermes gateway setup` |
| eBay returns 0 records | CAPTCHA / over-restrictive query | Enable Playwright fallback; loosen negatives |
| Prices are wildly off | Parallels leaking into the search | Tighten `search_query` negatives |
| `rookiecard` tools missing in Hermes | MCP config not reloaded | `hermes gateway restart` |
| PNG is blank / missing fonts | Playwright Chromium not installed | `playwright install chromium` |
| `ModuleNotFoundError: rookiecard` | Wrong `PYTHONPATH` in Hermes config | Use absolute path, no `~` |

For anything else: `hermes doctor` and the gateway logs (`~/.hermes/logs/`) are your friends.

---

## 8. What's next

- **More tools** — add technical-analysis prompts (`get_card_prices` returns MA/RSI/support/resistance)
- **Trade journal** — the schema already includes a `trade_journal` table; expose it as an MCP tool to log sells
- **Alerts** — create a second cron with a tighter threshold (`pnl_pct <= -20%`) that pages you mid-day
- **Other platforms** — swap `--deliver weixin` for `telegram`, `discord`, `slack`, etc. (Hermes handles them all)

Happy holding.
