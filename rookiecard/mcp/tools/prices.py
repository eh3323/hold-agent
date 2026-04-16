"""MCP Tools: get_card_prices / get_pricing_analysis / compare_cards — 卡牌价格工具。

第一性目的:
    把卡牌的价格数据和技术分析指标暴露给 LLM Agent，让它能回答：
    - "这张卡现在值多少？"      → get_card_prices
    - "该挂什么价？"            → get_pricing_analysis
    - "Prizm Silver 和 Optic 哪个值？" → compare_cards

数据流:
    三个 tool 共享 _find_player_id / _get_recent_sales 辅助函数，
    核心计算复用 PriceAnalyzer（无状态，每次实例化开销极小）。

依赖:
    读取: players, card_sales, pop_reports 表
    内部: PriceAnalyzer, get_db
    写入: 无
"""

from __future__ import annotations

from datetime import datetime, timedelta

from rookiecard.db.connection import get_db
from rookiecard.engine.price_analyzer import PriceAnalyzer
from rookiecard.mcp.app import mcp

# eBay 费率：基础 12.9% + 支付处理 0.3% ≈ 13.13%
EBAY_FEE_RATE = 0.1313


# ─────────────────────────────────────────────
# 共享辅助函数
# ─────────────────────────────────────────────

def _find_player_id(db_path: str | None, player_name: str) -> tuple[int, str] | None:
    """
    模糊匹配球员姓名，返回 (player_id, matched_name)。

    未找到时返回 None。
    """
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT id, name FROM players WHERE name LIKE ? ORDER BY name LIMIT 1",
            (f"%{player_name}%",),
        ).fetchone()
    if row is None:
        return None
    return row["id"], row["name"]


def _get_recent_sales(
    db_path: str | None,
    player_id: int,
    series: str,
    parallel: str,
    grade: str,
    days: int = 90,
) -> list[dict]:
    """
    查 card_sales 最近 days 天的成交记录，按 sale_date DESC。

    返回 [{price, sale_date, listing_type}]。
    """
    cutoff = (datetime.now().date() - timedelta(days=days)).isoformat()
    with get_db(db_path) as conn:
        rows = conn.execute(
            """SELECT price, sale_date, listing_type
               FROM card_sales
               WHERE player_id = ? AND series = ? AND parallel = ? AND grade = ?
                 AND sale_date >= ?
               ORDER BY sale_date DESC""",
            (player_id, series, parallel, grade, cutoff),
        ).fetchall()
    return [
        {"price": float(r["price"]), "sale_date": r["sale_date"], "listing_type": r["listing_type"]}
        for r in rows
    ]


def _percentile(values: list[float], p: float) -> float | None:
    """
    线性插值百分位。p 取 0-1（e.g. 0.25, 0.5, 0.75）。

    空列表时返回 None。
    """
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    idx = p * (n - 1)
    lo, hi = int(idx), min(int(idx) + 1, n - 1)
    frac = idx - lo
    return round(s[lo] + frac * (s[hi] - s[lo]), 2)


# ─────────────────────────────────────────────
# Tool 1: get_card_prices
# ─────────────────────────────────────────────

@mcp.tool(
    name="get_card_prices",
    description="Get price data for a specific card (player + series + parallel + grade).",
)
def get_card_prices(
    player: str,
    series: str = "Prizm",
    parallel: str = "Base",
    grade: str = "Raw",
    db_path: str | None = None,
) -> dict:
    """
    查询单张卡的价格数据和技术指标。

    调用 PriceAnalyzer.analyze() 获取 MA/RSI/趋势等全套指标，
    并附加最近10条成交记录供 LLM 生成叙述。

    Returns:
        {player, series, parallel, grade, current_median, ma_7d, ma_30d,
         rsi, price_trend, volume, recent_sales, percentile_90d}
    """
    match = _find_player_id(db_path, player)
    if match is None:
        return {"error": f"Player not found: '{player}'."}
    pid, matched_name = match

    # PriceAnalyzer 做全套技术分析
    analyzer = PriceAnalyzer(db_path)
    analysis = analyzer.analyze(matched_name, series, parallel, grade)

    # 最近10条成交
    recent = _get_recent_sales(db_path, pid, series, parallel, grade, days=90)[:10]
    recent_formatted = [{"price": s["price"], "date": s["sale_date"]} for s in recent]

    return {
        "player": matched_name,
        "series": series,
        "parallel": parallel,
        "grade": grade,
        "current_median": analysis.current_median,
        "ma_7d": analysis.ma_7d,
        "ma_30d": analysis.ma_30d,
        "ma_90d": analysis.ma_90d,
        "rsi": analysis.rsi_14d,
        "price_trend": analysis.price_trend,
        "volume": analysis.volume_7d,
        "volume_trend": analysis.volume_trend,
        "ma_cross_signal": analysis.ma_cross_signal,
        "support_price": analysis.support_price,
        "resistance_price": analysis.resistance_price,
        "percentile_90d": analysis.price_percentile_90d,
        "data_points": analysis.data_points,
        "date_range": analysis.date_range,
        "recent_sales": recent_formatted,
    }


# ─────────────────────────────────────────────
# Tool 2: get_pricing_analysis
# ─────────────────────────────────────────────

