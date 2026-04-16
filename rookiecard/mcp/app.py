"""MCP 应用实例 — 独立模块，避免 server.py 与 tools/ 之间的循环导入。

所有 tools/*.py 通过 `from rookiecard.mcp.app import mcp` 获取实例，
用 @mcp.tool() 装饰器注册自己。
server.py 导入 tools 触发注册，再调用 mcp.run() 启动服务。
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    name="rookiecard",
    instructions=(
        "NBA Rookie Card Trading Agent — 提供新秀球员数据、卡牌价格分析、"
        "投资组合管理、买卖信号等工具，帮助用户做出球星卡投资决策。"
    ),
)
