import os
import asyncio
import secrets
import logging
import time
from datetime import datetime, timedelta
from urllib.parse import quote

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, PreCheckoutQuery, LabeledPrice
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

from .config import load_config
from .xui_client import XUIClient
from .database import init_db, get_connection, check_expired_subscriptions

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Планы подписки
SUBSCRIPTION_PLANS = {
    "1_month": {
        "title": "1 месяц",
        "duration": 1,
        "traffic_gb": 100,
        "price_rub": 19900,  # 199₽
        "price_stars": 199,
        "new_user": True
    },
    "3_months": {
        "title": "3 месяца",
        "duration": 3,
        "traffic_gb": 300,
        "price_rub": 49900,  # 499₽
        "price_stars": 499,
        "new_user": True
    },
    "6_months": {
        "title": "6 месяцев",
        "duration": 6,
        "traffic_gb": 600,
        "price_rub": 89900,  # 899₽
        "price_stars": 899,
        "new_user": True
    },
    "12_months": {
        "title": "12 месяцев",
        "duration": 12,
        "traffic_gb": 1200,
        "price_rub": 149900,  # 1499₽
        "price_stars": 1499,
        "new_user": True
    }
}

RENEWAL_PLANS = {
    "1_month_renew": {
        "title": "1 месяц 🔥",
        "duration": 1,
        "traffic_gb": 100,
        "price_rub": 14900,  # 149₽
        "price_stars": 149,
        "new_user": False
    },
    "3_months_renew": {
        "title": "3 месяца 🔥",
        "duration": 3,
        "traffic_gb": 300,
        "price_rub": 39900,  # 399₽
        "price_stars": 399,
        "new_user": False
    },
    "6_months_renew": {
        "title": "6 месяцев 🔥",
        "duration": 6,
        "traffic_gb": 600,
        "price_rub": 74900,  # 749₽
        "price_stars": 749,
        "new_user": False
    },
    "12_months_renew": {
        "title": "12 месяцев 🔥",
        "duration": 12,
        "traffic_gb": 1200,
        "price_rub": 119900,  # 1199₽
        "price_stars": 1199,
        "new_user": False
    }
}

# Методы оплаты
PAYMENT_METHODS = {
    "stars": {
        "title": "Telegram Stars",
        "provider_token": "",
        "currency": "XTR"
    },
    "yookassa": {
        "title": "Юкасса",
        "provider_token": "381764678:TEST:150431",
        "currency": "RUB"
    }
}

POLICY_LINK = "https://telegra.ph/Konfidencialnost-i-usloviya-02-01"

class SubscriptionSteps(StatesGroup):
    CHOOSING_PLAN = State()
    CHOOSING_PAYMENT_METHOD = State()
    CHOOSING_SERVER = State()

class AddServerSteps(StatesGroup):
    WAITING_NAME = State()
    WAITING_PANEL_URL = State()
    WAITING_USERNAME = State()
    WAITING_PASSWORD = State()
    WAITING_INBOUND_ID = State()
    CONFIRMING = State()

class AdminEditStates(StatesGroup):
    EDIT_ANNOUNCEMENT = State()

class KeyManagementStates(StatesGroup):
    CHOOSING_SERVER_FOR_KEY = State()
    ENTERING_KEY_NAME = State()
    VIEWING_KEY = State()
    CONFIRMING_DELETE = State()
    CONFIRMING_REPLACE = State()

def get_announcement_text() -> str:
    """Получает текст объявления из БД"""
    with get_connection(cfg.database.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT text FROM announcements ORDER BY id DESC LIMIT 1')
        result = cursor.fetchone()
        if result:
            return result[0]
    # Дефолтный текст, если в БД ничего нет
    return "!!!ВНИМАНИЕ!!! Это бета-тест, VPN работает нестабильно, платежи также находятся в тестировании - они не реальны!!!\n"

def set_announcement_text(new_text: str):
    """Сохраняет текст объявления в БД"""
    with get_connection(cfg.database.db_path) as conn:
        cursor = conn.cursor()
        # Проверяем наличие колонки updated_at
        cursor.execute("PRAGMA table_info(announcements)")
        columns = [column[1] for column in cursor.fetchall()]
        has_updated_at = 'updated_at' in columns
        
        # Удаляем старые объявления и добавляем новое
        cursor.execute('DELETE FROM announcements')
        if has_updated_at:
            cursor.execute('''
                INSERT INTO announcements (text, updated_at) VALUES (?, CURRENT_TIMESTAMP)
            ''', (new_text.strip(),))
        else:
            cursor.execute('''
                INSERT INTO announcements (text) VALUES (?)
            ''', (new_text.strip(),))
        conn.commit()

cfg = load_config()
bot = Bot(token=cfg.bot.bot_token)
dp = Dispatcher()
xui_client = XUIClient(cfg.xui)

init_db(cfg.database.db_path)

def get_main_keyboard(user_id: int):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="💳 Premium", callback_data="open_premium"),
        InlineKeyboardButton(text="🎁 Рефералка", callback_data="open_invite")
    )
    builder.row(
        InlineKeyboardButton(text="🔑 Мои ключи", callback_data="manage_keys")
    )
    builder.row(
        InlineKeyboardButton(text="🆘 Помощь", callback_data="open_help")
    )
    if is_admin(user_id):
        builder.row(InlineKeyboardButton(text="✏️ Редактировать объявление", callback_data="edit_announcement"))
    return builder.as_markup()

