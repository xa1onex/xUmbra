import asyncio
import logging
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message

from .config import load_config
from .xui_client import XUIClient


logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")


@asynccontextmanager
async def lifespan(_: Dispatcher):
    yield


def create_dp(cfg) -> Dispatcher:
    dp = Dispatcher(lifespan=lifespan)
    xui = XUIClient(cfg.xui)

    @dp.message(CommandStart())
    async def on_start(msg: Message):
        await msg.answer(
            "Привет! Я выдам тебе VLESS ссылку. Используй команду /vless для получения.\n"
            "Например: /vless 30 30 — трафик 30 ГБ на 30 дней."
        )

    @dp.message(Command("vless"))
    async def on_vless(msg: Message):
        args = (msg.text or "").split()[1:]
        try:
            traffic_gb = int(args[0]) if len(args) >= 1 else 30
            days_valid = int(args[1]) if len(args) >= 2 else 30
        except ValueError:
            await msg.answer("Неверные аргументы. Пример: /vless 30 30")
            return

        await msg.answer("Генерирую ссылку, подождите…")
        try:
            result = xui.add_vless_client(
                telegram_user_id=msg.from_user.id,
                display_name=(msg.from_user.username or str(msg.from_user.id)),
                traffic_gb=traffic_gb,
                days_valid=days_valid,
            )
        except Exception as e:
            logging.exception("Failed to create vless client")
            await msg.answer(f"Ошибка при создании пользователя: {e}")
            return

        link = result["link"]
        await msg.answer(
            "Готово! Твоя VLESS ссылка:\n"
            f"{link}\n\n"
            "Подключайся в своем клиенте (v2ray, sing-box и т.п.)."
        )

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


