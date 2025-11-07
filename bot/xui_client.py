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

        import datetime
        now_dt = datetime.datetime.now()
        expiry_time = int(now_dt.timestamp() * 1000) + (86400000 * (days_valid or 30))
        client_uuid = str(uuid.uuid4())
        email = f"tg_{telegram_user_id}_{int(time.time())}@xui"

        # Формируем клиента как в вашем рабочем скрипте
        # Если traffic_gb is None или 0, то безлимит (0 байт = безлимит в x-ui)
        total_gb_bytes = 0 if (traffic_gb is None or traffic_gb == 0) else traffic_gb * 1073741824
        
        client_dict = {
            "id": client_uuid,
            "email": email,
            "alterId": 64,  # default for vless (можно поменять)
            "limitIp": 3,    # примерная квота (можно поменять)
            "totalGB": total_gb_bytes,
            "expiryTime": expiry_time,
            "enable": True,
            "tgId": email,
            "subId": "",
            "flow": "xtls-rprx-vision",
        }
        inbound_id = self.cfg.inbound_id
        payload = {
            "id": inbound_id,
            "settings": json.dumps({"clients": [client_dict]})
        }
        headers = {"Content-Type": "application/json", **self._auth_headers()}
        endpoint = "/panel/api/inbounds/addClient"
        print(f"[xui] POST {endpoint} payload: {payload}")
        resp = self._client.post(endpoint, json=payload, headers=headers)
        print(f"[xui] Status={resp.status_code} Response={resp.text}")
        if resp.status_code != 200:
            raise RuntimeError(f"addClient failed: {resp.status_code} {resp.text}")
        data = resp.json()
        if not data.get("success", True):
            raise RuntimeError(f"addClient error: {data}")

        # Теперь получим данные inbound через лист (для формирования корректной ссылки)
        inbs = self._client.get("/panel/api/inbounds/list").json().get("obj", [])
        chosen=None
        for i in inbs:
            if i.get('id') == inbound_id:
                chosen=i
                break
        if not chosen:
            raise RuntimeError('Не найден inbound для формирования ссылки')
        port = chosen.get('port') or 'PORT'
        stream_settings = json.loads(chosen.get('streamSettings', '{}'))
        reality_settings = stream_settings.get('realitySettings') or {}
        
        # Извлекаем параметры REALITY
        pbk = ''
        sid = ''
        sni = 'google.com'
        if reality_settings:
            pbk = reality_settings.get('settings', {}).get('publicKey', '')
            sid = reality_settings.get('shortId', '')
            sni = reality_settings.get('serverNames', [])
            if isinstance(sni, list) and len(sni) > 0:
                sni = sni[0]
            elif not sni or sni == []:
                sni = 'google.com'
        
        # Получаем IP сервера из настроек inbound или из base_url
        server_ip = chosen.get('listen') or ''
        if not server_ip or server_ip == '0.0.0.0' or server_ip == '':
            # Берем IP из base_url
            server_ip = self.cfg.base_url.split('//')[-1].split(':')[0]
        
        # Формируем ссылку vless в правильном порядке параметров
        link = f"vless://{client_uuid}@{server_ip}:{port}/?type=tcp&encryption=none&security=reality"
        if pbk:
            link += f"&pbk={pbk}"
        link += "&fp=chrome"
        link += f"&sni={sni}"
        link += f"&sid={sid if sid else '3d'}"
        link += "&spx=%2F&flow=xtls-rprx-vision"
        link += f"#{display_name or email.split('@')[0]}"

        return {
            "email": email,
            "id": client_uuid,
            "expires_ms": expiry_time,
            "traffic_gb": traffic_gb or 0,
            "link": link,
        }


