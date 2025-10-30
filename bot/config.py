from pydantic import BaseModel, Field
import os
from dotenv import load_dotenv


class XUIConfig(BaseModel):
    base_url: str = Field(..., description="Base URL of x-ui/3x-ui panel, e.g. https://your-host:54321")
    username: str | None = Field(None, description="Panel username (if using session-based login)")
    password: str | None = Field(None, description="Panel password (if using session-based login)")
    api_token: str | None = Field(None, description="Bearer token, if your panel uses token-based auth")
    inbound_id: int = Field(..., description="Inbound ID for VLESS to attach clients to")


class BotConfig(BaseModel):
    bot_token: str
    admin_ids: list[int] = Field(default_factory=list)


class AppConfig(BaseModel):
    bot: BotConfig
    xui: XUIConfig


def load_config() -> AppConfig:
    load_dotenv()
    bot = BotConfig(
        bot_token=os.environ["BOT_TOKEN"],
        admin_ids=[int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()],
    )
    xui = XUIConfig(
        base_url=os.environ["XUI_BASE_URL"].rstrip("/"),
        username=os.getenv("XUI_USERNAME"),
        password=os.getenv("XUI_PASSWORD"),
        api_token=os.getenv("XUI_API_TOKEN"),
        inbound_id=int(os.environ["XUI_INBOUND_ID"]),
    )
    return AppConfig(bot=bot, xui=xui)


