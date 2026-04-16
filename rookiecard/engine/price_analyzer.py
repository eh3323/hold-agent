"""Price Technical Analyzer — 卡牌价格技术指标计算引擎。

第一性目的:
    把 card_sales 表里的原始成交记录，转化为可被 LLM 直接用来做买卖决策的技术信号。
    没有它，Agent 无法判断"现在是低点还是高点"、"价格在涨还是在跌"。

整体架构:
    PriceAnalyzer 类分为 3 层:
    1. 数据读取层 (_load_prices)         — 从 card_sales 读取价格序列
    2. 指标计算层 (_moving_average 等)   — 纯函数，不依赖 DB，可独立测试
    3. 编排层     (analyze)              — 一次调用返回完整 PriceAnalysis

数据流:
    (player, series, parallel, grade) → SQL 查询 → [(date, price), ...]
    → 计算 MA / RSI / 支撑压力 / 趋势 → PriceAnalysis dataclass

输出 PriceAnalysis 使能了:
    - Scout Agent: rsi_14d < 30 → 超卖信号，考虑入场
    - Hold Agent:  price_trend + volume_trend → 判断是否需要预警
    - Exit Agent:  resistance_price + ma_cross_signal → 判断卖出时机

Usage:
    analyzer = PriceAnalyzer(db_path="data/rookiecard.db")
    analysis = analyzer.analyze(
        player="Victor Wembanyama",
        series="Prizm",
        parallel="Silver",
        grade="PSA 10",
    )

CLI:
    python -m rookiecard.engine.price_analyzer "Victor Wembanyama" "Prizm" "Silver" "PSA 10"
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from rookiecard.db.connection import get_db


# ─────────────────────────────────────────────
# 数据类
# ─────────────────────────────────────────────

@dataclass
class PriceAnalysis:
    """一张卡的完整价格技术分析结果。None 表示数据不足，无法计算该指标。"""

    player: str
    series: str
    parallel: str
    grade: str

    # 基础价格
    current_median: float | None    # 最近7天成交中位数（无近期数据则取全部最新）
    ma_7d: float | None             # 7日移动均线
    ma_30d: float | None            # 30日移动均线
    ma_90d: float | None            # 90日移动均线

    # 动量
    rsi_14d: float | None           # 0-100；<30 超卖，>70 超买
    price_percentile_90d: float | None  # 当前价格在90日区间的百分位 (0-1)

    # 成交量
    volume_7d: int                  # 近7天成交笔数
    volume_trend: str | None        # "increasing" | "stable" | "decreasing"

    # 趋势信号
    price_trend: str | None         # "up" | "sideways" | "down"
    ma_cross_signal: str | None     # "golden_cross" | "death_cross" | None

    # 支撑/压力（P10 / P90 分位数法）
    support_price: float | None     # 估算价格底部
    resistance_price: float | None  # 估算价格顶部

    # 元数据
    data_points: int                # 用于计算的总数据点数
    date_range: tuple[str, str] | None = field(default=None)
    # (最早日期, 最新日期) ISO 格式


# ─────────────────────────────────────────────
# 主类
# ─────────────────────────────────────────────

class PriceAnalyzer:
    """
    从 card_sales 读取价格序列，计算全套技术指标，返回 PriceAnalysis。

    所有 _xxx 方法均为纯函数（无 DB 调用），便于单元测试。
    """

    def __init__(self, db_path: str | None = None):
        # db_path=None 时 get_db() 使用 Config.DB_PATH 默认值
        self._db_path = db_path

    # ──────────────────────────────────────────
    # 数据读取层
    # ──────────────────────────────────────────

    def _load_prices(
        self,
        player: str,
        series: str,
        parallel: str,
        grade: str,
        days: int = 90,
    ) -> list[tuple[date, float]]:
        """
        从 card_sales 表读取指定卡的成交记录。

        返回 [(sale_date, price), ...] 按 sale_date ASC 排序。
        days 控制往前查多少天（None 表示全部历史）。
        """
        cutoff = (datetime.now().date() - timedelta(days=days)).isoformat()

        sql = """
            SELECT cs.sale_date, cs.price
            FROM card_sales cs
            JOIN players p ON p.id = cs.player_id
            WHERE p.name = ?
              AND cs.series = ?
              AND cs.parallel = ?
              AND cs.grade = ?
              AND cs.sale_date >= ?
            ORDER BY cs.sale_date ASC
        """
        with get_db(self._db_path) as conn:
            rows = conn.execute(sql, (player, series, parallel, grade, cutoff)).fetchall()

        result: list[tuple[date, float]] = []
        for row in rows:
            d = row["sale_date"]
            if isinstance(d, str):
                d = date.fromisoformat(d)
            result.append((d, float(row["price"])))
        return result

    # ──────────────────────────────────────────
    # 指标计算层（纯函数）
    # ──────────────────────────────────────────

    @staticmethod
    def _moving_average(prices: list[float], window: int) -> float | None:
        """
        取最近 window 个价格的算术均值。
        数据点不足 window 时返回 None。
        """
        if len(prices) < window:
            return None
        return sum(prices[-window:]) / window

    @staticmethod
    def _rsi(prices: list[float], period: int = 14) -> float | None:
        """
        Wilder RSI（标准 14 日 RSI）。

        需要至少 period+1 个价格才能算出第一个 RSI 值。
        计算逻辑:
            1. 取最近 period+1 个价格，计算 period 个涨跌幅
            2. 分离 gains / losses（负值取 0）
            3. avg_gain = mean(gains), avg_loss = mean(losses)
            4. RS = avg_gain / avg_loss
            5. RSI = 100 - 100 / (1 + RS)
        """
        if len(prices) < period + 1:
            return None

        recent = prices[-(period + 1):]
        changes = [recent[i] - recent[i - 1] for i in range(1, len(recent))]
        gains = [max(c, 0) for c in changes]
        losses = [abs(min(c, 0)) for c in changes]

        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period

        if avg_loss == 0:
            return 100.0  # 全是涨，极端超买

        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 2)

    @staticmethod
    def _price_percentile(current: float, prices: list[float]) -> float | None:
        """
        当前价格在给定价格列表区间中的百分位 (0-1)。

        公式: (current - min) / (max - min)
        max == min（全部价格相同）时返回 0.5，避免除零。
        """
        if not prices:
            return None
        lo, hi = min(prices), max(prices)
        if hi == lo:
            return 0.5
        return round((current - lo) / (hi - lo), 4)

    @staticmethod
    def _volume_trend(records: list[tuple[date, float]]) -> str | None:
        """
        通过比较最近7天成交量 vs 前21天均值来判断成交量趋势。

        需要至少 14 天跨度的数据（2周）才能给出判断。
        规则:
            最近7天笔数 vs 前21天每周均值
            +20% 以上 → "increasing"
            -20% 以下 → "decreasing"
            其余       → "stable"
        """
        if not records:
            return None

        today = records[-1][0]
        cutoff_7d = today - timedelta(days=7)
        cutoff_28d = today - timedelta(days=28)

        recent_7 = sum(1 for d, _ in records if d > cutoff_7d)
        prev_21 = sum(1 for d, _ in records if cutoff_28d <= d <= cutoff_7d)

        # 数据跨度不足两周时无法判断
        oldest = records[0][0]
        if (today - oldest).days < 14:
            return None

        # 前21天每周均值
        prev_weekly_avg = prev_21 / 3.0 if prev_21 > 0 else 0

        if prev_weekly_avg == 0:
            return "increasing" if recent_7 > 0 else "stable"

        change_ratio = (recent_7 - prev_weekly_avg) / prev_weekly_avg
        if change_ratio > 0.20:
            return "increasing"
        elif change_ratio < -0.20:
            return "decreasing"
        return "stable"

    @staticmethod
    def _price_trend(prices: list[float]) -> str | None:
        """
        用最小二乘线性回归斜率判断价格趋势。

        需要至少 5 个数据点才返回有效趋势。
        规则（斜率相对于均价的日变化率）:
            > +0.5%/天 → "up"
            < -0.5%/天 → "down"
            其余        → "sideways"
        """
        n = len(prices)
        if n < 5:
            return None

        # 最小二乘斜率: slope = (n*Σxy - Σx*Σy) / (n*Σx² - (Σx)²)
        xs = list(range(n))
        sum_x = sum(xs)
        sum_y = sum(prices)
        sum_xy = sum(x * y for x, y in zip(xs, prices))
        sum_x2 = sum(x * x for x in xs)

        denom = n * sum_x2 - sum_x ** 2
        if denom == 0:
            return "sideways"

        slope = (n * sum_xy - sum_x * sum_y) / denom
        mean_price = sum_y / n

        if mean_price == 0:
            return "sideways"

        daily_change_pct = slope / mean_price
        if daily_change_pct > 0.005:
            return "up"
        elif daily_change_pct < -0.005:
            return "down"
        return "sideways"

    @staticmethod
    def _ma_cross_signal(prices: list[float], short: int = 7, long: int = 30) -> str | None:
        """
        检测移动均线金叉/死叉。

        需要至少 long+1 个数据点（才能对比今天和昨天的均线位置）。
        逻辑:
            今天 short_MA > long_MA 且 昨天 short_MA < long_MA → "golden_cross"（金叉，看涨信号）
            今天 short_MA < long_MA 且 昨天 short_MA > long_MA → "death_cross"（死叉，看跌信号）
            其余 → None
        """
        if len(prices) < long + 1:
            return None

        # 今天的均线（用最近 short/long 个价格）
        short_today = sum(prices[-short:]) / short
        long_today = sum(prices[-long:]) / long

        # 昨天的均线（向左移一个点）
        short_prev = sum(prices[-(short + 1):-1]) / short
        long_prev = sum(prices[-(long + 1):-1]) / long

        if short_prev < long_prev and short_today > long_today:
            return "golden_cross"
        elif short_prev > long_prev and short_today < long_today:
            return "death_cross"
        return None

    @staticmethod
    def _support_resistance(prices: list[float]) -> tuple[float, float] | tuple[None, None]:
        """
        用 P10/P90 百分位估算支撑价和压力价。

        这种方法在球星卡这种低流动性市场比复杂聚类算法更稳健：
        数据少时仍能给出合理估计，且结果直观可解释。
        数据点不足 10 个时返回 (None, None)。
        """
        if len(prices) < 10:
            return None, None

        sorted_prices = sorted(prices)
        n = len(sorted_prices)

        # 线性插值取百分位
        def percentile(p: float) -> float:
            idx = p * (n - 1)
            lo, hi = int(idx), min(int(idx) + 1, n - 1)
            frac = idx - lo
            return sorted_prices[lo] + frac * (sorted_prices[hi] - sorted_prices[lo])

        support = round(percentile(0.10), 2)
        resistance = round(percentile(0.90), 2)
        return support, resistance

    @staticmethod
    def _median(prices: list[float]) -> float | None:
        """计算中位数。"""
        if not prices:
            return None
        s = sorted(prices)
        n = len(s)
        mid = n // 2
        if n % 2 == 1:
            return s[mid]
        return (s[mid - 1] + s[mid]) / 2

    # ──────────────────────────────────────────
    # 编排层
    # ──────────────────────────────────────────

    def analyze(
        self,
        player: str,
        series: str,
        parallel: str,
        grade: str,
        days: int = 90,
    ) -> PriceAnalysis:
        """
        主入口：加载价格数据并计算全套技术指标。

        days 控制往前取多少天的数据（默认90天）。
        数据不足时各指标优雅降级为 None，不抛异常。
        """
        records = self._load_prices(player, series, parallel, grade, days)

        # 提取价格序列和元数据
        prices = [p for _, p in records]
        n = len(prices)

        if n == 0:
            return PriceAnalysis(
                player=player, series=series, parallel=parallel, grade=grade,
                current_median=None, ma_7d=None, ma_30d=None, ma_90d=None,
                rsi_14d=None, price_percentile_90d=None,
                volume_7d=0, volume_trend=None,
                price_trend=None, ma_cross_signal=None,
                support_price=None, resistance_price=None,
                data_points=0, date_range=None,
            )

        # 日期范围
        date_range = (records[0][0].isoformat(), records[-1][0].isoformat())

        # 近7天成交量
        today = records[-1][0]
        cutoff_7d = today - timedelta(days=7)
        volume_7d = sum(1 for d, _ in records if d > cutoff_7d)

        # current_median: 优先取近7天，若为空则取全部最新3条中位数
        recent_prices = [p for d, p in records if d > cutoff_7d]
        if recent_prices:
            current_median = self._median(recent_prices)
        else:
            current_median = self._median(prices[-3:])

        # 支撑/压力
        support, resistance = self._support_resistance(prices)

        # current 用于百分位计算（最新一笔成交价）
        current = prices[-1]

        return PriceAnalysis(
            player=player,
            series=series,
            parallel=parallel,
            grade=grade,
            current_median=current_median,
            ma_7d=self._moving_average(prices, 7),
            ma_30d=self._moving_average(prices, 30),
            ma_90d=self._moving_average(prices, 90),
            rsi_14d=self._rsi(prices),
            price_percentile_90d=self._price_percentile(current, prices),
            volume_7d=volume_7d,
            volume_trend=self._volume_trend(records),
            price_trend=self._price_trend(prices),
            ma_cross_signal=self._ma_cross_signal(prices),
            support_price=support,
            resistance_price=resistance,
            data_points=n,
            date_range=date_range,
        )


# ─────────────────────────────────────────────
# CLI 入口
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from dataclasses import fields

    if len(sys.argv) < 5:
        print("Usage: python -m rookiecard.engine.price_analyzer <player> <series> <parallel> <grade>")
        sys.exit(1)

    player_arg, series_arg, parallel_arg, grade_arg = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

    analyzer = PriceAnalyzer()
    result = analyzer.analyze(player_arg, series_arg, parallel_arg, grade_arg)

    print(f"\n=== Price Analysis: {player_arg} | {series_arg} {parallel_arg} | {grade_arg} ===")
    for f in fields(result):
        val = getattr(result, f.name)
        print(f"  {f.name:<25} {val}")
