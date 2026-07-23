# MiMo2API

MiMo2API 将小米 MiMo AI Studio 转换为 OpenAI-compatible API。当前版本使用 TypeScript、Node.js、Fastify 和 SQLite，只保留 Chat Completions、Responses、文本/图片/文件输入、工具调用、账号池与必要运维能力。

## 能力

- `POST /v1/chat/completions`：流式/非流式、reasoning、工具调用、`stream_options.include_usage`
- Responses 协议簇：create、typed SSE、retrieve、cancel、delete、input items、input tokens、compact
- 图片与文件：支持 data URL/base64 和远程 URL，自动执行 MiMo `genUploadInfo -> PUT -> parse`
- 工具调用：注入完整 JSON Schema，并在返回前使用 JSON Schema 校验参数
- SQLite：账号、配置、Responses 状态、usage 和保活队列统一持久化
- 账号保活：SQLite 租约、初始分散、低并发 worker、成功周期抖动、失败指数退避
- 账号并发：单账号请求硬上限、最空闲账号调度、可取消等待队列；保活和测号与推理共用账号租约
- 注册代理：每个注册会话独占一个 VLESS 节点、随机本地端口、配置目录和 `sing-box` 子进程；成功、失败、取消或超时后立即回收
- 会话：`X-MiMo-Session-Id` / `session_id` / `user` 驱动的 SQLite 粘性路由，同一会话固定账号和 MiMo `conversationId`
- 上下文：会话查询缓存带 TTL；累计 prompt tokens 达阈值后自动压缩上下文并轮换上游会话
- 管理接口：账号、temp-mail、VLESS 代理测活、用量、运行状态

不再提供 Anthropic、TTS、ASR、语音克隆和非文本生成接口。

## 启动

环境要求：Node.js 22 或更高版本。

```bash
npm ci
npm run check
npm run build
npm start
```

默认地址为 `http://0.0.0.0:8080`。管理页面使用 HTTP Basic，用户名固定为 `admin`，密码来自配置。

## 旧配置迁移

首次启动会读取 `MIMO2API_CONFIG_FILE` 指向的旧 `config.json`，将以下内容导入 SQLite：

- `mimo_accounts`，包括账号对象中的未知扩展字段
- `proxy_pool.sub_url` 及代理设置
- `temp_mail.api_base`、管理口令、站点口令和域名
- API keys、管理密码、模型列表和工具模式

迁移完成后在 SQLite `meta` 表写入 `legacy_config_imported`。原 JSON 不修改，后续在线读写只使用 SQLite。

默认数据库：

```text
${MIMO2API_DATA_DIR}/mimo2api.sqlite
```

生产环境应显式设置 `MIMO2API_DATABASE_FILE` 并将所在目录挂载到持久卷。

## Chat Completions

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{"model":"mimo-v2.5-pro","stream":true,"messages":[{"role":"user","content":"你好"}]}'
```

流式响应会立即发送 SSE comment，并每 10 秒发送 heartbeat。客户端断开后上游请求会取消；写入遵守 Node stream 背压。若设置 `stream_options.include_usage=true`，`[DONE]` 前会返回 `choices: []` 的 usage chunk。

连续 Chat 请求可传 `X-MiMo-Session-Id: <stable-id>`，或在请求体传 `session_id`。Responses 首次请求会自动创建会话；后续使用 `previous_response_id` 时会继承同一账号和上游会话。会话记录按 API Key 隔离，默认 3 天过期。累计输入达到 `MIMO2API_SESSION_COMPACT_THRESHOLD_TOKENS`（默认 `150000`）时，服务先生成压缩上下文，再切换到新的上游会话。

同一会话的请求会严格串行执行，避免多个请求并发写入同一个 MiMo `conversationId`。默认每个 MiMo 账号同时只承载一个推理、测号或保活操作；无会话请求选择当前最空闲账号，有会话请求在粘性账号前排队，不会为规避拥塞而换号。相关环境变量：

- `MIMO2API_ACCOUNT_MAX_CONCURRENCY=1`
- `MIMO2API_ACCOUNT_QUEUE_LIMIT=200`
- `MIMO2API_ACCOUNT_QUEUE_TIMEOUT_MS=600000`
- `MIMO2API_SESSION_QUEUE_LIMIT=200`

`GET /healthz` 的 `inference` 字段会返回当前活动请求数、等待请求数、忙碌账号数和会话队列状态。当前 SQLite 部署应保持单副本；若扩展为多副本，需要先把账号租约迁移为跨 Pod 协调机制。

## Responses

```bash
curl http://localhost:8080/v1/responses \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{"model":"mimo-v2.5-pro","input":"读取当前目录并总结","stream":true}'
```

支持的端点：

```text
POST   /v1/responses
GET    /v1/responses/:id
DELETE /v1/responses/:id
POST   /v1/responses/:id/cancel
GET    /v1/responses/:id/input_items
POST   /v1/responses/input_tokens
POST   /v1/responses/compact
POST   /v1/responses/:id/compact
```

Responses 流使用 typed SSE，包括 `response.created`、`response.in_progress`、output item/content part、output text、reasoning summary、function call arguments、`response.completed/failed`，每个事件都有单调递增的 `sequence_number`。usage 位于最终 Response 对象中，不发送 Chat 风格的 `[DONE]`。

后台 Responses 可轮询 `GET /v1/responses/:id`。状态为 `queued` 或 `in_progress` 时响应附带 `Retry-After: 1` 和 `poll_after_ms: 1000`；完成、失败或取消后不再建议继续轮询。

## 多模态输入

Chat 使用 `image_url` / `file` content part；Responses 使用 `input_image` / `input_file`。允许：

- `data:image/...;base64,...`
- `file_data` base64
- HTTPS 图片/文件 URL

单文件默认上限为 25 MiB，可用 `MIMO2API_MAX_UPLOAD_BYTES` 调整。音频输入和音频模型会返回 `unsupported_model`。

## 保活

自动保活只使用 `passToken`，不会后台批量发送 OTP 或自动尝试密码登录。调度规则：

- 默认并发 `1`
- 首次启动将账号稳定分散到默认 1 小时时间窗
- 每次任务持有 SQLite 租约，防止同一账号被重复领取
- 成功后约 6 小时再次续期，并加入 20% 抖动
- 失败从 15 分钟开始指数退避，最高不超过正常周期
- “续期全部”只排队，默认在 15 分钟窗口内分散执行

关键环境变量见 [deploy.env.example](./deploy.env.example)。

## 注册验证码

生产部署使用同 Pod 的 Python `ddddocr` sidecar。每张图片会执行原图、灰度增强、对比度增强和多阈值识别；默认连续 3 轮只使用本地 OCR，仍未通过后才调用已配置的 OpenAI-compatible 视觉模型（例如现有 `grok`）兜底。Node 主容器通过 `MIMO2API_CAPTCHA_OCR_URL=http://127.0.0.1:8090` 访问本地服务；OCR 模型及运行时不进入 Node 进程。

批次任务停止时会先进入 `stopping` 状态，取消当前 OCR、邮件轮询或网络请求，最终落为 `cancelled`。前端会持续轮询到取消完成。

## 验证

```bash
npm run check
docker build -t mimo2api:local .
```

测试覆盖配置迁移、SQLite 租约、工具参数校验、跨 chunk 流解析、Responses 状态和 typed SSE。
