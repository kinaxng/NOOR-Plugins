from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Any

from app.plugins.secrets import plugin_secret_store

from .client import AUTH_BASE, PLUGIN_ID, QR_STATUS_URL, Client115


def new_verifier() -> str:
    return secrets.token_urlsafe(64)


def code_challenge(verifier: str) -> str:
    return base64.b64encode(hashlib.sha256(verifier.encode()).digest()).decode()


async def start_device_authorization(config: dict[str, Any]) -> dict[str, Any]:
    client_id = str(config.get("client_id") or "").strip()
    if not client_id:
        raise ValueError("请先填写 115 Open Platform Client ID")
    verifier = new_verifier()
    client = Client115(config)
    body = await client._http("POST", f"{AUTH_BASE}/open/authDeviceCode", data={
        "client_id": client_id,
        "code_challenge": code_challenge(verifier),
        "code_challenge_method": "sha256",
    })
    data = client._unwrap(body)
    return {"uid": str(data.get("uid") or ""), "time": str(data.get("time") or ""), "sign": str(data.get("sign") or ""), "qrcode": str(data.get("qrcode") or ""), "verifier": verifier}


async def poll_device_authorization(config: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
    client = Client115(config)
    body = await client._http("GET", QR_STATUS_URL, params={"uid": session["uid"], "time": session["time"], "sign": session["sign"]})
    status_data = client._unwrap(body)
    status = int(status_data.get("status") or 0)
    if status != 2:
        return {"connected": False, "status": status, "message": str(status_data.get("msg") or "等待授权")}
    token_body = await client._http("POST", f"{AUTH_BASE}/open/deviceCodeToToken", data={"uid": session["uid"], "code_verifier": session["verifier"]})
    tokens = client._unwrap(token_body)
    access_token = str(tokens.get("access_token") or "")
    refresh_token = str(tokens.get("refresh_token") or "")
    if not access_token or not refresh_token:
        raise ValueError("115 授权完成但未返回完整 Token")
    plugin_secret_store.set(PLUGIN_ID, "access_token", access_token)
    plugin_secret_store.set(PLUGIN_ID, "refresh_token", refresh_token)
    return {"connected": True, "status": 2, "message": "115 授权成功"}

