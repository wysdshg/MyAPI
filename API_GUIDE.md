# MyAPI 统一网关接入说明

> 给客户端 / AI 助手的接入速查。网关兼容 OpenAI Chat Completions 协议，任何支持
> "自定义 OpenAI 兼容接口"的工具都能直接使用。

## 基本信息

| 项 | 值 |
|---|---|
| Base URL | `http://localhost:9377/v1` （仅本机可用） |
| 鉴权 | `Authorization: Bearer <统一APIKey>` |
| 统一APIKey | `sk-myapi-xxxx`（占位符；真实值在本地 `api.yaml` 的 `api_keys[0].api`，不要外传） |
| 协议 | OpenAI 兼容（/v1/chat/completions、/v1/embeddings、/v1/rerank、/v1/models） |

## 可用模型

| 对外模型名 | 上游 | 计费 | 特点 |
|---|---|---|---|
| `qwen3.8-flash-next` | 魔搭 Qwen/Qwen3.8-Flash-Next | 1 魔粒/次 | 快，支持图片多模态 |
| `glm-5.3-flash` | 魔搭 ZhipuAI/GLM-5.3-Flash | 1 魔粒/次 | 支持图片多模态 |
| `qwen3-8b` | 硅基流动 Qwen/Qwen3-8B | token 计量 | **默认开思考，建议显式关闭** |

### 向量与重排序（硅基流动）

| 对外模型名 | 类型 | 调用接口 | 用途 |
|---|---|---|---|
| `BAAI/bge-m3` | 向量 | `POST /v1/embeddings` | 中英文通用向量，1024 维 |
| `BAAI/bge-large-en-v1.5` | 向量 | `POST /v1/embeddings` | 英文向量，1024 维 |
| `BAAI/bge-reranker-v2-m3` | 重排序 | `POST /v1/rerank` | 文档相关性重排 |

```python
# 向量
e = client.embeddings.create(model="BAAI/bge-m3", input="要向量化的文本")
vec = e.data[0].embedding          # 长度 1024

# 重排序（注意：非 OpenAI SDK 内置方法，用 httpx 或 requests 直接 POST）
import httpx
r = httpx.post("http://localhost:9377/v1/rerank",
    headers={"Authorization": "Bearer <统一APIKey>"},
    json={
        "model": "BAAI/bge-reranker-v2-m3",
        "query": "苹果公司创始人是谁",
        "documents": ["史蒂夫·乔布斯创办了苹果公司", "今天天气很好", "苹果是一种水果"],
        "top_n": 3,                 # 可选：只返回前 N 条
    }, timeout=60)
# r.json()["results"] = [{"index": 0, "relevance_score": 0.9989}, ...]
# results 按 relevance_score 从高到低排列，index 是 documents 数组下标
```

## 客户端示例

### OpenAI SDK（推荐）

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:9377/v1",
    api_key="<统一APIKey>",
)

resp = client.chat.completions.create(
    model="qwen3-8b",
    messages=[{"role": "user", "content": "你好"}],
    extra_body={"enable_thinking": False},   # 关思考：1秒出结果；不关要等60~90秒
    timeout=300,                              # 全满排队最长120秒，超时务必给足
)
print(resp.choices[0].message.content)
```

### curl

```bash
curl http://localhost:9377/v1/chat/completions \
  -H "Authorization: Bearer <统一APIKey>" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3-8b","messages":[{"role":"user","content":"你好"}],"enable_thinking":false}'
```

### 流式输出

请求加 `"stream": true`，按 OpenAI SSE 格式逐块返回，`[DONE]` 结束。
思考型模型流式输出中会先出现 `reasoning_content` 增量，再出现 `content` 增量。

### 图片多模态（魔搭模型）

```python
resp = client.chat.completions.create(
    model="qwen3.8-flash-next",
    messages=[{"role": "user", "content": [
        {"type": "text", "text": "描述这幅图"},
        {"type": "image_url", "image_url": {"url": "https://example.com/pic.jpg"}},
    ]}],
)
```

## 高级参数（可选）

| 参数 | 说明 |
|---|---|
| `provider_key_index` | 请求体字段或 `X-Key-Index` 请求头，整数 N 表示使用该渠道第 N+1 把上游 Key（0 = 第一把，越界自动取模）。网关读取后剥离，不会透传给上游。不传则自动轮换 |
| `enable_thinking` | `false` 关闭思考模式（qwen3-8b 强烈建议，速度差 60 倍）；默认跟随上游 |

## 限流与 429 处理（重要）

- **魔搭**：每账号每天 250 魔粒，本地时间 0 点重置；多账号自动轮换，用完自动切下一个
- **硅基**：每把 Key 49000 tokens/分钟滑动窗口；发送前预检，全满时网关内部排队最长 120 秒
- **收到 429 必须这样做**：读取响应头 `Retry-After`（秒数），休眠该时长后重试同一请求
- **客户端超时必须 ≥ 300 秒**：排队等待 + 生成时间都在这一窗口内
- 429 的 `detail` 字段会说明具体原因（额度耗尽 / token 窗口满 / 排队满）

## 状态查询

```
GET /v1/quota-status      # 需鉴权；返回每把 Key 的额度用量/窗口余量/排队人数
```

配置面板（人工查看）：http://localhost:9378

## 错误码速查

| 状态码 | 含义 |
|---|---|
| 200 | 成功 |
| 400 | 请求格式错误（检查 JSON / 模型名） |
| 401 | 统一 APIKey 错误 |
| 429 | 额度/窗口/排队限制，按 Retry-After 重试 |
| 5xx | 上游异常，稍后重试 |