def get_subscription_status(user_id: int) -> str:
    """Получает статус подписки пользователя"""
    with get_connection(cfg.database.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT subscription_end, pay_subscribed 
            FROM users 
            WHERE user_id = ?
        ''', (user_id,))
        user_data = cursor.fetchone()

        if user_data and user_data[1] == 1 and user_data[0]:
            end_date = datetime.strptime(user_data[0], "%Y-%m-%d")
            if end_date >= datetime.now():
                return f"активен до {end_date.strftime('%d.%m.%Y')}"
    return "неактивен"

def get_main_text(first_name: str, subscription_status: str, user_id: int = None) -> str:
    """Возвращает основной текст с объявлением"""
    ann = get_announcement_text()
    msg = (
        f"👋 Рады видеть тебя снова, <b>{first_name}</b>!\n\n"
        f"<b>VPN</b>: <i>{subscription_status}</i>\n\n"
        f"📌 <b>Команды:</b>\n"
        "<i>/start</i> - Перезагрузить бота\n"
        "<i>/prem</i> - Покупка VPN\n"
        "<i>/invite</i> - Пригласи друга\n\n"
        f"<code>{ann}\nb1.1.12</code>"
    )
    return msg

@dp.message(CommandStart())
async def handle_start(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    args = message.text.split()

    # Парсим реферальный код
    referral_code = args[1][4:] if len(args) > 1 and args[1].startswith('ref_') else None

    with get_connection(cfg.database.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = cursor.fetchone()

        if not user:
            # Создаем нового пользователя
            new_referral_code = secrets.token_hex(4)
            cursor.execute('''
                INSERT INTO users (
                    user_id, 
                    username, 
                    first_name, 
                    registration_date,
                    last_activity,
                    subscribed,
                    referral_code,
                    invited_by,
                    pay_subscribed,
                    subscription_end
                ) VALUES (?, ?, ?, datetime('now'), datetime('now'), FALSE, ?, NULL, FALSE, NULL)
            ''', (user_id, username, first_name, new_referral_code))
            conn.commit()

            # Обработка реферального кода
            has_referral = False
            if referral_code:
                cursor.execute('SELECT user_id FROM users WHERE referral_code = ?', (referral_code,))
                inviter = cursor.fetchone()

                if inviter:
                    inviter_id = inviter[0]
                    # Обновляем данные пригласившего
                    cursor.execute('''
                        UPDATE users SET
                            referral_count = referral_count + 1,
                            subscription_end = CASE 
                                WHEN subscription_end IS NULL OR subscription_end < DATE('now') 
                                THEN DATE('now', '+5 days')
                                ELSE DATE(subscription_end, '+5 days')
                            END,
                            pay_subscribed = 1
                        WHERE user_id = ?
                    ''', (inviter_id,))

                    # Обновляем данные нового пользователя
                    cursor.execute('''
                        UPDATE users SET
                            invited_by = ?,
                            subscription_end = DATE('now', '+3 days'),
                            pay_subscribed = 1
                        WHERE user_id = ?
                    ''', (inviter_id, user_id))
                    conn.commit()

                    # Уведомления
                    try:
                        await bot.send_message(
                            inviter_id,
                            f"🎉 Вы получили +5 дней VPN за приглашение друга!\n"
                            f"Теперь ваш VPN активен до: {(datetime.now() + timedelta(days=5)).strftime('%d.%m.%Y')}"
                        )
                    except Exception as e:
                        logging.error(f"Ошибка отправки уведомления: {e}")

                    has_referral = True

            # Формируем приветственное сообщение
            welcome_msg_parts = [
                "<b>VPN бот</b> — быстрый и надежный VPN сервис\n\n"
            ]

            if has_referral:
                expiration_date = (datetime.now() + timedelta(days=3)).strftime("%d.%m.%Y")
                welcome_msg_parts.append(
                    f"🎁 Вы получили +3 дня <b>VPN</b> за регистрацию по реферальной ссылке!\n"
                    f"Ваш <b>VPN</b> активен до: {expiration_date}\n\n"
                )

            welcome_msg_parts.extend([
                "<b>Бот предоставляет</b>:\n"
                "• Безопасный и быстрый VPN\n"
                "• Обход блокировок\n"
                "• Высокая скорость\n\n"
                "👉 Больше информации в разделе <b>помощь</b> - /help\n\n"
                "‼️ Продолжая использовать бота, вы принимаете <a href='https://telegra.ph/Konfidencialnost-i-usloviya-02-01'>нашу политику и конфиденциальность</a>!\n\n"
            ])

            welcome_msg = "".join(welcome_msg_parts)

            await message.answer(
                welcome_msg,
                reply_markup=get_main_keyboard(user_id),
                disable_web_page_preview=True,
                parse_mode='HTML'
            )
        else:
            # Обновляем активность
            cursor.execute("UPDATE users SET last_activity = datetime('now') WHERE user_id = ?", (user_id,))
            conn.commit()

            subscription_status = get_subscription_status(user_id)
            await message.answer(
                get_main_text(first_name, subscription_status, user_id),
                parse_mode="HTML",
                reply_markup=get_main_keyboard(user_id)
            )

async def _get_subscription_info(user_id: int):
    """Вспомогательная функция для получения информации о подписке"""
    with get_connection(cfg.database.db_path) as conn:
        cursor = conn.cursor()
        # Получаем данные пользователя - работаем с тем что есть
        # Сначала проверяем, какие колонки есть в таблице
        cursor.execute("PRAGMA table_info(users)")
        columns = [col[1] for col in cursor.fetchall()]
        
        # Формируем запрос только с существующими колонками
        select_fields = ['subscription_end', 'pay_subscribed']
        if 'vless_link' in columns:
            select_fields.insert(1, 'vless_link')
        else:
            select_fields.insert(1, 'NULL as vless_link')
        
        try:
            query = f'SELECT {", ".join(select_fields)} FROM users WHERE user_id = ?'
            cursor.execute(query, (user_id,))
            result = cursor.fetchone()
        except Exception as e:
            # Если ошибка - используем дефолтные значения
            logger.error(f"Database error in subscription info: {e}")
            result = None
    
    # Обрабатываем данные пользователя
    if result:
        # Обрабатываем результат в зависимости от количества колонок
        if len(result) >= 3:
            subscription_end, vless_link, pay_subscribed = result[0], result[1], result[2]
        elif len(result) == 2:
            # Если vless_link нет в таблице
            subscription_end, pay_subscribed = result[0], result[1]
            vless_link = None
        else:
            subscription_end = None
            vless_link = None
            pay_subscribed = 0
        
        is_active = False
        
        # Проверяем, активна ли подписка
        if pay_subscribed == 1 and subscription_end:
            try:
                # Парсим дату окончания
                if isinstance(subscription_end, str):
                    # Может быть формат 'YYYY-MM-DD' или 'YYYY-MM-DD HH:MM:SS'
                    if ' ' in subscription_end:
                        end_date = datetime.strptime(subscription_end.split()[0], "%Y-%m-%d")
                    else:
                        end_date = datetime.strptime(subscription_end, "%Y-%m-%d")
                else:
                    end_date = subscription_end
                
                # Проверяем, не истекла ли подписка (сравниваем только даты)
                today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                end_date_only = end_date.replace(hour=0, minute=0, second=0, microsecond=0)
                
                if end_date_only >= today:
                    is_active = True
                    days_remaining = (end_date_only - today).days
                    end_date_str = end_date.strftime("%d.%m.%Y")
                else:
                    days_remaining = 0
                    end_date_str = None
            except Exception as e:
                logger.error(f"Error parsing subscription date: {e}, date: {subscription_end}")
                is_active = False
                days_remaining = 0
                end_date_str = None
        else:
            is_active = False
            days_remaining = 0
            end_date_str = None
    else:
        # Пользователь не найден в базе - используем дефолтные значения
        subscription_end = None
        vless_link = None
        pay_subscribed = 0
        is_active = False
        days_remaining = 0
        end_date_str = None
    
    return {
        'is_active': is_active,
        'subscription_end': subscription_end,
        'vless_link': vless_link,
        'pay_subscribed': pay_subscribed,
        'days_remaining': days_remaining,
        'end_date_str': end_date_str
    }

async def _build_subscription_message(info: dict, state: FSMContext):
    """Строит сообщение и клавиатуру для подписки"""
    builder = InlineKeyboardBuilder()
    is_active = info['is_active']
    days_remaining = info['days_remaining']
    end_date_str = info['end_date_str']
    vless_link = info['vless_link']
    
    if is_active:
        # Если подписка активна - показываем информацию и VPN ссылку
        text = (
            "✅ Ваш <b>VPN</b> <b>активен</b>!\n\n"
            f"📅 Дата окончания: <i>{end_date_str}</i>\n"
            f"⏰ Осталось дней: <i>{days_remaining}</i>\n\n"
        )
        
        # Показываем VPN ссылку если она есть
        if vless_link:
            text += (
                f"🔗 <b>Ваша VPN ссылка:</b>\n"
                f"<code>{vless_link}</code>\n\n"
                f"📱 <b>Как использовать:</b>\n"
                f"1. Нажмите на ссылку выше, чтобы скопировать\n"
                f"2. Скачайте приложение (v2rayNG, sing-box и т.п.)\n"
                f"3. Импортируйте ссылку в приложение\n"
                f"4. Подключитесь!\n\n"
            )
        else:
            text += (
                "⚠️ VPN ссылка не найдена. Обратитесь в поддержку.\n\n"
            )
        
        text += (
            "<b>Детали VPN</b>:\n"
            "• Быстрый и безопасный VPN\n"
            "• Обход всех блокировок\n"
            "• Высокая скорость\n\n"
        )
        
        # Показываем кнопки продления только если осталось <= 3 дня
        if days_remaining <= 3:
            text += (
                "🎁 <b>Специальное предложение!</b>\n\n"
                "🔥 Успей продлить <b>VPN</b> по специальной цене:\n"
                f"1 месяц <s>199₽</s> - 149₽\n"
                f"3 месяца <s>499₽</s> - 399₽\n"
                f"6 месяцев <s>899₽</s> - 749₽\n"
                f"12 месяцев <s>1499₽</s> - 1199₽\n\n"
            )
            for plan_id, plan_data in RENEWAL_PLANS.items():
                builder.button(
                    text=f"{plan_data['title']} - {plan_data['price_rub'] // 100}₽ | {plan_data['price_stars']}⭐",
                    callback_data=f"plan:{plan_id}"
                )
            builder.adjust(1)
            await state.set_state(SubscriptionSteps.CHOOSING_PLAN)
        else:
            text += "💡 Ваша подписка активна. Вы сможете продлить её за 3 дня до окончания.\n\n"
            await state.clear()
        
        # Кнопка "Назад" всегда
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="go_back"))
    else:
        # Если подписка неактивна или пользователя нет - показываем планы
        text = "💳 <b>Информация о вашем VPN:</b>\n\n"
        text += (
            "❌ Ваш VPN <b>неактивен</b>!\n\n"
            "Что ты получишь с <b>VPN</b>?\n"
            "• Быстрый и безопасный VPN\n"
            "• Обход всех блокировок\n"
            "• Высокая скорость подключения\n\n"
            "Выберите план подписки:\n"
        )
        for plan_id, plan_data in SUBSCRIPTION_PLANS.items():
            builder.button(
                text=f"{plan_data['title']} - {plan_data['price_rub'] // 100}₽ | {plan_data['price_stars']}⭐",
                callback_data=f"plan:{plan_id}"
            )
        builder.adjust(1)
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="go_back"))
        await state.set_state(SubscriptionSteps.CHOOSING_PLAN)
    
    return text, builder

@dp.callback_query(F.data == "open_premium")
async def handle_open_premium_callback(callback: CallbackQuery, state: FSMContext):
    """Обработчик кнопки Premium (callback)"""
    user_id = callback.from_user.id
    await callback.answer()
    
    info = await _get_subscription_info(user_id)
    text, builder = await _build_subscription_message(info, state)
    
    await callback.message.edit_text(
        text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

@dp.message(Command("prem"))
async def handle_prem_command(message: Message, state: FSMContext):
    """Обработчик команды /prem"""
    user_id = message.from_user.id
    
    info = await _get_subscription_info(user_id)
    text, builder = await _build_subscription_message(info, state)
    
    await message.answer(
        text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

async def handle_sub_info(callback: CallbackQuery, state: FSMContext):
    """Обертка для обратной совместимости - вызывает callback обработчик"""
    await handle_open_premium_callback(callback, state)

@dp.callback_query(SubscriptionSteps.CHOOSING_PLAN, F.data.startswith("plan:"))
async def select_plan(callback: CallbackQuery, state: FSMContext):
    plan_id = callback.data.split(":")[1]
    user_id = callback.from_user.id

    ALL_PLANS = {**SUBSCRIPTION_PLANS, **RENEWAL_PLANS}

    if plan_id not in ALL_PLANS:
        await callback.answer("❌ Неверный план")
        return

    is_renewal = plan_id in RENEWAL_PLANS
    plan_data = RENEWAL_PLANS[plan_id] if is_renewal else SUBSCRIPTION_PLANS[plan_id]

    # Проверяем, есть ли у пользователя активная подписка
    with get_connection(cfg.database.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT subscription_end 
            FROM users 
            WHERE user_id = ? 
                AND pay_subscribed = 1 
                AND subscription_end >= DATE('now')
        ''', (user_id,))
        active_sub = cursor.fetchone()

    # Если пользователь пытается купить новую подписку, но у него уже есть активная
    if not is_renewal and active_sub:
        await callback.answer("❌ У вас уже есть активная подписка! Используйте продление.", show_alert=True)
        # Возвращаем к меню подписки
        await handle_open_premium_callback(callback, state)
        return
    
    # Если пользователь пытается продлить, но подписка еще не заканчивается (осталось > 3 дня)
    if is_renewal:
        if not active_sub:
            await callback.answer("❌ У вас нет активной подписки для продления!", show_alert=True)
            await handle_open_premium_callback(callback, state)
            return

        cursor.execute('''
            SELECT julianday(subscription_end) - julianday('now') as days_remaining 
            FROM users 
            WHERE user_id = ? 
                AND pay_subscribed = 1 
                AND subscription_end >= DATE('now')
        ''', (user_id,))
        days_result = cursor.fetchone()
        if days_result and days_result[0] and int(days_result[0]) > 3:
            await callback.answer("❌ Продление доступно только за 3 дня до окончания подписки!", show_alert=True)
            await handle_open_premium_callback(callback, state)
            return

    await state.update_data(
        selected_plan_id=plan_id,
        selected_plan_data=plan_data,
        is_renewal=is_renewal
    )
    
    # Проверяем, есть ли активные серверы
    active_servers = get_active_servers()
    if not active_servers:
        await callback.answer("❌ Нет доступных серверов. Обратитесь к администратору.", show_alert=True)
        return
    
    # Если это продление и у пользователя уже есть сервер - используем его
    if is_renewal:
        with get_connection(cfg.database.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT server_id FROM users WHERE user_id = ?', (user_id,))
            result = cursor.fetchone()
            if result and result[0]:
                # Используем существующий сервер
                server_id = result[0]
                # Проверяем, что сервер активен
                server_data = get_server_by_id(server_id)
                if server_data and any(s[0] == server_id for s in active_servers):
                    await state.update_data(selected_server_id=server_id)
                    # Переходим к выбору метода оплаты
                    await show_payment_methods(callback, state)
                    return
    
    # Показываем выбор сервера
    builder = InlineKeyboardBuilder()
    text = f"🖥️ <b>Выберите сервер</b>\n\n"
    text += f"План: <b>{plan_data['title']}</b>\n"
    text += f"Цена: {plan_data['price_rub'] // 100}₽ | {plan_data['price_stars']}⭐\n\n"
    text += "Выберите сервер для подключения:\n"
    
    for server_id, name, ip, _ in active_servers:
        builder.button(
            text=f"🖥️ {name} ({ip})",
            callback_data=f"server:{server_id}"
        )
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="sub_back_to_plan"))
    
    await callback.message.edit_text(
        text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await state.set_state(SubscriptionSteps.CHOOSING_SERVER)
    await callback.answer()

@dp.callback_query(SubscriptionSteps.CHOOSING_SERVER, F.data.startswith("server:"))
async def select_server(callback: CallbackQuery, state: FSMContext):
    """Обработчик выбора сервера"""
    server_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    
    # Проверяем, что сервер активен
    server_data = get_server_by_id(server_id)
    if not server_data:
        await callback.answer("❌ Сервер не найден", show_alert=True)
        return
    
    active_servers = get_active_servers()
    if not any(s[0] == server_id for s in active_servers):
        await callback.answer("❌ Сервер неактивен", show_alert=True)
        return
    
    await state.update_data(selected_server_id=server_id)
    await show_payment_methods(callback, state)

async def show_payment_methods(callback: CallbackQuery, state: FSMContext):
    """Показать методы оплаты"""
    data = await state.get_data()
    plan_data = data.get('selected_plan_data')
    
    builder = InlineKeyboardBuilder()
    for method_id, method_data in PAYMENT_METHODS.items():
        builder.button(
            text=method_data['title'],
            callback_data=f"method:{method_id}"
        )
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="sub_back_to_plan"))
    builder.adjust(1)

    # Форматируем цены для отображения
    price_rub = plan_data['price_rub'] // 100
    price_stars = plan_data['price_stars']

    await callback.message.edit_text(
        f"📝 Выбранный план: <i>{plan_data['title']}</i>\n"
        f"💳 Сумма оплаты: <i>{price_rub}₽</i> или <i>{price_stars}⭐</i>\n\n"
        "Выберите способ оплаты:",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )

    await state.set_state(SubscriptionSteps.CHOOSING_PAYMENT_METHOD)

