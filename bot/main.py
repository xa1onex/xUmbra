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
        "provider_token": os.getenv("YOOKASSA_TOKEN", ""),
        "currency": "RUB"
    }
}

POLICY_LINK = "https://telegra.ph/Konfidencialnost-i-usloviya-02-01"

class SubscriptionSteps(StatesGroup):
    CHOOSING_PLAN = State()
    CHOOSING_PAYMENT_METHOD = State()

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

@dp.message(Command("prem"))
@dp.callback_query(F.data == "open_premium")
async def handle_sub_info(message_or_callback: Message | CallbackQuery, state: FSMContext):
    if isinstance(message_or_callback, CallbackQuery):
        message = message_or_callback.message
        await message_or_callback.answer()
    else:
        message = message_or_callback
    
    user_id = message.from_user.id
    with get_connection(cfg.database.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT 
                subscription_end,
                julianday(subscription_end) - julianday('now') as days_remaining 
            FROM users 
            WHERE user_id = ? 
                AND pay_subscribed = 1 
                AND subscription_end >= DATE('now')
        ''', (user_id,))
        result = cursor.fetchone()

    builder = InlineKeyboardBuilder()
    text = "💳 <b>Информация о вашем VPN:</b>\n\n"

    if result:
        subscription_end, days_remaining = result
        days_remaining = int(days_remaining)
        end_date = datetime.strptime(subscription_end, "%Y-%m-%d").strftime("%d.%m.%Y")

        if days_remaining <= 3:
            text = (
                "✅ Ваш <b>VPN</b> <b>активен</b>!\n\n"
                f"Дата окончания: <i>{end_date}</i>\n\n"
                "<b>Детали VPN</b>:\n"
                "• Быстрый и безопасный VPN\n"
                "• Обход всех блокировок\n"
                "• Высокая скорость\n\n"
                "🎁 <b>Специальное предложение!</b>\n\n"
                "🔥 Успей продлить <b>VPN</b> по специальной цене:\n"
                f"1 месяц <s>199₽</s> - 149₽\n"
                f"3 месяца <s>499₽</s> - 399₽\n"
                f"6 месяцев <s>899₽</s> - 749₽\n"
                f"12 месяцев <s>1499₽</s> - 1199₽\n\n"
                "Спасибо за использование <b>VPN</b>!"
            )
            for plan_id, plan_data in RENEWAL_PLANS.items():
                builder.button(
                    text=f"{plan_data['title']} - {plan_data['price_rub'] // 100}₽ | {plan_data['price_stars']}⭐",
                    callback_data=f"plan:{plan_id}"
                )
        else:
            text += (
                "✅ Ваш <b>VPN</b> <b>активен</b>!\n\n"
                f"Дата окончания: <i>{end_date}</i>\n\n"
                "<b>Детали VPN</b>:\n"
                "• Быстрый и безопасный VPN\n"
                "• Обход всех блокировок\n"
                "• Высокая скорость\n\n"
                "Спасибо за использование <b>VPN</b>!"
            )
    else:
        text += (
            "❌ Ваш VPN <b>неактивен</b>!\n\n"
            "Что ты получишь с <b>VPN</b>?\n"
            "• Быстрый и безопасный VPN\n"
            "• Обход всех блокировок\n"
            "• Высокая скорость подключения\n"
        )
        for plan_id, plan_data in SUBSCRIPTION_PLANS.items():
            builder.button(
                text=f"{plan_data['title']} - {plan_data['price_rub'] // 100}₽ | {plan_data['price_stars']}⭐",
                callback_data=f"plan:{plan_id}"
            )

    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="go_back"))
    builder.adjust(1)

    await message.answer(
        text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await state.set_state(SubscriptionSteps.CHOOSING_PLAN)

@dp.callback_query(SubscriptionSteps.CHOOSING_PLAN, F.data.startswith("plan:"))
async def select_plan(callback: CallbackQuery, state: FSMContext):
    plan_id = callback.data.split(":")[1]

    ALL_PLANS = {**SUBSCRIPTION_PLANS, **RENEWAL_PLANS}

    if plan_id not in ALL_PLANS:
        await callback.answer("❌ Неверный план")
        return

    is_renewal = plan_id in RENEWAL_PLANS
    plan_data = RENEWAL_PLANS[plan_id] if is_renewal else SUBSCRIPTION_PLANS[plan_id]

    await state.update_data(
        selected_plan_id=plan_id,
        selected_plan_data=plan_data,
        is_renewal=is_renewal
    )

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

    payload = f"{plan_id}|{method_id}"

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
        if len(parts) != 2:
            raise ValueError("Неверный формат payload")
        else:
            # Обработка подписки
            plan_id, method_id = payload.split("|")

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
            
            # Создаем VPN подключение
            try:
                result = xui_client.add_vless_client(
                    telegram_user_id=user_id,
                    display_name=username,
                    traffic_gb=traffic_gb,
                    days_valid=duration_months * 30,
                )
                vless_client_id = result.get("id")
                vless_link = result.get("link")
            except Exception as e:
                logger.error(f"Failed to create x-ui client: {e}")
                raise ValueError(f"Ошибка при создании VPN подключения: {e}")

            # Обновление подписки в базе данных
            with get_connection(cfg.database.db_path) as conn:
                cursor = conn.cursor()

                if is_new_subscription:
                    # Новая подписка
                    cursor.execute('''
                        UPDATE users 
                        SET 
                            pay_subscribed = 1,
                            subscription_end = DATE('now', ?),
                            renewal_used = 0,
                            vless_client_id = ?,
                            vless_link = ?
                        WHERE user_id = ?
                    ''', (f"+{duration_months} months", vless_client_id, vless_link, user_id))
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
@dp.message(Command("invite"))
async def handle_open_invite(message_or_callback: Message | CallbackQuery):
    if isinstance(message_or_callback, CallbackQuery):
        message = message_or_callback.message
        await message_or_callback.answer()
    else:
        message = message_or_callback
    
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
            await message.answer("❌ Сначала запустите бота через /start")
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
        f"👥 Приглашено друзей: <i>{referral_count}</i>\n"
        f"За каждого друга вы получаете +5 дней VPN, а друг получает +3 дня!"
    )

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
    await callback.message.edit_text(
        "👋 Главное меню",
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

    await message.answer(
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
        "/invite - Пригласи друга\n",
        reply_markup=report_button,
        parse_mode="HTML"
    )

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
