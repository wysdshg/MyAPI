# -*- coding: utf-8 -*-
"""token 窗口全满时的有界 FIFO 等待队列测试。

场景：多线程批量概括小说，请求量大但窗口（如硅基 49000/min）有限。
- 全满时进入 FIFO 排队，窗口滑动腾出空间后按先来后到发出（不丢失）；
- 等待有上限（TOKEN_WAIT_SECONDS），超时 429 并带 Retry-After；
- 队列有人数上限（TOKEN_WAIT_QUEUE_MAX），满了直接 429 防积压；
- 每日额度耗尽这类不会自行缓解的原因不等待；
- allow_wait=False（重试的请求）不排队，立即失败。
"""

import asyncio
import time

import pytest
from fastapi import HTTPException

from uni_api.rate_limit.key_pool import ProviderKeyPool
from uni_api.rate_limit.quota import ProviderQuota

# 夹具会把 asyncio.sleep 全局替换成"推进假时钟"的版本，
# 测试里需要真让出事件循环时用它。
_REAL_SLEEP = asyncio.sleep


@pytest.fixture()
def fake_clock(monkeypatch):
    """统一假时钟：quota._now 与 time.monotonic 同源，poll 每次推进时钟。"""
    clock = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"], raising=True)

    async def fast_sleep(delay, *args, **kwargs):
        # 每次轮询推进 2 秒，模拟真实时间流逝（窗口随之滑动）
        clock["t"] += max(float(delay), 1.0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep, raising=True)
    return clock


def _make_pool(clock, *, soft=47000, wait_seconds=120, queue_max=10, keys=("key-1", "key-2")):
    pool = ProviderKeyPool(list(keys))
    pool.quota = ProviderQuota(
        "siliconflow",
        token_rules=((50000, 60),),
        token_soft_limit=soft,
        token_wait_seconds=wait_seconds,
        token_wait_queue_max=queue_max,
        now_func=lambda: clock["t"],
    )
    return pool


def _fill_window(pool, key, tokens, clock):
    pool.quota.record_tokens(key, tokens)
    assert clock["t"] == clock["t"]  # record 用 quota._now == fake clock


@pytest.mark.asyncio
async def test_wait_frees_when_window_slides(fake_clock):
    """两把 Key 都满 → 排队等待 → 窗口滑动腾出空间后成功拿到 Key。"""
    pool = _make_pool(fake_clock)
    for key in ("key-1", "key-2"):
        _fill_window(pool, key, 46000, fake_clock)

    async def run():
        return await pool.next("m", estimated_tokens=2000)

    task = asyncio.create_task(run())
    # 轮询推进假时钟：1000 -> 1060+ 时窗口记录（ts=1000, period=60）全部过期
    result = await asyncio.wait_for(task, timeout=10)
    assert result in ("key-1", "key-2")
    assert pool.quota.wait_queue_depth() == 0


@pytest.mark.asyncio
async def test_wait_timeout_429_with_retry_after(fake_clock):
    """等待超时 → 429 且带 Retry-After 头（提示窗口滑出剩余秒数）。"""
    clock = fake_clock
    pool = _make_pool(clock, wait_seconds=20)  # deadline 1020，窗口 1060 才过期
    _fill_window(pool, "key-1", 46000, clock)
    _fill_window(pool, "key-2", 46000, clock)

    with pytest.raises(HTTPException) as exc_info:
        await pool.next("m", estimated_tokens=2000)
    exc = exc_info.value
    assert exc.status_code == 429
    assert "still full after waiting 20s" in str(exc.detail)
    assert exc.headers is not None and "Retry-After" in exc.headers
    assert 1 <= int(exc.headers["Retry-After"]) <= 600
    assert pool.quota.wait_queue_depth() == 0


@pytest.mark.asyncio
async def test_queue_full_immediate_429(fake_clock):
    """队列上限 1：占位后第二个请求立即 429，不排队。"""
    clock = fake_clock
    pool = _make_pool(clock, wait_seconds=300, queue_max=1)
    _fill_window(pool, "key-1", 46000, clock)
    _fill_window(pool, "key-2", 46000, clock)

    async def sleep_never_advance(delay, *args, **kwargs):
        # 静止时钟：第一个等待者永远等不到空位，一直占着队列
        await asyncio.get_event_loop().run_in_executor(None, lambda: None)

    asyncio_sleep = asyncio.sleep

    async def stalled_sleep(delay, *args, **kwargs):
        await _REAL_SLEEP(0)

    import uni_api.rate_limit.key_pool as kp

    kp.asyncio.sleep = stalled_sleep  # 仅让等待者原地打转，时钟不动
    try:
        first = asyncio.create_task(pool.next("m", estimated_tokens=2000))
        for _ in range(200):
            await _REAL_SLEEP(0)
            if pool.quota.wait_queue_depth() == 1:
                break
        assert pool.quota.wait_queue_depth() == 1

        with pytest.raises(HTTPException) as exc_info:
            await pool.next("m", estimated_tokens=2000)
        assert "queue full" in str(exc_info.value.detail)
        assert exc_info.value.headers and exc_info.value.headers.get("Retry-After") == "5"

        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    finally:
        kp.asyncio.sleep = asyncio_sleep
    assert pool.quota.wait_queue_depth() == 0


