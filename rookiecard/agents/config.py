"""Agent 配置层 — 模型常量、Tool 注册表、权限矩阵（Hold Agent 精简版）。

职责:
    1. 定义模型名称、token 限制等常量
    2. 构建 TOOL_REGISTRY: {name → {function, description, parameters_schema}}
    3. 定义 Hold Agent 允许使用的 tools
"""

from __future__ import annotations

import inspect
import typing
from pathlib import Path
from typing import Any, get_args, get_origin


# ─────────────────────────────────────────────
# 模型常量
# ─────────────────────────────────────────────

MODEL_SPECIALIST = "claude-sonnet-4-20250514"
MAX_TOKENS_SPECIALIST = 4096
MAX_AGENT_TURNS = 15

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


# ─────────────────────────────────────────────
# Agent Tool 权限矩阵
# ─────────────────────────────────────────────

AGENT_TOOLS: dict[str, list[str]] = {
    "hold": [
        "get_portfolio",
        "add_to_portfolio",
        "refresh_portfolio_prices",
        "export_portfolio_report",
        "get_card_prices",
        "get_pricing_analysis",
    ],
}


# ─────────────────────────────────────────────
# Python type → JSON Schema 映射
# ─────────────────────────────────────────────

def _python_type_to_json_schema(annotation: Any) -> dict:
    """将 Python type hint 转为 JSON Schema 类型描述。"""
    if annotation is inspect.Parameter.empty or annotation is Any:
        return {"type": "string"}

    origin = get_origin(annotation)
    if origin is typing.Union:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return _python_type_to_json_schema(args[0])
        return {"type": "string"}

    type_map = {
        str:   {"type": "string"},
        int:   {"type": "integer"},
        float: {"type": "number"},
        bool:  {"type": "boolean"},
    }
    if annotation in type_map:
        return type_map[annotation]

    if annotation is list or origin is list:
        args = get_args(annotation)
        if args:
            return {"type": "array", "items": _python_type_to_json_schema(args[0])}
        return {"type": "array"}

    if annotation is dict or origin is dict:
        return {"type": "object"}

    return {"type": "string"}


def _build_input_schema(fn: callable) -> dict:
    """从函数签名自动生成 Anthropic API 的 input_schema，跳过 db_path。"""
    sig = inspect.signature(fn)
    properties: dict[str, dict] = {}
    required: list[str] = []

    for name, param in sig.parameters.items():
        if name == "db_path":
            continue
        schema = _python_type_to_json_schema(param.annotation)
        schema["description"] = name.replace("_", " ").title()
        if param.default is not inspect.Parameter.empty:
            if param.default is not None:
                schema["default"] = param.default
        else:
            required.append(name)
        properties[name] = schema

    result: dict = {"type": "object", "properties": properties}
    if required:
        result["required"] = required
    return result


# ─────────────────────────────────────────────
# Tool 注册表
# ─────────────────────────────────────────────

def _build_tool_registry() -> dict[str, dict]:
    """构建全局 Tool 注册表。"""
    from rookiecard.mcp.tools import portfolio, prices  # noqa: F401
    from rookiecard.mcp.app import mcp

    registry: dict[str, dict] = {}
    for tool_name, tool_obj in mcp._tool_manager._tools.items():
        fn = tool_obj.fn
        registry[tool_name] = {
            "function": fn,
            "description": tool_obj.description,
            "input_schema": _build_input_schema(fn),
        }
    return registry


_registry_cache: dict[str, dict] | None = None


def get_tool_registry() -> dict[str, dict]:
    """获取 Tool 注册表（单例，首次调用时构建）。"""
    global _registry_cache
    if _registry_cache is None:
        _registry_cache = _build_tool_registry()
    return _registry_cache


def get_tools_for_agent(agent_name: str) -> list[str]:
    """获取指定 Agent 允许使用的 tool 名称列表。"""
    return AGENT_TOOLS.get(agent_name, [])
