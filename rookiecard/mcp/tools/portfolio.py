"""MCP Tools: get_portfolio / add_to_portfolio — 投资组合管理。

第一性目的:
    管理用户的持仓状态——记录买了什么卡、花了多少钱、现在赚还是亏。
    这是 Agent 从"信息工具"变为"投资助手"的关键跨越。

数据流:
    get_portfolio:
        portfolio JOIN players → 每条持仓 → 查 card_sales 当前估价
        → 计算 pnl_pct + holding_days + health_status
        → 返回 {holdings, summary}

    add_to_portfolio:
        球员模糊匹配 → INSERT INTO portfolio → 返回 {id, status}

依赖:
    读取: portfolio, players, card_sales 表
    写入: portfolio 表（仅 add_to_portfolio）
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from statistics import median

from rookiecard.config import Config
from rookiecard.db.connection import get_db
from rookiecard.mcp.app import mcp

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# 内部辅助函数
# ─────────────────────────────────────────────

def _find_player_id(db_path: str | None, player_name: str) -> tuple[int, str] | None:
    """模糊匹配球员，返回 (player_id, matched_name) 或 None。"""
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT id, name FROM players WHERE name LIKE ? ORDER BY name LIMIT 1",
            (f"%{player_name}%",),
        ).fetchone()
    if row is None:
        return None
    return row["id"], row["name"]


def _get_current_price(
    db_path: str | None,
    player_id: int,
    series: str,
    parallel: str,
    grade: str,
) -> float | None:
    """
    估算当前市场价：近7天同卡种中位价。

    无近7天数据时取最近3条成交的中位数。
    完全无数据返回 None。
    """
    cutoff_7d = (datetime.now().date() - timedelta(days=7)).isoformat()

    with get_db(db_path) as conn:
        # 优先取近7天
        rows = conn.execute(
            """SELECT price FROM card_sales
               WHERE player_id = ? AND series = ? AND parallel = ? AND grade = ?
                 AND sale_date >= ?
               ORDER BY sale_date DESC""",
            (player_id, series, parallel, grade, cutoff_7d),
        ).fetchall()

        if rows:
            return round(median(r["price"] for r in rows), 2)

        # 降级：取最近3条
        rows = conn.execute(
            """SELECT price FROM card_sales
               WHERE player_id = ? AND series = ? AND parallel = ? AND grade = ?
               ORDER BY sale_date DESC
               LIMIT 3""",
            (player_id, series, parallel, grade),
        ).fetchall()

    if not rows:
        return None
    return round(median(r["price"] for r in rows), 2)


def _holding_health(pnl_pct: float | None, holding_days: int) -> str:
    """
    基于盈亏和持有时间判断持仓健康度。

    规则:
      healthy:  pnl ≥ 0，或持有 < 14天（新买入波动正常）
      watch:    -10% < pnl < 0 且持有 ≥ 14天
      at_risk:  pnl ≤ -10% 且持有 ≥ 14天
      unknown:  无法估价（pnl=None）
    """
    if pnl_pct is None:
        return "unknown"
    if holding_days < 14 or pnl_pct >= 0:
        return "healthy"
    if pnl_pct > -0.10:
        return "watch"
    return "at_risk"


# ─────────────────────────────────────────────
# Tool 1: get_portfolio
# ─────────────────────────────────────────────

@mcp.tool(
    name="get_portfolio",
    description="Get user's current card holdings with P&L data.",
)
def get_portfolio(
    status: str = "active",
    db_path: str | None = None,
) -> dict:
    """
    查询用户持仓，附带当前估价和盈亏。

    Parameters:
        status:  过滤条件 — "active" | "sold" | "all"
        db_path: 数据库路径（测试用）

    Returns:
        {
            "status_filter": str,
            "count": int,
            "holdings": [{
                "id": int,
                "player": str,
                "series": str,
                "parallel": str,
                "grade": str,
                "buy_price": float,
                "buy_date": str,
                "current_price": float | None,
                "pnl_pct": float | None,
                "holding_days": int,
                "health_status": str,
                "notes": str | None,
                "target_price": float | None,
            }],
            "summary": {
                "total_cost": float,
                "total_value": float | None,
                "total_pnl_pct": float | None,
                "holdings_count": int,
            }
        }
    """
    valid_statuses = ("active", "sold", "all")
    if status not in valid_statuses:
        return {"error": f"Invalid status: '{status}'. Must be one of {valid_statuses}."}

    # 查询持仓
    with get_db(db_path) as conn:
        if status == "all":
            rows = conn.execute(
                """SELECT pf.id, p.id AS player_id, p.name, pf.series, pf.parallel, pf.grade,
                          pf.buy_price, pf.buy_date, pf.sell_price, pf.sell_date,
                          pf.status, pf.notes, pf.target_price
                   FROM portfolio pf
                   JOIN players p ON p.id = pf.player_id
                   ORDER BY pf.buy_date DESC""",
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT pf.id, p.id AS player_id, p.name, pf.series, pf.parallel, pf.grade,
                          pf.buy_price, pf.buy_date, pf.sell_price, pf.sell_date,
                          pf.status, pf.notes, pf.target_price
                   FROM portfolio pf
                   JOIN players p ON p.id = pf.player_id
                   WHERE pf.status = ?
                   ORDER BY pf.buy_date DESC""",
                (status,),
            ).fetchall()

    today = date.today()
    holdings = []
    total_cost = 0.0
    total_value = 0.0
    all_priced = True  # 所有持仓都有估价？

    for row in rows:
        buy_price = float(row["buy_price"])
        buy_date_str = row["buy_date"]

        # 持有天数
        try:
            buy_dt = date.fromisoformat(buy_date_str)
            holding_days = (today - buy_dt).days
        except (ValueError, TypeError):
            holding_days = 0

        # 当前估价
        if row["status"] == "sold" and row["sell_price"] is not None:
            current_price = float(row["sell_price"])
        else:
            current_price = _get_current_price(
                db_path, row["player_id"], row["series"], row["parallel"], row["grade"]
            )

        # 盈亏
        if current_price is not None and buy_price > 0:
            pnl_pct = round((current_price - buy_price) / buy_price, 4)
        else:
            pnl_pct = None

        health = _holding_health(pnl_pct, holding_days)

        holdings.append({
            "id": row["id"],
            "player": row["name"],
            "series": row["series"],
            "parallel": row["parallel"],
            "grade": row["grade"],
            "buy_price": buy_price,
            "buy_date": buy_date_str,
            "current_price": current_price,
            "pnl_pct": pnl_pct,
            "holding_days": holding_days,
            "health_status": health,
            "notes": row["notes"],
            "target_price": float(row["target_price"]) if row["target_price"] else None,
        })

        total_cost += buy_price
        if current_price is not None:
            total_value += current_price
        else:
            all_priced = False

    # Summary
    if total_cost > 0 and all_priced:
        summary_pnl = round((total_value - total_cost) / total_cost, 4)
    else:
        summary_pnl = None

    summary = {
        "total_cost": round(total_cost, 2),
        "total_value": round(total_value, 2) if all_priced else None,
        "total_pnl_pct": summary_pnl,
        "holdings_count": len(holdings),
    }

    return {
        "status_filter": status,
        "count": len(holdings),
        "holdings": holdings,
        "summary": summary,
    }


