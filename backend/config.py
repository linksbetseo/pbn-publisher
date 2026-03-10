import os
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
CSV_PATH = os.getenv("CSV_PATH", r"C:\SEO_DODAWARKA\ALL_DOMAINS_DZIALA.csv")
DB_PATH = os.getenv("DB_PATH", "pbn_publisher.db").strip()
DATAFORSEO_LOGIN = os.getenv("DATAFORSEO_LOGIN", "")
DATAFORSEO_PASSWORD = os.getenv("DATAFORSEO_PASSWORD", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
