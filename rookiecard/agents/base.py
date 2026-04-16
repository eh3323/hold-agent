"""BaseAgent — 封装 Anthropic Messages API agentic loop 的基类。

第一性目的:
    将 "调 API → 模型决定调 tool → 执行 tool → 结果喂回模型 → 循环"
    的完整 agentic loop 封装为一个类，让 Scout/Hold/Exit Agent
    只需指定 model + prompt + tools 就能工作。

核心流程:
    user_message
    → messages = [{"role": "user", "content": user_message}]
    → loop:
        → client.messages.create(model, system, tools, messages)
        → if stop_reason == "end_turn": return text
        → if stop_reason == "tool_use":
            → execute each tool call
            → append tool_results to messages
            → continue loop
    → max_turns exceeded: return last text + warning

依赖:
    外部: anthropic SDK (messages.create)
    内部: config.py (get_tool_registry, MAX_AGENT_TURNS)
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

from rookiecard.agents.config import (
    MAX_AGENT_TURNS,
    MAX_TOKENS_SPECIALIST,
    get_tool_registry,
)

logger = logging.getLogger(__name__)


class BaseAgent:
    """
    Anthropic Messages API agentic loop 基类。

    子类（Scout、Hold、Exit）通过传入不同的 system_prompt 和 tool_names 来定制行为。
    所有 Agent 共享同一套 loop 逻辑和 tool 执行机制。
    """

    def __init__(
        self,
        model: str,
        system_prompt: str,
        tool_names: list[str],
        *,
        db_path: str | None = None,
        max_tokens: int = MAX_TOKENS_SPECIALIST,
        client: anthropic.Anthropic | None = None,
    ):
        """
        Parameters:
            model:         Anthropic 模型名（如 "claude-sonnet-4-20250514"）
            system_prompt: 系统提示词（角色定义 + 规则）
            tool_names:    该 Agent 允许使用的 tool 名称列表
            db_path:       数据库路径（测试注入用，None 则用默认路径）
            max_tokens:    每次 API 调用的最大生成 token 数
            client:        Anthropic client（测试注入用，None 则自动创建）
        """
        self.model = model
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens
        self.db_path = db_path

        # Anthropic client（支持注入 mock）
        self._client = client or anthropic.Anthropic()

        # 从全局 registry 中取出该 Agent 可用的 tools
        registry = get_tool_registry()
        self._tools: dict[str, dict] = {}
        for name in tool_names:
            if name in registry:
                self._tools[name] = registry[name]
            else:
                logger.warning("Tool '%s' not found in registry, skipping.", name)

        # 构建 Anthropic API 格式的 tools schema
        self._tools_schema = self._build_tools_schema()

    # ─────────────────────────────────────────
    # Schema 构建层
    # ─────────────────────────────────────────

    def _build_tools_schema(self) -> list[dict]:
        """
        将内部 tool 注册信息转为 Anthropic API 的 tools 参数格式。

        返回 [{"name": str, "description": str, "input_schema": dict}, ...]
        """
        schemas = []
        for name, tool_info in self._tools.items():
            schemas.append({
                "name": name,
                "description": tool_info["description"],
                "input_schema": tool_info["input_schema"],
            })
        return schemas

    # ─────────────────────────────────────────
    # Tool 执行层
    # ─────────────────────────────────────────

    def _execute_tool(self, tool_name: str, tool_input: dict) -> Any:
        """
        执行一次 tool 调用。

        从注册表查找函数 → 注入 db_path → 调用 → 返回结果。
        任何异常被捕获，返回 {"error": str} 让模型能看到错误。
        """
        if tool_name not in self._tools:
            return {"error": f"Tool '{tool_name}' not available for this agent."}

        fn = self._tools[tool_name]["function"]

        # 自动注入 db_path（如果函数接受该参数）
        import inspect
        sig = inspect.signature(fn)
        if "db_path" in sig.parameters:
            tool_input = {**tool_input, "db_path": self.db_path}

        try:
            result = fn(**tool_input)
            return result
        except Exception as e:
            logger.exception("Tool '%s' execution failed: %s", tool_name, e)
            return {"error": f"Tool execution failed: {e!s}"}

    # ─────────────────────────────────────────
    # Agentic Loop（核心）
    # ─────────────────────────────────────────

    def run(self, user_message: str, *, max_turns: int | None = None) -> str:
        """
        主入口：运行 agentic loop，返回最终文本回答。

        Parameters:
            user_message: 用户输入
            max_turns:    最大循环轮数（默认 MAX_AGENT_TURNS）

        Returns:
            模型的最终文本回答
        """
        if max_turns is None:
            max_turns = MAX_AGENT_TURNS

        messages: list[dict] = [
            {"role": "user", "content": user_message},
        ]

        # 无 tools 时直接调 API（如 Router）
        api_tools = self._tools_schema if self._tools_schema else anthropic.NOT_GIVEN

        for turn in range(max_turns):
            logger.debug("Agent turn %d/%d", turn + 1, max_turns)

            response = self._client.messages.create(
                model=self.model,
                system=self.system_prompt,
                messages=messages,
                tools=api_tools,
                max_tokens=self.max_tokens,
            )

            # 检查 stop_reason
            if response.stop_reason == "end_turn":
                return self._extract_text(response)

            if response.stop_reason == "tool_use":
                # 把 assistant 的完整回复（可能包含 text + tool_use blocks）追加
                messages.append({
                    "role": "assistant",
                    "content": self._serialize_content(response.content),
                })

                # 执行所有 tool 调用，收集结果
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        logger.info("Calling tool: %s(%s)", block.name,
                                    json.dumps(block.input, ensure_ascii=False)[:200])
                        result = self._execute_tool(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result, ensure_ascii=False, default=str),
                        })

                # 把 tool results 作为 user message 追加
                messages.append({
                    "role": "user",
                    "content": tool_results,
                })
            else:
                # 其他 stop_reason (max_tokens 等)
                text = self._extract_text(response)
                if text:
                    return text
                return f"[Agent stopped: {response.stop_reason}]"

        # 超过 max_turns
        logger.warning("Agent reached max turns (%d)", max_turns)
        return self._extract_text(response) + "\n\n[Warning: Agent reached maximum reasoning turns.]"

    # ─────────────────────────────────────────
    # 辅助方法
    # ─────────────────────────────────────────

    @staticmethod
    def _extract_text(response) -> str:
        """从 API response 中提取所有 TextBlock 的文本拼接。"""
        texts = []
        for block in response.content:
            if getattr(block, "type", None) == "text":
                texts.append(block.text)
        return "\n".join(texts)

    @staticmethod
    def _serialize_content(content) -> list[dict]:
        """
        将 response.content（Pydantic models）序列化为 dict 列表。

        Anthropic SDK 返回的 content blocks 是 Pydantic 对象，
        需要转为 dict 才能作为后续 messages 传入。
        """
        serialized = []
        for block in content:
            if block.type == "text":
                serialized.append({
                    "type": "text",
                    "text": block.text,
                })
            elif block.type == "tool_use":
                serialized.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
        return serialized
