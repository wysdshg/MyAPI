import asyncio
import json

import pytest
from fastapi import HTTPException

from uni_api.rate_limit import ProviderKeyPool
from uni_api.rate_limit.quota import (
    ProviderQuota,
    QuotaStore,
    configure_provider_quota,
    record_response_usage,
)


def test_from_provider_returns_none_without_new_prefs():
    assert ProviderQuota.from_provider({"provider": "p", "preferences": {"api_key_rate_limit": "15/min"}}) is None
    assert ProviderQuota.from_provider({"provider": "p"}) is None
    assert ProviderQuota.from_provider({"provider": "p", "preferences": None}) is None
    assert ProviderQuota.from_provider({"provider": "p", "preferences": {}}) is None


def test_from_provider_parses_preferences(tmp_path):
    store = QuotaStore(tmp_path / "parse.json")
    # model_dict 是 外部名 -> 上游名；pool.next() 收上游名，usage 侧收外部名
    model_dict = {"qwen-7b": "Qwen/Qwen2.5-7B-Instruct"}

    quota = ProviderQuota.from_provider(
        {
            "provider": "modelscope",
            "preferences": {
                "DAILY_QUOTA": "250",
                "MODEL_COST": {"Qwen/Qwen2.5-7B-Instruct": 0.5, "default": 1},
            },
        },
        model_dict,
        store=store,
    )
    assert quota is not None
    assert quota.daily_quota == 250
    assert quota.cost("Qwen/Qwen2.5-7B-Instruct") == 0.5
    assert quota.cost("qwen-7b") == 0.5  # 按上游名写成本，对外名也识别
    assert quota.cost("unknown-model") == 1

    # 反向：按对外名写成本，上游名也识别
    quota2 = ProviderQuota.from_provider(
        {"provider": "p2", "preferences": {"DAILY_QUOTA": 250, "MODEL_COST": {"qwen-7b": 0.5}}},
        model_dict,
        store=store,
    )
    assert quota2.cost("Qwen/Qwen2.5-7B-Instruct") == 0.5

    # MODEL_COST 写成数字 = 所有模型统一成本
    quota3 = ProviderQuota.from_provider(
        {"provider": "p3", "preferences": {"DAILY_QUOTA": 10, "MODEL_COST": 2}},
        store=store,
    )
    assert quota3.cost("anything") == 2

    # TOKEN_RATE_LIMIT 字符串与整数两种写法等价；纯 token 配置不落盘
    token_a = ProviderQuota.from_provider({"provider": "sf", "preferences": {"TOKEN_RATE_LIMIT": "50000/min"}})
    token_b = ProviderQuota.from_provider({"provider": "sf", "preferences": {"TOKEN_RATE_LIMIT": 50000}})
    assert token_a.token_rules == token_b.token_rules == ((50000, 60),)
    assert token_a.store is None

    # 无效配置降级为“不启用”而不是抛异常
    bad = ProviderQuota.from_provider(
        {"provider": "bad", "preferences": {"DAILY_QUOTA": "abc", "TOKEN_RATE_LIMIT": "??"}},
        store=store,
    )
    assert bad is not None
    assert bad.daily_quota is None
    assert bad.token_rules == ()


def test_daily_quota_check_and_charge(tmp_path):
    today = {"value": "2026-09-24"}
    quota = ProviderQuota(
        "modelscope",
        daily_quota=2.0,
        model_cost={"cheap": 0.5, "expensive": 2.0},
        store=QuotaStore(tmp_path / "daily.json"),
        today_func=lambda: today["value"],
    )

    assert quota.block_reason("key-1", "cheap") is None
    quota.charge("key-1", "cheap")
    quota.charge("key-1", "cheap")  # spent 1.0

    # 1.0 + 2.0 > 2.0 → 昂贵模型不可用；便宜模型仍可继续
    reason = quota.block_reason("key-1", "expensive")
    assert reason is not None
    assert "Daily quota exhausted" in reason
    assert "modelscope" in reason
    assert quota.block_reason("key-1", "cheap") is None

    quota.charge("key-1", "cheap")  # spent 1.5
    quota.charge("key-1", "cheap")  # spent 2.0 正好用完
    assert quota.block_reason("key-1", "cheap") is not None
    # 每个账号 Key 独立记账
    assert quota.block_reason("key-2", "expensive") is None
    quota.charge("key-2", "expensive")  # spent 2.0
    assert quota.block_reason("key-2", "cheap") is not None


def test_daily_state_persists_and_resets_on_new_day(tmp_path):
    path = tmp_path / "persist.json"
    today = {"value": "2026-09-24"}

    def make_quota():
        return ProviderQuota(
            "modelscope",
            daily_quota=1,
            store=QuotaStore(path),
            today_func=lambda: today["value"],
        )

    q1 = make_quota()
    q1.charge("sk-abc", "m")  # 默认成本 1 → 当日用完
    assert q1.block_reason("sk-abc", "m") is not None

    # 模拟重启/热更新：同路径重建，状态从文件恢复
    q2 = make_quota()
    assert q2.block_reason("sk-abc", "m") is not None

    # 次日 0 点自动重置
    today["value"] = "2026-09-25"
    assert q2.block_reason("sk-abc", "m") is None
    q2.charge("sk-abc", "m")
    assert q2.block_reason("sk-abc", "m") is not None

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["date"] == "2026-09-25"
    entry = raw["providers"]["modelscope"]
    assert list(entry.values()) == [1.0]  # 重置后重新累计，key 不是明文