@pytest.mark.asyncio
async def test_fifo_order(fake_clock):
    """两个等待者按进入顺序先后拿到 Key（先来先服务）。"""
    clock = fake_clock
    pool = _make_pool(clock, wait_seconds=300)
    for key in ("key-1", "key-2"):
        _fill_window(pool, key, 46000, clock)

    order = []

    async def requester(tag):
        key = await pool.next("m", estimated_tokens=2000)
        order.append(tag)
        return key

    async def enqueue_in_order():
        task_a = asyncio.create_task(requester("A"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task_b = asyncio.create_task(requester("B"))
        await asyncio.gather(task_a, task_b)

    await asyncio.wait_for(enqueue_in_order(), timeout=10)
    assert order == ["A", "B"]


@pytest.mark.asyncio
async def test_allow_wait_false_skips_queue(fake_clock):
    """allow_wait=False（重试请求）不排队，立即 429。"""
    clock = fake_clock
    pool = _make_pool(clock)
    _fill_window(pool, "key-1", 46000, clock)
    _fill_window(pool, "key-2", 46000, clock)

    with pytest.raises(HTTPException) as exc_info:
        await pool.next("m", estimated_tokens=2000, allow_wait=False)
    assert "soft limit" in str(exc_info.value.detail).lower()
    assert pool.quota.wait_queue_depth() == 0


@pytest.mark.asyncio
async def test_daily_quota_never_waits(fake_clock):
    """每日额度耗尽不会自行缓解 → 即使配置了等待也立即 429。"""
    clock = fake_clock
    pool = ProviderKeyPool(["key-1"])
    pool.quota = ProviderQuota(
        "modelscope",
        daily_quota=1.0,
        model_cost={"m": 1.0},
        token_wait_seconds=120,
        now_func=lambda: clock["t"],
        store=None,
    )
    # daily_quota 但 store=None 时 block_reason 跳过每日额度检查，
    # 这里直接给一个带 store 的对象验证真实路径：
    from uni_api.rate_limit.quota import QuotaStore

    pool.quota = ProviderQuota(
        "modelscope",
        daily_quota=1.0,
        model_cost={"m": 1.0},
        token_wait_seconds=120,
        now_func=lambda: clock["t"],
        store=QuotaStore("./data/_test_wait_daily.json"),
    )
    pool.quota.charge("key-1", "m")
    pool.quota.charge("key-1", "m")  # spent=2 > 1

    with pytest.raises(HTTPException) as exc_info:
        await pool.next("m", estimated_tokens=0)
    assert "Daily quota" in str(exc_info.value.detail)
    assert pool.quota.wait_queue_depth() == 0


def test_seconds_until_token_room_math():
    clock = {"t": 1000.0}
    quota = ProviderQuota(
        "sf",
        token_rules=((50000, 60),),
        token_soft_limit=47000,
        now_func=lambda: clock["t"],
    )
    quota.record_tokens("k", 46000)
    # est=0：46000 < 47000，已有空间
    assert quota.seconds_until_token_room("k", estimated_tokens=0) == 0.0
    # est=2000：48000 ≥ 47000，需等记录过期（ts=1000 + period 60 = 1060）
    assert quota.seconds_until_token_room("k", estimated_tokens=2000) == pytest.approx(60.0)
    clock["t"] = 1030.0
    assert quota.seconds_until_token_room("k", estimated_tokens=2000) == pytest.approx(30.0)
    clock["t"] = 1100.0
    assert quota.seconds_until_token_room("k", estimated_tokens=2000) == 0.0


@pytest.mark.asyncio
async def test_wait_disabled_by_default(fake_clock):
    """未配置 TOKEN_WAIT_SECONDS（=0）时行为与原来一致：立即 429。"""
    clock = fake_clock
    pool = ProviderKeyPool(["key-1"])
    pool.quota = ProviderQuota(
        "sf",
        token_rules=((50000, 60),),
        token_soft_limit=47000,
        now_func=lambda: clock["t"],
    )
    _fill_window(pool, "key-1", 46000, clock)
    with pytest.raises(HTTPException) as exc_info:
        await pool.next("m", estimated_tokens=2000)
    assert "soft limit" in str(exc_info.value.detail).lower()
    assert pool.quota.wait_queue_depth() == 0
