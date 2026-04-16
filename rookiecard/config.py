import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Project root directory
PROJECT_ROOT = Path(__file__).parent.parent


class Config:
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
    PSA_API_KEY = os.getenv("PSA_API_KEY")
    DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

    DB_PATH = os.getenv("DB_PATH", str(PROJECT_ROOT / "data" / "rookiecard.db"))
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    CURRENT_SEASON = os.getenv("CURRENT_SEASON", "2025-26")

    NBA_API_DELAY = float(os.getenv("NBA_API_DELAY", "1.0"))
    EBAY_DELAY = float(os.getenv("EBAY_DELAY", "3.0"))
    EBAY_TIMEOUT = float(os.getenv("EBAY_TIMEOUT", "20.0"))
    EBAY_MAX_PAGES = int(os.getenv("EBAY_MAX_PAGES", "3"))
    EBAY_MAX_RETRIES = int(os.getenv("EBAY_MAX_RETRIES", "4"))
    EBAY_HOLDINGS_DAYS = int(os.getenv("EBAY_HOLDINGS_DAYS", "14"))
    EBAY_HOLDINGS_MAX_PAGES = int(os.getenv("EBAY_HOLDINGS_MAX_PAGES", "1"))
    EBAY_USE_PLAYWRIGHT_FALLBACK = os.getenv("EBAY_USE_PLAYWRIGHT_FALLBACK", "true").lower() == "true"
    EBAY_PROXY_URL = os.getenv("EBAY_PROXY_URL")
    EBAY_PLAYWRIGHT_PROXY_URL = os.getenv("EBAY_PLAYWRIGHT_PROXY_URL", EBAY_PROXY_URL)

    # Model configuration
    ROUTER_MODEL = "claude-haiku-4-5-20251001"
    SPECIALIST_MODEL = "claude-sonnet-4-20250514"

    # Factor weights
    FACTOR_WEIGHTS = {
        "performance": 0.35,
        "price": 0.30,
        "sentiment": 0.20,
        "scarcity": 0.15,
    }

    # Thresholds
    BREAKOUT_THRESHOLD_SIGMA = 2.0
    RSI_OVERSOLD = 30
    RSI_OVERBOUGHT = 70
    PRICE_DROP_ALERT_PCT = 0.15
    MIN_SIGNAL_CONFIDENCE = 0.60
