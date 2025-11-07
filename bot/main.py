import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from .config import load_config
from .xui_client import XUIClient
from .database import Database
from .subscription_service import SubscriptionService, SubscriptionPlan

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


class PaymentStates(StatesGroup):
    waiting_amount = State()
    waiting_payment_confirmation = State()


@asynccontextmanager
async def lifespan(dp: Dispatcher):
    # Cleanup on shutdown if needed
    yield


def get_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📦 Моя подписка"), KeyboardButton(text="💰 Баланс")],
            [KeyboardButton(text="🛒 Купить подписку"), KeyboardButton(text="🎁 Инвайт")],
            [KeyboardButton(text="📊 Статистика")],
        ],
        resize_keyboard=True,
    )


def get_plans_keyboard() -> InlineKeyboardMarkup:
    plans = SubscriptionService.PLANS
    buttons = []
    for i, plan in enumerate(plans):
        buttons.append([
            InlineKeyboardButton(
                text=f"{plan.name} - {plan.price}₽",
                callback_data=f"plan_{i}"
            )
        ])
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh_sub")],
            [InlineKeyboardButton(text="🛒 Купить подписку", callback_data="buy_sub")],
        ]
    )


def get_payment_keyboard(payment_id: int, amount: float) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить оплату", callback_data=f"confirm_pay_{payment_id}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_payment")],
        ]
    )


