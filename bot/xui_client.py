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
    def __init__(self, cfg: XUIConfig = None, base_url: str = None, username: str = None, 
                 password: str = None, api_token: str = None, inbound_id: int = None) -> None:
        """Инициализация клиента. Можно использовать cfg или отдельные параметры сервера"""
        if cfg:
            self.base_url = cfg.base_url
            # Убеждаемся, что base_url заканчивается на /
            if self.base_url and not self.base_url.endswith('/'):
                self.base_url += '/'
            self.username = cfg.username
            self.password = cfg.password
            self.api_token = cfg.api_token
            self.inbound_id = cfg.inbound_id
        else:
            if not base_url:
                raise ValueError("Either cfg or base_url must be provided")
            # Сохраняем base_url как есть (он уже должен заканчиваться на /)
            # Не добавляем лишний /, чтобы не нарушить путь, если он уже есть
            self.base_url = base_url if base_url else None
            # Если base_url не заканчивается на /, добавляем его
            if self.base_url and not self.base_url.endswith('/'):
                self.base_url += '/'
            self.username = username
            self.password = password
            self.api_token = api_token
            self.inbound_id = inbound_id
        
        # Создаем HTTP клиент
        # httpx автоматически обрабатывает пути в base_url
        # Например: base_url="http://host:port/path/" + endpoint="/panel/api/..." 
        # даст "http://host:port/path/panel/api/..."
        self._client = httpx.Client(
            base_url=self.base_url, 
            timeout=20.0, 
            verify=False,
            follow_redirects=True
        )
        self._authorized = False

    def _auth_headers(self) -> dict[str, str]:
        if self.api_token:
            return {"Authorization": f"Bearer {self.api_token}"}
        return {}

    def login(self) -> None:
        if self.api_token:
            self._authorized = True
            return
        if not (self.username and self.password):
            raise RuntimeError("Either API_TOKEN or USERNAME/PASSWORD must be provided")
        # Try common 3x-ui login route
        # Путь /login будет автоматически добавлен к base_url
        # Например: http://host:port/path/ + /login = http://host:port/path/login
        try:
            resp = self._client.post(
                "login",  # Без начального /, чтобы httpx правильно объединил с base_url
                data={"username": self.username, "password": self.password}
            )
            if resp.status_code not in (200, 302):
                raise RuntimeError(f"x-ui login failed: {resp.status_code} {resp.text}")
            self._authorized = True
        except httpx.ConnectError as e:
            raise RuntimeError(f"Connection error: {e}")
        except httpx.TimeoutException as e:
            raise RuntimeError(f"Connection timeout: {e}")
        except Exception as e:
            raise RuntimeError(f"Login error: {e}")

    def ensure_login(self) -> None:
        if not self._authorized:
            self.login()

    def add_vless_client(
        self,
        telegram_user_id: int,
        display_name: str,
        traffic_gb: int | None = 30,
        days_valid: int | None = 30,
        expiry_time_unix_ms: int | None = None,
    ) -> dict[str, Any]:
        self.ensure_login()

        import datetime
        # Если передан expiry_time_unix_ms, используем его, иначе вычисляем из days_valid
        if expiry_time_unix_ms is not None:
            expiry_time = expiry_time_unix_ms
        else:
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
        inbound_id = self.inbound_id
        if not inbound_id:
            raise RuntimeError("inbound_id is not set")
        payload = {
            "id": inbound_id,
            "settings": json.dumps({"clients": [client_dict]})
        }
        headers = {"Content-Type": "application/json", **self._auth_headers()}
        # Используем путь без начального /, чтобы httpx правильно объединил с base_url
        endpoint = "panel/api/inbounds/addClient"
        print(f"[xui] POST {self.base_url}{endpoint} payload: {payload}")
        resp = self._client.post(endpoint, json=payload, headers=headers)
        print(f"[xui] Status={resp.status_code} Response={resp.text}")
        if resp.status_code != 200:
            raise RuntimeError(f"addClient failed: {resp.status_code} {resp.text}")
        data = resp.json()
        if not data.get("success", True):
            raise RuntimeError(f"addClient error: {data}")

        # Теперь получим данные inbound через лист (для формирования корректной ссылки)
        inbs = self._client.get("panel/api/inbounds/list").json().get("obj", [])
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
        
        # Извлекаем параметры REALITY из настроек панели
        pbk = ''
        sid = ''
        sni = 'google.com'
        fp = 'chrome'  # дефолтное значение
        
        if reality_settings:
            # Получаем settings - может быть объектом или строкой JSON
            settings = reality_settings.get('settings', {})
            if isinstance(settings, str):
                try:
                    settings = json.loads(settings)
                except:
                    settings = {}
            elif not isinstance(settings, dict):
                settings = {}
            
            # Получаем publicKey из settings
            pbk = settings.get('publicKey', '')
            
            # Получаем shortId (sid) - может быть в realitySettings.shortId, shortIds или settings.shortId
            sid = reality_settings.get('shortId', '')
            if not sid:
                # Пробуем получить из shortIds (массив)
                short_ids = reality_settings.get('shortIds', [])
                if isinstance(short_ids, list) and len(short_ids) > 0:
                    sid = short_ids[0]
                elif isinstance(short_ids, str):
                    sid = short_ids
                # Пробуем получить из settings
                if not sid:
                    sid = settings.get('shortId', '')
                    if not sid:
                        short_ids = settings.get('shortIds', [])
                        if isinstance(short_ids, list) and len(short_ids) > 0:
                            sid = short_ids[0]
                        elif isinstance(short_ids, str):
                            sid = short_ids
            
            # Получаем serverNames (sni) - может быть массивом или строкой
            sni_list = reality_settings.get('serverNames', [])
            if isinstance(sni_list, str):
                try:
                    sni_list = json.loads(sni_list)
                except:
                    sni_list = [sni_list] if sni_list else []
            if isinstance(sni_list, list) and len(sni_list) > 0:
                sni = sni_list[0]
            elif isinstance(sni_list, str) and sni_list:
                sni = sni_list
            elif not sni_list or sni_list == []:
                sni = 'google.com'
            
            # Получаем fingerprints (fp) - может быть в settings.fingerprints или realitySettings.fingerprints
            fingerprints = settings.get('fingerprints', [])
            if not fingerprints:
                fingerprints = reality_settings.get('fingerprints', [])
            
            if isinstance(fingerprints, str):
                try:
                    fingerprints = json.loads(fingerprints)
                except:
                    fingerprints = [fingerprints] if fingerprints else []
            
            if isinstance(fingerprints, list) and len(fingerprints) > 0:
                fp = fingerprints[0]
            elif isinstance(fingerprints, str) and fingerprints:
                fp = fingerprints
            else:
                fp = 'chrome'  # дефолт если не найдено
        
        # Получаем IP сервера из настроек inbound или из base_url
        server_ip = chosen.get('listen') or ''
        if not server_ip or server_ip == '0.0.0.0' or server_ip == '':
            # Берем IP/домен из base_url
            # Убираем протокол и путь
            url_part = self.base_url.split('//')[-1].split('/')[0]
            # Извлекаем хост (без порта)
            server_ip = url_part.split(':')[0]
        
        # Формируем ссылку vless в правильном порядке параметров
        link = f"vless://{client_uuid}@{server_ip}:{port}/?type=tcp&encryption=none&security=reality"
        if pbk:
            link += f"&pbk={pbk}"
        link += f"&fp={fp}"
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

    def delete_client(self, client_id: str) -> None:
        """Удаляет клиента из inbound на панели x-ui/3x-ui"""
        self.ensure_login()
        
        inbound_id = self.inbound_id
        if not inbound_id:
            raise RuntimeError("inbound_id is not set")
        
        # Получаем текущий inbound
        inbs = self._client.get("panel/api/inbounds/list").json().get("obj", [])
        chosen = None
        for i in inbs:
            if i.get('id') == inbound_id:
                chosen = i
                break
        
        if not chosen:
            raise RuntimeError(f'Не найден inbound с ID {inbound_id}')
        
        # Получаем текущие настройки клиентов
        settings_str = chosen.get('settings', '{}')
        try:
            if isinstance(settings_str, str):
                settings = json.loads(settings_str)
            else:
                settings = settings_str
        except:
            settings = {}
        
        clients = settings.get('clients', [])
        if not isinstance(clients, list):
            clients = []
        
        # Удаляем клиента из списка
        original_count = len(clients)
        clients = [c for c in clients if c.get('id') != client_id]
        
        if len(clients) == original_count:
            # Клиент не найден в списке - возможно уже удален
            print(f"[xui] Client {client_id} not found in clients list, may already be deleted")
            return
        
        # Обновляем settings с новым списком клиентов
        settings['clients'] = clients
        updated_settings = json.dumps(settings)
        
        # Формируем payload для обновления inbound
        # Включаем только необходимые поля, исключая статистику (up, down, total, allTime, clientStats)
        required_fields = ['id', 'settings', 'streamSettings', 'sniffing', 'protocol', 
                          'port', 'listen', 'remark', 'enable', 'expiryTime', 
                          'trafficReset', 'lastTrafficResetTime', 'tag']
        payload = {}
        for key in required_fields:
            if key in chosen:
                if key == 'settings':
                    payload[key] = updated_settings
                else:
                    payload[key] = chosen[key]
        
        headers = {"Content-Type": "application/json", **self._auth_headers()}
        
        # Пробуем endpoint update с ID в пути через POST (работает на этой панели)
        try:
            endpoint = f"panel/api/inbounds/update/{inbound_id}"
            print(f"[xui] POST {self.base_url}{endpoint} payload: {payload}")
            resp = self._client.post(endpoint, json=payload, headers=headers)
            print(f"[xui] Status={resp.status_code} Response={resp.text}")
            
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success", True):
                    print(f"[xui] Successfully deleted client {client_id} using update/{inbound_id}")
                    return
        except Exception as e:
            print(f"[xui] update/{inbound_id} failed: {e}")
        
        # Пробуем endpoint updateAll (обычно работает в 3x-ui)
        try:
            payload_all = [payload]
            endpoint = "panel/api/inbounds/updateAll"
            print(f"[xui] POST {self.base_url}{endpoint} payload: {payload_all}")
            resp = self._client.post(endpoint, json=payload_all, headers=headers)
            print(f"[xui] Status={resp.status_code} Response={resp.text}")
            
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success", True):
                    print(f"[xui] Successfully deleted client {client_id} using updateAll")
                    return
        except Exception as e:
            print(f"[xui] updateAll failed: {e}")
        
        # Пробуем endpoint update без ID в пути
        try:
            endpoint = "panel/api/inbounds/update"
            print(f"[xui] POST {self.base_url}{endpoint} payload: {payload}")
            resp = self._client.post(endpoint, json=payload, headers=headers)
            print(f"[xui] Status={resp.status_code} Response={resp.text}")
            
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success", True):
                    print(f"[xui] Successfully deleted client {client_id} using update")
                    return
        except Exception as e:
            print(f"[xui] update failed: {e}")
        
        # Пробуем endpoint update с PUT методом
        try:
            endpoint = f"panel/api/inbounds/{inbound_id}"
            print(f"[xui] PUT {self.base_url}{endpoint} payload: {payload}")
            resp = self._client.put(endpoint, json=payload, headers=headers)
            print(f"[xui] Status={resp.status_code} Response={resp.text}")
            
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success", True):
                    print(f"[xui] Successfully deleted client {client_id} using PUT /{inbound_id}")
                    return
        except Exception as e:
            print(f"[xui] PUT /{inbound_id} failed: {e}")
        
        # Если ничего не помогло, пробуем через delClient
        try:
            del_payload = {
                "id": inbound_id,
                "settings": json.dumps({"clients": [{"id": client_id}]})
            }
            endpoint = "panel/api/inbounds/delClient"
            print(f"[xui] POST {self.base_url}{endpoint} payload: {del_payload}")
            resp = self._client.post(endpoint, json=del_payload, headers=headers)
            print(f"[xui] Status={resp.status_code} Response={resp.text}")
            
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success", True):
                    print(f"[xui] Successfully deleted client {client_id} using delClient")
                    return
        except Exception as e:
            print(f"[xui] delClient failed: {e}")
        
        # Если все методы не сработали, просто логируем ошибку, но не падаем
        # (ключ все равно удалится из БД)
        print(f"[xui] WARNING: Could not delete client {client_id} from server, but will continue with DB deletion")


