import os
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
CSV_PATH = os.getenv("CSV_PATH", r"C:\SEO_DODAWARKA\ALL_DOMAINS_DZIALA.csv")
DB_PATH = os.getenv("DB_PATH", "pbn_publisher.db")
# redeploy trigger
