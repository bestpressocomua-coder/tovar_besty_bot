"""Налаштування бота Bestpresso · Заявки.
Заповніть значення нижче перед запуском (див. README.md)."""
import os

# --- Telegram ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT-YOUR-BOT-TOKEN-HERE")

# Куди надсилати готові файли (chat_id користувача/групи або @username каналу).
# Можна задати різних отримувачів для замовлень та інвентаризації.
RECIPIENT_ORDERS = os.getenv("RECIPIENT_ORDERS", "1982086643")
RECIPIENT_INVENTORY = os.getenv("RECIPIENT_INVENTORY", "1982086643")

# --- Джерела даних (таблиці, які веде відділ обліку вручну) ---
CATALOG_CSV = os.getenv("CATALOG_CSV", "data/catalog.csv")
LOCATIONS_CSV = os.getenv("LOCATIONS_CSV", "data/locations.csv")
EMPLOYEES_CSV = os.getenv("EMPLOYEES_CSV", "data/employees.csv")

# Куди складати згенеровані xlsx (тимчасово, перед відправкою)
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "generated")

# Скільки кнопок на сторінці (пагінація)
ITEMS_PER_PAGE = 8
LOCS_PER_PAGE = 6

# --- Email (Gmail SMTP) ---
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "bestpresso.com.ua@gmail.com")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")   # App Password (16 симв.) — задати на Render
EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USER)
EMAIL_TO = os.getenv("EMAIL_TO", "Kostrubitskaya@bestpresso.com.ua, Stepanyuk@bestpresso.com.ua")
SEND_EMAIL = os.getenv("SEND_EMAIL", "1") == "1"
TELEGRAM_FALLBACK = os.getenv("TELEGRAM_FALLBACK", "1") == "1"
