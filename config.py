"""Налаштування. Значення ключів беруться ТІЛЬКИ з Railway → Variables, у коді їх немає."""
import os
from zoneinfo import ZoneInfo

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
# Telegram ID власниці (можна кілька через кому). Дізнатися свій ID: напишіть агенту /myid.
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_ID", "").replace(" ", "").split(",") if x.strip().isdigit()]
DB_PATH = os.environ.get("DB_PATH", "bot.db")  # на Railway: /data/bot.db (диск Volume у /data)
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Kyiv"))
# Мінізастосунок «Розрахунки»: адресу Railway дає сам після Settings → Networking → Generate Domain
PORT = int(os.environ.get("PORT", "8080") or 8080)
_DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").strip() or (f"https://{_DOMAIN}/calc" if _DOMAIN else "")


def is_admin(uid):
    return uid in ADMIN_IDS