@dp.callback_query(SubscriptionSteps.CHOOSING_PAYMENT_METHOD, F.data.startswith("method:"))
async def process_payment(callback: CallbackQuery, state: FSMContext):
    method_id = callback.data.split(":")[1]
    user_data = await state.get_data()
    plan_id = user_data.get('selected_plan_id')
    plan_data = user_data.get('selected_plan_data')

    if not all([method_id, plan_id, plan_data]):
        await callback.answer("❌ Ошибка данных")
        return

    # Получаем выбранный сервер из состояния
    data = await state.get_data()
    server_id = data.get('selected_server_id')
    if not server_id:
        # Если сервер не выбран, берем первый активный
        active_servers = get_active_servers()
        if active_servers:
            server_id = active_servers[0][0]
        else:
            await callback.answer("❌ Нет доступных серверов", show_alert=True)
            return

    payload = f"{plan_id}|{method_id}|{server_id}"

    currency_type = 'stars' if PAYMENT_METHODS[method_id]['currency'] == 'XTR' else 'rub'
    price = plan_data[f"price_{currency_type}"]

    await bot.send_invoice(
        chat_id=callback.message.chat.id,
        title=f"VPN подписка - {plan_data['title']}",
        description=f"Нажимая кнопку «Заплатить» Вы соглашаетесь с правилами VPN бота (/help)",
        provider_token=PAYMENT_METHODS[method_id]['provider_token'],
        currency=PAYMENT_METHODS[method_id]['currency'],
        prices=[LabeledPrice(label="VPN подписка", amount=price)],
        payload=payload,
        start_parameter='subscription'
    )

