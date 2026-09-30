# -*- coding: utf-8 -*-
"""api.yaml 落库加密（双后端，按平台自动选择）。

- Windows：DPAPI，绑定本机当前 Windows 账户，密文格式 "KVDPAPI1:<base64(DPAPI密文)>"
- Linux/macOS：Fernet 对称加密（cryptography 库），密钥文件
  <项目根>/data/keyvault.key（0600，首用自动生成），密文格式 "KVFERNET1:<base64>"

特点：开机免密（本机有 DPAPI 账户 / 密钥文件即可解）；文件拷到别的机器无法解密；
网关进程内存里始终只有明文，明文永不落盘（备份 .bak 同为密文）。
密文按前缀识别后端，与当前平台无关——跨平台拷贝的密文给出明确的迁移提示。
"""
from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path

_MAGIC_DPAPI = "KVDPAPI1:"
_MAGIC_FERNET = "KVFERNET1:"

# Fernet 后端的密钥文件（默认 <项目根>/data/keyvault.key，可用环境变量 KEYVAULT_KEY_FILE 覆盖）
FERNET_KEY_FILE = Path(
    os.getenv("KEYVAULT_KEY_FILE")
    or Path(__file__).resolve().parent.parent / "data" / "keyvault.key"
)


class KeyvaultError(RuntimeError):
    """api.yaml 落库加密解密失败。"""


# ---------------- Windows 后端：DPAPI ----------------

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

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
            ok = ctypes.windll.crypt32.CryptUnprotectData(
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

    def _dpapi_open(payload_b64: str) -> str:
        try:
            raw = base64.b64decode(payload_b64.strip(), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise KeyvaultError(f"api.yaml 密文格式损坏: {exc}") from exc
        return _dpapi_raw(raw, protect=False).decode("utf-8")


# ---------------- Linux/macOS 后端：Fernet + 密钥文件 ----------------

def _load_fernet():
    """按需加载 Fernet；密钥文件不存在则自动生成（0600，进程间并发安全）。"""
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise KeyvaultError(f"缺少 cryptography 库，无法使用 Fernet 加密: {exc}") from exc

    path = FERNET_KEY_FILE
    while True:
        try:
            raw = path.read_bytes().strip()
        except FileNotFoundError:
            key = Fernet.generate_key()
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue  # 另一个进程刚创建了密钥，回读它的
            try:
                os.write(fd, key)
            finally:
                os.close(fd)
            return Fernet(key)
        if raw:
            try:
                return Fernet(raw)
            except (binascii.Error, ValueError) as exc:
                raise KeyvaultError(
                    f"密钥文件损坏（{path}）: {exc}。删除该文件后重启可重新生成，"
                    "但已有的 KVFERNET1 密文将无法解密。"
                ) from exc
        # 空文件属异常状态（正常流程不会产生），重建之
        path.unlink()
        continue


def _fernet_seal(text: str) -> str:
    token = _load_fernet().encrypt(text.encode("utf-8"))
    return _MAGIC_FERNET + base64.b64encode(token).decode("ascii")


def _fernet_open(payload_b64: str) -> str:
    from cryptography.fernet import InvalidToken

    try:
        token = base64.b64decode(payload_b64.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise KeyvaultError(f"api.yaml 密文格式损坏: {exc}") from exc
    try:
        return _load_fernet().decrypt(token).decode("utf-8")
    except InvalidToken as exc:
        raise KeyvaultError(
            "api.yaml Fernet 解密失败：密文与本机密钥不匹配"
            f"（{FERNET_KEY_FILE}）。若该文件来自其他电脑，本机无法解密。"
        ) from exc


# ---------------- 公共接口 ----------------

def is_sealed(text: str) -> bool:
    return isinstance(text, str) and text.startswith((_MAGIC_DPAPI, _MAGIC_FERNET))


def seal_text(text: str) -> str:
    """明文 -> 密文（按当前平台选后端）；已是密文则原样返回（幂等）。"""
    if is_sealed(text):
        return text
    if os.name == "nt":
        cipher = _dpapi_raw(text.encode("utf-8"), protect=True)
        return _MAGIC_DPAPI + base64.b64encode(cipher).decode("ascii")
    return _fernet_seal(text)


def open_text(text: str) -> str:
    """密文 -> 明文（按密文前缀选后端）；输入不是密文格式则原样返回（兼容明文旧配置）。"""
    if not isinstance(text, str):
        return text
    if text.startswith(_MAGIC_DPAPI):
        if os.name != "nt":
            raise KeyvaultError(
                "api.yaml 由 Windows DPAPI 加密，当前系统无法解密。"
                "请在原 Windows 机器上导出明文配置后导入，保存时会自动改用本机加密。"
            )
        return _dpapi_open(text[len(_MAGIC_DPAPI):])
    if text.startswith(_MAGIC_FERNET):
        return _fernet_open(text[len(_MAGIC_FERNET):])
    return text
