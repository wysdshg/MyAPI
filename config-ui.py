"""uni-api 图形配置页面。

功能：
- 浏览器里增删改渠道（providers）和统一 Key 白名单（api_keys），保存到 api.yaml
  （ruamel round-trip 模式，未编辑部分的注释会保留）
- 一键重启/停止/启动主服务（端口 MAIN_PORT）
- 显示主服务进程的内存占用
- 一键试调用某个模型，验证渠道 Key 是否还有额度

仅监听 127.0.0.1，配置页面不对局域网开放。主服务请另行启动（start-uniapi.bat）。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from ruamel.yaml import YAML

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "api.yaml"
RUN_LOG = BASE / "uni-api-run.log"
MAIN_PORT = int(os.getenv("MAIN_PORT", "9377"))
UI_PORT = int(os.getenv("UI_PORT", "9378"))

yaml = YAML()  # round-trip 模式，尽量保留注释与引号风格
yaml.preserve_quotes = True
yaml.width = 4096

app = FastAPI()


# ---------------- 配置读写 ----------------

# api.yaml 落库加密（DPAPI）：读时自动解密，写时自动加密，明文不落盘
sys.path.insert(0, str(BASE / "uni-api"))
import keyvault  # noqa: E402


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.load(keyvault.open_text(f.read())) or {}


def save_config(data: dict) -> None:
    import io

    stream = io.StringIO()
    yaml.dump(data, stream)
    sealed = keyvault.seal_text(stream.getvalue())
    # 备份文件同样保存密文（.bak 里绝不允许出现明文 Key）
    backup = CONFIG_PATH.with_suffix(".yaml.bak")
    try:
        backup.write_text(keyvault.seal_text(CONFIG_PATH.read_text(encoding="utf-8")), encoding="utf-8")
    except OSError:
        pass
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(sealed)


def normalize_providers(raw: list, existing: list | None = None) -> tuple[list, list]:
    """把前端提交的渠道列表整理成 api.yaml 的结构。

    existing 是当前 api.yaml 里的渠道列表：同名渠道保留原有字段
    （preferences、headers、region 等手工添加的配置不会被保存清掉），
    只覆盖表单里编辑的 provider/base_url/api/model 四项。

    返回 (providers, warnings)：
    - 全空卡片（名字、URL、Key 都没填）视为误点"添加渠道"，直接忽略；
    - 填了一半的卡片不再静默丢弃，生成中文警告让前端弹出提示；
    - 渠道名相同的卡片自动合并（Key 与模型映射取并集），不产生重复渠道。
    """
    old_by_name: dict = {}
    for old in existing or []:
        old_name = str(old.get("provider") or "").strip()
        if old_name and old_name not in old_by_name:
            old_by_name[old_name] = old

    warnings: list = []
    merged: dict = {}
    meta: dict = {}   # 渠道名 -> {daily_quota, costs}（跨同名卡片合并）
    order: list = []

    def parse_models(item) -> tuple[list, dict]:
        """解析模型映射输入行，返回 (mapping, costs)。

        每行支持三种写法：
        - `对外名`（同名直通）
        - `对外名: 上游名`（重命名）
        - `对外名: 上游名: 单次消耗`（第 3 段为该模型每次调用消耗的额度，
          如魔搭魔粒；对外名作键写入 preferences.MODEL_COST）
        """
        mapping = []
        costs: dict = {}
        for line in item.get("models") or []:
            text = str(line).strip()
            if not text:
                continue
            parts = [seg.strip() for seg in text.split(":")]
            if len(parts) == 1:
                if parts[0]:
                    mapping.append(parts[0])      # 同名: 纯字符串
                continue
            external, upstream = parts[0], parts[1]
            if not external:
                continue
            if not upstream or upstream == external:
                mapping.append(external)          # 同名: 纯字符串
            else:
                mapping.append({upstream: external})  # 官方语义 {上游名: 对外名}
            if len(parts) >= 3 and parts[2]:
                try:
                    costs[external] = float(parts[2])
                except ValueError:
                    pass
        return mapping, costs

    for item in raw or []:
        name = str(item.get("provider") or "").strip()
        base_url = str(item.get("base_url") or "").strip()
        keys = [k.strip() for k in (item.get("keys") or []) if str(k).strip()]
        if not name and not base_url and not keys:
            continue  # 全空卡片：用户误点"添加渠道"，忽略
        if not name:
            preview = (keys[0][:8] + "…") if keys else "无"
            warnings.append(f"有一张渠道卡片没填「渠道名」（Key: {preview}），未保存")
            continue
        if not base_url:
            warnings.append(f"渠道「{name}」没填 Base URL，未保存")
            continue
        if not keys:
            warnings.append(f"渠道「{name}」没填任何 API Key，未保存")
            continue
        mapping, costs = parse_models(item)
        if name in merged:
            # 同名合并：Key 与模型映射取并集，顺序保持先出现者优先
            base = merged[name]
            old_keys = base["api"] if isinstance(base["api"], list) else [base["api"]]
            base["api"] = old_keys + [k for k in keys if k not in old_keys]
            base_models = base["model"]
            for m in mapping:
                if m not in base_models:
                    base_models.append(m)
            base_meta = meta[name]
            if base_meta.get("daily_quota") in (None, ""):
                base_meta["daily_quota"] = item.get("daily_quota")
            for cost_name, cost_value in costs.items():
                base_meta["costs"].setdefault(cost_name, cost_value)
            continue
        old = old_by_name.get(name, {})
        provider = {
            key: value
            for key, value in old.items()
            if key not in {"provider", "base_url", "api", "model", "preferences"}
            and not str(key).startswith("_")
        }
        provider.update(
            {
                "provider": name,
                "base_url": base_url,
                "api": keys[0] if len(keys) == 1 else keys,
                "model": mapping or ["gpt-4o-mini"],
            }
        )
        # 保留手工配置的 preferences（TOKEN_RATE_LIMIT 等），AUTO_RETRY 显式写入；
        # 表单里的每日额度/单次消耗写入 DAILY_QUOTA / MODEL_COST（留空则移除）。
        preferences = dict(old.get("preferences") or {})
        preferences["AUTO_RETRY"] = bool(item.get("auto_retry", True))
        daily_raw = item.get("daily_quota")
        if daily_raw in (None, ""):
            preferences.pop("DAILY_QUOTA", None)
        else:
            try:
                preferences["DAILY_QUOTA"] = float(daily_raw)
            except (TypeError, ValueError):
                warnings.append(f"渠道「{name}」的每日额度 {daily_raw!r} 不是数字，已忽略")
        if costs:
            preferences["MODEL_COST"] = dict(costs)
        provider["preferences"] = preferences
        meta[name] = {"daily_quota": item.get("daily_quota"), "costs": dict(costs)}
        merged[name] = provider
        order.append(name)

    providers = [merged[n] for n in order]
    return providers, warnings


def normalize_api_keys(raw: list, existing: list | None = None) -> list:
    """前端提交的统一 Key 白名单整理成 api.yaml 的结构。

    同名 Key 保留原有字段（preferences、weights 等手工配置不丢）。
    """
    old_by_key: dict = {}
    for old in existing or []:
        old_api = str(old.get("api") or "").strip()
        if old_api and old_api not in old_by_key:
            old_by_key[old_api] = old

    result = []
    for item in raw or []:
        key = str(item.get("api") or "").strip()
        if not key:
            continue
        models = [m.strip() for m in (item.get("model") or []) if str(m).strip()]
        entry = {
            k: v
            for k, v in old_by_key.get(key, {}).items()
            if k not in {"api", "model"} and not str(k).startswith("_")
        }
        entry["api"] = key
        entry["model"] = models or ["all"]
        result.append(entry)
    return result


# ---------------- 服务管理 ----------------

# pythonw 无控制台，子进程默认会各新开一个控制台窗口——页面每 5 秒轮询
# /api/status 时 netstat/tasklist 会不停闪烁，统一加上隐藏窗口标志。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run_text(cmd: list[str]) -> str:
    """中文 Windows 下 netstat/tasklist 输出 GBK，做自适应解码。"""
    r = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW)
    text = r.stdout.decode("utf-8", errors="replace")
    if "\ufffd" in text:
        text = r.stdout.decode("gbk", errors="replace")
    return text


def server_pid() -> str | None:
    out = _run_text(["netstat", "-ano"])
    for line in out.splitlines():
        parts = line.split()
        if (
            len(parts) == 5
            and parts[3] == "LISTENING"
            and parts[1].rsplit(":", 1)[-1] == str(MAIN_PORT)
        ):
            return parts[4]
    return None


def server_memory_mb(pid: str | None) -> float | None:
    if not pid:
        return None
    out = _run_text(["tasklist", "/FI", f"PID eq {pid}"])
    for line in out.splitlines():
        if f" {pid} " in line or line.strip().endswith(str(pid)):
            match = re.search(r"([\d,]+)\s*K\b", line)
            if match:
                return round(int(match.group(1).replace(",", "")) / 1024, 1)
    return None


def start_server() -> None:
    env = dict(os.environ, PORT=str(MAIN_PORT))
    log = open(RUN_LOG, "ab")
    subprocess.Popen(
        [sys.executable, str(BASE / "uni-api" / "main.py")],
        cwd=str(BASE),
        env=env,
        stdout=log,
        stderr=log,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
    )


# ---------------- 接口 ----------------

@app.get("/")
def index():
    # no-store：防止浏览器缓存旧版页面（历史上两次"保存后消失"都源于此）
    from fastapi.responses import HTMLResponse

    return HTMLResponse(content=HTML_PAGE, headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/api/status")
def status():
    pid = server_pid()
    return {
        "main_port": MAIN_PORT,
        "running": pid is not None,
        "pid": pid,
        "memory_mb": server_memory_mb(pid),
    }


@app.get("/api/quota")
def quota_proxy():
    """实时额度：代理主服务的 /v1/quota-status（token 窗口只在主服务内存里）。"""
    import json as _json
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{MAIN_PORT}/v1/quota-status", timeout=5
        ) as resp:
            return _json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return {"error": str(exc), "providers": []}


@app.get("/api/config")
def get_config():
    data = load_config()
    providers = []

    def model_pairs(model_field):
        """解析 model 字段，返回 (对外名, 上游真实名) 列表。

        官方语义: `- X: Y` 表示 X=上游服务商模型名, Y=重命名后的对外名字。
        兼容字典映射与 [字符串/单键映射] 列表两种写法。
        """
        pairs = []
        entries: list = []
        if isinstance(model_field, dict):
            entries = [{k: v} for k, v in model_field.items()]
        elif isinstance(model_field, list):
            entries = model_field
        for entry in entries:
            if isinstance(entry, dict):
                for upstream, external in entry.items():
                    pairs.append((str(external), str(upstream)))
            elif entry is not None:
                pairs.append((str(entry), str(entry)))
        return pairs

    for p in data.get("providers") or []:
        api = p.get("api")
        keys = api if isinstance(api, list) else [api]
        prefs = p.get("preferences") or {}
        cost_raw = prefs.get("MODEL_COST") or {}
        models = []
        for ext, up in model_pairs(p.get("model")):
            cost = cost_raw.get(ext, cost_raw.get(up))
            if cost is not None:
                models.append(f"{ext}: {up}: {cost}")
            elif up != ext:
                models.append(f"{ext}: {up}")
            else:
                models.append(ext)
        providers.append(
            {
                "provider": p.get("provider"),
                "base_url": p.get("base_url"),
                "keys": keys,
                "models": models,
                "auto_retry": bool(prefs.get("AUTO_RETRY", True)),
                "daily_quota": prefs.get("DAILY_QUOTA"),
            }
        )
    api_keys = [
        {"api": k.get("api"), "model": list(k.get("model") or [])}
        for k in data.get("api_keys") or []
    ]
    return {"providers": providers, "api_keys": api_keys}


def _save_log(msg: str) -> None:
    """保存链路专用日志（定位"保存后 Key 消失"用），带时间戳追加写入。"""
    from datetime import datetime

    try:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        with open("config-ui-save.log", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def _mask(key) -> str:
    key = str(key or "")
    return f"{key[:5]}****{key[-4:]}" if len(key) > 12 else ("(空)" if not key else key)


@app.post("/api/frontend-log")
def frontend_log(payload: dict):
    """接收前端自检日志（页面内存中的 Key 状态、JS 报错），写同一个日志文件。"""
    try:
        for card in payload.get("cards", []):
            _save_log(
            f"  [前端内存] 卡片 {card.get('name')!r}: keys={card.get('keys')}"
            + (f" models={card.get('models')}" if card.get("models") is not None else "")
        )
        if payload.get("js_error"):
            _save_log(f"  [JS报错] {payload['js_error']}")
    except Exception as exc:
        _save_log(f"[前端日志解析失败] {exc}")
    return {"ok": True}


@app.post("/api/config")
def post_config(payload: dict):
    _save_log("=" * 60)
    _save_log(f"收到保存请求：{len(payload.get('providers') or [])} 张渠道卡片")
    for idx, item in enumerate(payload.get("providers") or []):
        keys = [k for k in (item.get("keys") or []) if str(k).strip()]
        _save_log(
            f"  卡片[{idx}] 渠道名={item.get('provider')!r} "
            f"base_url={item.get('base_url')!r} keys={len(keys)}把{[_mask(k) for k in keys]} "
            f"模型={len(item.get('models') or [])}行 每日额度={item.get('daily_quota')!r} "
            f"auto_retry={item.get('auto_retry')!r}"
        )
    data = load_config()
    data["providers"], warnings = normalize_providers(
        payload.get("providers"), data.get("providers")
    )
    data["api_keys"] = normalize_api_keys(payload.get("api_keys"), data.get("api_keys"))
    if warnings:
        _save_log(f"归一化警告：{warnings}")
    else:
        _save_log("归一化警告：无")
    if not data["providers"]:
        _save_log("拒绝保存：没有任何可用渠道")
        return JSONResponse({"error": "至少需要一个渠道"}, status_code=400)
    for p in data["providers"]:
        api = p.get("api")
        keys = api if isinstance(api, list) else [api]
        _save_log(f"写入渠道 {p.get('provider')}: keys={[_mask(k) for k in keys if k]}")
    save_config(data)
    _save_log("保存完成（已写 api.yaml）")
    return {"saved": True, "warnings": warnings}


@app.post("/api/server/{action}")
def server_action(action: str):
    pid = server_pid()
    if action == "stop":
        if pid:
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, creationflags=_NO_WINDOW)
            return {"ok": True, "stopped": pid}
        return {"ok": False, "message": "服务未在运行"}
    if action == "restart":
        if pid:
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, creationflags=_NO_WINDOW)
            time.sleep(1.5)
        start_server()
        return {"ok": True}
    if action == "start":
        if pid:
            return {"ok": False, "message": "服务已在运行"}
        start_server()
        return {"ok": True}
    return JSONResponse({"error": "unknown action"}, status_code=404)


@app.post("/api/test/{model}")
async def test_model(model: str):
    """通过主服务试调用一个模型，验证渠道可用性。"""
    data = load_config()
    key = next(
        (k.get("api") for k in data.get("api_keys") or [] if k.get("api")), None
    )
    if not key:
        return {"ok": False, "error": "api.yaml 里没有配置统一 Key"}
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "回复\"测试成功\"四个字"}],
        "max_tokens": 20,
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"http://127.0.0.1:{MAIN_PORT}/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=body,
            )
        result = r.json()
        if r.status_code == 200 and result.get("choices"):
            text = result["choices"][0]["message"]["content"]
            return {"ok": True, "reply": text.strip()[:80]}
        return {"ok": False, "error": str(result)[:300]}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------------- 页面 ----------------

HTML_PAGE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>MyAPI 聚合网关 · 配置</title>
<style>
  :root { --bg:#f5f6f8; --card:#fff; --line:#e3e5ea; --txt:#1c2330; --sub:#6b7280;
          --pri:#2563eb; --ok:#16a34a; --bad:#dc2626; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--txt);
         font:14px/1.6 "Segoe UI","Microsoft YaHei",sans-serif; }
  .wrap { max-width:960px; margin:0 auto; padding:24px 16px 80px; }
  h1 { font-size:20px; margin:0 0 4px; }
  .sub { color:var(--sub); margin-bottom:20px; font-size:13px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:16px; margin-bottom:16px; }
  .row { display:flex; gap:12px; flex-wrap:wrap; align-items:center; }
  .field { flex:1; min-width:220px; }
  label { display:block; font-size:12px; color:var(--sub); margin-bottom:4px; }
  input, textarea { width:100%; border:1px solid var(--line); border-radius:6px;
          padding:6px 8px; font:inherit; background:#fbfbfc; }
  textarea { font-family:Consolas,monospace; font-size:12.5px; }
  input:focus, textarea:focus { outline:none; border-color:var(--pri); }
  button { border:none; border-radius:6px; padding:7px 14px; font:inherit;
          cursor:pointer; background:var(--pri); color:#fff; }
  button.ghost { background:#fff; color:var(--txt); border:1px solid var(--line); }
  button.danger { background:#fff; color:var(--bad); border:1px solid #f3c1c1; }
  button.mini { padding:3px 10px; font-size:12px; }
  .prov-head { display:flex; justify-content:space-between; align-items:center;
          margin-bottom:10px; }
  .prov-title { font-weight:600; }
  .mono { font-family:Consolas,monospace; font-size:12.5px;
          background:#eef2ff; padding:2px 6px; border-radius:4px; word-break:break-all; }
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%;
          margin-right:6px; }
  .ok { background:var(--ok); } .bad { background:var(--bad); } .warn { background:#f59e0b; }
  .muted { color:var(--sub); font-size:12px; }
  .testout { margin-top:8px; font-size:12.5px; white-space:pre-wrap; }
  .hint { color:var(--sub); font-size:12px; margin-top:6px; }
  .topbtns { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:16px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>MyAPI 聚合网关</h1>
  <div class="sub">
    调用入口 <span class="mono" id="entry">http://localhost:9377/v1</span>，
    统一 Key 见下方"对外 Key"。改完配置记得 <b>保存并重启</b>。
  </div>

  <div class="card">
    <div class="row">
      <div class="field">
        <label>主服务状态</label>
        <div><span class="dot warn" id="dot"></span><span id="stat">检查中…</span>
          <span class="muted" id="mem"></span></div>
      </div>
      <button class="ghost" onclick="serverAct('start')">启动</button>
      <button class="ghost" onclick="serverAct('restart')">重启</button>
      <button class="danger" onclick="serverAct('stop')">停止</button>
    </div>
  </div>

  <div class="card">
    <div class="prov-head">
      <span class="prov-title">额度状态（每 5 秒自动刷新）</span>
      <button class="ghost mini" onclick="loadQuota()">刷新</button>
    </div>
    <div id="quotaBody" class="hint" style="margin:0">加载中…</div>
  </div>

  <div class="card">
    <div class="prov-head"><span class="prov-title">对外 Key（填到客户端里的那个）</span></div>
    <div id="apikeyArea"></div>
    <div class="hint">这是你自己的统一入口 Key；真正的额度限制在各上游渠道。</div>
  </div>

  <div id="provList"></div>
  <div class="row">
    <button class="ghost" onclick="addProv()">＋ 添加渠道</button>
    <button onclick="saveAndRestart()">保存并重启服务</button>
    <button class="ghost" onclick="save()">仅保存</button>
    <span class="muted" id="saveMsg"></span>
  </div>
</div>

<script>
let CFG = {providers: [], api_keys: []};
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function refresh() {
  const [cfg, st] = await Promise.all([
    fetch('/api/config').then(r => r.json()),
    fetch('/api/status').then(r => r.json())
  ]);
  CFG = cfg;
  render(st);
}

function render(st) {
  const dot = document.getElementById('dot'), stat = document.getElementById('stat');
  if (st.running) { dot.className = 'dot ok'; stat.textContent = '运行中 (PID ' + st.pid + ')';
    document.getElementById('mem').textContent = '内存 ' + st.memory_mb + ' MB';
  } else { dot.className = 'dot bad'; stat.textContent = '未运行';
    document.getElementById('mem').textContent = ''; }

  const ka = document.getElementById('apikeyArea'); ka.innerHTML = '';
  (CFG.api_keys.length ? CFG.api_keys : [{api:'', model:[]}]).forEach((k, i) => {
    ka.innerHTML += `
      <div class="row" style="margin-bottom:8px">
        <div class="field"><label>Key</label>
          <input value="${esc(k.api)}" oninput="CFG.api_keys[${i}].api=this.value"></div>
        <div class="field"><label>允许调用的模型（每行一个，all=全部）</label>
          <textarea rows="2" oninput="CFG.api_keys[${i}].model=this.value.split('\\n')">${esc((k.model||[]).join('\\n'))}</textarea></div>
      </div>`;
  });

  const pl = document.getElementById('provList'); pl.innerHTML = '';
  CFG.providers.forEach((p, i) => {
    pl.innerHTML += `
    <div class="card">
      <div class="prov-head">
        <span class="prov-title">渠道 ${i + 1}: ${esc(p.provider)}</span>
        <div>
          <button class="ghost mini" onclick="testProv(${i}, this)">测试</button>
          <button class="danger mini" onclick="delProv(${i})">删除</button>
        </div>
      </div>
      <div class="row">
        <div class="field"><label>渠道名</label>
          <input value="${esc(p.provider)}" oninput="CFG.providers[${i}].provider=this.value"></div>
        <div class="field" style="flex:2"><label>Base URL</label>
          <input value="${esc(p.base_url)}" oninput="CFG.providers[${i}].base_url=this.value"></div>
      </div>
      <div class="row" style="margin-top:8px">
        <div class="field"><label>API Key（每行一个，多个自动轮询）</label>
          <textarea rows="3" oninput="setKeys(${i},this)">${esc((p.keys||[]).join('\\n'))}</textarea></div>
        <div class="field"><label>模型映射（每行: 对外名: 上游名[: 单次消耗]）</label>
          <textarea rows="3" oninput="setModels(${i},this)">${esc((p.models||[]).join('\\n'))}</textarea></div>
      </div>
      <div class="row" style="margin-top:8px">
        <div class="field"><label>每日额度（如魔搭 250 魔粒/天，留空不启用）</label>
          <input value="${esc(p.daily_quota ?? '')}" oninput="CFG.providers[${i}].daily_quota=this.value"></div>
        <label style="display:flex;align-items:center;gap:6px;margin:0">
          <input type="checkbox" style="width:auto" ${p.auto_retry !== false ? 'checked' : ''}
            onchange="CFG.providers[${i}].auto_retry=this.checked">
          失败自动切换下一个渠道 (AUTO_RETRY)</label>
      </div>
      <div class="testout" id="out-${i}"></div>
    </div>`;
  });
}

window.onerror = function(msg, src, line, col) {
  try { fetch('/api/frontend-log', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({js_error: msg + ' @' + (src || 'inline') + ':' + line + ':' + col})}); } catch (e) {}
};

var _lastKeyLog = 0;
function _reportCard(i) {
  var now = Date.now();
  if (now - _lastKeyLog > 1500) { _lastKeyLog = now;
    var p = CFG.providers[i] || {};
    try { fetch('/api/frontend-log', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({cards: [{name: p.provider,
        keys: (p.keys || []).map(function(k) {
          return k && k.trim() ? (k.slice(0, 5) + '****' + k.slice(-4) + '(长度' + k.length + ')') : '(空行)';
        }),
        models: (p.models || []).slice()}]})}); } catch (e) {}
  }
}
function setKeys(i, el) {
  CFG.providers[i].keys = el.value.split('\\n');
  _reportCard(i);
}
function setModels(i, el) {
  CFG.providers[i].models = el.value.split('\\n');
  _reportCard(i);
}

function collect() {
  return {
    providers: CFG.providers,
    api_keys: CFG.api_keys.filter(k => k.api && String(k.api).trim())
  };
}

async function save() {
  try { fetch('/api/frontend-log', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({cards: CFG.providers.map(function(p) {
      return {name: p.provider,
        keys: (p.keys || []).map(function(k) {
          return k && k.trim() ? (k.slice(0, 5) + '****' + k.slice(-4)) : '(空)';
        }),
        models: (p.models || []).slice()};
    })})}); } catch (e) {}
  const r = await fetch('/api/config', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(collect())});
  const j = await r.json();
  document.getElementById('saveMsg').textContent = j.saved ? '已保存 ✓' : ('失败: ' + j.error);
  if (j.warnings && j.warnings.length)
    alert('以下内容没有保存：\\n' + j.warnings.join('\\n') +
      '\\n\\n（同名渠道的多张卡片会自动合并 Key；全空卡片直接忽略）');
  setTimeout(() => document.getElementById('saveMsg').textContent = '', 3000);
  return j.saved;
}

async function saveAndRestart() { if (await save()) serverAct('restart', true); }

async function serverAct(act, skipSave) {
  if (!skipSave) await save();
  const r = await fetch('/api/server/' + act, {method: 'POST'});
  const j = await r.json();
  if (j.message) alert(j.message);
  setTimeout(refresh, act === 'stop' ? 300 : 2500);
}

function addProv() {
  CFG.providers.push({provider: '', base_url: '', keys: [''], models: [''], auto_retry: true});
  render({running: document.getElementById('dot').className.includes('ok')});
}

function delProv(i) {
  if (!confirm('删除渠道 ' + (CFG.providers[i].provider || (i + 1)) + ' ?')) return;
  CFG.providers.splice(i, 1); refresh();
}

async function testProv(i, btn) {
  btn.disabled = true; btn.textContent = '测试中…';
  const out = document.getElementById('out-' + i);
  out.textContent = '';
  if (!await save()) { btn.disabled = false; btn.textContent = '测试'; return; }
  const models = (CFG.providers[i].models || []).filter(Boolean);
  if (!models.length) { out.textContent = '该渠道没有配置模型映射'; }
  for (const m of models) {
    const name = m.includes(':') ? m.split(':')[0].trim() : m;
    const r = await fetch('/api/test/' + encodeURIComponent(name), {method: 'POST'});
    const j = await r.json();
    out.textContent += (j.ok ? '✓ ' : '✗ ') + name + ': ' +
      (j.ok ? ('回复正常: ' + j.reply) : j.error) + '\\n';
  }
  btn.disabled = false; btn.textContent = '测试';
}

refresh();
setInterval(() => fetch('/api/status').then(r => r.json()).then(st => {
  const dot = document.getElementById('dot'), stat = document.getElementById('stat');
  if (st.running) { dot.className = 'dot ok'; stat.textContent = '运行中 (PID ' + st.pid + ')';
    document.getElementById('mem').textContent = '内存 ' + st.memory_mb + ' MB';
  } else { dot.className = 'dot bad'; stat.textContent = '未运行';
    document.getElementById('mem').textContent = ''; }
}), 5000);

async function loadQuota() {
  const body = document.getElementById('quotaBody');
  try {
    const j = await fetch('/api/quota').then(r => r.json());
    if (j.error) { body.textContent = '读取失败: ' + j.error; return; }
    if (!j.providers || !j.providers.length) {
      body.textContent = '暂无额度数据（需要在渠道里配置每日额度或 token 限流）'; return;
    }
    let html = '';
    j.providers.forEach(pr => {
      html += '<div style="margin:6px 0 2px;font-weight:600">' + esc(pr.provider) + '</div>';
      (pr.keys || []).forEach(k => {
        const bits = [];
        if (k.daily_spent !== null && k.daily_spent !== undefined)
          bits.push('今日已用 ' + k.daily_spent + ' / ' + (pr.daily_quota ?? '—') +
            ' 魔粒，剩 ' + k.daily_remaining);
        (k.token_windows || []).forEach(w =>
          bits.push('本' + w.period + '秒窗口已用 ' + Math.round(w.used) + ' / ' + w.limit + ' tokens'));
        html += '<div style="margin-left:14px">' + esc(k.key) + '：' +
          esc(bits.join('；') || '无额度配置') + '</div>';
      });
      const mc = Object.entries(pr.model_cost || {}).map(([m, c]) => m + '=' + c).join(', ');
      html += '<div style="margin-left:14px;color:#777">单次消耗: ' +
        esc(mc || ('默认 ' + pr.default_cost)) + '</div>';
    });
    body.innerHTML = html;
  } catch (e) { body.textContent = '读取失败: ' + e; }
}
loadQuota();
setInterval(loadQuota, 5000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    if sys.stdout is None or sys.stderr is None:  # pythonw 无控制台，重定向到日志
        _log = open(BASE / "configui.log", "ab")
        sys.stdout = sys.stderr = _log
    uvicorn.run(app, host="127.0.0.1", port=UI_PORT, log_level="warning")
