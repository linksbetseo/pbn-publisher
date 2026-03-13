import os
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
CSV_PATH = os.getenv("CSV_PATH", "domains.csv")
DB_PATH = os.getenv("DB_PATH", "pbn_publisher.db").strip()
DATAFORSEO_LOGIN = os.getenv("DATAFORSEO_LOGIN", "")
DATAFORSEO_PASSWORD = os.getenv("DATAFORSEO_PASSWORD", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
PBN_ENCRYPTION_KEY = os.getenv("PBN_ENCRYPTION_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