# ─────────────────────────────────────────────
# Tool 2: add_to_portfolio
# ─────────────────────────────────────────────

@mcp.tool(
    name="add_to_portfolio",
    description="Record a new card purchase in the portfolio.",
)
def add_to_portfolio(
    player: str,
    series: str,
    parallel: str,
    grade: str,
    buy_price: float,
    buy_date: str | None = None,
    notes: str | None = None,
    db_path: str | None = None,
) -> dict:
    """
    记录一张新购买的卡到 portfolio 表。

    Parameters:
        player:    球员姓名（模糊匹配）
        series:    卡系列
        parallel:  平行版本
        grade:     评级
        buy_price: 购入价格 (USD)
        buy_date:  购入日期 ISO 格式，默认今天
        notes:     可选备注
        db_path:   数据库路径（测试用）

    Returns:
        {id: int, player: str, status: "added"} 或 {error: str}
    """
    # 验证球员
    match = _find_player_id(db_path, player)
    if match is None:
        return {"error": f"Player not found: '{player}'. Add the player to the database first."}
    pid, matched_name = match

    # 验证价格（兼容字符串传入，如 "40" 或 "$40"）
    try:
        buy_price = float(str(buy_price).lstrip("$").strip())
    except (ValueError, TypeError):
        return {"error": f"buy_price must be a valid number, got {buy_price!r}."}
    if buy_price <= 0:
        return {"error": f"buy_price must be positive, got {buy_price}."}

    # 默认日期
    if buy_date is None:
        buy_date = date.today().isoformat()

    # 验证日期格式
    try:
        date.fromisoformat(buy_date)
    except ValueError:
        return {"error": f"Invalid buy_date format: '{buy_date}'. Expected ISO format (YYYY-MM-DD)."}

    # 插入
    with get_db(db_path) as conn:
        cursor = conn.execute(
            """INSERT INTO portfolio
               (player_id, series, parallel, grade, buy_price, buy_date, status, notes)
               VALUES (?, ?, ?, ?, ?, ?, 'active', ?)""",
            (pid, series, parallel, grade, buy_price, buy_date, notes),
        )
        new_id = cursor.lastrowid

    return {
        "id": new_id,
        "player": matched_name,
        "series": series,
        "parallel": parallel,
        "grade": grade,
        "buy_price": buy_price,
        "buy_date": buy_date,
        "status": "added",
    }