@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    try:
        payload = message.successful_payment.invoice_payload
        if "|" not in payload:
            raise ValueError("Неверный формат платежа")

        parts = payload.split("|")
        if len(parts) < 2:
            raise ValueError("Неверный формат payload")
        
        # Обработка подписки
        plan_id = parts[0]
        method_id = parts[1]
        server_id_from_payload = int(parts[2]) if len(parts) > 2 else None

        # Определение типа подписки
        if plan_id in SUBSCRIPTION_PLANS:
            plan_data = SUBSCRIPTION_PLANS[plan_id]
            is_new_subscription = True
        elif plan_id in RENEWAL_PLANS:
            plan_data = RENEWAL_PLANS[plan_id]
            is_new_subscription = False
        else:
            raise ValueError(f"Неизвестный план: {plan_id}")

        # Валидация метода оплаты
        if method_id not in PAYMENT_METHODS:
            raise ValueError(f"Неизвестный метод оплаты: {method_id}")

        method_data = PAYMENT_METHODS[method_id]
        duration_months = plan_data['duration']
        traffic_gb = plan_data['traffic_gb']

        user_id = message.from_user.id
        username = message.from_user.username or f"user_{user_id}"
        
        # Получаем выбранный сервер
        # Приоритет: из payload > из БД пользователя > первый активный
        server_id = server_id_from_payload
        if not server_id:
            with get_connection(cfg.database.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT server_id FROM users WHERE user_id = ?', (user_id,))
                result_user = cursor.fetchone()
                server_id = result_user[0] if result_user and result_user[0] else None
            
            # Если сервер не найден, берем первый активный
            if not server_id:
                active_servers = get_active_servers()
                if not active_servers:
                    raise ValueError("Нет доступных серверов")
                server_id = active_servers[0][0]
        
        # Получаем данные сервера
        server_data = get_server_by_id(server_id)
        if not server_data:
            raise ValueError(f"Сервер {server_id} не найден")
        
        # Распаковываем данные сервера (может быть старый формат без port и protocol)
        if len(server_data) >= 9:
            server_id_db, server_name, server_ip, server_port, server_protocol, server_username, server_password, server_inbound_id, server_base_url = server_data
        else:
            # Старый формат (без port и protocol)
            server_id_db, server_name, server_ip, server_username, server_password, server_inbound_id, server_base_url = server_data
            server_port = 54321
            server_protocol = 'https'
        
        # Обновление подписки в базе данных (БЕЗ создания ключа)
        with get_connection(cfg.database.db_path) as conn:
            cursor = conn.cursor()

            if is_new_subscription:
                # Новая подписка
                days = duration_months * 30
                cursor.execute('''
                    UPDATE users 
                    SET 
                        pay_subscribed = 1,
                        subscription_end = DATE('now', '+' || ? || ' days'),
                        renewal_used = 0
                    WHERE user_id = ?
                ''', (days, user_id))
            else:
                # Продление существующей подписки
                cursor.execute('''
                    UPDATE users 
                    SET 
                        subscription_end = DATE(subscription_end, ?),
                        renewal_used = 1
                    WHERE user_id = ?
                ''', (f"+{duration_months} months", user_id))

            # Получаем обновленную дату окончания
            cursor.execute('''
                SELECT subscription_end FROM users WHERE user_id = ?
            ''', (user_id,))
            subscription_end = cursor.fetchone()[0]
            
            # Сохраняем платеж
            cursor.execute('''
                INSERT INTO payments (user_id, amount, currency, plan_id, plan_type, status, telegram_payment_charge_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                user_id,
                plan_data[f"price_{'stars' if method_data['currency'] == 'XTR' else 'rub'}"],
                method_data['currency'],
                plan_id,
                'subscription',
                'completed',
                message.successful_payment.telegram_payment_charge_id
            ))
            
            conn.commit()

        # Форматирование дат
        activation_date = datetime.now().strftime("%d.%m.%Y")
        end_date = datetime.strptime(subscription_end, "%Y-%m-%d").strftime("%d.%m.%Y")

        # Форматирование цены
        price_key = f"price_{'stars' if method_data['currency'] == 'XTR' else 'rub'}"
        price = plan_data[price_key]

        if method_data['currency'] == 'XTR':
            formatted_price = f"{price} Stars (≈ {price * 0.01:.2f}₽)"
        else:
            formatted_price = f"{price // 100}₽"

        # Формирование квитанции
        receipt = (
            f"💳 <b>VPN подписка</b> успешно активирована!\n\n"
            f"<b>Чек на оплату</b>\n"
            f"Дата активации: <i>{activation_date}</i>\n"
            f"Дата окончания: <i>{end_date}</i>\n"
            f"Способ оплаты: <i>{method_data['title']}</i>\n"
            f"Сумма оплаты: <i>{formatted_price}</i>\n\n"
            f"<b>Детали подписки</b>:\n"
            f"• План: <i>{plan_data['title']}</i>\n"
            f"• Трафик: <i>{traffic_gb} ГБ</i>\n"
            f"• Срок: <i>{duration_months} месяцев</i>\n\n"
            f"✅ Теперь вы можете создать до 3 VPN ключей!\n"
            f"Используйте раздел <b>🔑 Мои ключи</b> в главном меню.\n\n"
            f"ID транзакции: <blockquote>{message.successful_payment.telegram_payment_charge_id}</blockquote>"
        )

        await message.answer(receipt, parse_mode='HTML')

    except Exception as e:
        logging.error(f"Ошибка обработки платежа: {str(e)}", exc_info=True)
        await message.answer(
            "❌ Произошла ошибка при обработке платежа. "
            "Пожалуйста, обратитесь в поддержку."
        )

@dp.callback_query(F.data == "open_invite")
async def handle_open_invite_callback(callback: CallbackQuery):
    """Обработчик кнопки Рефералка (callback)"""
    user_id = callback.from_user.id

    with get_connection(cfg.database.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT referral_code, referral_count 
            FROM users 
            WHERE user_id = ?
        ''', (user_id,))
        result = cursor.fetchone()

        if not result:
            await callback.answer("❌ Сначала запустите бота через /start", show_alert=True)
            return

        referral_code, referral_count = result

        # Если код по какой-то причине отсутствует в БД
        if not referral_code:
            referral_code = secrets.token_hex(4)
            cursor.execute('''
                UPDATE users
                SET referral_code = ?
                WHERE user_id = ?
            ''', (referral_code, user_id))
            conn.commit()

    bot_username = (await bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start=ref_{referral_code}"
    text = (
        f"🎁 <b>Пригласи друга и получи +5 дней VPN!</b>\n\n"
        f"🔗 Ваша реферальная ссылка:\n<code>{ref_link}</code>\n\n"
        f"👥 Приглашено друзей: <i>{referral_count or 0}</i>\n"
        f"За каждого друга вы получаете +5 дней VPN, а друг получает +3 дня!"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📤 Поделиться",
            url=f"https://t.me/share/url?url={ref_link}&text={quote('Присоединяйся к VPN боту с моей подпиской!')}"
        )],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="go_back")]
    ])

    # Редактируем исходное сообщение с кнопкой
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
    await callback.answer()

