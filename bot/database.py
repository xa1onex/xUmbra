import sqlite3
import secrets
import pytz
import logging
from datetime import datetime, timedelta
from contextlib import contextmanager

DATABASE_FILE = "vpn_bot.db"
REFERRAL_BONUS_DAYS = 5

def init_db(db_path: str = DATABASE_FILE):
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                registration_date TEXT DEFAULT CURRENT_TIMESTAMP,
                last_activity TEXT,
                pay_subscribed BOOLEAN DEFAULT FALSE,
                subscription_end TEXT,
                referral_code TEXT UNIQUE,
                referral_count INTEGER DEFAULT 0,
                invited_by INTEGER,
                blacklisted BOOLEAN DEFAULT FALSE,
                subscribed BOOLEAN DEFAULT FALSE,
                renewal_used BOOLEAN DEFAULT FALSE,
                ban_reason TEXT DEFAULT '',
                last_announce TEXT
            )
        ''')
        
        # Проверяем и добавляем колонки для VPN, если их нет
        cursor.execute("PRAGMA table_info(users)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'vless_client_id' not in columns:
            try:
                cursor.execute('ALTER TABLE users ADD COLUMN vless_client_id TEXT')
                logging.info("Added vless_client_id column to users table")
            except Exception as e:
                logging.warning(f"Could not add vless_client_id column: {e}")
        
        if 'vless_link' not in columns:
            try:
                cursor.execute('ALTER TABLE users ADD COLUMN vless_link TEXT')
                logging.info("Added vless_link column to users table")
            except Exception as e:
                logging.warning(f"Could not add vless_link column: {e}")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                subscription_start TEXT,
                subscription_end TEXT,
                traffic_gb INTEGER,
                days INTEGER,
                vless_client_id TEXT,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                amount INTEGER,
                currency TEXT,
                plan_id TEXT,
                plan_type TEXT,
                status TEXT,
                telegram_payment_charge_id TEXT,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
        ''')

        conn.commit()

def get_connection(db_path: str = DATABASE_FILE):
    return sqlite3.connect(db_path)

async def check_expired_subscriptions(db_path: str = DATABASE_FILE):
    current_time = datetime.now(pytz.timezone('Europe/Moscow')).strftime('%Y-%m-%d %H:%M:%S')

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute('''
                UPDATE users 
                SET 
                    pay_subscribed = 0,
                    subscription_end = NULL,
                    renewal_used = 0 
                WHERE 
                    pay_subscribed = 1 
                    AND datetime(subscription_end) < datetime(?)
            ''', (current_time,))

            conn.commit()

            if cursor.rowcount > 0:
                logging.info(f"Отключено {cursor.rowcount} просроченных подписок")

        except Exception as e:
            logging.error(f"Ошибка при проверке подписок: {str(e)}")
            conn.rollback()