# ─────────────────────────────────────────────
# Tool 3: refresh_portfolio_prices
# ─────────────────────────────────────────────

@mcp.tool(
    name="refresh_portfolio_prices",
    description=(
        "Scrape fresh eBay sold prices for all active portfolio holdings and save to DB. "
        "Always call this before get_card_prices or get_portfolio to ensure data is current. "
        "Uses each holding's search_query for precise matching."
    ),
)
def refresh_portfolio_prices(
    days: int = 14,
    db_path: str | None = None,
) -> dict:
    """
    遍历所有 active 持仓，用各自的 search_query 抓取最新 eBay 成交数据并入库。

    Parameters:
        days:    抓取最近 N 天的成交记录（默认 Config.EBAY_HOLDINGS_DAYS = 14）
        db_path: 数据库路径（测试用）

    Returns:
        {
            "holdings_count": int,
            "results": [{"card": str, "new_sales": int, "total_fetched": int} | {"card": str, "error": str}],
            "total_new_sales": int,
        }
    """
    from rookiecard.scrapers.ebay import EbayScraper

    # 读取所有 active 持仓
    with get_db(db_path) as conn:
        rows = conn.execute(
            """SELECT pf.id, p.name AS player_name,
                      pf.series, pf.parallel, pf.grade, pf.search_query
               FROM portfolio pf
               JOIN players p ON p.id = pf.player_id
               WHERE pf.status = 'active'""",
        ).fetchall()

    if not rows:
        return {"holdings_count": 0, "results": [], "total_new_sales": 0}

    scraper = EbayScraper(
        max_pages=Config.EBAY_HOLDINGS_MAX_PAGES,
        delay=Config.EBAY_DELAY,
    )
    results = []
    total_new = 0

    try:
        for row in rows:
            player_name = row["player_name"]
            series      = row["series"]
            parallel    = row["parallel"]
            grade       = row["grade"]
            search_query = row["search_query"] or ""
            card_label  = f"{player_name} | {series} {parallel} {grade}"

            try:
                if search_query:
                    sales = scraper.get_sold_listings(search_query=search_query, days=days)
                else:
                    sales = scraper.get_sold_listings(
                        player=player_name, series=series,
                        parallel=parallel, grade=grade, days=days,
                    )

                inserted = scraper.save_sales(player_name, series, parallel, grade, sales, db_path)
                total_new += inserted
                results.append({
                    "card": card_label,
                    "new_sales": inserted,
                    "total_fetched": len(sales),
                })
                logger.info("refresh_portfolio_prices: %s → %d fetched, %d new", card_label, len(sales), inserted)

            except Exception as exc:
                logger.error("refresh_portfolio_prices failed for %s: %s", card_label, exc)
                results.append({"card": card_label, "error": str(exc)})

    finally:
        scraper.close()

    return {
        "holdings_count": len(rows),
        "results": results,
        "total_new_sales": total_new,
    }


