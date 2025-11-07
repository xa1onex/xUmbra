from __future__ import annotations

import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Optional
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


def parse_datetime(dt_str: Optional[str]) -> Optional[datetime]:
    """Parse datetime from SQLite string format."""
    if not dt_str:
        return None
    try:
        # SQLite stores datetime as 'YYYY-MM-DD HH:MM:SS' or ISO format
        if 'T' in dt_str:
            return datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
        else:
            return datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S')
    except (ValueError, AttributeError):
        try:
            return datetime.fromisoformat(dt_str)
        except (ValueError, AttributeError):
            logger.warning(f"Failed to parse datetime: {dt_str}")
            return None


class SubscriptionStatus(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class PaymentStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class User:
    user_id: int
    username: Optional[str]
    full_name: Optional[str]
    balance: float = 0.0
    referrer_id: Optional[int] = None
    invited_count: int = 0
    created_at: Optional[datetime] = None


@dataclass
class Subscription:
    id: int
    user_id: int
    traffic_gb: int
    days: int
    status: SubscriptionStatus
    start_date: datetime
    end_date: datetime
    vless_client_id: Optional[str] = None
    created_at: Optional[datetime] = None


@dataclass
class Payment:
    id: int
    user_id: int
    amount: float
    status: PaymentStatus
    subscription_id: Optional[int] = None
    payment_method: Optional[str] = None
    created_at: Optional[datetime] = None


@dataclass
class Invite:
    id: int
    inviter_id: int
    invite_code: str
    used_by: Optional[int] = None
    used_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class Database:
    def __init__(self, db_path: str = "vpn_bot.db"):
        self.db_path = db_path
        self.init_db()

    @contextmanager
    def get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Database error: {e}")
            raise
        finally:
            conn.close()

    def init_db(self):
        with self.get_connection() as conn:
            # Users table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    balance REAL DEFAULT 0.0,
                    referrer_id INTEGER,
                    invited_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (referrer_id) REFERENCES users(user_id)
                )
            """)

            # Subscriptions table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    traffic_gb INTEGER NOT NULL,
                    days INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    start_date TIMESTAMP NOT NULL,
                    end_date TIMESTAMP NOT NULL,
                    vless_client_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            """)

            # Payments table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    subscription_id INTEGER,
                    payment_method TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(user_id),
                    FOREIGN KEY (subscription_id) REFERENCES subscriptions(id)
                )
            """)

            # Invites table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS invites (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    inviter_id INTEGER NOT NULL,
                    invite_code TEXT UNIQUE NOT NULL,
                    used_by INTEGER,
                    used_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (inviter_id) REFERENCES users(user_id),
                    FOREIGN KEY (used_by) REFERENCES users(user_id)
                )
            """)

            # Create indexes
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_referrer ON users(referrer_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_status ON subscriptions(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_invites_code ON invites(invite_code)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_invites_inviter ON invites(inviter_id)")

    def get_or_create_user(self, user_id: int, username: Optional[str] = None, 
                          full_name: Optional[str] = None, referrer_id: Optional[int] = None) -> User:
        with self.get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            )
            row = cursor.fetchone()
            
            if row:
                user = User(
                    user_id=row["user_id"],
                    username=row["username"],
                    full_name=row["full_name"],
                    balance=row["balance"],
                    referrer_id=row["referrer_id"],
                    invited_count=row["invited_count"],
                    created_at=parse_datetime(row["created_at"]),
                )
                # Update username if changed
                if username or full_name:
                    conn.execute(
                        "UPDATE users SET username = ?, full_name = ? WHERE user_id = ?",
                        (username, full_name, user_id)
                    )
                return user
            
            # Create new user
            created_at = datetime.now()
            conn.execute(
                """INSERT INTO users (user_id, username, full_name, referrer_id, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, username, full_name, referrer_id, created_at.strftime('%Y-%m-%d %H:%M:%S'))
            )
            
            # If user has referrer, increment their invited_count
            if referrer_id:
                conn.execute(
                    "UPDATE users SET invited_count = invited_count + 1 WHERE user_id = ?",
                    (referrer_id,)
                )
            
            return User(
                user_id=user_id,
                username=username,
                full_name=full_name,
                balance=0.0,
                referrer_id=referrer_id,
                invited_count=0,
                created_at=created_at,
            )

    def get_user(self, user_id: int) -> Optional[User]:
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return User(
                user_id=row["user_id"],
                username=row["username"],
                full_name=row["full_name"],
                balance=row["balance"],
                referrer_id=row["referrer_id"],
                invited_count=row["invited_count"],
                created_at=parse_datetime(row["created_at"]),
            )

    def update_user_balance(self, user_id: int, amount: float):
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                (amount, user_id)
            )

    def create_subscription(self, user_id: int, traffic_gb: int, days: int, 
                           vless_client_id: Optional[str] = None) -> Subscription:
        with self.get_connection() as conn:
            start_date = datetime.now()
            end_date = start_date + timedelta(days=days)
            
            cursor = conn.execute(
                """INSERT INTO subscriptions 
                   (user_id, traffic_gb, days, status, start_date, end_date, vless_client_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id, traffic_gb, days, SubscriptionStatus.ACTIVE.value,
                    start_date.strftime('%Y-%m-%d %H:%M:%S'), end_date.strftime('%Y-%m-%d %H:%M:%S'), 
                    vless_client_id, start_date.strftime('%Y-%m-%d %H:%M:%S')
                )
            )
            subscription_id = cursor.lastrowid
            
            return Subscription(
                id=subscription_id,
                user_id=user_id,
                traffic_gb=traffic_gb,
                days=days,
                status=SubscriptionStatus.ACTIVE,
                start_date=start_date,
                end_date=end_date,
                vless_client_id=vless_client_id,
                created_at=start_date,
            )

    def get_user_active_subscription(self, user_id: int) -> Optional[Subscription]:
        with self.get_connection() as conn:
            cursor = conn.execute(
                """SELECT * FROM subscriptions 
                   WHERE user_id = ? AND status = 'active' 
                   ORDER BY created_at DESC LIMIT 1""",
                (user_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            
            # Check if subscription expired
            end_date = parse_datetime(row["end_date"])
            if end_date < datetime.now():
                # Mark as expired
                conn.execute(
                    "UPDATE subscriptions SET status = ? WHERE id = ?",
                    (SubscriptionStatus.EXPIRED.value, row["id"])
                )
                return None
            
            return Subscription(
                id=row["id"],
                user_id=row["user_id"],
                traffic_gb=row["traffic_gb"],
                days=row["days"],
                status=SubscriptionStatus(row["status"]),
                start_date=parse_datetime(row["start_date"]),
                end_date=end_date,
                vless_client_id=row["vless_client_id"],
                created_at=parse_datetime(row["created_at"]),
            )

    def get_user_subscriptions(self, user_id: int) -> list[Subscription]:
        with self.get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,)
            )
            subscriptions = []
            for row in cursor.fetchall():
                subscriptions.append(Subscription(
                    id=row["id"],
                    user_id=row["user_id"],
                    traffic_gb=row["traffic_gb"],
                    days=row["days"],
                    status=SubscriptionStatus(row["status"]),
                    start_date=parse_datetime(row["start_date"]),
                    end_date=parse_datetime(row["end_date"]),
                    vless_client_id=row["vless_client_id"],
                    created_at=parse_datetime(row["created_at"]),
                ))
            return subscriptions

    def create_payment(self, user_id: int, amount: float, 
                      subscription_id: Optional[int] = None,
                      payment_method: Optional[str] = None) -> Payment:
        with self.get_connection() as conn:
            cursor = conn.execute(
                """INSERT INTO payments 
                   (user_id, amount, status, subscription_id, payment_method, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    user_id, amount, PaymentStatus.PENDING.value,
                    subscription_id, payment_method, datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                )
            )
            payment_id = cursor.lastrowid
            return Payment(
                id=payment_id,
                user_id=user_id,
                amount=amount,
                status=PaymentStatus.PENDING,
                subscription_id=subscription_id,
                payment_method=payment_method,
                created_at=datetime.now(),
            )

    def complete_payment(self, payment_id: int):
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE payments SET status = ? WHERE id = ?",
                (PaymentStatus.COMPLETED.value, payment_id)
            )

    def create_invite_code(self, inviter_id: int, invite_code: str) -> Invite:
        with self.get_connection() as conn:
            cursor = conn.execute(
                """INSERT INTO invites (inviter_id, invite_code, created_at)
                   VALUES (?, ?, ?)""",
                (inviter_id, invite_code, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            )
            invite_id = cursor.lastrowid
            return Invite(
                id=invite_id,
                inviter_id=inviter_id,
                invite_code=invite_code,
                used_by=None,
                used_at=None,
                created_at=datetime.now(),
            )

    def use_invite_code(self, invite_code: str, user_id: int) -> bool:
        with self.get_connection() as conn:
            # Check if code exists and not used
            cursor = conn.execute(
                "SELECT * FROM invites WHERE invite_code = ? AND used_by IS NULL",
                (invite_code,)
            )
            row = cursor.fetchone()
            if not row:
                return False
            
            # Mark as used
            conn.execute(
                "UPDATE invites SET used_by = ?, used_at = ? WHERE id = ?",
                (user_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), row["id"])
            )
            
            # Update user referrer
            conn.execute(
                "UPDATE users SET referrer_id = ? WHERE user_id = ? AND referrer_id IS NULL",
                (row["inviter_id"], user_id)
            )
            
            # Increment inviter's count
            conn.execute(
                "UPDATE users SET invited_count = invited_count + 1 WHERE user_id = ?",
                (row["inviter_id"],)
            )
            
            return True

    def get_user_invite_code(self, user_id: int) -> Optional[str]:
        with self.get_connection() as conn:
            cursor = conn.execute(
                "SELECT invite_code FROM invites WHERE inviter_id = ? LIMIT 1",
                (user_id,)
            )
            row = cursor.fetchone()
            if row:
                return row["invite_code"]
            # Create new invite code
            invite_code = f"REF{user_id}{int(datetime.now().timestamp())}"
            self.create_invite_code(user_id, invite_code)
            return invite_code

    def get_all_users(self) -> list[User]:
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT * FROM users ORDER BY created_at DESC")
            users = []
            for row in cursor.fetchall():
                users.append(User(
                    user_id=row["user_id"],
                    username=row["username"],
                    full_name=row["full_name"],
                    balance=row["balance"],
                    referrer_id=row["referrer_id"],
                    invited_count=row["invited_count"],
                    created_at=parse_datetime(row["created_at"]),
                ))
            return users

