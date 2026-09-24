# MyAPI — 本地大模型 API 聚合网关（uni-api · Windows 原生）

把各平台领的免费 Key 全部挂进来，对外只暴露一个地址 + 一个 Key。
某个 Key/渠道额度用完、被限速或失效时，自动切换到下一个，调用方完全无感。

**无需 Docker**，直接用本机 Python 3.11 运行（源码已做 Windows 适配补丁）。

## 地址（已避开常见端口）

| 用途 | 地址 |
|------|------|
| **API 调用入口**（填到客户端里） | `http://localhost:9377/v1` |
| 统一 API Key | `sk-myapi-xxxx`（首次部署时自己在 `api.yaml` 里生成一个随机 Key） |
| **图形配置页面**（浏览器打开） | `http://localhost:9378` |

局域网其他设备把 `localhost` 换成本机 IP 即可（配置页面只监听本机，不对外）。

## 日常使用

**改渠道/加 Key（推荐用图形界面）**：
1. 双击 `start-configui.bat`，浏览器打开 `http://localhost:9378`
2. 界面里增删渠道、填 Key（多个 Key 每行一个自动轮询）、配模型映射
3. 点「保存并重启服务」，或每个渠道旁边的「测试」验证额度是否可用

**命令行方式**：直接编辑 `api.yaml`（模板示例在 `api-examples.yaml`，不会被覆盖），
重启主服务生效。

## 启动 / 停止

| 脚本 | 作用 |
|------|------|
| `start-uniapi.bat` | 启动主服务（窗口保持开启；放 `shell:startup` 可开机自启） |
| `stop-uniapi.bat` | 停止主服务 |
| `start-configui.bat` | 启动图形配置页面（后台无窗口） |
| `stop-configui.bat` | 停止配置页面（主服务不受影响） |

## 内存占用（实测）与优化

| 进程 | 物理内存 | 提交内存 |
|------|---------|---------|
| 主服务（网关本体） | ~89 MB | ~75 MB |
| 图形配置页面 | ~54 MB | ~43 MB |

- 主服务 75MB 已经是这套 FastAPI 技术栈的合理水平（相当于一个浏览器标签页），
  没有进一步压缩的必要；高负载转发时会到 100~150MB，仍很轻。
- **推荐优化**：配置页面不用常驻——需要改配置时 `start-configui.bat` 拉起，
  改完 `stop-configui.bat` 关掉，省 ~55MB。只跑主服务常驻即可。
- 长期挂机时 Windows 会自动把空闲进程换出物理内存，实际占用更低。

## 自动切换原理

- 上游返回 429（限速）/ 402（欠费）/ 403 / 5xx 时，uni-api 按
  「同渠道下一个 Key → 冷却该渠道 → 换下一个渠道」重试，无需手动干预。
- `preferences: AUTO_RETRY: true` 开启跨渠道自动重试（图形界面勾选框对应此项）。
- 额度第二天刷新后，冷却结束的渠道自动恢复参与轮询。
- 用量统计在 `data/`；主服务日志在 `uni-api-run.log`。

## 渠道账号额度限制（按 Key 记账）

在渠道的 `preferences` 里配置。没配这两项的渠道行为完全不变；
配置页面点「保存」不会清掉这些手写字段（`config-ui.py` 按渠道名合并保存）。

### 每日额度（例：魔搭每天 250 魔粒）

```yaml
preferences:
  DAILY_QUOTA: 250              # 该渠道每个账号 Key 每天的总消耗上限
  MODEL_COST:                   # 单次调用扣多少（没列出的模型默认扣 1）
    qwen2.5-7b-instruct: 0.5    # 外部名或上游真实名写法都认
    Qwen3-30B-A3B: 1
    Qwen3-235B-A22B: 2
    default: 1
```

- 按本地日期 0 点自动重置，状态存 `data/quota_state.json`
  （Key 存 sha256 摘要不落明文，重启/热更新不丢；文件损坏则按空状态重来）。
- 扣费发生在「选中 Key 发起请求」时，请求失败也会计入（宁多扣不超扣）。
- 某个账号当天用完 → 自动跳过它轮询同渠道下一个账号；
  该渠道全部账号用完 → 返回 429（原因写明 `Daily quota exhausted ...`）
  并自动切换到下一个渠道。

### 账号级 token 滑动窗口（例：硅基流动 50000 token/分钟）

```yaml
preferences:
  TOKEN_RATE_LIMIT: 50000/min   # 或直接写 50000（等于 50000/min）
```

- 提示词+回复总 token 按上游实际返回的 usage 在请求结束时累计。
- 窗口只存内存（重启清零），统计是「先查后放行」，超高并发下可能少量超出，
  上游真限速了仍走原有的 429 冷却兜底。
- 单个账号窗口满 → 跳过它；该渠道所有账号都满 → 429 并切下一个渠道
  （原因写明 `Token rate limit ...`）。

## 文件说明

- `api.yaml` — 实时配置（图形界面直接改写的就是它）
- `api-examples.yaml` — 渠道配置模板参考（不会被程序改写）
- `config-ui.py` — 图形配置页面（FastAPI 单文件，仅监听 127.0.0.1）
- `uni-api/` — 源码（含 Windows 适配补丁：`uni_api/admission/memory.py`
  用 msvcrt 替代 fcntl 文件锁、os.open 必须带 O_BINARY（否则共享预留账本
  被文本模式读坏、全机内存预留被拒产生 503）、`uni_api/admission/resources.py`
  对 resource 模块降级、`uni_api/rust_responses_snapshot.py` 跳过 Windows
  没有的 os.fchmod；测试侧 errno 名断言按平台推导（Windows 是
  WSAECONNRESET）、依赖 fcntl 的内核采样测试在 Windows 上跳过。
  `git pull` 更新源码后需确认补丁仍在）
- `uni-api/` 里另有本项目自己的功能改动（`git pull` 后如被覆盖需重做）：
  新增 `uni_api/rate_limit/quota.py`（账号额度记账），改动
  `uni_api/rate_limit/key_pool.py`、`uni_api/config/legacy_loader.py`、
  `uni_api/routing/core.py`、`uni_api/runtime.py`、
  `uni_api/streaming/logging_response.py` 的额度挂钩，
  以及 `test/test_provider_quota.py`（见上节「渠道账号额度限制」）
- `docker-compose.yml` — 旧的 Docker 方案（已弃用，可删）

## 模型映射写法（命令行编辑时用）

```yaml
model:
- gpt-4o-mini                        # 同名：直接写
- deepseek-ai/DeepSeek-V3: deepseek-v3   # 改名：左边=上游真实名，右边=对外名字
```