# ─────────────────────────────────────────────
# Tool 4: export_portfolio_report
# ─────────────────────────────────────────────

_HEALTH_COLOR = {
    "healthy": ("#16a34a", "🟢"),
    "watch":   ("#ca8a04", "🟡"),
    "at_risk": ("#dc2626", "🔴"),
    "unknown": ("#6b7280", "⚪"),
}


def _render_report_html(portfolio: dict, today: str) -> str:
    """把 get_portfolio() 的结果渲染为 HTML 报告字符串。"""
    holdings = portfolio.get("holdings", [])
    summary  = portfolio.get("summary", {})

    total_cost  = summary.get("total_cost") or 0.0
    total_value = summary.get("total_value")
    total_pnl   = summary.get("total_pnl_pct")

    # 汇总卡片
    total_value_str = f"${total_value:,.2f}" if total_value is not None else "—"
    if total_pnl is not None:
        pnl_color = "#16a34a" if total_pnl >= 0 else "#dc2626"
        pnl_sign = "+" if total_pnl >= 0 else ""
        total_pnl_str = f'<span style="color:{pnl_color}">{pnl_sign}{total_pnl*100:.1f}%</span>'
    else:
        total_pnl_str = "—"

    # 持仓行
    rows_html = ""
    for h in holdings:
        color, icon = _HEALTH_COLOR.get(h.get("health_status", "unknown"), _HEALTH_COLOR["unknown"])
        buy_price = h["buy_price"]
        current   = h.get("current_price")
        pnl       = h.get("pnl_pct")

        current_str = f"${current:,.2f}" if current is not None else "—"
        if pnl is not None:
            pnl_c = "#16a34a" if pnl >= 0 else "#dc2626"
            sign  = "+" if pnl >= 0 else ""
            pnl_str = f'<span style="color:{pnl_c};font-weight:600">{sign}{pnl*100:.1f}%</span>'
        else:
            pnl_str = "—"

        card_desc = f"{h['series']} · {h['parallel']} · {h['grade']}"
        rows_html += f"""
        <tr>
            <td style="font-size:20px">{icon}</td>
            <td>
                <div class="player">{h['player']}</div>
                <div class="card-desc">{card_desc}</div>
            </td>
            <td class="num">${buy_price:,.2f}</td>
            <td class="num">{current_str}</td>
            <td class="num">{pnl_str}</td>
            <td class="num">{h['holding_days']}d</td>
            <td><span class="badge" style="background:{color}">{h.get('health_status','unknown')}</span></td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<style>
  body {{
    font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
    margin: 0; padding: 32px; background: #f8fafc;
    color: #0f172a;
  }}
  .container {{ max-width: 980px; margin: 0 auto; }}
  h1 {{
    font-size: 26px; margin: 0 0 4px 0;
    display: flex; align-items: center; gap: 12px;
  }}
  .date {{ color: #64748b; font-size: 14px; margin-bottom: 20px; }}
  .summary {{
    display: flex; gap: 16px; margin-bottom: 24px;
  }}
  .summary .card {{
    flex: 1; background: white; border-radius: 12px; padding: 16px 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  }}
  .summary .label {{ font-size: 12px; color: #64748b; margin-bottom: 6px; }}
  .summary .value {{ font-size: 22px; font-weight: 600; }}
  table {{
    width: 100%; background: white; border-radius: 12px;
    border-collapse: collapse; overflow: hidden;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  }}
  thead {{ background: #f1f5f9; }}
  th {{
    text-align: left; padding: 12px 14px; font-size: 12px;
    font-weight: 600; color: #475569; text-transform: uppercase;
    letter-spacing: 0.5px;
  }}
  td {{ padding: 14px; border-top: 1px solid #e2e8f0; font-size: 14px; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  th.num {{ text-align: right; }}
  .player {{ font-weight: 600; font-size: 15px; }}
  .card-desc {{ font-size: 12px; color: #64748b; margin-top: 2px; }}
  .badge {{
    color: white; padding: 4px 10px; border-radius: 8px;
    font-size: 11px; font-weight: 600;
  }}
  footer {{ text-align: center; color: #94a3b8; font-size: 11px; margin-top: 20px; }}
</style>
</head>
<body>
<div class="container">
  <h1>📊 球星卡持仓日报</h1>
  <div class="date">{today} · 共 {len(holdings)} 张卡</div>

  <div class="summary">
    <div class="card">
      <div class="label">总成本</div>
      <div class="value">${total_cost:,.2f}</div>
    </div>
    <div class="card">
      <div class="label">当前估值</div>
      <div class="value">{total_value_str}</div>
    </div>
    <div class="card">
      <div class="label">整体盈亏</div>
      <div class="value">{total_pnl_str}</div>
    </div>
  </div>

  <table>
    <thead>
      <tr>
        <th></th>
        <th>球员 / 卡片</th>
        <th class="num">买入价</th>
        <th class="num">当前价</th>
        <th class="num">盈亏</th>
        <th class="num">持有</th>
        <th>状态</th>
      </tr>
    </thead>
    <tbody>{rows_html}
    </tbody>
  </table>

  <footer>Rookie Card Investment Agent · Generated by Hold Agent</footer>
</div>
</body>
</html>"""


