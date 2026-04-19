"""OTA API 加密與請求。

逆向自 dg7.java, RSAUtils.java, AESUtils.java。
Headers: appSdkRule (RSA encrypt "appSdk2025"), appSdkAgent (AES encrypt payload)
"""

from __future__ import annotations

import base64
import json
import time
import urllib.request
from typing import Any, Optional

# RSA 公鑰 (X509, 用於 encryptHeader) - 來自 RSAUtils.java
RSA_PUBLIC_KEY_B64 = (
    "MIHNMA0GCSqGSIb3DQEBAQUAA4G7ADCBtwKBrwzT77v1XOcdhRNpxIwnItPNSWwrioUnfJX+kMw8Jdke/OJMoiwDNfsyS0ZIkhstIUaueNgRbuXC9+MmcZRm275fhZ/zWuOQ/cSMaYNXmT9Ttfl+dZkpKabsVrtQFXKlNKcn8JJi7zkJErh66xJx1gOAA+XkcX/Te11VO41Zm26+YYgiDvCTJ5vSkdqfk/OouWWMM9jEzrxzxJSzAPOEY5OkUPA8pdLU0G0JUKBdAQkCAwEAAQ=="
)

# AES 金鑰 (16 字元)
AES_KEY = "ge48cs8b9dcfe4ab"


def encrypt_header(value: str = "appSdk2025") -> str:
    """RSA 加密 header 值。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.backends import default_backend

    key_bytes = base64.b64decode(RSA_PUBLIC_KEY_B64)
    public_key = serialization.load_der_public_key(key_bytes, default_backend())
    encrypted = public_key.encrypt(value.encode("utf-8"), padding.PKCS1v15())
    return base64.b64encode(encrypted).decode("ascii")


def encrypt_agent_payload() -> str:
    """AES 加密 appSdkAgent payload。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend

    payload = json.dumps({
        "timeStamp": str(int(time.time())),
        "saltr": "appSdkSaltr",
    })
    key = AES_KEY.encode("utf-8")
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    encryptor = cipher.encryptor()
    pad_len = 16 - (len(payload.encode("utf-8")) % 16)
    padded = payload.encode("utf-8") + bytes([pad_len] * pad_len)
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(encrypted).decode("ascii")


def build_ota_headers() -> dict[str, str]:
    """建立 OTA API 所需 headers。"""
    return {
        "appSdkRule": encrypt_header(),
        "appSdkAgent": encrypt_agent_payload(),
        "Content-Type": "application/json",
    }


def ota_check_url(ota_model: str = "dev") -> str:
    """OTA 檢查 API URL。"""
    return f"https://www.zgntec.com/hy-cloud/app/{ota_model}/upgrade"


def ota_check_payload(serial: str | int, version: str) -> dict[str, str]:
    """OTA 檢查請求 body。model 為 device_serial_num (bind 回應)。"""
    return {
        "model": str(serial),
        "type": "OTA",
        "version": version,
    }


def ota_check(serial: str | int, version: str, ota_model: str = "dev") -> dict[str, Any]:
    """發送 OTA 檢查請求，回傳解析後結果。"""
    url = ota_check_url(ota_model)
    headers = build_ota_headers()
    body = json.dumps(ota_check_payload(serial, version)).encode("utf-8")

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    code = str(data.get("code", ""))
    msg = str(data.get("msg", ""))
    body_data = data.get("body") or {}

    return {
        "code": code,
        "msg": msg,
        "body": {
            "download_address": body_data.get("download_address", ""),
            "upgrade_version": body_data.get("upgrade_version", ""),
            "upgrade_size": body_data.get("upgrade_size", ""),
            "upgrade_content": body_data.get("upgrade_content", ""),
            "upgrade_title": body_data.get("upgrade_title", ""),
        },
    }


def download_firmware(url: str, timeout: float = 60.0) -> bytes:
    """從 download_address 下載韌體。"""
    req = urllib.request.Request(url, headers={"User-Agent": "ZRun/2.1.7"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()
