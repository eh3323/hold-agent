"""MCP Server entry — register all Hold Agent tools and launch stdio transport.

Exposed tools (consumed by Hermes / Claude Code / other MCP clients):
    portfolio.py →
        get_portfolio                — list holdings + P&L
        add_to_portfolio             — record a new buy
        refresh_portfolio_prices     — scrape fresh eBay sold prices
        export_portfolio_report      — render PNG report
    prices.py →
        get_card_prices              — price + technical indicators
        get_pricing_analysis         — P25/P50/P75 pricing guidance
        compare_cards                — side-by-side card comparison

Launch:
    python -m rookiecard.mcp.server          # stdio transport
"""

# Get the FastMCP instance
from rookiecard.mcp.app import mcp  # noqa: F401

# Import tool modules → triggers @mcp.tool() registration
import rookiecard.mcp.tools.portfolio  # noqa: F401
import rookiecard.mcp.tools.prices     # noqa: F401


def main():
    """Launch the MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
