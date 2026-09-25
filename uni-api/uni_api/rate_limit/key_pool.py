from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Callable

from fastapi import HTTPException
from contextlib import suppress

from uni_api.rate_limit.policy import DEFAULT_RATE_LIMIT, RateLimitPolicy
from uni_api.rate_limit.state import RateLimitState


def _masked_api_key(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "-"
    if len(raw) <= 10:
        return "***"
    return f"{raw[:4]}...{raw[-4:]}"


class ProviderKeyPool:
    def __init__(
        self,
        items: list[str] | None = None,
        rate_limit: Any = None,
        schedule_algorithm: str = "round_robin",
        provider_name: str | None = None,
        *,
        now_func: Callable[[], float] | None = None,
        on_warning: Callable[[str], None] | None = None,
    ):
        items = list(items or [])
        rate_limit = {"default": DEFAULT_RATE_LIMIT} if rate_limit is None else rate_limit
        self.provider_name = provider_name
        self.original_items = list(items)
        self.schedule_algorithm = self._normalize_schedule_algorithm(schedule_algorithm, on_warning)
        self.items = random.sample(items, len(items)) if self.schedule_algorithm == "random" else items
        self.index = 0
        self.lock = asyncio.Lock()
        self.state = RateLimitState(now_func=now_func)
        self.policy = RateLimitPolicy.from_config(rate_limit)
        self.rate_limits = self.policy.as_legacy_dict()
        self.reordering_task = None
        self._warn = on_warning
        # 每日额度/账号 token 窗口（uni_api.rate_limit.quota.ProviderQuota），
        # 由配置加载挂载；未配置的渠道保持 None，行为与原来完全一致。
        self.quota = None

        if self.schedule_algorithm == "smart_round_robin":
            self._trigger_reorder()

    @property
    def requests(self):
        return self.state.requests

    @property
    def cooling_until(self):
        return self.state.cooling_until

    async def reset_items(self, new_items: list[str]):
        async with self.lock:
            if self.items != new_items:
                self.items = list(new_items)
                self.index = 0

    def _trigger_reorder(self) -> None:
        if self.provider_name and (self.reordering_task is None or self.reordering_task.done()):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                self._log_warning(f"No running event loop to trigger reorder for '{self.provider_name}'.")
                return
            self.reordering_task = loop.create_task(self._reorder_keys())

    async def _reorder_keys(self) -> None:
        try:
            sorted_keys = await self._load_reordered_items()
            if sorted_keys:
                await self.reset_items(sorted_keys)
        except Exception as exc:
            self._log_warning(f"Error during key reordering for provider '{self.provider_name}': {exc}")

    async def _load_reordered_items(self) -> list[str] | None:
        return None

    async def set_cooling(self, item: str, cooling_time: int = 60):
        async with self.lock:
            self.state.set_cooling(item, cooling_time)
        self._log_warning(
            f"API key {_masked_api_key(item)} 已进入冷却状态，冷却时间 {cooling_time} 秒"
        )

    async def is_rate_limited(self, item: str, model: str | None = None, is_check: bool = False) -> bool:
        async with self.lock:
            limited = self.state.is_rate_limited(item, model, self.policy, commit=not is_check)
        if limited and not is_check:
            self._log_warning(
                f"API key {_masked_api_key(item)}: model: {model or 'default'} "
                "has been rate limited"
            )
        return limited

    def rollback_rate_limit_record(self, item: str, model: str | None = None) -> None:
        self.state.rollback_last_record(item, model)

    async def _select_locked(
        self,
        model: str | None = None,
        *,
        provider_key_index: int | None = None,
        estimated_tokens: int = 0,
    ) -> tuple[str | None, str | None]:
        """轮换选一把可用 Key。调用方必须已持有 self.lock。

        返回 (key, block_detail)：全部不可用时 key=None，block_detail 为
        第一条不可用原因（Token/Daily 开头的是额度类原因）。
        """
        if self.schedule_algorithm == "fixed_priority":
            self.index = 0

        if self.schedule_algorithm == "smart_round_robin" and self.index == len(self.items) - 1:
            self._trigger_reorder()

        # 请求级指定上游 Key：body 字段 provider_key_index 或 X-Key-Index 头，
        # 0,1,2,... 选择该渠道第几个 Key，越界自动取模（3 个 Key 传 5 => 第 2 个）。
        if provider_key_index is not None:
            self.index = int(provider_key_index) % len(self.items)

        start_index = self.index
        quota = self.quota
        block_detail: str | None = None
        while True:
            item = self.items[self.index]
            self.index = (self.index + 1) % len(self.items)

            # 额度/token 窗口优先于请求限流检查：不可用的 Key 直接跳过，
            # 不消耗它的请求计数；可用的 Key 选中即预扣当日额度。
            quota_reason = (
                quota.block_reason(item, model, estimated_tokens=estimated_tokens)
                if quota is not None
                else None
            )
            if quota_reason is not None:
                block_detail = block_detail or quota_reason
            elif not self.state.is_rate_limited(item, model, self.policy, commit=True):
                if quota is not None:
                    quota.charge(item, model)
                return item, None

            if self.index == start_index:
                return None, block_detail

    async def next(
        self,
        model: str | None = None,
        *,
        provider_key_index: int | None = None,
        estimated_tokens: int = 0,
        allow_wait: bool = True,
    ):
        if not self.items:
            self._log_warning("All API keys are rate limited!")
            raise HTTPException(status_code=429, detail="Too many requests")

        async with self.lock:
            item, block_detail = await self._select_locked(
                model,
                provider_key_index=provider_key_index,
                estimated_tokens=estimated_tokens,
            )
        if item is not None:
            return item

        # 全部 Key 都被额度类原因挡下。仅当原因是 token 窗口（会随滑动窗口
        # 推移而缓解）且配置了 TOKEN_WAIT_SECONDS 时，进入有界 FIFO 等待；
        # 每日额度耗尽等长期原因不等待，立即 429。
        quota = self.quota
        wait_seconds = int(getattr(quota, "token_wait_seconds", 0) or 0)
        token_kind = bool(block_detail) and block_detail.startswith("Token")
        if not (allow_wait and quota is not None and wait_seconds > 0 and token_kind):
            if block_detail:
                self._log_warning(f"All API keys unavailable: {block_detail}")
                raise HTTPException(status_code=429, detail=block_detail)
            self._log_warning("All API keys are rate limited!")
            raise HTTPException(status_code=429, detail="Too many requests")

        ticket = quota.wait_enter()
        if ticket is None:
            detail = (
                f"Token window for {quota.provider_name} full and wait queue full "
                f"(max {quota.token_wait_queue_max} waiting), retry later"
            )
            self._log_warning(detail)
            raise HTTPException(status_code=429, detail=detail, headers={"Retry-After": "5"})
        try:
            deadline = time.monotonic() + wait_seconds
            poll_interval = 1.0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # 等待超时：带上建议的重试秒数（窗口滑出最早空闲点）
                    retry_after = 60
                    for candidate in self.items:
                        wait_est = quota.seconds_until_token_room(
                            candidate, model, estimated_tokens=estimated_tokens
                        )
                        if wait_est is not None:
                            retry_after = max(1, min(600, int(wait_est) + 1))
                            break
                    detail = (
                        f"Token window for {quota.provider_name} still full after "
                        f"waiting {wait_seconds}s (queue position expired)"
                    )
                    self._log_warning(detail)
                    raise HTTPException(
                        status_code=429, detail=detail, headers={"Retry-After": str(retry_after)}
                    )
                if not quota.wait_head(ticket):
                    await asyncio.sleep(min(poll_interval, remaining))
                    continue
                async with self.lock:
                    item, block_detail = await self._select_locked(
                        model, estimated_tokens=estimated_tokens
                    )
                if item is not None:
                    return item
                await asyncio.sleep(min(poll_interval, remaining))
        finally:
            quota.wait_leave(ticket)

    async def is_tpr_exceeded(self, model: str | None = None, tokens: int = 0) -> bool:
        async with self.lock:
            return self.state.is_tpr_exceeded(model, tokens, self.policy)

    async def is_all_rate_limited(self, model: str | None = None) -> bool:
        if not self.items:
            return False

        quota = self.quota
        async with self.lock:
            for item in self.items:
                if quota is not None and quota.block_reason(item, model) is not None:
                    continue
                if not self.state.is_rate_limited(item, model, self.policy, commit=False):
                    return False
            return True

    def quota_block_reason(self, model: str | None = None) -> str | None:
        """所有 Key 都被每日额度/token 窗口挡下时返回原因文本，否则 None。

        供路由层在“全部不可用”的 429 里带上比“全部限速”更具体的原因。"""
        quota = self.quota
        if quota is None or not self.items:
            return None
        reason: str | None = None
        for item in self.items:
            item_reason = quota.block_reason(item, model)
            if item_reason is None:
                return None
            reason = reason or item_reason
        return reason

    async def after_next_current(self):
        if not self.items:
            return None
        async with self.lock:
            return self.items[(self.index - 1) % len(self.items)]

    def get_items_count(self) -> int:
        return len(self.items)

    def snapshot(self) -> dict[str, Any]:
        task = self.reordering_task
        return {
            "provider_name": self.provider_name,
            "item_count": len(self.items),
            "schedule_algorithm": self.schedule_algorithm,
            "reordering_task_active": bool(task and not task.done()),
            "reordering_task_done": bool(task and task.done()),
        }

    async def close(self) -> None:
        task = self.reordering_task
        if task is None:
            return
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self.reordering_task = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_name": self.provider_name,
            "items": list(self.items),
            "original_items": list(self.original_items),
            "schedule_algorithm": self.schedule_algorithm,
            "index": self.index,
            "rate_limits": self.rate_limits,
            "cooling_until": dict(self.cooling_until),
        }

    @staticmethod
    def _normalize_schedule_algorithm(
        schedule_algorithm: str,
        on_warning: Callable[[str], None] | None,
    ) -> str:
        allowed = {"round_robin", "random", "fixed_priority", "smart_round_robin"}
        if schedule_algorithm in allowed:
            return schedule_algorithm
        if on_warning is not None:
            on_warning(
                f"Unknown schedule algorithm: {schedule_algorithm}, use "
                "(round_robin, random, fixed_priority, smart_round_robin) instead"
            )
        return "round_robin"

    def _log_warning(self, message: str) -> None:
        if self._warn is not None:
            self._warn(message)