@mcp.tool(
    name="get_pricing_analysis",
    description="Get detailed pricing recommendation for selling a card.",
)
def get_pricing_analysis(
    player: str,
    series: str,
    parallel: str,
    grade: str,
    db_path: str | None = None,
) -> dict:
    """
    基于近30天成交数据给出定价建议。

    定价逻辑:
        recommended_bin_price     = P75（一口价取高位，吸引愿出价的买家）
        recommended_auction_start = P25（起拍价取低位，吸引竞拍流量）
        ebay_fee_estimate         = median * EBAY_FEE_RATE (13.13%)

    Returns:
        {player, series, parallel, grade,
         recommended_bin_price, recommended_auction_start,
         p25_price, median_price, p75_price,
         total_sales_30d, ebay_fee_estimate}
    """
    match = _find_player_id(db_path, player)
    if match is None:
        return {"error": f"Player not found: '{player}'."}
    pid, matched_name = match

    sales = _get_recent_sales(db_path, pid, series, parallel, grade, days=30)
    prices = [s["price"] for s in sales]

    if not prices:
        return {
            "player": matched_name,
            "series": series,
            "parallel": parallel,
            "grade": grade,
            "message": "No sales data in the last 30 days.",
            "total_sales_30d": 0,
        }

    p25 = _percentile(prices, 0.25)
    median = _percentile(prices, 0.50)
    p75 = _percentile(prices, 0.75)

    return {
        "player": matched_name,
        "series": series,
        "parallel": parallel,
        "grade": grade,
        "recommended_bin_price": p75,
        "recommended_auction_start": p25,
        "p25_price": p25,
        "median_price": median,
        "p75_price": p75,
        "total_sales_30d": len(prices),
        "ebay_fee_estimate": round(median * EBAY_FEE_RATE, 2) if median else None,
    }


# ─────────────────────────────────────────────
# Tool 3: compare_cards
# ─────────────────────────────────────────────

@mcp.tool(
    name="compare_cards",
    description=(
        "Compare multiple card versions side-by-side "
        "(e.g., Prizm Silver PSA 10 vs Optic Holo PSA 10)."
    ),
)
def compare_cards(
    player: str,
    cards: list[dict] | None = None,
    db_path: str | None = None,
) -> dict:
    """
    同一球员多张卡横向对比。

    对每张卡调用 PriceAnalyzer 获取价格指标，
    查 pop_reports 获取 PSA 数据，计算 90d 回报率和流动性评分。

    Parameters:
        player: 球员姓名
        cards:  [{series, parallel, grade}, ...]

    Returns:
        {player, comparisons: [{series, parallel, grade, current_price,
         pop_count, psa10_ratio, return_90d, volume_7d, liquidity_score}]}
    """
    if not cards:
        return {"error": "No cards provided for comparison."}

    match = _find_player_id(db_path, player)
    if match is None:
        return {"error": f"Player not found: '{player}'."}
    pid, matched_name = match

    analyzer = PriceAnalyzer(db_path)
    comparisons = []

    for card in cards:
        s = card.get("series", "Prizm")
        p = card.get("parallel", "Base")
        g = card.get("grade", "Raw")

        # 技术分析
        analysis = analyzer.analyze(matched_name, s, p, g)

        # 90d 回报率：(当前价 - 最早价) / 最早价
        return_90d = None
        return_days = None
        if analysis.data_points >= 2 and analysis.current_median is not None:
            # 取最早成交价作为基准
            sales = _get_recent_sales(db_path, pid, s, p, g, days=90)
            if sales:
                oldest_price = sales[-1]["price"]  # sales 按 DESC，最后一条最旧
                if oldest_price > 0:
                    return_90d = round((analysis.current_median - oldest_price) / oldest_price, 4)
                    # 实际跨度天数
                    from datetime import date as _date
                    if analysis.date_range:
                        try:
                            d0 = _date.fromisoformat(analysis.date_range[0])
                            d1 = _date.fromisoformat(analysis.date_range[1])
                            return_days = (d1 - d0).days
                        except (ValueError, TypeError):
                            pass

        # Pop report（可能不存在）
        pop_count = None
        psa10_ratio = None
        with get_db(db_path) as conn:
            pop_row = conn.execute(
                """SELECT total_graded, psa_10_ratio
                   FROM pop_reports
                   WHERE player_id = ?
                   ORDER BY fetched_at DESC
                   LIMIT 1""",
                (pid,),
            ).fetchone()
        if pop_row:
            pop_count = pop_row["total_graded"]
            psa10_ratio = pop_row["psa_10_ratio"]

        # 流动性评分：volume_7d 归一化（简单映射：0笔=0, 1-3=0.3, 4-7=0.6, 8+=1.0）
        v = analysis.volume_7d
        if v >= 8:
            liquidity = 1.0
        elif v >= 4:
            liquidity = 0.6
        elif v >= 1:
            liquidity = 0.3
        else:
            liquidity = 0.0

        comparisons.append({
            "series": s,
            "parallel": p,
            "grade": g,
            "current_price": analysis.current_median,
            "price_trend": analysis.price_trend,
            "rsi": analysis.rsi_14d,
            "pop_count": pop_count,
            "psa10_ratio": psa10_ratio,
            "return_90d": return_90d,
            "return_days": return_days,
            "volume_7d": analysis.volume_7d,
            "liquidity_score": liquidity,
        })

    return {
        "player": matched_name,
        "comparisons": comparisons,
    }
