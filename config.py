import os
from dotenv import load_dotenv

load_dotenv()

# ── Site de simulation ────────────────────────────────────────────────────────
FINANCEACADEMY_URL = os.getenv("FINANCEACADEMY_URL", "http://192.168.0.241:5000")
BOT_EMAIL          = os.getenv("BOT_EMAIL", "bot@trading.com")
BOT_PASSWORD       = os.getenv("BOT_PASSWORD", "BotTrading2024!")
BOT_USERNAME       = os.getenv("BOT_USERNAME", "TradingBot")

# ── LM Studio ────────────────────────────────────────────────────────────────
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://192.168.0.100:1234/v1")
LLM_MODEL    = os.getenv("LLM_MODEL", "qwen2.5-14b-instruct")
LLM_API_KEY  = os.getenv("LLM_API_KEY", "lm-studio")
LLM_TIMEOUT  = int(os.getenv("LLM_TIMEOUT", "300"))

# ── Paramètres de trading ─────────────────────────────────────────────────────
MAX_POSITION_PCT     = float(os.getenv("MAX_POSITION_PCT", "15.0"))
MAX_POSITIONS        = int(os.getenv("MAX_POSITIONS", "12"))
MIN_CASH_PCT_BULL    = float(os.getenv("MIN_CASH_PCT_BULL", "10.0"))
MIN_CASH_PCT_BEAR    = float(os.getenv("MIN_CASH_PCT_BEAR", "40.0"))
MIN_CASH_PCT_NEUTRAL = float(os.getenv("MIN_CASH_PCT_NEUTRAL", "20.0"))
MAX_DAILY_LOSS_PCT   = float(os.getenv("MAX_DAILY_LOSS_PCT", "4.0"))
DEFAULT_STOP_LOSS_PCT = float(os.getenv("DEFAULT_STOP_LOSS_PCT", "6.0"))
TOP_STOCKS_COUNT     = int(os.getenv("TOP_STOCKS_COUNT", "20"))

# ── Finnhub (news financières spécialisées) ──────────────────────────────────
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE  = os.getenv("LOG_FILE", "/app/logs/bot.log")

# ── Heures de marché (EST) ────────────────────────────────────────────────────
MARKET_OPEN_HOUR   = 9
MARKET_OPEN_MIN    = 30
MARKET_CLOSE_HOUR  = 16
MARKET_CLOSE_MIN   = 0
MARKET_TIMEZONE    = "America/New_York"

# ── Cycle d'analyse (minutes entre chaque décision) ──────────────────────────
CYCLE_INTERVAL_MIN = 60
