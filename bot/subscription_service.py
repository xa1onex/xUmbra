from __future__ import annotations

import logging
from typing import Optional
from datetime import datetime

from .database import Database, Subscription, User
from .xui_client import XUIClient

logger = logging.getLogger(__name__)


class SubscriptionPlan:
    def __init__(self, name: str, traffic_gb: int, days: int, price: float, description: str = ""):
        self.name = name
        self.traffic_gb = traffic_gb
        self.days = days
        self.price = price
        self.description = description

    def __repr__(self):
        return f"<SubscriptionPlan {self.name}: {self.traffic_gb}GB/{self.days}days - {self.price}₽>"


class SubscriptionService:
    # Предустановленные планы подписки
    PLANS = [
        SubscriptionPlan("Базовый", 30, 30, 199.0, "30 ГБ трафика на 30 дней"),
        SubscriptionPlan("Стандарт", 100, 30, 399.0, "100 ГБ трафика на 30 дней"),
        SubscriptionPlan("Премиум", 300, 30, 799.0, "300 ГБ трафика на 30 дней"),
        SubscriptionPlan("Безлимит", 0, 30, 1299.0, "Безлимитный трафик на 30 дней"),
        SubscriptionPlan("Недельный", 50, 7, 149.0, "50 ГБ трафика на 7 дней"),
        SubscriptionPlan("Месячный", 200, 90, 999.0, "200 ГБ трафика на 90 дней"),
    ]

    def __init__(self, db: Database, xui_client: XUIClient):
        self.db = db
        self.xui_client = xui_client

    def get_plans(self) -> list[SubscriptionPlan]:
        return self.PLANS

    def get_plan_by_index(self, index: int) -> Optional[SubscriptionPlan]:
        if 0 <= index < len(self.PLANS):
            return self.PLANS[index]
        return None

    def create_subscription_for_user(
        self, 
        user_id: int, 
        plan: SubscriptionPlan,
        username: Optional[str] = None
    ) -> tuple[Subscription, str]:
        """
        Создает подписку для пользователя и возвращает подписку и VPN ссылку.
        """
        # Проверяем активную подписку
        active_sub = self.db.get_user_active_subscription(user_id)
        if active_sub:
            raise ValueError("У вас уже есть активная подписка")

        # Создаем клиента в x-ui
        try:
            display_name = username or f"user_{user_id}"
            result = self.xui_client.add_vless_client(
                telegram_user_id=user_id,
                display_name=display_name,
                traffic_gb=plan.traffic_gb if plan.traffic_gb > 0 else None,
                days_valid=plan.days,
            )
            vless_client_id = result.get("id")
            vless_link = result.get("link")
        except Exception as e:
            logger.error(f"Failed to create x-ui client: {e}")
            raise ValueError(f"Ошибка при создании VPN подключения: {e}")

        # Создаем подписку в БД
        subscription = self.db.create_subscription(
            user_id=user_id,
            traffic_gb=plan.traffic_gb,
            days=plan.days,
            vless_client_id=vless_client_id,
        )

        return subscription, vless_link

    def get_user_subscription_info(self, user_id: int) -> Optional[dict]:
        """
        Возвращает информацию о текущей подписке пользователя.
        """
        subscription = self.db.get_user_active_subscription(user_id)
        if not subscription:
            return None

        now = datetime.now()
        days_left = (subscription.end_date - now).days
        hours_left = (subscription.end_date - now).seconds // 3600

        return {
            "subscription": subscription,
            "days_left": max(0, days_left),
            "hours_left": max(0, hours_left),
            "is_active": subscription.end_date > now,
        }

    def format_subscription_message(self, user_id: int) -> str:
        """
        Форматирует сообщение о подписке пользователя.
        """
        info = self.get_user_subscription_info(user_id)
        if not info:
            return "❌ У вас нет активной подписки.\n\nИспользуйте /buy для покупки."

        sub = info["subscription"]
        days_left = info["days_left"]
        hours_left = info["hours_left"]

        traffic_info = "♾️ Безлимит" if sub.traffic_gb == 0 else f"📊 {sub.traffic_gb} ГБ"

        message = f"""✅ <b>Ваша подписка активна</b>

📦 План: {traffic_info}
⏱ Срок действия: {sub.days} дней
📅 Начало: {sub.start_date.strftime('%d.%m.%Y %H:%M')}
📅 Окончание: {sub.end_date.strftime('%d.%m.%Y %H:%M')}

⏰ Осталось: {days_left} дней {hours_left} часов

Статус: 🟢 Активна"""

        return message

