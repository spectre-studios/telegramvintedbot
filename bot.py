import asyncio
import logging
import os
import threading
from functools import wraps
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from flask import Flask
from vinted_scraper import VintedWrapper
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import TimedOut, NetworkError 

load_dotenv()

CHECK_INTERVAL_SECONDS = 180
DATABASE_URL = os.getenv("DATABASE_URL")

def parse_allowed_users(env_var_name: str) -> list[int]:
    raw_val = os.getenv(env_var_name, "")
    user_ids = []
    for item in raw_val.split(","):
        cleaned = item.strip()
        try:
            user_ids.append(int(cleaned))
        except ValueError:
            logging.error(f"Failed to parse ID: '{cleaned}' from environment variable.")
    logging.info(f"Loaded ALLOWED_USERS: {user_ids}")
    return list(set(user_ids))

ALLOWED_USERS = parse_allowed_users("TELEGRAM_CHAT_ID")

def restricted(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id if update.effective_user else None
        chat_id = update.effective_chat.id if update.effective_chat else None
        
        if user_id not in ALLOWED_USERS and chat_id not in ALLOWED_USERS:
            logging.warning(f"Unauthorized access denied. Incoming User ID: {user_id}, Chat ID: {chat_id}. Allowed: {ALLOWED_USERS}")
            if update.message:
                await update.message.reply_text("⛔ Sorry! This is a private bot.")
            elif update.callback_query:
                await update.callback_query.answer("Unauthorized user.", show_alert=True)
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

flask_app = Flask(__name__)

@flask_app.route('/')
def health_check():
    return 'Vinted Bot is live!', 200

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    flask_app.run(host='0.0.0.0', port=port)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, (MemoryError, TimedOut, NetworkError)):
        logging.warning(f"Temporary network issue: {context.error}. Retrying automatically...")
    else:
        logging.error(f"Update {update} caused error {context.error}", exc_info=context.error)

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    if not DATABASE_URL:
        logging.error("DATABASE_URL environment variable missing!")
        return
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS queries (
                id SERIAL PRIMARY KEY,
                query TEXT UNIQUE,
                max_price REAL
            )
            """)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS seen_items (
                item_id TEXT PRIMARY KEY
            )
            """)
            conn.commit()

@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "Welcome to your Vinted Deals Bot!\n\n"
        "To add a search query, type:\n"
        "<code>/add &lt;item_name&gt;, &lt;max_price&gt;</code>\n"
        "Example: <code>/add New Balance 530, 10</code>\n\n"
        "To edit an existing query, type:\n"
        "<code>/edit &lt;old_name&gt; &gt; &lt;new_name&gt;, &lt;max_price&gt;</code>\n\n"
        "To view or delete your current searches, type:\n"
        "<code>/list</code>"
    )
    await update.message.reply_text(welcome_text, parse_mode="HTML")

vinted_cookie = os.getenv("VINTED_COOKIE", "")
scraper_wrapper = VintedWrapper("https://www.vinted.fr")
if vinted_cookie:
    scraper_wrapper._client.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Cookie": f"access_token_web={vinted_cookie}"
    })

async def fetch_vinted_items_async(query: str, max_price: float):
    try:
        params = {
            "search_text": query,
            "price_to": max_price,
            "order": "newest_first"
        }
        res = await asyncio.to_thread(scraper_wrapper.search, params)
        if isinstance(res, dict):
            return res.get("items", [])
        elif isinstance(res, list):
            return res
        return []
        
    except Exception as e:
        logging.error(f"Error fetching Vinted listings: {e}")
        return []

@restricted
async def add_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        raw_args = " ".join(context.args).split(",")
        if len(raw_args) < 2:
            raise ValueError("Missing price")

        query_text = raw_args[0].strip().lower()
        max_price = float(raw_args[1].strip())

        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO queries (query, max_price) VALUES (%s, %s) ON CONFLICT (query) DO UPDATE SET max_price = EXCLUDED.max_price",
                    (query_text, max_price),
                )
                conn.commit()

        existing_items = await fetch_vinted_items_async(query_text, max_price)
        
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                for item in existing_items:
                    if not isinstance(item, dict):
                        continue
                    item_id = str(item.get("id"))
                    cursor.execute("INSERT INTO seen_items (item_id) VALUES (%s) ON CONFLICT DO NOTHING", (item_id,))
                conn.commit()

        await update.message.reply_text(
            f"Added search for <b>{query_text.title()}</b> under <b>€{max_price:.2f}</b>",
            parse_mode="HTML",
        )
    except Exception:
        await update.message.reply_text(
            "Format Error! Please use: <code>/add &lt;Item Name&gt;, &lt;Max Price&gt;</code>\nExample: <code>/add Cowboy Hats, 5</code>",
            parse_mode="HTML",
        )

