# config.py
from pathlib import Path

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "bot.db"
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)