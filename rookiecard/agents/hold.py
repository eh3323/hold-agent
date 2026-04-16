"""Hold Agent — 持仓监控 Agent。

第一性目的:
    回答"我的卡怎么样了"——对用户持仓逐一体检，用信号灯标注健康状态，
    标记风险事件，给出继续持有/关注/考虑卖出的建议。

架构:
    极薄包装层：加载 prompts/hold.md + 创建 BaseAgent(Sonnet, HOLD_TOOLS)。
    也负责通过 add_to_portfolio 工具记录用户新购入的卡。

依赖:
    prompts/hold.md, base.py, config.py
被谁���用:
    Router.dispatch() 当 intent=HOLD 时
"""

from __future__ import annotations

import logging

from rookiecard.agents.base import BaseAgent
from rookiecard.agents.config import (
    MODEL_SPECIALIST,
    MAX_TOKENS_SPECIALIST,
    PROMPTS_DIR,
    get_tools_for_agent,
)

logger = logging.getLogger(__name__)


class HoldAgent:
    """
    持仓监控 Agent：体检持仓健康状态，标记风险，记录新购入。

    内部持有 BaseAgent 实例（组合模式），通过 run() 透传调用。
    """

    def __init__(
        self,
        *,
        db_path: str | None = None,
        client=None,
    ):
        prompt_path = PROMPTS_DIR / "hold.md"
        system_prompt = prompt_path.read_text(encoding="utf-8")

        self._agent = BaseAgent(
            model=MODEL_SPECIALIST,
            system_prompt=system_prompt,
            tool_names=get_tools_for_agent("hold"),
            db_path=db_path,
            max_tokens=MAX_TOKENS_SPECIALIST,
            client=client,
        )

    def run(self, user_message: str, **kwargs) -> str:
        """运行 Hold Agent，返回最终回答。"""
        return self._agent.run(user_message, **kwargs)
