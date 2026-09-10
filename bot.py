import logging
import os
import sqlite3
import threading
import urllib.parse
import requests
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import TimedOut

CHECK_INTERVAL_SECONDS = 180

ALLOWED_USERS = [1656101417, 8381946664]

def restricted(func):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in ALLOWED_USERS:
            logging.warning(f"Unauthorized access denied for User ID: {user_id}")
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
    if isinstance(context.error, (MemoryError, TimedOut)):
        logging.warning(f"Temporary network issue: {context.error}. Retrying automatically...")
    else:
        logging.error(f"Update {update} caused error {context.error}", exc_info=context.error)

def init_db():
    conn = sqlite3.connect("vinted_monitor.db")
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS queries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    conn.close()

@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "Welcome Jess, to your Vinted Deals Bot!\n\n"
        "To add a search query, type:\n"
        "`/add <item_name>, <max_price>`\n"
        "Example: `/add New Balance 530, 10`\n\n"
        "To edit an existing query, type:\n"
        "`/edit <old_name> > <new_name>, <max_price>`\n\n"
        "To view or delete your current searches, type:\n"
        "`/list`"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

@restricted
async def add_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        raw_args = " ".join(context.args).split(",")
        if len(raw_args) < 2:
            raise ValueError("Missing price")

        query_text = raw_args[0].strip().lower()
        max_price = float(raw_args[1].strip())

        conn = sqlite3.connect("vinted_monitor.db")
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO queries (query, max_price) VALUES (?, ?)",
            (query_text, max_price),
        )
        existing_items = fetch_vinted_items(query_text, max_price)
        for item in existing_items:
            item_id = str(item.get("id"))
            cursor.execute("INSERT OR IGNORE INTO seen_items (item_id) VALUES (?)", (item_id,))
        conn.commit()
        conn.close()

        await update.message.reply_text(
            f"Added search for **{query_text.title()}** under **€{max_price:.2f}**",
            parse_mode="Markdown",
        )
    except Exception:
        await update.message.reply_text(
            "Format Error! Please use: `/add <Item Name>, <Max Price>`\nExample: `/add Cowboy Hats, 5`",
            parse_mode="Markdown",
        )

@restricted
async def list_queries(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect("vinted_monitor.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, query, max_price FROM queries")
    rows = cursor.fetchall()    
    conn.close()

    if not rows:
        await update.message.reply_text("You have no saved searches. Add one using `/add <Item Name>, <Max Price>`", parse_mode="Markdown")
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
        await update.message.reply_text(" Format: `/edit Old Name > New Name, MaxPrice` or `/edit Name, NewMaxPrice`", parse_mode="Markdown")
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

        conn = sqlite3.connect("vinted_monitor.db")
        cursor = conn.cursor()
        
        cursor.execute("SELECT id FROM queries WHERE query = ?", (old_query,))
        if not cursor.fetchone():
            await update.message.reply_text(f" Could not find an active search matching **{old_query.title()}**.", parse_mode="Markdown")
            conn.close()
            return

        cursor.execute("UPDATE queries SET query = ?, max_price = ? WHERE query = ?", (new_query, max_price, old_query))
        conn.commit()
        conn.close()

        await update.message.reply_text(f" Updated search: **{new_query.title()}** with max price **€{max_price:.2f}**.", parse_mode="Markdown")

    except Exception as e:
        await update.message.reply_text(f" Error updating query: {e}")

@restricted
async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data.startswith("delete_"):
        row_id = query.data.split("_")[1]
        conn = sqlite3.connect("vinted_monitor.db")
        cursor = conn.cursor()
        cursor.execute("DELETE FROM queries WHERE id = ?", (row_id,))
        conn.commit()
        conn.close()

        await query.edit_message_text("Search deleted successfully.")

def fetch_vinted_items(query, max_price):
    session = requests.Session()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        "Referer": "https://www.vinted.es/",
    }
    session.headers.update(headers)

    try:
        home_resp = session.get("https://www.vinted.es/", timeout=5)
        if home_resp.status_code != 200:
            logging.warning(f"Failed to fetch homepage session: {home_resp.status_code}")

        encoded_query = urllib.parse.quote(query)
        url = f"https://www.vinted.es/api/v2/catalog/items?search_text={encoded_query}&price_to={max_price}&order=newest_first"
        response = session.get(url, timeout=5)

        logging.info(f"Fetching Vinted items for query '{query}' with max price {max_price}. Status code: {response.status_code}")
        
        if response.status_code == 200:
            return response.json().get("items", [])
        else:
            logging.warning(f"Vinted returned status code {response.status_code}")

    except Exception as e:
        logging.error(f"Error fetching Vinted data: {e}")

    return []

async def monitor_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = sqlite3.connect("vinted_monitor.db")
        cursor = conn.cursor()
        cursor.execute("SELECT query, max_price FROM queries")
        queries = cursor.fetchall()

        for query, max_price in queries:
            try:
                items = fetch_vinted_items(query, max_price)
            except Exception as e:
                logging.error(f"Error executing fetch for query '{query}': {e}")
                continue

            for item in items:
                item_id = str(item.get("id"))
                cursor.execute("SELECT 1 FROM seen_items WHERE item_id = ?", (item_id,))
                if cursor.fetchone():
                    continue

                title = item.get("title", "") or ""
                description = item.get("description", "") or ""
                full_text = f"{title} {description}".lower()
                
                search_keywords = query.lower().split()

                if not all(kw in full_text for kw in search_keywords):
                    logging.info(f"Skipping unrelated item '{title}' for query '{query}'")
                    cursor.execute("INSERT INTO seen_items (item_id) VALUES (?)", (item_id,))
                    conn.commit()
                    continue

                raw_price = item.get("price")
                if isinstance(raw_price, dict):
                    price = float(raw_price.get("amount", 0.0))
                elif raw_price is not None:
                    price = float(raw_price)
                else:
                    price = 0.0
                item_url = item.get("url")
                
                photos = item.get("photos", [])
                photo_url = photos[0].get("url") if photos else None

                cursor.execute("INSERT INTO seen_items (item_id) VALUES (?)", (item_id,))
                conn.commit()

                caption = f"🚨 **NEW ITEM FOUND!**\n\n**Title:** {title}\n**Price:** €{price:.2f}"
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
                                parse_mode="Markdown"
                            )
                        else:
                            await context.bot.send_message(
                                chat_id=user_id, 
                                text=caption, 
                                reply_markup=reply_markup, 
                                parse_mode="Markdown"
                            )
                    except Exception as e:
                        logging.error(f"Failed to send alert to user {user_id}: {e}")

        conn.close()
    except Exception as e:
        logging.error(f"Unhandled exception in monitor_job: {e}")

def main():
    init_db()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable is missing!")

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
    app.run_polling()

if __name__ == "__main__":
    main()