@dp.message(Command("invite"))
async def handle_invite_command(message: Message):
    """Обработчик команды /invite"""
    user_id = message.from_user.id

    with get_connection(cfg.database.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT referral_code, referral_count 
            FROM users 
            WHERE user_id = ?
        ''', (user_id,))
        result = cursor.fetchone()

        if not result:
            await message.answer("❌ Пожалуйста, сначала запустите бота с помощью команды /start")
            return

        referral_code, referral_count = result

        # Если реферальный код отсутствует, генерируем новый
        if not referral_code:
            referral_code = secrets.token_hex(4)
            cursor.execute('''
                UPDATE users
                SET referral_code = ?
                WHERE user_id = ?
            ''', (referral_code, user_id))
            conn.commit()

    bot_username = (await bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start=ref_{referral_code}"
    text = (
        f"🎁 <b>Пригласи друга и получи +5 дней VPN!</b>\n\n"
        f"🔗 Ваша реферальная ссылка:\n<code>{ref_link}</code>\n\n"
        f"👥 Приглашено друзей: <i>{referral_count or 0}</i>\n"
        f"За каждого друга вы получаете +5 дней VPN, а друг получает +3 дня!"
    )

    # Клавиатура с кнопкой поделиться
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📤 Поделиться",
            url=f"https://t.me/share/url?url={ref_link}&text={quote('Присоединяйся к VPN боту с моей подпиской!')}"
        )],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="go_back")]
    ])

    await message.answer(text, parse_mode='HTML', reply_markup=keyboard)

@dp.callback_query(F.data == "go_back")
async def go_back_handler(callback: CallbackQuery):
    """Обработчик кнопки Назад"""
    user_id = callback.from_user.id
    first_name = callback.from_user.first_name or "Пользователь"
    subscription_status = get_subscription_status(user_id)

    await callback.message.edit_text(
        text=get_main_text(first_name, subscription_status, user_id),
        parse_mode='HTML',
        reply_markup=get_main_keyboard(user_id)
    )
    await callback.answer()

@dp.callback_query(F.data == "sub_back_to_plan")
async def handle_sub_back_to_plan(callback: CallbackQuery, state: FSMContext):
    await handle_sub_info(callback, state)

@dp.callback_query(F.data == "open_help")
@dp.message(Command("help"))
async def handle_open_help(message_or_callback: Message | CallbackQuery):
    if isinstance(message_or_callback, CallbackQuery):
        message = message_or_callback.message
        await message_or_callback.answer()
    else:
        message = message_or_callback
    
    report_button = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="go_back")]
    ])

    help_text = (
        "🤖<b>VPN бот</b> — быстрый и надежный VPN сервис\n\n"
        "<b>Бот предоставляет</b>:\n"
        "• Быстрый и безопасный VPN\n"
        "• Обход всех блокировок\n"
        "• Высокая скорость подключения\n\n"
        "<b>Как пользоваться</b>?\n"
        "• Купите подписку через /prem\n"
        "• Получите VPN ссылку\n"
        "• Импортируйте ссылку в приложение (v2rayNG, sing-box и т.п.)\n"
        "• Подключитесь!\n\n"
        "<b>Реферальная программа</b>:\n"
        "• Пригласите друга через /invite\n"
        "• Вы получите +5 дней VPN\n"
        "• Друг получит +3 дня VPN\n\n"
        "📌 <b>Команды</b>:\n"
        "/start - Перезагрузить бота\n"
        "/prem - Покупка VPN\n"
        "/invite - Пригласи друга\n"
    )

    if isinstance(message_or_callback, CallbackQuery):
        await message_or_callback.message.edit_text(
            help_text,
            reply_markup=report_button,
            parse_mode="HTML"
        )
    else:
        await message.answer(
            help_text,
            reply_markup=report_button,
            parse_mode="HTML"
        )

@dp.callback_query(F.data == "edit_announcement")
async def start_edit_announcement(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет прав", show_alert=True)
        return
    await callback.message.edit_text(
        "✏️ Введите новый текст объявления.\n\n<code>Он будет показан в главном меню всем пользователям.</code>",
        parse_mode="HTML"
    )
    await state.set_state(AdminEditStates.EDIT_ANNOUNCEMENT)
    await callback.answer()

@dp.message(AdminEditStates.EDIT_ANNOUNCEMENT)
async def save_announcement_text(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Нет прав", parse_mode="HTML")
        await state.clear()
        return
    new_ann = message.text[:2048] if message.text else ''
    if not new_ann.strip():
        await message.answer("Сообщение не может быть пустым. Попробуйте снова (или отмените командой /start)")
        return
    set_announcement_text(new_ann)
    await message.answer("✅ Объявление обновлено! Теперь оно показывается всем пользователям.", parse_mode="HTML")
    await state.clear()

def is_admin(user_id: int) -> bool:
    """Проверка, является ли пользователь админом"""
    return user_id in cfg.bot.admin_ids

def get_active_servers():
    """Получить список активных серверов"""
    with get_connection(cfg.database.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, name, ip, inbound_id 
            FROM servers 
            WHERE is_active = TRUE
            ORDER BY name
        ''')
        return cursor.fetchall()

def get_server_by_id(server_id: int):
    """Получить данные сервера по ID"""
    with get_connection(cfg.database.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, name, ip, username, password, inbound_id, base_url
            FROM servers 
            WHERE id = ?
        ''', (server_id,))
        return cursor.fetchone()

def check_user_subscription(user_id: int) -> bool:
    """Проверка, есть ли у пользователя активная подписка"""
    with get_connection(cfg.database.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT pay_subscribed, subscription_end 
            FROM users 
            WHERE user_id = ?
        ''', (user_id,))
        result = cursor.fetchone()
        if not result or result[0] != 1:
            return False
        if result[1]:
            try:
                end_date = datetime.strptime(result[1], "%Y-%m-%d")
                return end_date >= datetime.now()
            except:
                return False
        return False

def get_user_keys_count(user_id: int) -> int:
    """Получить количество активных ключей пользователя"""
    with get_connection(cfg.database.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT COUNT(*) FROM vpn_keys 
            WHERE user_id = ? AND is_active = TRUE
        ''', (user_id,))
        return cursor.fetchone()[0]

def get_user_keys(user_id: int):
    """Получить список всех ключей пользователя"""
    with get_connection(cfg.database.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT k.id, k.key_name, k.vless_link, k.created_at, k.expires_at, 
                   k.traffic_gb, k.is_active, s.name as server_name
            FROM vpn_keys k
            LEFT JOIN servers s ON k.server_id = s.id
            WHERE k.user_id = ?
            ORDER BY k.created_at DESC
        ''', (user_id,))
        return cursor.fetchall()

