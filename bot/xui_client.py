from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from .config import XUIConfig


@dataclass
class VlessClient:
    id: str
    email: str
    flow: str | None = None
    limit_ip: int | None = None
    total_gb: int | None = None  # in GB
    expiry_time_unix_ms: int | None = None

    def to_3xui_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "email": self.email,
            "enable": True,
            "flow": self.flow or "",
            "limitIp": self.limit_ip or 0,
            "totalGB": (self.total_gb or 0) * 1073741824,  # bytes
            "expiryTime": self.expiry_time_unix_ms or 0,
            "subId": "",
            "reset": 0,
        }
        return payload


class XUIClient:
    def __init__(self, cfg: XUIConfig) -> None:
        self.cfg = cfg
        self._client = httpx.Client(base_url=cfg.base_url, timeout=20.0, verify=False)
        self._authorized = False

    def _auth_headers(self) -> dict[str, str]:
        if self.cfg.api_token:
            return {"Authorization": f"Bearer {self.cfg.api_token}"}
        return {}

    def login(self) -> None:
        if self.cfg.api_token:
            self._authorized = True
            return
        if not (self.cfg.username and self.cfg.password):
            raise RuntimeError("Either XUI_API_TOKEN or XUI_USERNAME/XUI_PASSWORD must be provided")
        # Try common 3x-ui login route
        resp = self._client.post(
            "/login", data={"username": self.cfg.username, "password": self.cfg.password}
        )
        if resp.status_code not in (200, 302):
            raise RuntimeError(f"x-ui login failed: {resp.status_code} {resp.text}")
        self._authorized = True

    def ensure_login(self) -> None:
        if not self._authorized:
            self.login()

    def add_vless_client(
        self,
        telegram_user_id: int,
        display_name: str,
        traffic_gb: int | None = 30,
        days_valid: int | None = 30,
    ) -> dict[str, Any]:
        self.ensure_login()

        now_ms = int(time.time() * 1000)
        expiry_ms = now_ms + (days_valid or 0) * 24 * 3600 * 1000
        client_uuid = str(uuid.uuid4())
        email = f"tg_{telegram_user_id}_{int(time.time())}@xui"
        vless_client = VlessClient(
            id=client_uuid,
            email=email,
            flow="",  # set if you use REALITY/XTLS flow
            limit_ip=0,
            total_gb=traffic_gb or 0,
            expiry_time_unix_ms=expiry_ms if days_valid else 0,
        )

        # Common 3x-ui API pattern for adding a client
        # POST /panel/inbound/addClient or /xui/inbound/addClient
        # Some panels expect form data, others JSON.
        payload = {
            "id": self.cfg.inbound_id,
            "settings": json.dumps({"clients": [vless_client.to_3xui_json()]}),
        }

        # Try JSON first
        resp = self._client.post(
            "/panel/inbound/addClient",
            headers={"Content-Type": "application/json", **self._auth_headers()},
            json=payload,
        )
        if resp.status_code == 404:
            # Try alternative path used by some x-ui forks
            resp = self._client.post(
                "/xui/inbound/addClient",
                headers={**self._auth_headers()},
                data=payload,
            )

        if resp.status_code != 200:
            raise RuntimeError(f"addClient failed: {resp.status_code} {resp.text}")

        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else None
        if data and not data.get("success", True):
            raise RuntimeError(f"addClient error: {data}")

        # Build VLESS link (basic form; adjust for your REALITY/WS/GRPC params)
        # Example: vless://<uuid>@host:port?encryption=none&security=none&type=tcp#name
        # For REALITY/XTLS-vision etc., you need to fill params accordingly.
        vless_link = f"vless://{client_uuid}@YOUR_HOST:YOUR_PORT?encryption=none&security=none&type=tcp#{display_name}"

        return {
            "email": email,
            "id": client_uuid,
            "expires_ms": expiry_ms if days_valid else 0,
            "traffic_gb": traffic_gb or 0,
            "link": vless_link,
        }


