# MiMo2API

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-teal)](https://fastapi.tiangolo.com/)

将**小米 MiMo AI Studio** 网页端对话转换为 **OpenAI 兼容 API**，支持多模态（文本 + 图片 + 文件）、工具调用（Function Calling）、Anthropic Messages API、多账号负载均衡。


本项目基于原[mimo2api](https://github.com/Water008/MiMo2API) 修改。
本项目所修改代码均为ai完成，不含任何一句人工代码，望周知！

> **💡 不需要工具调用或需要 TTS 语音合成？** 建议使用 [`no-tools` 分支](#无工具分支-no-tools) — 不注入工具 prompt，上下文更干净、输出质量更高，且完整保留 TTS 语音合成功能。



## 目录

- [特性](#特性)
- [架构](#架构)
- [快速开始](#快速开始)
  - [一键部署](#一键部署)
  - [手动安装](#手动安装)
- [配置凭证](#配置凭证)
  - [方法1：Cookie 导入](#方法1cookie-导入)
  - [方法2：cURL 导入](#方法2curl-导入)
  - [多账号管理](#多账号管理)
- [API 使用](#api-使用)
  - [列出模型](#1-列出模型)
  - [文本对话](#2-文本对话)
  - [流式对话](#3-流式对话)
  - [多模态（图片理解）](#4-多模态图片理解)
  - [文件上传](#5-文件上传文本文件)
  - [工具调用（Function Calling）](#6-工具调用function-calling)
  - [深度思考模式](#7-深度思考模式)
  - [模型发现与刷新](#8-模型发现与刷新)
- [Anthropic Messages API](#9-anthropic-messages-api)
- [Responses API 详解](#responses-api-详解)
- [工具调用详解](#工具调用详解)
- [无工具分支 (no-tools)](#无工具分支-no-tools)
- [管理命令](#管理命令)
- [项目结构](#项目结构)
- [配置参考](#配置参考)
- [依赖](#依赖)
- [限制与已知问题](#限制与已知问题)
- [常见问题](#常见问题)
- [许可](#许可)

## 特性

- **OpenAI 完全兼容** — 标准 `/v1/chat/completions`（流式/非流式）、`/v1/models`、`/v1/models/{id}` 端点，可直接对接 ChatBox、NextChat、LobeChat 等任何 OpenAI 客户端
- **Anthropic Messages API 兼容** — 完整支持 `/v1/messages`（流式/非流式）+ count_tokens + batches CRUD + message_get，共 9 个 Anthropic 端点，可对接 RikkaHub 等 Anthropic 客户端
- **工具调用（Function Calling）** — 5 种提取策略覆盖 MiMo 原生 XML (`<tool_call>`)、TOOL_CALL 标签、JSON、`<function_call>` XML、自由文本匹配，自动清洗响应中的工具残留
- **流式筛分** — 有工具调用时实时分离正文与工具调用内容，客户端无需等待完整响应即可逐步接收，RikkaHub 等不再全文缓冲
- **多模态支持** — omni 模型支持图片输入（URL、base64），自动完成三步上传流程（genUploadInfo → PUT → resource/parse）；所有模型支持文本文件上传（.md / .txt 等），同样走 MiMo 原生上传流程
- **深度思考** — 支持 reasoning_effort 参数，自动分离 `<think>` 块输出
- **多账号池** — 管理面板配置多个 MiMo 账号，轮询负载均衡，自动故障转移
- **动态模型发现** — 启动时从 MiMo 官方 API 实时拉取可用模型列表，无需手动维护
- **凭证管理** — 支持 Cookie 导入、cURL 导入两种配置方式
- **CORS 全开** — 允许任意来源跨域访问
- **无工具分支** — 提供 `no-tools` 分支，移除工具调用逻辑，适合纯对话场景，输出质量更高

## 架构

```
┌──────────────────────────────────────────────────────────┐
│                     OpenAI 兼容客户端                        │
│            (ChatBox / LobeChat / curl / SDK)              │
└───────────────┬──────────────────────────────────────────┘
                │  /v1/chat/completions
                ▼
┌──────────────────────────────────────────────────────────┐
│                     MiMo2API (FastAPI)                      │
│  ┌─────────┐  ┌──────────────┐  ┌──────────────────────┐ │
│  │ routes  │  │ tool_sieve │  │  tool_call   │  │     mimo_client      │ │
│  │ (API)   │──│ (流式筛分)  │──│ (5策略提取)   │──│ (HTTP/SSE 代理)       │ │
│  │anthropic │  │ anthropic  │  │    batch     │                      │ │
│  │ (路由)   │  │ (格式转换)  │  │ (存储/批处理) │                      │ │
│  └─────────┘  └──────────────┘  └──────────────────────┘ │
│  ┌─────────┐  ┌──────────────┐  ┌──────────────────────┐ │
│  │ config  │  │    utils     │  │      models           │ │
│  │ (多账号) │  │ (图片上传等)  │  │ (OpenAI 数据模型)     │ │
│  └─────────┘  └──────────────┘  └──────────────────────┘ │
└───────────────┬──────────────────────────────────────────┘
                │  HTTPS (SSE)
                ▼
┌──────────────────────────────────────────────────────────┐
│              MiMo API (aistudio.xiaomimimo.com)           │
│              /open-apis/bot/chat (SSE)                    │
└──────────────────────────────────────────────────────────┘
```

## 快速开始

### 一键部署

```bash
# 直接克隆（推荐）
git clone https://github.com/Fly143/MiMo2API.git
cd MiMo2API
chmod +x deploy.sh
./deploy.sh

```

部署完成后，服务已在 **前台** 启动。见下方[管理命令](#管理命令)了解后台运行等方式。

> 💡 **不需要工具调用或需要 TTS？** 克隆 [`no-tools` 分支](https://github.com/Fly143/MiMo2API/tree/no-tools) 即可获得更干净的纯对话版本（无 prompt 注入，输出质量更高），且包含完整语音合成（TTS）功能。

### 手动安装

```bash
# 1. 创建虚拟环境
python3 -m venv venv
source venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 创建配置文件
cp config.example.json config.json

# 4. 启动
python main.py
```

启动后访问：**http://localhost:8080**

## 配置凭证

打开管理面板 http://localhost:8080 进行配置。

### 方法1：Cookie 导入

1. 访问 https://aistudio.xiaomimimo.com 并登录
2. 打开 **开发者工具** → **Application** → **Storage → Cookies**
3. 找到以下三个关键 Cookie：
   - `serviceToken` — 服务凭证（最重要）
   - `userId` — 用户 ID（纯数字）
   - `xiaomichatbot_ph` — 会话标识
4. 填入管理面板 → 保存

> **提示：** serviceToken 有效期很短（约 24 小时），过期后需要重新导入。

### 方法2：cURL 导入

1. 登录 aistudio.xiaomimimo.com
2. 打开**开发者工具** → **Network** 面板
3. 发送一条消息，找到 `chat` 请求（SSE 类型）
4. 右键 → **Copy as cURL**
5. 粘贴到管理面板 → 自动解析并保存

### 多账号管理

支持添加**多个账号**，代理会**自动轮询**使用：
- 每个请求从账号池取下一个 → 降低单账号限频风险
- 支持测试连接、删除、替换已有账号
- 同一个 userId 重复导入会自动更新（不重复添加）

## API 使用

### 1. 列出模型

```bash
curl http://localhost:8080/v1/models \
  -H "Authorization: Bearer sk-mimo"
```

返回模型列表会显示所有 MiMo 官方当前可用的模型。

### 2. 文本对话

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2-flash",
    "messages": [
      {"role": "user", "content": "你好，请用中文回复"}
    ]
  }'
```

### 3. 流式对话

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2-flash",
    "messages": [
      {"role": "user", "content": "讲个故事"}
    ],
    "stream": true
  }'
```

返回标准 SSE 流（`data: ...\n\n`），以 `data: [DONE]\n\n` 结束。

### 4. 多模态（图片理解）

需要选择 **omni/v2.5** 模型。支持两种图片格式：

**URL 方式：**
```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2-omni",
    "messages": [{
      "role": "user",
      "content": [
        {"type": "text", "text": "这张图片里有什么？"},
        {"type": "image_url", "image_url": {"url": "https://example.com/photo.jpg"}}
      ]
    }]
  }'
```

**Base64 方式：**
```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2-omni",
    "messages": [{
      "role": "user",
      "content": [
        {"type": "text", "text": "描述这张图片"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/4AAQ..."}}
      ]
    }]
  }'
```

> **原理：** 代理会自动完成三步上传流程：`genUploadInfo` 获取签名 URL → `PUT` 上传原始数据 → `resource/parse` 注册解析，然后将 `multiMedias` 参数传入聊天 API。

### 5. 文件上传（文本文件）

支持上传文本文件（`.md`、`.txt` 等），MiMo 会读取文件内容并基于内容回答：

```bash
# 先读取文件并转为 base64
BASE64=$(base64 -w0 yourfile.md)

curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d "{
    \"model\": \"mimo-v2-pro\",
    \"messages\": [{
      \"role\": \"user\",
      \"content\": [
        {\"type\": \"text\", \"text\": \"总结这个文件\"},
        {\"type\": \"file\", \"file\": {\"filename\": \"yourfile.md\", \"file_data\": \"$BASE64\"}}
      ]
    }]
  }"
```

> **支持的格式：** `.txt`、`.md`、`.py`、`.json`、`.yaml` 等纯文本文件。文件走 MiMo 原生上传流程（`mediaType: "file"`），MiMo 按 token 预算自动读取可用部分。

### 6. 工具调用（Function Calling）

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2-pro",
    "messages": [
      {"role": "user", "content": "北京今天天气怎么样？"}
    ],
    "tools": [{
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "查询指定城市的天气",
        "parameters": {
          "type": "object",
          "properties": {
            "city": {"type": "string", "description": "城市名称"}
          },
          "required": ["city"]
        }
      }
    }],
    "tool_choice": "auto"
  }'
```

成功时返回 `finish_reason: "tool_calls"`，`message.tool_calls` 包含结构化的函数调用：

```json
{
  "choices": [{
    "finish_reason": "tool_calls",
    "message": {
      "role": "assistant",
      "content": null,
      "tool_calls": [{
        "id": "call_abc123...",
        "type": "function",
        "function": {
          "name": "get_weather",
          "arguments": "{\"city\": \"北京\"}"
        }
      }]
    }
  }]
}
```

### 7. 深度思考模式

使用 `reasoning_effort` 参数启用深度思考：

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2-pro",
    "messages": [
      {"role": "user", "content": "证明根号2是无理数"}
    ],
    "reasoning_effort": "high",
    "stream": true
  }'
```

流式响应中会包含 `reasoning` 字段（对应 MiMo 的 `<think>` 块），内容与文本分开输出。

### 7. 模型发现与刷新

模型列表**启动时自动探测**，从 `https://aistudio.xiaomimimo.com/open-apis/bot/config` 实时拉取，无需手动配置。

```bash
# 强制刷新模型列表
curl -X POST http://localhost:8080/v1/models/refresh \
  -H "Authorization: Bearer sk-mimo"
```

### 8. Responses API

OpenAI 最新 Responses API 格式，`/v1/responses` 端点：

```bash
curl http://localhost:8080/v1/responses \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2-pro",
    "input": [
      {"role": "user", "content": "你好"}
    ]
  }'
```

支持流式（`"stream": true`）、工具调用、深度思考、系统指令等，详见下方 [Responses API 详解](#responses-api-详解)。

## 9. Anthropic Messages API

MiMo2API v2.0.0 新增 Anthropic Messages API 完整兼容支持。只需将 API 地址和密钥换过来即可：

```bash
# 非流式对话
curl -X POST http://localhost:8080/v1/messages \
  -H "x-api-key: sk-mimo" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "mimo-v2-flash",
    "max_tokens": 1024,
    "messages": [
      {"role": "user", "content": "你好"}
    ]
  }'

# 流式对话
curl -N -X POST http://localhost:8080/v1/messages \
  -H "x-api-key: sk-mimo" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "mimo-v2-flash",
    "max_tokens": 1024,
    "stream": true,
    "messages": [
      {"role": "user", "content": "讲个故事"}
    ]
  }'
```

### 支持的端点（9 个）

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/messages` | POST | 发消息（流式/非流式，含思考链） |
| `/v1/messages/count_tokens` | POST | 计算 token 数（本地估算，需 tiktoken） |
| `/v1/messages/{message_id}` | GET | 查询已存储的消息 |
| `/v1/messages/batches` | POST | 创建批量任务 |
| `/v1/messages/batches` | GET | 批量任务列表 |
| `/v1/messages/batches/{batch_id}` | GET | 批量任务详情 |
| `/v1/messages/batches/{batch_id}/cancel` | POST | 取消批量任务 |
| `/v1/messages/batches/{batch_id}/results` | GET | 下载结果 JSONL |
| `/v1/messages/batches/{batch_id}` | DELETE | 删除批量任务 |

### Anthropic 模型名映射

Claude Code CLI 等工具期望 Anthropic 风格的模型名，无法直接使用 `mimo-*` 原生名。本代理在 Anthropic 端点内部自动映射：

| Claude 模型名 | → MiMo 内部模型 |
|---|---|
| `claude-opus-4-6` | `mimo-v2.5-pro` |
| `claude-sonnet-4-6` | `mimo-v2-pro` |
| `claude-haiku-4-5` | `mimo-v2-flash` |
| `claude-3-7-sonnet` | `mimo-v2-pro` |
| `claude-3-5-sonnet` | `mimo-v2-flash` |
| `claude-3-opus` | `mimo-v2.5` |

也支持 search/nothinking 变体和 Claude 4.x 历史名。MiMo 原生名（`mimo-*`）继续直接使用，`/v1/models` 返回不变，不影响其他软件。

### 认证

Anthropic 客户端使用 `x-api-key` 头（RikkaHub 自动切换），也兼容 `Authorization: Bearer`：

```bash
# x-api-key（Anthropic 原生）
curl -H "x-api-key: sk-mimo" ...

# Authorization Bearer（向后兼容）
curl -H "Authorization: Bearer sk-mimo" ...
```

### 思考链

MiMo 的 `<think>` 标签内容自动转换为 Anthropic thinking block。流式响应按 **thinking → text → tool_use** 顺序输出 content blocks：

```
message_start
  content_block_start (thinking)
    content_block_delta (thinking_delta ×N)
  content_block_stop
  content_block_start (text)
    content_block_delta (text_delta ×N)
  content_block_stop
message_delta + message_stop
```

### 工具调用

支持 Anthropic 格式的工具定义（`input_schema` → OpenAI `parameters` 自动转换）：

```bash
curl -X POST http://localhost:8080/v1/messages \
  -H "x-api-key: sk-mimo" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "mimo-v2-flash",
    "max_tokens": 1024,
    "messages": [
      {"role": "user", "content": "现在几点"}
    ],
    "tools": [{
      "name": "get_time",
      "description": "获取当前时间",
      "input_schema": {"type": "object", "properties": {}}
    }]
  }'
```

返回 Anthropic 格式的 `tool_use` blocks：

```json
{
  "content": [
    {"type": "tool_use", "id": "tu_xxx", "name": "get_time", "input": {}}
  ],
  "stop_reason": "tool_use"
}
```

> **注意：** MiMo 的工具调用基于文本 TOOL_CALL 格式模拟，非原生 function calling。`no-tools` 分支不含工具调用支持。

## 工具调用详解

MiMo API 本身**不支持** OpenAI function calling 格式。本代理通过**提示词注入 + 多策略提取**实现：

### 提示词注入

将 OpenAI tools 定义转换为极简文本，注入到 system 消息中：

```text
# Tools
- get_weather(city) — 查询指定城市的天气
- search_web(query, page) — 搜索网页
```

### 5 种提取策略（按优先级）

| 策略 | 格式 | 说明 |
|------|------|------|
| 1 | `TOOL_CALL: name(key=value)` | 正则匹配，最可靠 |
| 2 | `{"name": "x", "arguments": {...}}` | JSON 块解析 |
| 3 | `name(args)` | 自由文本关键词匹配 |
| 4 | `<tool_call><function=NAME><parameter=K>V</parameter></function></tool_call>` | MiMo 原生 XML 格式 |
| 5 | `<function_call>{"name":"x","arguments":{...}}</function_call>` | XML 包裹 JSON |

### 响应清理

提取成功后，自动清理响应中的工具残留文本（TOOL_CALL 行、XML 标签、JSON 块）。

### 流式筛分

有工具调用且 `stream: true` 时，`tool_sieve` 引擎逐字扫描 MiMo 响应流，实时分离**正文内容**和**工具调用文本**：

- **正文** → 即时转为 `delta.content` 逐块输出，客户端无需等待即可显示
- **工具调用** → 缓冲至流结束后解析，然后作为 `tool_calls` 一次性输出

非筛分模式（无工具流、非流）不受影响，保持原有逻辑。筛选检测支持三种格式：`TOOL_CALL:`、`<tool_call>`、`<function=`，同时白名单排除 `<think>` 深度思考标签。

## 无工具分支 (no-tools)

### 为什么注入太多 Prompt 会让模型变笨

工具调用（Function Calling）的实现方式是**将工具定义以文本形式注入到 system/user 消息中**。这带来不可忽视的副作用：

**每注入一个工具定义，就消耗一部分模型的"注意力预算"。**

具体影响：

- **注意力稀释** — 大量工具描述占据上下文，模型分配到用户实际问题的注意力比例下降，回答质量明显变差
- **格式过拟合** — 模型过度关注 `TOOL_CALL` 输出格式，在不需要调用工具的纯对话中也可能产生格式残留或奇怪的输出
- **混淆增加** — 工具名称、参数描述与正常对话内容混在一起，增加了模型混淆的概率，尤其是参数较多的工具
- **Token 浪费** — 工具 prompt 每次请求都占用 token，既浪费上下文窗口又增加上游处理时间，而大部分对话根本不需要工具

**简单说：prompt 越多，模型越容易"分心"，回答质量越差。**

### 无工具分支

如果你的使用场景**不需要**工具调用（纯对话、写作、翻译、代码生成、问答等），强烈建议使用 `no-tools` 分支：

```bash
# 克隆无工具版本
git clone -b no-tools https://github.com/Fly143/MiMo2API.git
```

`no-tools` 分支与 `main` 分支的区别：

| | main | no-tools |
|---|---|---|
| 工具 prompt 注入 | ✅ 每次请求注入工具描述 | ❌ 不注入任何 prompt |
| 工具提取解析 | ✅ 5 种策略提取 TOOL_CALL | ❌ 不解析 |
| 响应清理 | ✅ 清理工具残留文本 | ❌ 不需要 |
| Responses API | ✅ `/v1/responses`（含工具调用） | ✅ `/v1/responses`（纯对话） |
| Anthropic API | ✅ `/v1/messages`（含工具调用） | ✅ `/v1/messages`（纯对话） |
| 多模态 | ✅ | ✅ |
| 文件上传（.md/.txt） | ✅ | ✅ |
| 深度思考 | ✅ | ✅ |
| 多账号 | ✅ | ✅ |
| 模型发现 | ✅ | ✅ |
| TTS 语音合成 | ❌ 不包含 | ✅ `/v1/audio/speech` |

**效果：** 上下文更干净，模型注意力完全集中在用户问题上，回答更专注、质量更高，代码也更简洁。对于大多数日常使用场景，无工具分支是更好的选择。

## Responses API 详解

端点：`POST /v1/responses`

MiMo2API 完整实现了 OpenAI Responses API 格式，支持与 Chat Completions 相同的底层能力。

### 与 Chat Completions 的区别

| | Chat Completions | Responses API |
|---|---|---|
| 端点 | `/v1/chat/completions` | `/v1/responses` |
| 消息字段 | `messages` | `input` |
| 系统指令 | `messages[role=system]` | `instructions` |
| 工具格式 | `tool.function.name` | `tool.name` |
| 响应格式 | `choices[0].message` | `output[]` 数组 |
| 思考内容 | `reasoning_content` | `output[type=reasoning]` |
| 工具调用 | `message.tool_calls` | `output[type=function_call]` |

### 基本用法

```bash
# 非流式
curl http://localhost:8080/v1/responses \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2-pro",
    "input": [{"role": "user", "content": "你好"}]
  }'

# 流式（SSE）
curl http://localhost:8080/v1/responses \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2-pro",
    "input": [{"role": "user", "content": "讲个故事"}],
    "stream": true
  }'
```

### 工具调用

```bash
curl http://localhost:8080/v1/responses \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2-pro",
    "input": [{"role": "user", "content": "现在几点"}],
    "tools": [{
      "type": "function",
      "name": "get_time",
      "description": "获取当前时间",
      "parameters": {
        "type": "object",
        "properties": {
          "timezone": {"type": "string"}
        }
      }
    }]
  }'
```

> **注意工具格式：** Responses API 的 `tools` 没有 `function` 嵌套层，`name` 直接在顶层（不同于 Chat Completions 的 `tool.function.name`）。MiMo2API 兼容两种格式。

### 响应格式

```json
{
  "output": [
    {
      "type": "reasoning",
      "summary": [{"type": "summary_text", "text": "模型思考内容..."}]
    },
    {
      "type": "function_call",
      "id": "fc_abc123...",
      "call_id": "call_xyz789...",
      "name": "get_time",
      "arguments": "{}"
    },
    {
      "type": "message",
      "role": "assistant",
      "status": "completed",
      "content": [{"type": "output_text", "text": "现在是..."}]
    }
  ]
}
```

`output` 按顺序包含：reasoning（如有）→ function_call（如有）→ message。

## 管理命令

```bash
# 前台运行（Ctrl+C 停止）
./venv/bin/python main.py

# 后台运行
nohup ./venv/bin/python main.py > mimo.log 2>&1 &
echo $! > mimo.pid

# 从 PID 文件停止
kill $(cat mimo.pid)

# 按进程名停止
pkill -f "python main.py"

# 查看实时日志
tail -f mimo.log

# 查看进程状态
ps aux | grep "python main.py"

# 查看端口占用
lsof -i :8080
```

**启动后：**

| 地址 | 说明 |
|------|------|
| `http://localhost:8080` | Web 管理后台（配置账号） |
| `http://localhost:8080/v1` | OpenAI + Anthropic 兼容 API 根路径 |
| `http://localhost:8080/docs` | Swagger API 文档 |
| `http://localhost:8080/v1/messages` | Anthropic Messages API |
| `http://localhost:8080/v1/responses` | OpenAI Responses API |

## 项目结构

```
MiMo2API/
├── main.py                  # 入口，FastAPI 应用创建 + uvicorn 启动
├── deploy.sh                # 一键部署脚本（安装依赖、初始化配置）
├── requirements.txt         # Python 依赖
├── config.example.json      # 配置文件模板
├── config.json              # 实际配置（.gitignore，含凭证）
├── app/
    ├── __init__.py
    ├── routes.py            # API 路由（chat/models/管理面板/账号CRUD）
    ├── anthropic_routes.py  # Anthropic Messages API 路由（9 个端点）
    ├── anthropic.py         # Anthropic ↔ OpenAI 格式转换核心
    ├── batch.py             # Anthropic 批量任务 + count_tokens
    ├── models.py            # OpenAI 兼容数据模型（Pydantic）
    ├── mimo_client.py       # MiMo API 客户端（HTTP SSE 流处理）
    ├── config.py            # 配置管理（多账号、线程安全、轮询）
    ├── utils.py             # 工具函数（cURL解析、图片上传、消息构建）
    ├── tool_sieve.py        # 流式筛分引擎（实时分离工具调用与正文）
    ├── tool_call.py         # 工具调用（提示词注入 + 5策略提取 + 清理）
    ├── usage_store.py       # 用量数据持久化
    ├── session_store.py     # 会话管理（指纹续接 conversationId）
    ├── response_store.py    # Responses API 记录持久化
    └── web/
        └── index.html       # Web 管理面板
```

## 配置参考

`config.json` 完整配置项：

```json
{
  "api_keys": "sk-mimo,sk-another",
  "mimo_accounts": [
    {
      "service_token": "eyJ...",
      "user_id": "123456",
      "xiaomichatbot_ph": "abc123...",
      "is_valid": true,
      "login_time": "04-26 17:00",
      "last_test": "04-26 17:05"
    }
  ],
  "models": []
}
```

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `api_keys` | 逗号分隔的 API Key 列表 | `sk-mimo` |
| `mimo_accounts` | MiMo 账号列表（可多个） | `[]` |
| `models` | 自定义模型列表（空数组=自动探测） | `[]` |

**环境变量：** `PORT` — 监听端口（默认 `8080`）

## 依赖

- **Python 3.10+**
- FastAPI 0.115
- uvicorn 0.32
- httpx 0.27
- Pydantic v1

```bash
pip install -r requirements.txt
```

## 限制与已知问题

| 限制 | 说明 |
|------|------|
| Token 有效期 & 静默降级 | serviceToken 约 24 小时过期。过期后基础聊天（flash/pro）可能仍然正常，但 **mimo-v2.5 / mimo-v2-omni 多模态识图**会静默失效。管理面板"测试连接"只检查普通 chat 端点，无法发现此问题。修复需网页端退出并重新登录，见下方 FAQ |
| 多模态模型 | `mimo-v2.5` / `mimo-v2-omni` 支持识图；全系模型支持文件上传与图片 OCR 文字提取 |
| 并发限制 | 取决于 MiMo 服务端限制（通常 1-2 并发/账号），多账号可缓解 |
| 不支持 Embeddings | 仅实现 Chat Completions 和 Responses 端点 |
| 非流式实际走 SSE | MiMo API 只提供 SSE 流，非流式请求会缓冲全部 SSE 后合并返回 |

## 常见问题

**Q: 为什么返回 401 "invalid api key"？**
A: 检查 `Authorization` header 是否携带了正确的 API Key。默认是 `sk-mimo`，可在 `config.json` 中修改。

**Q: 为什么返回 503 "no mimo account"？**
A: 管理面板中没有配置账号，或者所有账号都已失效。请登录 http://localhost:8080 添加有效账号。

**Q: 图片上传失败怎么办？模型说"没有看到图片"？**  
A: 通常是因为服务端 session 状态异常，仅重新获取 Cookie 无效。正确步骤：  
1. 浏览器打开 https://aistudio.xiaomimimo.com  
2. **退出登录**（必须退出，不能只刷新页面）  
3. 重新登录  
4. 在管理面板重新导入 Cookie  
如果是账号被限制，换另一个账号。  

**Q: mimo-v2.5 / mimo-v2-omni 多模态识图突然失效，但测试连接显示正常？**  
A: 这是 serviceToken 过期后的**静默降级**现象。MiMo API 对多模态识图的凭证校验比普通聊天严格。Token 过期后：  
- 基础聊天（flash/pro）可能仍能正常使用  
- 管理面板"测试连接"也显示正常（它只检查普通 chat 端点）  
- 但多模态识图会返回胡说八道的结果或报错  

**症状判断：** 如果普通对话正常，但多模态识图突然失效，大概率是凭证过期。  
**修复：** 同上——网页端退出重新登录，再导入新 Cookie。如果换了新 Cookie 仍无效，换另一个账号试试。

**Q: tool_call 没有被提取？**
A: 查看日志确认响应内容。如果 MiMo 没有按预期输出工具调用格式，可能是提示词不够清晰，或者该模型理解力有限。推荐使用 `mimo-v2.5-pro` 进行工具调用。

**Q: 可以部署到公网吗？**
A: 可以，但注意修改默认 API Key（`sk-mimo` 太简单），建议使用 Nginx 反向代理 + HTTPS。

## 许可

MIT License

---

**致谢：** 小米 MiMo AI Studio 提供的基础 API 服务。
[GoblinHonest/mimo2api_mimoapi](https://github.com/GoblinHonest/mimo2api_mimoapi) — 会话管理（消息指纹续接 MiMo conversationId）设计参考。