def get_key_by_id(key_id: int, user_id: int):
    """Получить информацию о ключе по ID"""
    with get_connection(cfg.database.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT k.id, k.key_name, k.vless_link, k.vless_client_id, k.created_at, 
                   k.expires_at, k.traffic_gb, k.is_active, k.server_id, s.name as server_name
            FROM vpn_keys k
            LEFT JOIN servers s ON k.server_id = s.id
            WHERE k.id = ? AND k.user_id = ?
        ''', (key_id, user_id))
        return cursor.fetchone()

# ==================== УПРАВЛЕНИЕ КЛЮЧАМИ ====================

@dp.callback_query(F.data == "manage_keys")
async def handle_manage_keys(callback: CallbackQuery):
    """Обработчик раздела управления ключами"""
    user_id = callback.from_user.id
    
    # Проверяем подписку
    if not check_user_subscription(user_id):
        await callback.answer("❌ У вас нет активной подписки. Купите подписку через /prem", show_alert=True)
        return
    
    keys = get_user_keys(user_id)
    keys_count = get_user_keys_count(user_id)
    
    text = (
        f"🔑 <b>Мои VPN ключи</b>\n\n"
        f"Активных ключей: <i>{keys_count}/3</i>\n\n"
    )
    
    if not keys:
        text += "У вас пока нет ключей. Создайте первый ключ!"
    else:
        text += "<b>Ваши ключи:</b>\n"
        for key_id, key_name, vless_link, created_at, expires_at, traffic_gb, is_active, server_name in keys:
            status = "✅" if is_active else "❌"
            name = key_name or f"Ключ #{key_id}"
            text += f"{status} <b>{name}</b>\n"
            if server_name:
                text += f"   Сервер: {server_name}\n"
            if created_at:
                try:
                    created = datetime.strptime(created_at.split()[0], "%Y-%m-%d").strftime("%d.%m.%Y")
                    text += f"   Создан: {created}\n"
                except:
                    pass
            text += "\n"
    
    builder = InlineKeyboardBuilder()
    if keys_count < 3:
        builder.row(InlineKeyboardButton(text="➕ Создать ключ", callback_data="create_key"))
    
    if keys:
        builder.row(InlineKeyboardButton(text="📋 Просмотреть ключ", callback_data="view_key_list"))
    
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="go_back"))
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data == "create_key")
async def handle_create_key(callback: CallbackQuery, state: FSMContext):
    """Обработчик создания нового ключа"""
    user_id = callback.from_user.id
    
    # Проверяем подписку
    if not check_user_subscription(user_id):
        await callback.answer("❌ У вас нет активной подписки", show_alert=True)
        return
    
    # Проверяем лимит ключей
    keys_count = get_user_keys_count(user_id)
    if keys_count >= 3:
        await callback.answer("❌ У вас уже максимальное количество ключей (3)", show_alert=True)
        return
    
    # Получаем список активных серверов
    active_servers = get_active_servers()
    if not active_servers:
        await callback.answer("❌ Нет доступных серверов", show_alert=True)
        return
    
    # Формируем клавиатуру с серверами
    builder = InlineKeyboardBuilder()
    for server_id, server_name, server_ip, inbound_id in active_servers:
        builder.row(InlineKeyboardButton(
            text=f"🖥️ {server_name}",
            callback_data=f"key_server:{server_id}"
        ))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="manage_keys"))
    
    await callback.message.edit_text(
        "🔑 <b>Создание нового ключа</b>\n\n"
        "Выберите сервер для создания ключа:",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("key_server:"))
async def handle_key_server_selection(callback: CallbackQuery, state: FSMContext):
    """Обработчик выбора сервера для ключа"""
    server_id = int(callback.data.split(":")[1])
    await state.update_data(selected_server_id=server_id)
    
    # Сразу создаем ключ без запроса названия
    user_id = callback.from_user.id
    key_to_replace = (await state.get_data()).get('key_to_replace')
    
    # Получаем данные сервера
    server_data = get_server_by_id(server_id)
    if not server_data:
        await callback.answer("❌ Сервер не найден", show_alert=True)
        await state.clear()
        return
    
    # Распаковываем данные сервера
    if len(server_data) >= 7:
        server_id_db, server_name, server_ip, server_username, server_password, server_inbound_id, server_base_url = server_data
    else:
        await callback.answer("❌ Ошибка данных сервера", show_alert=True)
        await state.clear()
        return
    
    # Получаем информацию о подписке для определения трафика и срока
    with get_connection(cfg.database.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT subscription_end FROM users WHERE user_id = ?
        ''', (user_id,))
        result = cursor.fetchone()
        if not result or not result[0]:
            await callback.answer("❌ Ошибка: подписка не найдена", show_alert=True)
            await state.clear()
            return
        
        subscription_end = result[0]
        try:
            end_date = datetime.strptime(subscription_end, "%Y-%m-%d")
            days_valid = (end_date - datetime.now()).days
            if days_valid <= 0:
                await callback.answer("❌ Ваша подписка истекла", show_alert=True)
                await state.clear()
                return
        except:
            await callback.answer("❌ Ошибка при расчете срока подписки", show_alert=True)
            await state.clear()
            return
    
    # Создаем ключ на сервере
    try:
        server_client = XUIClient(
            base_url=server_base_url,
            username=server_username,
            password=server_password,
            inbound_id=server_inbound_id
        )
        
        # Используем стандартные значения для трафика (можно настроить)
        traffic_gb = 100  # Можно брать из подписки
        
        # Создаем клиента с display_name = server_id (в конце VLESS ссылки будет server_id)
        result = server_client.add_vless_client(
            telegram_user_id=user_id,
            display_name=str(server_id),  # В конце VLESS ссылки будет server_id
            traffic_gb=traffic_gb,
            days_valid=days_valid,
        )
        
        vless_client_id = result.get("id")
        vless_link = result.get("link")
        
        # Сохраняем ключ в БД (key_name будет обновлен после получения key_id)
        with get_connection(cfg.database.db_path) as conn:
            cursor = conn.cursor()
            expires_at = end_date.strftime("%Y-%m-%d")
            cursor.execute('''
                INSERT INTO vpn_keys (user_id, server_id, vless_client_id, vless_link, 
                                    key_name, expires_at, traffic_gb, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, TRUE)
            ''', (user_id, server_id, vless_client_id, vless_link, None, expires_at, traffic_gb))
            conn.commit()
            key_id = cursor.lastrowid
        
        # Обновляем key_name и vless_link с правильными значениями
        key_name = f"{server_name} #{key_id}"
        # Обновляем VLESS ссылку: заменяем последнюю часть (после #) на server_id
        if '#' in vless_link:
            vless_link = vless_link.rsplit('#', 1)[0] + f"#{server_id}"
        else:
            vless_link = vless_link + f"#{server_id}"
        
        # Обновляем запись в БД с правильным key_name и vless_link
        with get_connection(cfg.database.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE vpn_keys 
                SET key_name = ?, vless_link = ?
                WHERE id = ?
            ''', (key_name, vless_link, key_id))
            conn.commit()
        
        # Если это замена ключа, удаляем старый
        if key_to_replace:
            old_key_data = get_key_by_id(key_to_replace, user_id)
            if old_key_data:
                old_key_id_db, old_key_name, old_vless_link, old_vless_client_id, old_created_at, old_expires_at, old_traffic_gb, old_is_active, old_server_id, old_server_name = old_key_data
                
                # Удаляем старый ключ из БД
                with get_connection(cfg.database.db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute('DELETE FROM vpn_keys WHERE id = ? AND user_id = ?', (key_to_replace, user_id))
                    conn.commit()
                
                # Удаляем старый клиент с сервера
                old_server_data = get_server_by_id(old_server_id)
                if old_server_data:
                    old_server_id_db, old_server_name, old_server_ip, old_server_username, old_server_password, old_server_inbound_id, old_server_base_url = old_server_data
                    try:
                        old_server_client = XUIClient(
                            base_url=old_server_base_url,
                            username=old_server_username,
                            password=old_server_password,
                            inbound_id=old_server_inbound_id
                        )
                        old_server_client.delete_client(old_vless_client_id)
                        logger.info(f"Successfully deleted old client {old_vless_client_id} from server {old_server_id}")
                    except Exception as e:
                        logger.error(f"Failed to delete old client from server: {e}")
                        # Продолжаем работу даже если не удалось удалить старый клиент
        
        await callback.message.edit_text(
            f"✅ <b>Ключ успешно {'заменен' if key_to_replace else 'создан'}!</b>\n\n"
            f"<b>Информация:</b>\n"
            f"Название: <i>{key_name}</i>\n"
            f"Сервер: <i>{server_name}</i>\n"
            f"Срок действия: <i>{end_date.strftime('%d.%m.%Y')}</i>\n\n"
            f"🔗 <b>VPN ссылка:</b>\n"
            f"<code>{vless_link}</code>\n\n"
            f"Используйте раздел <b>🔑 Мои ключи</b> для управления ключами.",
            parse_mode="HTML"
        )
        await callback.answer()
        
    except Exception as e:
        logger.error(f"Failed to create key: {e}")
        await callback.message.edit_text(
            f"❌ <b>Ошибка при создании ключа:</b>\n<code>{str(e)}</code>\n\n"
            f"Попробуйте позже или обратитесь в поддержку.",
            parse_mode="HTML"
        )
        await callback.answer()
    
    await state.clear()

@dp.message(KeyManagementStates.ENTERING_KEY_NAME)
async def handle_key_name_input(message: Message, state: FSMContext):
    """Обработка названия ключа - больше не используется, но оставляем для совместимости"""
    # Эта функция больше не используется, так как мы сразу создаем ключ после выбора сервера
    await message.answer("❌ Эта функция больше не используется. Выберите сервер заново через раздел '🔑 Мои ключи'.")
    await state.clear()

@dp.callback_query(F.data == "view_key_list")
async def handle_view_key_list(callback: CallbackQuery):
    """Показать список ключей для просмотра"""
    user_id = callback.from_user.id
    keys = get_user_keys(user_id)
    
    if not keys:
        await callback.answer("У вас нет ключей", show_alert=True)
        return
    
    builder = InlineKeyboardBuilder()
    for key_id, key_name, vless_link, created_at, expires_at, traffic_gb, is_active, server_name in keys:
        name = key_name or f"Ключ #{key_id}"
        builder.row(InlineKeyboardButton(
            text=f"{'✅' if is_active else '❌'} {name}",
            callback_data=f"view_key:{key_id}"
        ))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="manage_keys"))
    
    await callback.message.edit_text(
        "🔑 <b>Выберите ключ для просмотра:</b>",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("view_key:"))
async def handle_view_key(callback: CallbackQuery):
    """Просмотр информации о ключе"""
    user_id = callback.from_user.id
    key_id = int(callback.data.split(":")[1])
    
    key_data = get_key_by_id(key_id, user_id)
    if not key_data:
        await callback.answer("❌ Ключ не найден", show_alert=True)
        return
    
    key_id_db, key_name, vless_link, vless_client_id, created_at, expires_at, traffic_gb, is_active, server_id, server_name = key_data
    
    status = "✅ Активен" if is_active else "❌ Неактивен"
    name = key_name or f"Ключ #{key_id_db}"
    
    text = (
        f"🔑 <b>{name}</b>\n\n"
        f"Статус: <i>{status}</i>\n"
        f"Сервер: <i>{server_name or 'Неизвестно'}</i>\n"
    )
    
    if created_at:
        try:
            created = datetime.strptime(created_at.split()[0], "%Y-%m-%d").strftime("%d.%m.%Y")
            text += f"Создан: <i>{created}</i>\n"
        except:
            pass
    
    if expires_at:
        try:
            expires = datetime.strptime(expires_at, "%Y-%m-%d").strftime("%d.%m.%Y")
            text += f"Истекает: <i>{expires}</i>\n"
        except:
            pass
    
    if traffic_gb:
        text += f"Трафик: <i>{traffic_gb} ГБ</i>\n"
    
    text += f"\n🔗 <b>VPN ссылка:</b>\n<code>{vless_link}</code>"
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"delete_key:{key_id_db}"))
    builder.row(InlineKeyboardButton(text="🔄 Заменить", callback_data=f"replace_key:{key_id_db}"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="view_key_list"))
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("delete_key:"))
async def handle_delete_key(callback: CallbackQuery, state: FSMContext):
    """Удаление ключа"""
    user_id = callback.from_user.id
    key_id = int(callback.data.split(":")[1])
    
    key_data = get_key_by_id(key_id, user_id)
    if not key_data:
        await callback.answer("❌ Ключ не найден", show_alert=True)
        return
    
    key_id_db, key_name, vless_link, vless_client_id, created_at, expires_at, traffic_gb, is_active, server_id, server_name = key_data
    name = key_name or f"Ключ #{key_id_db}"
    
    await state.update_data(key_to_delete=key_id_db, key_client_id=vless_client_id, key_server_id=server_id)
    await state.set_state(KeyManagementStates.CONFIRMING_DELETE)
    
    await callback.message.edit_text(
        f"⚠️ <b>Подтверждение удаления</b>\n\n"
        f"Вы уверены, что хотите удалить ключ <b>{name}</b>?\n\n"
        f"Это действие нельзя отменить.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"confirm_delete:{key_id_db}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"view_key:{key_id_db}")]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("confirm_delete:"))
async def handle_confirm_delete(callback: CallbackQuery, state: FSMContext):
    """Подтверждение удаления ключа"""
    user_id = callback.from_user.id
    key_id = int(callback.data.split(":")[1])
    
    key_data = get_key_by_id(key_id, user_id)
    if not key_data:
        await callback.answer("❌ Ключ не найден", show_alert=True)
        await state.clear()
        return
    
    key_id_db, key_name, vless_link, vless_client_id, created_at, expires_at, traffic_gb, is_active, server_id, server_name = key_data
    name = key_name or f"Ключ #{key_id_db}"
    
    # Получаем данные сервера для удаления клиента
    server_data = get_server_by_id(server_id)
    if server_data:
        server_id_db, server_name, server_ip, server_username, server_password, server_inbound_id, server_base_url = server_data
        
        # Удаляем клиент с сервера
        try:
            server_client = XUIClient(
                base_url=server_base_url,
                username=server_username,
                password=server_password,
                inbound_id=server_inbound_id
            )
            server_client.delete_client(vless_client_id)
            logger.info(f"Successfully deleted client {vless_client_id} from server {server_id}")
        except Exception as e:
            logger.error(f"Failed to delete client from server: {e}")
            # Продолжаем удаление из БД даже если не удалось удалить с сервера
    
    # Удаляем ключ из БД
    with get_connection(cfg.database.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM vpn_keys WHERE id = ? AND user_id = ?', (key_id_db, user_id))
        conn.commit()
    
    await callback.message.edit_text(
        f"✅ Ключ <b>{name}</b> успешно удален!",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад к ключам", callback_data="manage_keys")]
        ])
    )
    await callback.answer()
    await state.clear()

@dp.callback_query(F.data.startswith("replace_key:"))
async def handle_replace_key(callback: CallbackQuery, state: FSMContext):
    """Замена ключа"""
    user_id = callback.from_user.id
    key_id = int(callback.data.split(":")[1])
    
    key_data = get_key_by_id(key_id, user_id)
    if not key_data:
        await callback.answer("❌ Ключ не найден", show_alert=True)
        return
    
    # При замене ключа мы удаляем старый и создаем новый, поэтому лимит не проверяем
    
    # Сохраняем ID ключа для замены
    await state.update_data(key_to_replace=key_id)
    
    # Показываем выбор сервера
    active_servers = get_active_servers()
    if not active_servers:
        await callback.answer("❌ Нет доступных серверов", show_alert=True)
        return
    
    builder = InlineKeyboardBuilder()
    for server_id, server_name, server_ip, inbound_id in active_servers:
        builder.row(InlineKeyboardButton(
            text=f"🖥️ {server_name}",
            callback_data=f"replace_key_server:{server_id}:{key_id}"
        ))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data=f"view_key:{key_id}"))
    
    await callback.message.edit_text(
        "🔄 <b>Замена ключа</b></b>\n\n"
        "Выберите сервер для нового ключа:",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("replace_key_server:"))
async def handle_replace_key_server(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора сервера для замены ключа"""
    parts = callback.data.split(":")
    server_id = int(parts[1])
    old_key_id = int(parts[2])
    
    await state.update_data(selected_server_id=server_id, key_to_replace=old_key_id)
    
    # Сразу создаем ключ (используем ту же логику, что и при создании нового)
    # Вызываем обработчик выбора сервера, который теперь создает ключ сразу
    await handle_key_server_selection(callback, state)

# ==================== АДМИНСКИЕ КОМАНДЫ ДЛЯ УПРАВЛЕНИЯ СЕРВЕРАМИ ====================

@dp.message(Command("add_server"))
async def cmd_add_server(message: Message, state: FSMContext):
    """Команда для добавления нового сервера"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этой команде.")
        return
    
    await message.answer(
        "🔧 <b>Добавление нового сервера</b>\n\n"
        "Введите название сервера (будет видно пользователям):",
        parse_mode="HTML"
    )
    await state.set_state(AddServerSteps.WAITING_NAME)

@dp.message(AddServerSteps.WAITING_NAME)
async def process_server_name(message: Message, state: FSMContext):
    """Обработка названия сервера"""
    await state.update_data(name=message.text)
    await message.answer(
        "🔗 Введите полную ссылку на панель 3x-ui:\n\n"
        "Примеры:\n"
        "• <code>http://79.137.204.85:8080/</code>\n"
        "• <code>http://176.109.105.175:8080/YF0nOS5FD5nBM5MmWq/</code>\n"
        "• <code>https://example.com:54321/</code>\n\n"
        "⚠️ Важно: Укажите полную ссылку, включая протокол (http:// или https://), "
        "адрес, порт и путь (если есть).",
        parse_mode="HTML"
    )
    await state.set_state(AddServerSteps.WAITING_PANEL_URL)

@dp.message(AddServerSteps.WAITING_PANEL_URL)
async def process_server_panel_url(message: Message, state: FSMContext):
    """Обработка ссылки на панель"""
    from urllib.parse import urlparse
    
    panel_url = message.text.strip()
    
    # Убеждаемся, что URL заканчивается на /
    if not panel_url.endswith('/'):
        panel_url += '/'
    
    # Парсим URL
    try:
        parsed = urlparse(panel_url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("Неверный формат URL")
        
        protocol = parsed.scheme.lower()
        if protocol not in ['http', 'https']:
            await message.answer("❌ Поддерживаются только протоколы HTTP и HTTPS. Попробуйте снова:")
            return

        # Извлекаем IP/домен и порт
        netloc = parsed.netloc
        if ':' in netloc:
            host, port_str = netloc.rsplit(':', 1)
            try:
                port = int(port_str)
            except ValueError:
                await message.answer("❌ Неверный формат порта. Попробуйте снова:")
                return
        else:
            # Если порт не указан, используем стандартный
            host = netloc
            port = 443 if protocol == 'https' else 80
        
        # Путь из URL
        path = parsed.path
        
        # Формируем base_url (убираем путь из base_url, так как он будет использоваться в запросах)
        # Но сохраняем полный URL для отображения
        base_url = f"{protocol}://{host}:{port}{path}".rstrip('/')
        
        # Извлекаем IP или домен
        ip_or_domain = host
        
        await state.update_data(
            ip=ip_or_domain,
            port=port,
            protocol=protocol,
            base_url=base_url,
            panel_url=panel_url
        )
        
        await message.answer(
            f"✅ URL успешно распознан!\n\n"
            f"<b>Данные:</b>\n"
            f"Протокол: <i>{protocol.upper()}</i>\n"
            f"Адрес: <i>{ip_or_domain}</i>\n"
            f"Порт: <i>{port}</i>\n"
            f"Путь: <i>{path if path else '/'}</i>\n"
            f"Base URL: <i>{base_url}</i>\n\n"
            f"Введите username для панели 3x-ui:",
            parse_mode="HTML"
        )
        await state.set_state(AddServerSteps.WAITING_USERNAME)
        
    except Exception as e:
        await message.answer(
            f"❌ <b>Ошибка парсинга URL:</b>\n<code>{str(e)}</code>\n\n"
            f"Пожалуйста, введите полную ссылку в формате:\n"
            f"<code>http://IP:ПОРТ/ПУТЬ/</code>\n\n"
            f"Пример: <code>http://79.137.204.85:8080/</code>",
            parse_mode="HTML"
        )

@dp.message(AddServerSteps.WAITING_USERNAME)
async def process_server_username(message: Message, state: FSMContext):
    """Обработка username"""
    await state.update_data(username=message.text)
    await message.answer("Введите password для панели 3x-ui:")
    await state.set_state(AddServerSteps.WAITING_PASSWORD)

@dp.message(AddServerSteps.WAITING_PASSWORD)
async def process_server_password(message: Message, state: FSMContext):
    """Обработка password"""
    await state.update_data(password=message.text)
    await message.answer("Введите Inbound ID (число):")
    await state.set_state(AddServerSteps.WAITING_INBOUND_ID)

@dp.message(AddServerSteps.WAITING_INBOUND_ID)
async def process_server_inbound_id(message: Message, state: FSMContext):
    """Обработка Inbound ID"""
    try:
        inbound_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Inbound ID должен быть числом. Попробуйте снова:")
        return
    
    data = await state.get_data()
    name = data.get('name')
    ip = data.get('ip')
    port = data.get('port', 54321)
    protocol = data.get('protocol', 'https')
    username = data.get('username')
    password = data.get('password')
    base_url = data.get('base_url')
    
    # Проверяем подключение к серверу
    try:
        test_client = XUIClient(
            base_url=base_url,
            username=username,
            password=password,
            inbound_id=inbound_id
        )
        test_client.login()
        await message.answer(
            f"✅ <b>Подключение к серверу успешно!</b>\n\n"
            f"<b>Данные сервера:</b>\n"
            f"Название: <i>{name}</i>\n"
            f"IP: <i>{ip}</i>\n"
            f"Протокол: <i>{protocol.upper()}</i>\n"
            f"Порт: <i>{port}</i>\n"
            f"Base URL: <i>{base_url}</i>\n"
            f"Username: <i>{username}</i>\n"
            f"Inbound ID: <i>{inbound_id}</i>\n\n"
            f"Сохранить этот сервер? (да/нет)",
            parse_mode="HTML"
        )
        await state.update_data(inbound_id=inbound_id)
        await state.set_state(AddServerSteps.CONFIRMING)
    except Exception as e:
        error_msg = str(e)
        # Предлагаем попробовать другой протокол при SSL ошибке
        if "SSL" in error_msg or "WRONG_VERSION_NUMBER" in error_msg:
            suggestion = "\n\n💡 <b>Совет:</b> Попробуйте использовать HTTP вместо HTTPS. Используйте /add_server для повторного ввода."
        else:
            suggestion = "\n\nПроверьте данные и попробуйте снова. Используйте /add_server для повторного ввода."
        
        await message.answer(
            f"❌ <b>Ошибка подключения к серверу:</b>\n<code>{error_msg}</code>{suggestion}"
        )
        await state.clear()

@dp.message(AddServerSteps.CONFIRMING)
async def process_server_confirmation(message: Message, state: FSMContext):
    """Обработка подтверждения добавления сервера"""
    if message.text.lower() not in ['да', 'yes', 'y', 'д']:
        await message.answer("❌ Добавление сервера отменено.")
        await state.clear()
        return
    
    data = await state.get_data()
    name = data.get('name')
    ip = data.get('ip')
    port = data.get('port', 54321)
    protocol = data.get('protocol', 'https')
    username = data.get('username')
    password = data.get('password')
    base_url = data.get('base_url')
    inbound_id = data.get('inbound_id')
    
    # Сохраняем сервер в БД
    with get_connection(cfg.database.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO servers (name, ip, port, protocol, username, password, inbound_id, base_url, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, TRUE)
        ''', (name, ip, port, protocol, username, password, inbound_id, base_url))
        conn.commit()
        server_id = cursor.lastrowid
    
    await message.answer(
        f"✅ <b>Сервер успешно добавлен!</b>\n\n"
        f"ID: <i>{server_id}</i>\n"
        f"Название: <i>{name}</i>\n"
        f"IP: <i>{ip}</i>",
        parse_mode="HTML"
    )
    await state.clear()

