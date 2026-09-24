"""渠道账号级额度限制：

1. 每日额度（例：魔搭每个账号每天 250 魔粒）——preferences.DAILY_QUOTA
   按 MODEL_COST 记单次调用消耗，本地日期 0 点重置，状态持久化到
   ./data/quota_state.json（key 存 sha256 摘要，不落明文）。
   在选 Key 时“检查并预扣”，请求失败不回滚（宁可多扣也不超扣）。

2. 账号级 token 滑动窗口（例：硅基流动每账号 50000 token/分钟）——
   preferences.TOKEN_RATE_LIMIT，按实际返回的 usage 在请求结束时累计，
   只存内存（重启清零）。

未配置这两个偏好的渠道不会创建 quota 对象，行为与原来完全一致。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import deque
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping

from core.log_config import logger
from uni_api.rate_limit.policy import parse_rate_limit

DEFAULT_STATE_PATH = "./data/quota_state.json"
DEFAULT_COST = 1.0


def _key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8", "ignore")).hexdigest()[:16]


class QuotaStore:
    """每日额度状态的共享存储。同一路径在进程内共享同一份数据，
    所以配置热更新重建 quota 对象、渠道间互不影响都不会丢状态。"""

    _shared: dict[str, "QuotaStore"] = {}
    _shared_lock = threading.Lock()

    @classmethod
    def get(cls, path: str | Path | None = None) -> "QuotaStore":
        resolved = str(Path(path or os.getenv("QUOTA_STATE_PATH") or DEFAULT_STATE_PATH))
        with cls._shared_lock:
            store = cls._shared.get(resolved)
            if store is None:
                store = cls(resolved)
                cls._shared[resolved] = store
            return store

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self._data: dict[str, Any] | None = None

    @property
    def data(self) -> dict[str, Any]:
        with self.lock:
            if self._data is None:
                self._data = self._read()
            return self._data

    def _read(self) -> dict[str, Any]:
        try:
            if self.path.is_file():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return raw
        except Exception as exc:
            logger.warning("额度状态文件 %s 读取失败，按空状态处理: %s", self.path, exc)
        return {}

    def save(self) -> None:
        with self.lock:
            if self._data is None:
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_name(self.path.name + ".tmp")
                tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, self.path)
            except Exception as exc:
                logger.warning("额度状态文件 %s 写入失败: %s", self.path, exc)

    def spent(self, provider: str, today: str, key_hash: str) -> float:
        with self.lock:
            self._roll_locked(today)
            value = self._data.get("providers", {}).get(provider, {}).get(key_hash, 0)
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0

    def add_spent(self, provider: str, today: str, key_hash: str, cost: float) -> float:
        with self.lock:
            self._roll_locked(today)
            entry = self._data.setdefault("providers", {}).setdefault(provider, {})
            new_total = round(float(entry.get(key_hash, 0) or 0) + cost, 6)
            entry[key_hash] = new_total
            self.save()
            return new_total

    def _roll_locked(self, today: str) -> None:
        if self._data is None:
            self._data = self._read()
        # 读路径上的跨天重置只改内存；下一次 add_spent 会随写入落盘，
        # 崩溃时文件里留旧日期，重启后再滚一次即可，不会丢也不会多扣。
        if self._data.get("date") == today:
            return
        self._data["date"] = today
        self._data["providers"] = {}


def _parse_token_rules(raw: Any) -> tuple[tuple[int, int], ...]:
    """TOKEN_RATE_LIMIT 支持 "50000/min" 或直接写 50000（= 50000/min）。"""
    if isinstance(raw, bool):
        return ()
    if isinstance(raw, int):
        raw = f"{raw}/min"
    elif isinstance(raw, str):
        raw = raw.strip()
    else:
        return ()
    try:
        parsed = parse_rate_limit(raw)
    except ValueError:
        return ()
    # tpr（period<=0）是单次请求上限，不是时间窗口，这里不支持
    return tuple((count, period) for count, period in parsed if period > 0 and count > 0)


def _expand_cost_aliases(
    costs: dict[str, float],
    model_dict: Mapping[str, str],
) -> dict[str, float]:
    """model_dict 是 外部名 -> 上游名；用户按任一名字写 MODEL_COST，两边都补上。"""
    derived: dict[str, float] = {}
    upstream_to_externals: dict[str, list[str]] = {}
    for external, upstream in model_dict.items():
        upstream_to_externals.setdefault(str(upstream), []).append(str(external))
    for name, cost in costs.items():
        upstream = model_dict.get(name)
        if upstream is not None:
            derived.setdefault(str(upstream), cost)
        for external in upstream_to_externals.get(name, []):
            derived.setdefault(external, cost)
    merged = dict(derived)
    merged.update(costs)  # 用户显式写的优先于派生别名
    return merged


class ProviderQuota:
    def __init__(
        self,
        provider_name: str,
        *,
        daily_quota: float | None = None,
        model_cost: Mapping[str, float] | None = None,
        default_cost: float = DEFAULT_COST,
        token_rules: tuple[tuple[int, int], ...] = (),
        token_soft_limit: int | None = None,
        store: QuotaStore | None = None,
        today_func: Callable[[], str] | None = None,
        now_func: Callable[[], float] | None = None,
    ):
        self.provider_name = provider_name
        self.daily_quota = daily_quota
        self.model_cost = dict(model_cost or {})
        self.default_cost = default_cost
        self.token_rules = tuple(token_rules)
        # 软上限：已用+预估 ≥ 软上限就跳过该 Key（TOKEN_SOFT_LIMIT，稳态保护）
        self.token_soft_limit = token_soft_limit
        if store is None and daily_quota is not None:
            store = QuotaStore.get()
        self.store = store
        self._today = today_func or (lambda: date.today().isoformat())
        self._now = now_func or time.time
        # key_hash -> deque[(时间戳, token数)]，只存内存
        self._windows: dict[str, deque[tuple[float, float]]] = {}
        self._windows_lock = threading.Lock()

    @property
    def settings_key(self) -> tuple:
        """配置身份：配置热更新时配置没变则复用旧对象，保留 token 窗口。"""
        return (
            self.daily_quota,
            tuple(sorted(self.model_cost.items())),
            self.default_cost,
            self.token_rules,
            self.token_soft_limit,
            str(self.store.path) if self.store is not None else None,
        )

    @classmethod
    def from_provider(
        cls,
        provider: Mapping[str, Any],
        model_dict: Mapping[str, str] | None = None,
        *,
        store: QuotaStore | None = None,
        today_func: Callable[[], str] | None = None,
        now_func: Callable[[], float] | None = None,
    ) -> "ProviderQuota | None":
        """从 provider.preferences 构建；没有任何新配置时返回 None。"""
        prefs = provider.get("preferences")
        if not isinstance(prefs, Mapping):
            return None
        daily_raw = prefs.get("DAILY_QUOTA")
        cost_raw = prefs.get("MODEL_COST")
        token_raw = prefs.get("TOKEN_RATE_LIMIT")
        if daily_raw is None and cost_raw is None and token_raw is None:
            return None

        name = str(provider.get("provider") or "")

        daily_quota: float | None = None
        if daily_raw is not None:
            try:
                daily_quota = float(daily_raw)
            except (TypeError, ValueError):
                logger.warning("provider %s: DAILY_QUOTA 无效 %r，已忽略", name, daily_raw)
            else:
                if daily_quota <= 0:
                    logger.warning("provider %s: DAILY_QUOTA 必须大于 0，已忽略", name)
                    daily_quota = None

        default_cost = DEFAULT_COST
        model_cost: dict[str, float] = {}
        if isinstance(cost_raw, Mapping):
            for raw_name, raw_value in cost_raw.items():
                cost_name = str(raw_name).strip()
                if not cost_name:
                    continue
                try:
                    value = float(raw_value)
                except (TypeError, ValueError):
                    logger.warning("provider %s: MODEL_COST[%s] 无效 %r，已忽略", name, raw_name, raw_value)
                    continue
                if cost_name == "default":
                    default_cost = value
                else:
                    model_cost[cost_name] = value
        elif cost_raw is not None:
            try:
                default_cost = float(cost_raw)
            except (TypeError, ValueError):
                logger.warning("provider %s: MODEL_COST 无效 %r，已忽略", name, cost_raw)
        if model_dict:
            model_cost = _expand_cost_aliases(model_cost, model_dict)

        token_rules = _parse_token_rules(token_raw) if token_raw is not None else ()
        if token_raw is not None and not token_rules:
            logger.warning("provider %s: TOKEN_RATE_LIMIT 无效 %r，已忽略", name, token_raw)

        token_soft_limit: int | None = None
        soft_raw = prefs.get("TOKEN_SOFT_LIMIT")
        if soft_raw is not None:
            try:
                token_soft_limit = int(float(soft_raw))
            except (TypeError, ValueError):
                logger.warning("provider %s: TOKEN_SOFT_LIMIT 无效 %r，已忽略", name, soft_raw)
            else:
                if token_soft_limit <= 0:
                    logger.warning(
                        "provider %s: TOKEN_SOFT_LIMIT 必须大于 0，已忽略", name, soft_raw
                    )
                    token_soft_limit = None

        return cls(
            name,
            daily_quota=daily_quota,
            model_cost=model_cost,
            default_cost=default_cost,
            token_rules=token_rules,
            token_soft_limit=token_soft_limit,
            store=store,
            today_func=today_func,
            now_func=now_func,
        )

    def cost(self, model: str | None) -> float:
        if model and model in self.model_cost:
            return self.model_cost[model]
        return self.default_cost

    def block_reason(
        self,
        key: str,
        model: str | None = None,
        estimated_tokens: int = 0,
    ) -> str | None:
        """该账号 Key 当前不可用时返回具体原因，可用时返回 None。

        estimated_tokens：请求前对本次 prompt 的预估 token 数（粗估，宁多勿少）。
        配置了 TOKEN_SOFT_LIMIT 时，已用+预估 ≥ 软上限就判定该 Key 不可用，
        由轮换逻辑自动跳到窗口还有余量的下一把 Key。
        """
        if self.daily_quota is not None and self.store is not None:
            today = self._today()
            spent = self.store.spent(self.provider_name, today, _key_hash(key))
            if spent + self.cost(model) > self.daily_quota + 1e-9:
                return (
                    f"Daily quota exhausted for {self.provider_name} "
                    f"({spent:g}/{self.daily_quota:g}), resets at midnight"
                )
        if self.token_rules:
            key_hash = _key_hash(key)
            now = self._now()
            for limit, period in self.token_rules:
                used = self._window_used(key_hash, period, now)
                projected = used + max(0, estimated_tokens)
                if self.token_soft_limit and projected >= self.token_soft_limit:
                    return (
                        f"Token soft limit for {self.provider_name} "
                        f"(used {int(used)} + estimated {int(max(0, estimated_tokens))} "
                        f">= {self.token_soft_limit} tokens per {period}s sliding window), "
                        f"skip key"
                    )
                if used >= limit:
                    return (
                        f"Token rate limit for {self.provider_name} "
                        f"({int(used)}/{limit} tokens per {period}s sliding window), retry later"
                    )
        return None

    def charge(self, key: str, model: str | None = None) -> None:
        """选中 Key 时预扣当日额度（不回滚，宁可多扣不超扣）。"""
        if self.daily_quota is None or self.store is None:
            return
        cost = self.cost(model)
        if cost <= 0:
            return
        self.store.add_spent(self.provider_name, self._today(), _key_hash(key), cost)

    def record_tokens(self, key: str, tokens: Any) -> None:
        """请求结束时把实际 token 数记入滑动窗口。"""
        if not self.token_rules:
            return
        try:
            value = float(tokens)
        except (TypeError, ValueError):
            return
        if not (value > 0):  # 同时挡掉 0、负数和 NaN
            return
        key_hash = _key_hash(key)
        now = self._now()
        max_period = max(period for _, period in self.token_rules)
        with self._windows_lock:
            window = self._windows.get(key_hash)
            if window is None:
                window = self._windows[key_hash] = deque()
            window.append((now, value))
            cutoff = now - max_period
            while window and window[0][0] <= cutoff:
                window.popleft()

    def _window_used(self, key_hash: str, period: int, now: float) -> float:
        with self._windows_lock:
            window = self._windows.get(key_hash)
            if not window:
                return 0.0
            # 不在读路径上删除：一条记录可能同时属于更长的窗口
            cutoff = now - period
            return sum(tokens for timestamp, tokens in window if timestamp > cutoff)


_quota_registry: dict[str, ProviderQuota] = {}


def configure_provider_quota(
    provider: Mapping[str, Any],
    model_dict: Mapping[str, str] | None = None,
    *,
    store: QuotaStore | None = None,
    today_func: Callable[[], str] | None = None,
    now_func: Callable[[], float] | None = None,
) -> ProviderQuota | None:
    """构建（或更新）某渠道的额度对象并登记到注册表。

    配置未变化时复用旧对象，热更新不会清掉已累计的 token 窗口。
    """
    name = str(provider.get("provider") or "")
    quota = ProviderQuota.from_provider(
        provider, model_dict, store=store, today_func=today_func, now_func=now_func
    )
    if not name:
        return quota
    previous = _quota_registry.get(name)
    if quota is None:
        _quota_registry.pop(name, None)
        return None
    if previous is not None and previous.settings_key == quota.settings_key:
        return previous
    _quota_registry[name] = quota
    return quota


def _to_tokens(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


def record_response_usage(current_info: Any) -> None:
    """请求结束时把 prompt+completion 实际 token 记入该渠道账号的窗口。

    从 update_stats 顶部调用，与数据库开关无关；缺 provider、
    provider_api_key、usage 或未配置额度的渠道都直接跳过。"""
    if not isinstance(current_info, Mapping):
        return
    provider = current_info.get("provider")
    key = current_info.get("provider_api_key")
    if not provider or not key:
        return
    quota = _quota_registry.get(str(provider))
    if quota is None:
        return
    tokens = _to_tokens(current_info.get("prompt_tokens")) + _to_tokens(
        current_info.get("completion_tokens")
    )
    if tokens <= 0:
        tokens = _to_tokens(current_info.get("total_tokens"))
    if tokens > 0:
        quota.record_tokens(key, tokens)