def test_token_sliding_window_blocks_and_recovers():
    clock = {"now": 1000.0}
    quota = ProviderQuota(
        "siliconflow",
        token_rules=((50000, 60),),
        now_func=lambda: clock["now"],
    )

    quota.record_tokens("key-1", 49999)
    assert quota.block_reason("key-1") is None

    quota.record_tokens("key-1", 2)  # 窗口内 50001 → 超限
    reason = quota.block_reason("key-1")
    assert reason is not None
    assert "Token rate limit" in reason
    assert "siliconflow" in reason
    assert "50001/50000" in reason
    # 其他账号不受影响
    assert quota.block_reason("key-2") is None

    # 滑出窗口后恢复
    clock["now"] += 61
    assert quota.block_reason("key-1") is None

    # 非法/非正数 token 直接忽略
    quota.record_tokens("key-1", "not-a-number")
    quota.record_tokens("key-1", -5)
    quota.record_tokens("key-1", float("nan"))
    assert quota.block_reason("key-1") is None


def test_pool_next_charges_and_skips_exhausted_keys(tmp_path):
    today = {"value": "2026-09-24"}
    pool = ProviderKeyPool(["key-1", "key-2"])
    assert pool.quota is None  # 未配置额度的渠道行为不变
    pool.quota = ProviderQuota(
        "modelscope",
        daily_quota=1,
        model_cost={"m": 1},
        store=QuotaStore(tmp_path / "pool.json"),
        today_func=lambda: today["value"],
    )

    async def run():
        first = await pool.next("m")
        assert first in ("key-1", "key-2")
        second = await pool.next("m")
        assert second != first  # 第一个账号已预扣完，轮到第二个

        # 两个账号都耗尽 → 429 且原因为额度而非笼统的限速
        with pytest.raises(HTTPException) as exc_info:
            await pool.next("m")
        assert exc_info.value.status_code == 429
        assert "Daily quota exhausted" in str(exc_info.value.detail)

        # 额度耗尽视同“全部不可用”，路由层因此自动切换渠道
        assert await pool.is_all_rate_limited("m") is True
        assert "Daily quota exhausted" in (pool.quota_block_reason("m") or "")

        # 次日重置后自动恢复参与轮询
        today["value"] = "2026-09-25"
        assert await pool.is_all_rate_limited("m") is False
        assert pool.quota_block_reason("m") is None

    asyncio.run(run())


def test_pool_token_window_blocks_key_selection(tmp_path):
    clock = {"now": 1000.0}
    pool = ProviderKeyPool(["key-1"])
    pool.quota = ProviderQuota(
        "siliconflow",
        token_rules=((50000, 60),),
        now_func=lambda: clock["now"],
    )

    async def run():
        assert await pool.next("m") == "key-1"  # token 窗口不限制无 usage 的选择
        pool.quota.record_tokens("key-1", 60000)
        with pytest.raises(HTTPException) as exc_info:
            await pool.next("m")
        assert exc_info.value.status_code == 429
        assert "Token rate limit" in str(exc_info.value.detail)
        assert await pool.is_all_rate_limited("m") is True

        clock["now"] += 61
        assert await pool.is_all_rate_limited("m") is False
        assert await pool.next("m") == "key-1"

    asyncio.run(run())


def test_configure_registry_and_record_response_usage(tmp_path):
    name = "registry-sf-test"
    provider = {"provider": name, "preferences": {"TOKEN_RATE_LIMIT": 1000}}
    quota = configure_provider_quota(provider, None, store=QuotaStore(tmp_path / "reg.json"))
    assert quota is not None

    # 配置未变化 → 复用旧对象（热更新不清 token 窗口）
    again = configure_provider_quota(provider, None, store=QuotaStore(tmp_path / "reg.json"))
    assert again is quota

    # 请求结束按实际 usage 计入：prompt + completion
    record_response_usage(
        {
            "provider": name,
            "provider_api_key": "sk-up-1",
            "prompt_tokens": 700,
            "completion_tokens": 400,
        }
    )
    reason = quota.block_reason("sk-up-1")
    assert reason is not None and "Token rate limit" in reason
    assert quota.block_reason("sk-up-2") is None  # 其他账号不受影响

    # 只有 total_tokens 时用它兜底
    record_response_usage({"provider": name, "provider_api_key": "sk-up-3", "total_tokens": 50})
    assert quota.block_reason("sk-up-3") is None

    # 缺 provider / key / usage / 未登记渠道 / 非 dict → 都直接跳过不抛异常
    record_response_usage({"provider": "unknown-prov", "provider_api_key": "k", "prompt_tokens": 5})
    record_response_usage({"provider": name, "prompt_tokens": 5})
    record_response_usage(None)

    # 配置移除后注销
    assert configure_provider_quota({"provider": name, "preferences": {}}, None) is None
    assert configure_provider_quota({"provider": name}, None) is None