def create_dp(cfg) -> Dispatcher:
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage, lifespan=lifespan)
    dp["config"] = cfg
    
    # Initialize database and services BEFORE registering handlers
    db = Database(cfg.database.db_path)
    xui = XUIClient(cfg.xui)
    sub_service = SubscriptionService(db, xui)
    
    dp["db"] = db
    dp["xui"] = xui
    dp["sub_service"] = sub_service

    @dp.message(CommandStart())
    async def on_start(msg: Message, state: FSMContext):
        await state.clear()
        args = msg.text.split()[1:] if len(msg.text.split()) > 1 else []
        
        db: Database = dp["db"]
        user_id = msg.from_user.id
        username = msg.from_user.username
        full_name = msg.from_user.get_full_name()
        
        # Check for invite code
        referrer_id = None
        if args:
            invite_code = args[0]
            # Try to use invite code
            if db.use_invite_code(invite_code, user_id):
                # Get referrer from invite
                user = db.get_user(user_id)
                if user and user.referrer_id:
                    referrer_id = user.referrer_id
                    # Add bonus to referrer
                    db.update_user_balance(user.referrer_id, cfg.payment.referral_bonus)
                    await msg.bot.send_message(
                        user.referrer_id,
                        f"🎉 Ваш реферал зарегистрировался! Вы получили {cfg.payment.referral_bonus}₽ бонуса."
                    )
        
        # Get or create user
        user = db.get_or_create_user(user_id, username, full_name, referrer_id)
        
        welcome_text = f"""👋 <b>Добро пожаловать в VPN бот!</b>

🔐 Безопасный и быстрый VPN
🌐 Обход блокировок
⚡ Высокая скорость

Используйте меню для управления подпиской."""
        
        if referrer_id:
            welcome_text += f"\n\n🎁 Вы зарегистрировались по реферальной ссылке!"
        
        await msg.answer(welcome_text, reply_markup=get_main_keyboard(), parse_mode="HTML")

    @dp.message(F.text == "📦 Моя подписка")
    @dp.message(Command("subscription"))
    async def on_subscription(msg: Message):
        db: Database = dp["db"]
        sub_service: SubscriptionService = dp["sub_service"]
        
        user_id = msg.from_user.id
        info_text = sub_service.format_subscription_message(user_id)
        
        await msg.answer(
            info_text,
            reply_markup=get_subscription_keyboard(),
            parse_mode="HTML"
        )

    @dp.message(F.text == "🛒 Купить подписку")
    @dp.message(Command("buy"))
    async def on_buy(msg: Message):
        plans = SubscriptionService.PLANS
        plans_text = "🛒 <b>Доступные тарифы:</b>\n\n"
        for i, plan in enumerate(plans):
            traffic_info = "♾️ Безлимит" if plan.traffic_gb == 0 else f"{plan.traffic_gb} ГБ"
            plans_text += f"{i+1}. <b>{plan.name}</b>\n"
            plans_text += f"   📊 Трафик: {traffic_info}\n"
            plans_text += f"   ⏱ Срок: {plan.days} дней\n"
            plans_text += f"   💰 Цена: {plan.price}₽\n\n"
        
        await msg.answer(
            plans_text,
            reply_markup=get_plans_keyboard(),
            parse_mode="HTML"
        )

    @dp.callback_query(F.data.startswith("plan_"))
    async def on_plan_selected(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        plan_index = int(callback.data.split("_")[1])
        sub_service: SubscriptionService = dp["sub_service"]
        db: Database = dp["db"]
        
        plan = sub_service.get_plan_by_index(plan_index)
        if not plan:
            await callback.message.answer("❌ План не найден")
            return
        
        user_id = callback.from_user.id
        user = db.get_user(user_id)
        
        if not user:
            await callback.message.answer("❌ Пользователь не найден")
            return
        
        # Check if user has enough balance
        if user.balance < plan.price:
            needed = plan.price - user.balance
            await callback.message.answer(
                f"❌ Недостаточно средств на балансе.\n\n"
                f"💰 Ваш баланс: {user.balance}₽\n"
                f"💵 Нужно: {plan.price}₽\n"
                f"💸 Не хватает: {needed}₽\n\n"
                f"Пополните баланс через команду /balance или /payment",
                parse_mode="HTML"
            )
            return
        
        # Check if user already has active subscription
        active_sub = db.get_user_active_subscription(user_id)
        if active_sub:
            await callback.message.answer(
                "❌ У вас уже есть активная подписка. Дождитесь её окончания или отмените текущую.",
                parse_mode="HTML"
            )
            return

        # Create subscription
        try:
            subscription, vless_link = sub_service.create_subscription_for_user(
                user_id=user_id,
                plan=plan,
                username=callback.from_user.username,
            )
            
            # Deduct from balance
            db.update_user_balance(user_id, -plan.price)
            
            # Create payment record
            db.create_payment(
                user_id=user_id,
                amount=plan.price,
                subscription_id=subscription.id,
                payment_method="balance",
            )
            
            success_text = f"""✅ <b>Подписка успешно активирована!</b>

📦 План: {plan.name}
📊 Трафик: {"Безлимит" if plan.traffic_gb == 0 else f"{plan.traffic_gb} ГБ"}
⏱ Срок: {plan.days} дней
💰 Списано: {plan.price}₽

🔗 <b>Ваша VPN ссылка:</b>
<code>{vless_link}</code>

📱 <b>Как использовать:</b>
1. Скачайте приложение (v2rayNG, sing-box и т.п.)
2. Импортируйте ссылку
3. Подключитесь!

💡 Сохраните ссылку в безопасном месте."""
            
            await callback.message.answer(success_text, parse_mode="HTML")
            
        except Exception as e:
            logger.exception("Failed to create subscription")
            await callback.message.answer(
                f"❌ Ошибка при создании подписки: {e}\n\nПопробуйте позже или свяжитесь с поддержкой."
            )

    @dp.message(F.text == "💰 Баланс")
    @dp.message(Command("balance"))
    async def on_balance(msg: Message):
        db: Database = dp["db"]
        user_id = msg.from_user.id
        user = db.get_user(user_id)
        
        if not user:
            await msg.answer("❌ Пользователь не найден")
            return

        balance_text = f"""💰 <b>Ваш баланс</b>

💵 Текущий баланс: <b>{user.balance}₽</b>

💡 Пополните баланс через команду /payment"""
        
        await msg.answer(balance_text, parse_mode="HTML")

    @dp.message(Command("payment"))
    async def on_payment(msg: Message, state: FSMContext):
        await state.set_state(PaymentStates.waiting_amount)
        await msg.answer(
            "💳 <b>Пополнение баланса</b>\n\n"
            "Введите сумму для пополнения (минимум 100₽):",
            parse_mode="HTML"
        )

    @dp.message(PaymentStates.waiting_amount)
    async def on_payment_amount(msg: Message, state: FSMContext):
        try:
            amount = float(msg.text)
            if amount < dp["config"].payment.min_payment:
                await msg.answer(
                    f"❌ Минимальная сумма пополнения: {dp['config'].payment.min_payment}₽"
                )
                return
            
            db: Database = dp["db"]
            payment = db.create_payment(
                user_id=msg.from_user.id,
                amount=amount,
                payment_method="manual",
            )
            
            payment_info = f"""💳 <b>Платеж создан</b>

💰 Сумма: {amount}₽
🆔 ID платежа: {payment.id}

⚠️ <b>ВНИМАНИЕ:</b>
Для завершения платежа свяжитесь с администратором.

После подтверждения оплаты баланс будет пополнен автоматически."""
            
            await msg.answer(payment_info, parse_mode="HTML")
            
            # Notify admin
            for admin_id in dp["config"].bot.admin_ids:
                try:
                    await msg.bot.send_message(
                        admin_id,
                        f"💳 Новый платеж:\n"
                        f"👤 Пользователь: @{msg.from_user.username or msg.from_user.id}\n"
                        f"🆔 ID: {msg.from_user.id}\n"
                        f"💰 Сумма: {amount}₽\n"
                        f"🆔 ID платежа: {payment.id}",
                        reply_markup=get_payment_keyboard(payment.id, amount),
                    )
                except Exception as e:
                    logger.error(f"Failed to notify admin {admin_id}: {e}")
            
            await state.clear()
            
        except ValueError:
            await msg.answer("❌ Неверный формат суммы. Введите число (например: 500)")

    @dp.callback_query(F.data.startswith("confirm_pay_"))
    async def on_confirm_payment(callback: CallbackQuery):
        await callback.answer()
        payment_id = int(callback.data.split("_")[2])
        db: Database = dp["db"]
        
        # Get payment
        with db.get_connection() as conn:
            cursor = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,))
            row = cursor.fetchone()
            if not row:
                await callback.message.answer("❌ Платеж не найден")
                return
            
            if row["status"] == "completed":
                await callback.message.answer("⚠️ Платеж уже обработан")
                return
            
            # Complete payment
            db.complete_payment(payment_id)
            db.update_user_balance(row["user_id"], row["amount"])
            
            await callback.message.answer(f"✅ Платеж #{payment_id} подтвержден. Баланс пополнен.")
            
            # Notify user
            try:
                await callback.bot.send_message(
                    row["user_id"],
                    f"✅ Ваш платеж #{payment_id} подтвержден!\n\n"
                    f"💰 Пополнено: {row['amount']}₽\n"
                    f"💵 Текущий баланс: {db.get_user(row['user_id']).balance}₽"
                )
            except Exception as e:
                logger.error(f"Failed to notify user: {e}")

    @dp.message(F.text == "🎁 Инвайт")
    @dp.message(Command("invite"))
    async def on_invite(msg: Message):
        db: Database = dp["db"]
        user_id = msg.from_user.id
        invite_code = db.get_user_invite_code(user_id)
        user = db.get_user(user_id)
        
        bot_username = (await msg.bot.get_me()).username
        invite_link = f"https://t.me/{bot_username}?start={invite_code}"
        
        invite_text = f"""🎁 <b>Реферальная программа</b>

🔗 <b>Ваша реферальная ссылка:</b>
<code>{invite_link}</code>

📋 <b>Ваш инвайт-код:</b>
<code>{invite_code}</code>

👥 Приглашено пользователей: {user.invited_count if user else 0}
💰 Бонус за каждого: {dp['config'].payment.referral_bonus}₽

💡 Поделитесь ссылкой с друзьями и получайте бонусы!"""
        
        await msg.answer(invite_text, parse_mode="HTML")

    @dp.message(F.text == "📊 Статистика")
    @dp.message(Command("stats"))
    async def on_stats(msg: Message):
        db: Database = dp["db"]
        user_id = msg.from_user.id
        user = db.get_user(user_id)
        
        if not user:
            await msg.answer("❌ Пользователь не найден")
            return
        
        subscriptions = db.get_user_subscriptions(user_id)
        active_sub = db.get_user_active_subscription(user_id)
        
        stats_text = f"""📊 <b>Ваша статистика</b>

👤 ID: {user_id}
💰 Баланс: {user.balance}₽
👥 Приглашено: {user.invited_count}
📦 Всего подписок: {len(subscriptions)}
✅ Активных: {1 if active_sub else 0}

📅 Регистрация: {user.created_at.strftime('%d.%m.%Y') if user.created_at else 'N/A'}"""
        
        await msg.answer(stats_text, parse_mode="HTML")

    @dp.message(Command("admin"))
    async def on_admin(msg: Message):
        if msg.from_user.id not in dp["config"].bot.admin_ids:
            await msg.answer("❌ У вас нет доступа к админ-панели")
            return
        
        db: Database = dp["db"]
        users = db.get_all_users()
        
        total_users = len(users)
        total_balance = sum(u.balance for u in users)
        total_referrals = sum(u.invited_count for u in users)
        
        admin_text = f"""👨‍💼 <b>Админ-панель</b>

👥 Всего пользователей: {total_users}
💰 Общий баланс: {total_balance}₽
🎁 Всего рефералов: {total_referrals}"""
        
        await msg.answer(admin_text, parse_mode="HTML")

    @dp.callback_query(F.data == "refresh_sub")
    async def on_refresh_sub(callback: CallbackQuery):
        await callback.answer("Обновлено")
        db: Database = dp["db"]
        sub_service: SubscriptionService = dp["sub_service"]
        
        user_id = callback.from_user.id
        info_text = sub_service.format_subscription_message(user_id)
        
        await callback.message.edit_text(
            info_text,
            reply_markup=get_subscription_keyboard(),
            parse_mode="HTML"
        )

    @dp.callback_query(F.data == "buy_sub")
    async def on_buy_sub_callback(callback: CallbackQuery):
        await callback.answer()
        plans = SubscriptionService.PLANS
        plans_text = "🛒 <b>Доступные тарифы:</b>\n\n"
        for i, plan in enumerate(plans):
            traffic_info = "♾️ Безлимит" if plan.traffic_gb == 0 else f"{plan.traffic_gb} ГБ"
            plans_text += f"{i+1}. <b>{plan.name}</b>\n"
            plans_text += f"   📊 Трафик: {traffic_info}\n"
            plans_text += f"   ⏱ Срок: {plan.days} дней\n"
            plans_text += f"   💰 Цена: {plan.price}₽\n\n"
        
        await callback.message.answer(
            plans_text,
            reply_markup=get_plans_keyboard(),
            parse_mode="HTML"
        )

    @dp.callback_query(F.data == "cancel")
    async def on_cancel(callback: CallbackQuery):
        await callback.answer("Отменено")
        await callback.message.delete()

    @dp.callback_query(F.data == "cancel_payment")
    async def on_cancel_payment(callback: CallbackQuery):
        await callback.answer("Отменено")

    return dp


async def main() -> None:
    cfg = load_config()
    bot = Bot(cfg.bot.bot_token)
    dp = create_dp(cfg)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
