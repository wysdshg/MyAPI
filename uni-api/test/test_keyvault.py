# -*- coding: utf-8 -*-
"""keyvault（api.yaml DPAPI 落库加密）单元测试。仅 Windows 有效。"""

import pytest

import keyvault
from keyvault import KeyvaultError, is_sealed, open_text, seal_text


def test_seal_open_roundtrip():
    text = "provider: siliconflow\napi:\n- sk-secret-1234567890\n"
    sealed = seal_text(text)
    assert sealed != text
    assert is_sealed(sealed)
    assert sealed.startswith("KVDPAPI1:")
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
        open_text("KVDPAPI1:not-base64-@@@")


def test_is_sealed_edge_cases():
    assert not is_sealed("")
    assert not is_sealed(None)
    assert not is_sealed("plain: yaml")
    assert is_sealed("KVDPAPI1:AAAA")
