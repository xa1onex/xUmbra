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
        
        # Добавляем server_id в таблицу users
        if 'server_id' not in columns:
            try:
                cursor.execute('ALTER TABLE users ADD COLUMN server_id INTEGER')
                logging.info("Added server_id column to users table")
            except Exception as e:
                logging.warning(f"Could not add server_id column: {e}")

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
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS feedback_ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                payment_id INTEGER,
                rating INTEGER,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(user_id),
                FOREIGN KEY(payment_id) REFERENCES payments(id)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                ip TEXT NOT NULL,
                port INTEGER DEFAULT 54321,
                protocol TEXT DEFAULT 'https',
                username TEXT,
                password TEXT,
                inbound_id INTEGER NOT NULL,
                base_url TEXT NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT
            )
        ''')
        
        # Проверяем и добавляем колонки protocol и port, если их нет
        cursor.execute("PRAGMA table_info(servers)")
        server_columns = [column[1] for column in cursor.fetchall()]
        
        if 'protocol' not in server_columns:
            try:
                cursor.execute('ALTER TABLE servers ADD COLUMN protocol TEXT DEFAULT "https"')
                logging.info("Added protocol column to servers table")
            except Exception as e:
                logging.warning(f"Could not add protocol column: {e}")
        
        if 'port' not in server_columns:
            try:
                cursor.execute('ALTER TABLE servers ADD COLUMN port INTEGER DEFAULT 54321')
                logging.info("Added port column to servers table")
            except Exception as e:
                logging.warning(f"Could not add port column: {e}")

        # Таблица для объявлений
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS announcements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Проверяем и добавляем колонку updated_at, если её нет
        cursor.execute("PRAGMA table_info(announcements)")
        announcement_columns = [column[1] for column in cursor.fetchall()]
        
        if 'updated_at' not in announcement_columns:
            try:
                cursor.execute('ALTER TABLE announcements ADD COLUMN updated_at TEXT DEFAULT CURRENT_TIMESTAMP')
                logging.info("Added updated_at column to announcements table")
                # Обновляем список колонок после добавления
                announcement_columns.append('updated_at')
            except Exception as e:
                logging.warning(f"Could not add updated_at column: {e}")
        
        # Если таблица пустая, добавляем дефолтное объявление
        cursor.execute('SELECT COUNT(*) FROM announcements')
        if cursor.fetchone()[0] == 0:
            default_text = "!!!ВНИМАНИЕ!!! Это бета-тест, VPN работает нестабильно, платежи также находятся в тестировании - они не реальны!!!\n"
            # Проверяем наличие колонки updated_at перед вставкой
            if 'updated_at' in announcement_columns:
                cursor.execute('''
                    INSERT INTO announcements (text, updated_at) VALUES (?, CURRENT_TIMESTAMP)
                ''', (default_text,))
            else:
                cursor.execute('''
                    INSERT INTO announcements (text) VALUES (?)
                ''', (default_text,))

        # Таблица для VPN ключей
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS vpn_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                server_id INTEGER NOT NULL,
                vless_client_id TEXT NOT NULL,
                vless_link TEXT NOT NULL,
                key_name TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                expires_at TEXT,
                traffic_gb INTEGER,
                is_active BOOLEAN DEFAULT TRUE,
                FOREIGN KEY(user_id) REFERENCES users(user_id),
                FOREIGN KEY(server_id) REFERENCES servers(id)
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
