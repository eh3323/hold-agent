# Hold Agent — NBA Rookie Card Portfolio Monitor

A focused **Hold Agent** for tracking a portfolio of NBA rookie cards. It scrapes real eBay sold-listing data, computes price technicals (MA / RSI / trend), and emits a daily portfolio health report as a PNG table.

Exposed as an **MCP server** so any MCP-compatible runtime — [Hermes Agent](https://github.com/NousResearch/hermes-agent), Claude Code, Claude Desktop — can call it and deliver reports to WeChat, Telegram, Discord, Slack, etc.

---

## What it does

- **`refresh_portfolio_prices`** — For each active holding, scrapes eBay sold listings (via `curl_cffi` with Chrome TLS fingerprinting + Playwright-stealth fallback) and persists them to SQLite.
- **`get_portfolio`** — Lists current holdings with buy/sell price, P&L %, holding days, and a health flag (`healthy` / `watch` / `at_risk`).
- **`get_card_prices`** — Returns MA7 / MA30 / RSI14 / trend / volume / support & resistance for a given card.
- **`get_pricing_analysis`** — Pricing guidance for a sell: P25 (auction start), median, P75 (BIN), estimated eBay fees.
- **`export_portfolio_report`** — Renders a styled HTML table of the whole portfolio to a PNG. Any MCP host can include `MEDIA:<path>` in its reply to deliver the image to a messaging platform.
- **`add_to_portfolio`** — Records a new buy.

---

## Architecture

```
┌─── Messaging Platform (WeChat / Telegram / ...) ───┐
│                                                     │
│            Hermes Agent (gateway + cron)            │
│                         │                           │
│              MCP stdio transport                    │
│                         ▼                           │
│  ┌────────────── rookiecard MCP server ──────────┐  │
│  │   portfolio.py  │  prices.py                  │  │
│  │   refresh       │  get_card_prices            │  │
│  │   export (PNG)  │  get_pricing_analysis       │  │
│  │   add/get       │  compare_cards              │  │
│  └───────────────┬──────────────────────────────┬┘  │
│                  ▼                              ▼    │
│         engine/price_analyzer          scrapers/ebay │
│         (MA/RSI/trend/support)         (curl_cffi    │
│                                         + Playwright)│
│                  │                              │    │
│                  └────── SQLite (data/*.db) ───┘    │
└─────────────────────────────────────────────────────┘
```

- **`scrapers/ebay.py`** — Anti-bot hardened: `curl_cffi` (impersonate=chrome131) + session warm-up + `playwright-stealth` fallback. Supports eBay negative keyword syntax (`-rainbow -foilboard -PSA`) in `search_query`.
- **`engine/price_analyzer.py`** — Pure-Python technicals: moving averages, RSI, trend classification, support/resistance.
- **`agents/hold.py`** — Optional Python wrapper around Claude Sonnet for natural-language portfolio Q&A (not required if you drive it from Hermes).

---

## Quick start

### 1. Install

```bash
git clone https://github.com/<YOUR-USER>/hold-agent.git
cd hold-agent
python -m venv .venv && source .venv/bin/activate
pip install -e .
playwright install chromium
```

### 2. Initialize the DB

```bash
cp .env.example .env   # optional — tweak delays / proxy
python -m rookiecard.db.migrate
```

### 3. Seed your holdings

Add players first, then your cards. A one-off SQL is fine:

```sql
INSERT INTO players (name, draft_year, draft_position) VALUES ('Cooper Flagg', 2025, 1);

INSERT INTO portfolio
  (player_id, series, parallel, grade, buy_price, buy_date, search_query)
VALUES
  (1, '2025-26 Topps Flagship No Limit', 'Base', 'Raw', 10.00, '2026-04-06',
   'Cooper Flagg No Limit NL-1 RC -rainbow -foilboard -purple -PSA -BGS -SGC -graded');
```

`search_query` is crucial — it's the **exact eBay search** used for that holding. Use eBay negative keywords (`-foilboard`, `-PSA`, ...) to strip parallel / graded versions so prices stay comparable.

### 4. Smoke-test the eBay scraper

```bash
python -m rookiecard.scrapers.ebay --player "Cooper Flagg" --series "Topps" --days 14
```

### 5. Generate a PNG report

```bash
python -c "from rookiecard.mcp.tools.portfolio import refresh_portfolio_prices, export_portfolio_report; \
           refresh_portfolio_prices(); print(export_portfolio_report())"
```

The PNG lands in `data/reports/portfolio_<YYYY-MM-DD>.png`.

---

## Hermes integration (WeChat daily report)

> Full walkthrough: **[docs/SETUP.md](docs/SETUP.md)** — covers installing Hermes, pairing WeChat, seeding the portfolio, wiring the MCP server, and scheduling the daily cron.

Add this under `mcp_servers` in `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  rookiecard:
    command: /absolute/path/to/hold-agent/.venv/bin/python
    args: ["-m", "rookiecard.mcp.server"]
    env:
      PYTHONPATH: /absolute/path/to/hold-agent
      DB_PATH: /absolute/path/to/hold-agent/data/rookiecard.db
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

Then schedule a daily cron in Hermes:

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

The `MEDIA:<file_path>` line is extracted by Hermes and the PNG is sent as an image attachment.

---

## Layout

```
rookiecard/
├── config.py              # env-driven settings
├── agents/
│   ├── base.py            # Claude Sonnet agentic loop
│   ├── config.py          # tool registry, permissions
│   └── hold.py            # HoldAgent (optional wrapper)
├── scrapers/
│   └── ebay.py            # curl_cffi + Playwright-stealth
├── engine/
│   └── price_analyzer.py  # MA / RSI / trend / support
├── db/
│   ├── connection.py      # sqlite3 Row-factory context manager
│   └── migrate.py         # schema DDL
├── mcp/
│   ├── app.py             # FastMCP instance
│   ├── server.py          # stdio entry
│   └── tools/
│       ├── portfolio.py   # get/add/refresh/export
│       └── prices.py      # prices/pricing/compare
└── prompts/
    └── hold.md            # HoldAgent system prompt
```

---

## License

MIT