@dp.message(Command("servers"))
async def cmd_list_servers(message: Message):
    """Список всех серверов"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этой команде.")
        return
    
    with get_connection(cfg.database.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, name, ip, is_active 
            FROM servers 
            ORDER BY id
        ''')
        servers = cursor.fetchall()
    
    if not servers:
        await message.answer("📭 Серверы не найдены. Используйте /add_server для добавления.")
        return
    
    text = "🖥️ <b>Список серверов:</b>\n\n"
    for server_id, name, ip, is_active in servers:
        status = "✅ Активен" if is_active else "❌ Неактивен"
        text += f"{server_id}. <b>{name}</b> ({ip})\n   {status}\n\n"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить сервер", callback_data="admin_add_server")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_refresh_servers")]
    ])
    
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)

@dp.message(Command("toggle_server"))
async def cmd_toggle_server(message: Message):
    """Активация/деактивация сервера"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этой команде.")
        return
    
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Использование: /toggle_server <server_id>")
        return
    
    try:
        server_id = int(args[1])
    except ValueError:
        await message.answer("❌ Server ID должен быть числом.")
        return
    
    with get_connection(cfg.database.db_path) as conn:
        cursor = conn.cursor()
        # Получаем текущий статус
        cursor.execute('SELECT is_active FROM servers WHERE id = ?', (server_id,))
        result = cursor.fetchone()
        
        if not result:
            await message.answer(f"❌ Сервер с ID {server_id} не найден.")
            return
        
        current_status = result[0]
        new_status = not current_status
        
        cursor.execute('''
            UPDATE servers 
            SET is_active = ?, updated_at = datetime('now')
            WHERE id = ?
        ''', (new_status, server_id))
        conn.commit()
        
        status_text = "активирован" if new_status else "деактивирован"
        await message.answer(f"✅ Сервер {server_id} {status_text}.")

