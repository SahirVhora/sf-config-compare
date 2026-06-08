import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "db" / "vault.db"
REPORTS_DIR = BASE_DIR / "reports"
LOGS_DIR = BASE_DIR / "logs"

# FLASK_SECRET_KEY (.env.example) or SECRET_KEY both work
SECRET_KEY = os.getenv("SECRET_KEY") or os.getenv("FLASK_SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY or FLASK_SECRET_KEY must be set in .env. "
        'Generate one with: python3 -c "import secrets; print(secrets.token_hex(32))"'
    )
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Optional shared secret to guard report endpoints
REPORT_ACCESS_TOKEN = os.getenv("REPORT_ACCESS_TOKEN") or None