@restricted
async def list_queries(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, query, max_price FROM queries")
            rows = cursor.fetchall()    

    if not rows:
        await update.message.reply_text("You have no saved searches. Add one using <code>/add &lt;Item Name&gt;, &lt;Max Price&gt;</code>", parse_mode="HTML")
        return

    for item_id, query, max_price in rows:
        keyboard = [[InlineKeyboardButton("Delete", callback_data=f"delete_{item_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"<b>Query:</b> {query.title()}\n<b>Max Price:</b> €{max_price:.2f}",
            reply_markup=reply_markup,
            parse_mode="HTML"
        )

@restricted
async def edit_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    if "," not in text:
        await update.message.reply_text("Format: <code>/edit Old Name &gt; New Name, MaxPrice</code> or <code>/edit Name, NewMaxPrice</code>", parse_mode="HTML")
        return

    try:
        if ">" in text:
            old_part, new_part = text.split(">", 1)
            old_query = old_part.strip().lower()
            new_query, max_price = [x.strip().lower() for x in new_part.split(",", 1)]
            max_price = float(max_price)
        else:
            raw_query, max_price = [x.strip().lower() for x in text.split(",", 1)]
            old_query = raw_query
            new_query = raw_query
            max_price = float(max_price)

        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT id FROM queries WHERE query = %s", (old_query,))
                if not cursor.fetchone():
                    await update.message.reply_text(f"Could not find an active search matching <b>{old_query.title()}</b>.", parse_mode="HTML")
                    return

                cursor.execute("UPDATE queries SET query = %s, max_price = %s WHERE query = %s", (new_query, max_price, old_query))
                conn.commit()

        await update.message.reply_text(f"Updated search: <b>{new_query.title()}</b> with max price <b>€{max_price:.2f}</b>.", parse_mode="HTML")

    except Exception as e:
        await update.message.reply_text(f"Error updating query: {e}")

@restricted
async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data.startswith("delete_"):
        row_id = query.data.split("_")[1]
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM queries WHERE id = %s", (row_id,))
                conn.commit()

        await query.edit_message_text("Search deleted successfully.")

async def monitor_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT query, max_price FROM queries")
                queries = cursor.fetchall()

        for query, max_price in queries:
            items = await fetch_vinted_items_async(query, max_price)
            if not items:
                continue

            for item in items:
                if not isinstance(item, dict):
                    continue

                item_id = str(item.get("id"))
                
                with get_db_connection() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute("SELECT 1 FROM seen_items WHERE item_id = %s", (item_id,))
                        if cursor.fetchone():
                            continue

                title = item.get("title") or ""
                description = item.get("description") or ""
                full_text = f"{title} {description}".lower()
                
                search_keywords = query.lower().split()

                if not all(kw in full_text for kw in search_keywords):
                    logging.info(f"Skipping unrelated item '{title}' for query '{query}'")
                    with get_db_connection() as conn:
                        with conn.cursor() as cursor:
                            cursor.execute("INSERT INTO seen_items (item_id) VALUES (%s) ON CONFLICT DO NOTHING", (item_id,))
                            conn.commit()
                    continue

                raw_price = item.get("price")
                if isinstance(raw_price, dict):
                    price = float(raw_price.get("amount", 0.0))
                elif raw_price is not None:
                    try:
                        price = float(raw_price)
                    except ValueError:
                        price = 0.0
                else:
                    price = 0.0

                item_url = item.get("url") or ""
                if item_url.startswith("/"):
                    item_url = f"https://www.vinted.fr{item_url}"
                elif not item_url.startswith("http"):
                    item_url = f"https://www.vinted.fr/{item_url.lstrip('/')}"
                
                photos = item.get("photos", [])
                photo_url = photos[0].get("url") if (photos and isinstance(photos, list) and isinstance(photos[0], dict)) else None

                with get_db_connection() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute("INSERT INTO seen_items (item_id) VALUES (%s) ON CONFLICT DO NOTHING", (item_id,))
                        conn.commit()

                safe_title = str(title).replace("<", "&lt;").replace(">", "&gt;")
                caption = f"🚨 <b>NEW ITEM FOUND!</b>\n\n<b>Title:</b> {safe_title}\n<b>Price:</b> €{price:.2f}"
                keyboard = [[InlineKeyboardButton("View Item", url=item_url)]]
                reply_markup = InlineKeyboardMarkup(keyboard)

                for user_id in ALLOWED_USERS:
                    try:
                        if photo_url:
                            await context.bot.send_photo(
                                chat_id=user_id, 
                                photo=photo_url, 
                                caption=caption, 
                                reply_markup=reply_markup, 
                                parse_mode="HTML"
                            )
                        else:
                            await context.bot.send_message(
                                chat_id=user_id, 
                                text=caption, 
                                reply_markup=reply_markup, 
                                parse_mode="HTML"
                            )
                    except Exception as e:
                        logging.error(f"Failed to send alert to user {user_id}: {e}")

    except Exception as e:
        logging.error(f"Unhandled exception in monitor_job: {e}")

def main():
    init_db()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable is missing!")

    if not ALLOWED_USERS:
        logging.warning("No ALLOWED_USERS configured in TELEGRAM_CHAT_ID!")

    app = (
        Application.builder()
        .token(token)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_query))
    app.add_handler(CommandHandler("list", list_queries))
    app.add_handler(CommandHandler("edit", edit_query))
    app.add_handler(CallbackQueryHandler(button_click))
    app.add_error_handler(error_handler)
    
    app.job_queue.run_repeating(monitor_job, interval=CHECK_INTERVAL_SECONDS, first=10)
    
    threading.Thread(target=run_flask, daemon=True).start()
    
    print("Bot is running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()