@dp.message(Command("delete_server"))
async def cmd_delete_server(message: Message):
    """Удаление сервера"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этой команде.")
        return
    
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Использование: /delete_server <server_id>")
        return
    
    try:
        server_id = int(args[1])
    except ValueError:
        await message.answer("❌ Server ID должен быть числом.")
        return
    
    with get_connection(cfg.database.db_path) as conn:
        cursor = conn.cursor()
        # Проверяем, используется ли сервер
        cursor.execute('SELECT COUNT(*) FROM users WHERE server_id = ?', (server_id,))
        users_count = cursor.fetchone()[0]
        
        if users_count > 0:
            await message.answer(
                f"❌ Нельзя удалить сервер, который используется {users_count} пользователями.\n"
                f"Сначала деактивируйте сервер: /toggle_server {server_id}"
            )
            return
        
        cursor.execute('DELETE FROM servers WHERE id = ?', (server_id,))
        conn.commit()
        
        if cursor.rowcount > 0:
            await message.answer(f"✅ Сервер {server_id} удален.")
        else:
            await message.answer(f"❌ Сервер с ID {server_id} не найден.")

async def daily_scheduler():
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(
        check_expired_subscriptions,
        'cron',
        hour=12,
        minute=0,
        args=[cfg.database.db_path]
    )
    scheduler.start()

async def main():
    asyncio.create_task(daily_scheduler())
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    print("Бот запущен!")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    print("\nБот остановлен!")