def _html_to_png(html: str, output_path: str) -> None:
    """用 Playwright 把 HTML 渲染为 PNG。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1040, "height": 800}, device_scale_factor=2)
            page.set_content(html, wait_until="load")
            page.wait_for_timeout(300)
            # 只截取内容区域（去掉 body 的 padding 外的空白）
            page.locator(".container").screenshot(path=output_path, omit_background=False)
        finally:
            browser.close()


@mcp.tool(
    name="export_portfolio_report",
    description=(
        "Generate a PNG image report of the current active portfolio (P&L, trend, health). "
        "Returns the file path. To deliver the image on a messaging platform, "
        "include `MEDIA:<file_path>` in the final response text alongside the summary."
    ),
)
def export_portfolio_report(db_path: str | None = None) -> dict:
    """
    生成持仓 PNG 报告。

    流程:
        1. 调用 get_portfolio("active") 读取持仓 + P&L
        2. 渲染为 HTML（含样式 + 表格 + 健康状态着色）
        3. 用 Playwright 把 HTML 截图为 PNG
        4. 保存到 data/reports/portfolio_<date>.png

    Returns:
        {
          "file_path": str,     # PNG 绝对路径（用于 MEDIA: 标签）
          "holdings_count": int,
          "summary": dict,      # total_cost / total_value / total_pnl_pct
        }
    """
    from pathlib import Path as _Path

    portfolio = get_portfolio(status="active", db_path=db_path)
    if portfolio.get("count", 0) == 0:
        return {"error": "No active holdings to report."}

    today = date.today().isoformat()
    html = _render_report_html(portfolio, today)

    # 输出目录：项目根/data/reports/
    project_root = _Path(__file__).resolve().parents[3]
    output_dir = project_root / "data" / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    png_path = output_dir / f"portfolio_{today}.png"

    try:
        _html_to_png(html, str(png_path))
    except Exception as exc:
        logger.error("export_portfolio_report render failed: %s", exc)
        return {"error": f"PNG rendering failed: {exc}"}

    return {
        "file_path": str(png_path),
        "holdings_count": portfolio["count"],
        "summary": portfolio["summary"],
    }
