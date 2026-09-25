# -*- coding: utf-8 -*-
"""api.yaml 落库加密（Windows DPAPI，绑定本机当前 Windows 账户）。

磁盘格式：单行 "KVDPAPI1:<base64(DPAPI密文)>"。

- seal_text(text) -> str   明文转密文；已是密文则原样返回
- open_text(text) -> str   密文转明文；输入不是密文格式（明文旧配置）原样返回，
                           平滑迁移：老配置第一次被保存时自动升级为密文
- 解密失败（文件被拷到其他电脑/其他 Windows 账户）抛 KeyvaultError

特点：开机免密（当前用户自动可解）；换电脑/换账户无法解密；
网关进程内存里始终只有明文，明文永不落盘。
"""
from __future__ import annotations

import base64
import binascii
import ctypes
from ctypes import wintypes

_MAGIC = "KVDPAPI1:"


class KeyvaultError(RuntimeError):
    """DPAPI 加解密失败。"""


class _CRYPT_INTEGER_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _dpapi_raw(data: bytes, protect: bool) -> bytes:
    buf_in = ctypes.create_string_buffer(data, len(data))
    blob_in = _CRYPT_INTEGER_BLOB(len(data), ctypes.cast(buf_in, ctypes.POINTER(ctypes.c_char)))
    blob_out = _CRYPT_INTEGER_BLOB()
    crypt32 = ctypes.windll.crypt32
    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(blob_in),
            "myapi-keyvault",
            None, None, None, 0,
            ctypes.byref(blob_out),
        )
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(blob_in),
            None, None, None, None, 0,
            ctypes.byref(blob_out),
        )
    if not ok:
        err = ctypes.GetLastError()
        hint = (
            ""
            if protect
            else " 若该配置文件来自其他电脑或其他 Windows 账户，本机无法解密。"
        )
        raise KeyvaultError(f"DPAPI {'加密' if protect else '解密'}失败 (WinError {err})。{hint}")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def is_sealed(text: str) -> bool:
    return isinstance(text, str) and text.startswith(_MAGIC)


def seal_text(text: str) -> str:
    """明文 -> 密文；已是密文则原样返回（幂等）。"""
    if is_sealed(text):
        return text
    cipher = _dpapi_raw(text.encode("utf-8"), protect=True)
    return _MAGIC + base64.b64encode(cipher).decode("ascii")


def open_text(text: str) -> str:
    """密文 -> 明文；输入不是密文格式则原样返回（兼容明文旧配置）。"""
    if not is_sealed(text):
        return text
    try:
        raw = base64.b64decode(text[len(_MAGIC):].strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise KeyvaultError(f"api.yaml 密文格式损坏: {exc}") from exc
    return _dpapi_raw(raw, protect=False).decode("utf-8")
