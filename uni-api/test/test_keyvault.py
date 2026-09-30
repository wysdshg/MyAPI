# -*- coding: utf-8 -*-
"""keyvault（api.yaml 落库加密）单元测试。

双后端：Windows 走 DPAPI（KVDPAPI1:），Linux/macOS 走 Fernet（KVFERNET1:）。
公共接口测试按当前平台断言；Fernet 后端本身与平台无关，两种系统上都测。
"""

import os
import stat

import pytest

import keyvault
from keyvault import KeyvaultError, is_sealed, open_text, seal_text

IS_WINDOWS = os.name == "nt"
MAGIC = "KVDPAPI1:" if IS_WINDOWS else "KVFERNET1:"


def test_seal_open_roundtrip():
    text = "provider: siliconflow\napi:\n- sk-secret-1234567890\n"
    sealed = seal_text(text)
    assert sealed != text
    assert is_sealed(sealed)
    assert sealed.startswith(MAGIC)
    assert "sk-secret" not in sealed          # 密文里搜不到明文片段
    assert open_text(sealed) == text


def test_seal_idempotent():
    sealed = seal_text("hello")
    assert seal_text(sealed) == sealed


def test_open_plaintext_passthrough():
    """明文旧配置原样返回（平滑迁移，不抛错）。"""
    assert open_text("provider: siliconflow\n") == "provider: siliconflow\n"


def test_open_corrupted_b64_raises():
    sealed = seal_text("hello")
    bad = sealed[:-4] + "!!!!"  # 破坏 base64 尾部
    with pytest.raises(KeyvaultError):
        open_text(bad)


def test_unrelated_magic_text_raises():
    with pytest.raises(KeyvaultError):
        open_text(MAGIC + "not-base64-@@@")


def test_foreign_backend_magic_raises():
    """另一种后端的密文（跨平台拷贝场景）必须报错，不能静默当明文。"""
    foreign = "KVFERNET1:" if IS_WINDOWS else "KVDPAPI1:"
    with pytest.raises(KeyvaultError):
        open_text(foreign + "AAAA")


def test_is_sealed_edge_cases():
    assert not is_sealed("")
    assert not is_sealed(None)
    assert not is_sealed("plain: yaml")
    assert is_sealed("KVDPAPI1:AAAA")
    assert is_sealed("KVFERNET1:AAAA")


# ---------------- Fernet 后端（与平台无关，两种系统都测） ----------------

def test_fernet_backend_roundtrip(tmp_path, monkeypatch):
    keyfile = tmp_path / "data" / "keyvault.key"
    monkeypatch.setattr(keyvault, "FERNET_KEY_FILE", keyfile)
    text = "provider: siliconflow\napi:\n- sk-fernet-secret\n"
    sealed = keyvault._fernet_seal(text)
    assert sealed.startswith("KVFERNET1:")
    assert "sk-fernet-secret" not in sealed
    assert keyvault._fernet_open(sealed[len("KVFERNET1:"):]) == text
    # 密钥文件首用自动生成且仅属主可读写
    assert keyfile.exists()
    assert stat.S_IMODE(keyfile.stat().st_mode) & 0o077 == 0


def test_fernet_backend_wrong_key_raises(tmp_path, monkeypatch):
    first = tmp_path / "a" / "keyvault.key"
    monkeypatch.setattr(keyvault, "FERNET_KEY_FILE", first)
    sealed = keyvault._fernet_seal("hello")
    # 换一把密钥（模拟拷到别的机器）后无法解密
    second = tmp_path / "b" / "keyvault.key"
    monkeypatch.setattr(keyvault, "FERNET_KEY_FILE", second)
    with pytest.raises(KeyvaultError):
        open_text(sealed)
