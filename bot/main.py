import os
import asyncio
import secrets
import logging
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
    WAITING_IP = State()
    WAITING_PORT = State()
    WAITING_USERNAME = State()
    WAITING_PASSWORD = State()
    WAITING_INBOUND_ID = State()
    CONFIRMING = State()

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
        InlineKeyboardButton(text="🆘 Помощь", callback_data="open_help")
    )
    return builder.as_markup()

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

            # Проверяем статус подписки
            cursor.execute('''
                SELECT subscription_end, pay_subscribed 
                FROM users 
                WHERE user_id = ?
            ''', (user_id,))
            user_data = cursor.fetchone()
            
            subscription_status = "неактивен"
            if user_data and user_data[1] == 1 and user_data[0]:
                end_date = datetime.strptime(user_data[0], "%Y-%m-%d")
                if end_date >= datetime.now():
                    subscription_status = f"активен до {end_date.strftime('%d.%m.%Y')}"

            await message.answer(
                f"👋 Рады видеть тебя снова, <b>{first_name}</b>!\n\n"
                f"<b>VPN</b>: <i>{subscription_status}</i>\n\n"
                f"📌 <b>Команды:</b>\n"
                "<i>/start</i> - Перезагрузить бота\n"
                "<i>/prem</i> - Покупка VPN\n"
                "<i>/invite</i> - Пригласи друга\n\n"
                "Используйте кнопки ниже для управления.",
                parse_mode='HTML', 
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
        
        server_id_db, server_name, server_ip, server_username, server_password, server_inbound_id, server_base_url = server_data
        
        # Создаем клиент для конкретного сервера
        try:
            server_client = XUIClient(
                base_url=server_base_url,
                username=server_username,
                password=server_password,
                inbound_id=server_inbound_id
            )
            result = server_client.add_vless_client(
                telegram_user_id=user_id,
                display_name=username,
                traffic_gb=traffic_gb,
                days_valid=duration_months * 30,
            )
            vless_client_id = result.get("id")
            vless_link = result.get("link")
        except Exception as e:
            logger.error(f"Failed to create x-ui client on server {server_id}: {e}")
            raise ValueError(f"Ошибка при создании VPN подключения на сервере {server_name}: {e}")

        # Обновление подписки в базе данных
        with get_connection(cfg.database.db_path) as conn:
            cursor = conn.cursor()

            if is_new_subscription:
                # Новая подписка
                days = duration_months * 30
                cursor.execute('''
                    UPDATE users 
                    SET 
                        pay_subscribed = 1,
                        server_id = ?,
                        vless_client_id = ?,
                        vless_link = ?,
                        subscription_end = DATE('now', '+' || ? || ' days'),
                        renewal_used = 0
                    WHERE user_id = ?
                ''', (server_id, vless_client_id, vless_link, days, user_id))
            else:
                # Продление существующей подписки
                cursor.execute('''
                    UPDATE users 
                    SET 
                        subscription_end = DATE(subscription_end, ?),
                        renewal_used = 1,
                        vless_client_id = ?,
                        vless_link = ?
                    WHERE user_id = ?
                ''', (f"+{duration_months} months", vless_client_id, vless_link, user_id))

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
            f"💳 <b>VPN</b> успешно активирован!\n\n"
            f"<b>Чек на оплату</b>\n"
            f"Дата активации: <i>{activation_date}</i>\n"
            f"Дата окончания: <i>{end_date}</i>\n"
            f"Способ оплаты: <i>{method_data['title']}</i>\n"
            f"Сумма оплаты: <i>{formatted_price}</i>\n\n"
            f"<b>Детали VPN</b>:\n"
            f"• План: <i>{plan_data['title']}</i>\n"
            f"• Сервер: <i>{server_name}</i>\n"
            f"• Трафик: <i>{traffic_gb} ГБ</i>\n"
            f"• Срок: <i>{duration_months} месяцев</i>\n\n"
            f"🔗 <b>Ваша VPN ссылка:</b>\n"
            f"<code>{vless_link}</code>\n\n"
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
    
    # Получаем статус подписки
    with get_connection(cfg.database.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT subscription_end, pay_subscribed 
            FROM users 
            WHERE user_id = ?
        ''', (user_id,))
        user_data = cursor.fetchone()
        
        subscription_status = "неактивен"
        if user_data and user_data[1] == 1 and user_data[0]:
            end_date = datetime.strptime(user_data[0], "%Y-%m-%d")
            if end_date >= datetime.now():
                subscription_status = f"активен до {end_date.strftime('%d.%m.%Y')}"
    
    await callback.message.edit_text(
        f"👋 Рады видеть тебя снова, <b>{first_name}</b>!\n\n"
        f"<b>VPN</b>: <i>{subscription_status}</i>\n\n"
        f"📌 <b>Команды:</b>\n"
        "<i>/start</i> - Перезагрузить бота\n"
        "<i>/prem</i> - Покупка VPN\n"
        "<i>/invite</i> - Пригласи друга\n\n"
        "Используйте кнопки ниже для управления.",
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

# ==================== АДМИНСКИЕ КОМАНДЫ ДЛЯ УПРАВЛЕНИЯ СЕРВЕРАМИ ====================

@dp.message(Command("add_server"))
async def cmd_add_server(message: Message, state: FSMContext):
    """Команда для добавления нового сервера"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этой команде.")
        return
    
    await message.answer(
        "🔧 <b>Добавление нового сервера</b>\n\n"
        "Введите название сервера (будет видно пользователям):"
    )
    await state.set_state(AddServerSteps.WAITING_NAME)

@dp.message(AddServerSteps.WAITING_NAME)
async def process_server_name(message: Message, state: FSMContext):
    """Обработка названия сервера"""
    await state.update_data(name=message.text)
    await message.answer("Введите IP адрес сервера:")
    await state.set_state(AddServerSteps.WAITING_IP)

@dp.message(AddServerSteps.WAITING_IP)
async def process_server_ip(message: Message, state: FSMContext):
    """Обработка IP адреса"""
    ip = message.text.strip()
    await state.update_data(ip=ip)
    await message.answer("Введите порт панели 3x-ui (по умолчанию 54321, нажмите Enter для использования стандартного):")
    await state.set_state(AddServerSteps.WAITING_PORT)

@dp.message(AddServerSteps.WAITING_PORT)
async def process_server_port(message: Message, state: FSMContext):
    """Обработка порта"""
    port_text = message.text.strip()
    if not port_text:
        port = 54321  # Стандартный порт
    else:
        try:
            port = int(port_text)
            if port < 1 or port > 65535:
                await message.answer("❌ Порт должен быть в диапазоне 1-65535. Попробуйте снова:")
                return
        except ValueError:
            await message.answer("❌ Порт должен быть числом. Попробуйте снова:")
            return

    data = await state.get_data()
    ip = data.get('ip')
    base_url = f"https://{ip}:{port}"
    await state.update_data(port=port, base_url=base_url)
    await message.answer("Введите username для панели 3x-ui:")
    await state.set_state(AddServerSteps.WAITING_USERNAME)

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
            f"Base URL: <i>{base_url}</i>\n"
            f"Username: <i>{username}</i>\n"
            f"Inbound ID: <i>{inbound_id}</i>\n\n"
            f"Сохранить этот сервер? (да/нет)"
        )
        await state.update_data(inbound_id=inbound_id)
        await state.set_state(AddServerSteps.CONFIRMING)
    except Exception as e:
        await message.answer(
            f"❌ <b>Ошибка подключения к серверу:</b>\n<code>{str(e)}</code>\n\n"
            f"Проверьте данные и попробуйте снова. Используйте /add_server для повторного ввода."
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
    username = data.get('username')
    password = data.get('password')
    base_url = data.get('base_url')
    inbound_id = data.get('inbound_id')
    
    # Сохраняем сервер в БД
    with get_connection(cfg.database.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO servers (name, ip, username, password, inbound_id, base_url, is_active)
            VALUES (?, ?, ?, ?, ?, ?, TRUE)
        ''', (name, ip, username, password, inbound_id, base_url))
        conn.commit()
        server_id = cursor.lastrowid
    
    await message.answer(
        f"✅ <b>Сервер успешно добавлен!</b>\n\n"
        f"ID: <i>{server_id}</i>\n"
        f"Название: <i>{name}</i>\n"
        f"IP: <i>{ip}</i>"
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
