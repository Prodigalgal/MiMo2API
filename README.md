# MiMo2API

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-teal)](https://fastapi.tiangolo.com/)

将**小米 MiMo AI Studio** 网页端能力转换为 **OpenAI / Anthropic 兼容 API**，并支持：

- **Cloudflare 临时邮箱**自动注册小米账号  
- 图片验证码 **ddddocr 自动识别**  
- 批量注册（成功目标数 / 并发 / 间隔）  
- 多账号池、token 保活与 **mail code 自动重登**

> 本项目在 [mimo2api](https://github.com/Water008/MiMo2API) 基础上演进，已形成完整的「代理 + 自动注册 + 保活」方案。

---

## 目录

- [特性](#特性)
- [架构](#架构)
- [快速开始](#快速开始)
- [管理面板](#管理面板)
- [临时邮箱与自动注册](#临时邮箱与自动注册)
- [配置说明](#配置说明)
- [OpenAI 兼容 API](#openai-兼容-api)
- [账号保活](#账号保活)
- [项目结构](#项目结构)
- [依赖](#依赖)
- [常见问题](#常见问题)
- [许可](#许可)

---

## 特性

| 模块 | 说明 |
|------|------|
| OpenAI 兼容 | `/v1/chat/completions`、`/v1/models`、流式 / 非流式 |
| Anthropic 兼容 | `/v1/messages` 等 |
| 多模态 | 图片 / 文本文件上传（官方三步流程） |
| 工具调用 | 多策略提取 + 流式筛分 |
| TTS / ASR | OpenAI 兼容语音接口 |
| **临时邮箱** | 对接 [cloudflare_temp_email](https://github.com/dreamhunter2333/cloudflare_temp_email) |
| **自动注册** | 随机地区（非中国）、随机密码、OCR 过图、自动收信验证 |
| **批量注册** | 成功目标数即停、并发与间隔；**后台任务 + 轮询**（避免网关 504） |
| **保活** | passToken 续期；失效时用 temp-mail 自动取 mail code 重登 |
| 多账号 | 管理面板导入 / 轮询负载 |

管理界面为**中文**（无国际化切换）。

---

## 架构

```
客户端 (ChatBox / OpenAI SDK / Anthropic)
        │
        ▼
┌──────────────────────────────────────────┐
│              MiMo2API (FastAPI)            │
│  /v1/* 代理 · 管理面板 · 自动注册 · 保活   │
└───────────────┬──────────────────────────┘
                │
      ┌─────────┴──────────┐
      ▼                    ▼
 aistudio.xiaomimimo.com   account.xiaomi.com
 (对话 / token)             (注册 / 登录)
      │
      ▼
  自建 Cloudflare Temp Mail
  (创建地址 · 收验证码)
```

---

## 快速开始

### 环境要求

- Python 3.10+
- 已部署的 Cloudflare 临时邮箱后端（admin 可创建地址）

### 安装

```bash
git clone <your-repo-url> MiMo2API
cd MiMo2API
python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/macOS:
# source venv/bin/activate

pip install -r requirements.txt
cp config.example.json config.json
# 编辑 config.json：api_keys、admin_password、temp_mail 等
python main.py
```

默认监听：`http://0.0.0.0:8080`  
管理面板：`http://localhost:8080`（Basic 认证，用户名 `admin`，密码为配置中的 `admin_password`）

### Docker

```bash
docker run -d -p 8080:8080 -v $(pwd)/config.json:/app/config.json <image>
```

---

## 管理面板

浏览器打开服务根路径，使用管理员账号登录。

| 标签 | 用途 |
|------|------|
| cURL / Cookie / 邮箱 | 导入已有 MiMo 会话 |
| **自动注册** | 注册参数、单次 / 批量注册、OCR |
| **临时邮箱** | 仅邮箱 API（地址、管理口令、域名） |
| 账号 | 列表、测试、续期、删除 |
| API Key | 对外调用密钥、管理密码 |
| 用量统计 | 请求量与 token 统计 |

说明：

- **临时邮箱**页只配邮箱服务，不重复放注册参数  
- **自动注册**页配置并发、成功目标、地区（含 RANDOM）等，可「保存注册参数」

---

## 临时邮箱与自动注册

### 1. 配置临时邮箱

在 **临时邮箱** 页填写：

| 字段 | 说明 |
|------|------|
| API 地址 | 如 `https://apimail.example.com` |
| 管理口令 | 对应部署的 `ADMIN_PASSWORDS`，请求头 `x-admin-auth` |
| 默认域名 | 可选，空则用服务端第一个域名 |
| 站点密码 | 若启用了 `x-custom-auth` 再填 |

点「测试连接」确认可创建测试邮箱。

### 2. 注册参数（自动注册页）

| 参数 | 说明 |
|------|------|
| 注册地区 | 固定国家，或 **RANDOM**（每个账号独立随机，永不 CN） |
| 成功目标数 | 成功注册到 N 个即停止；`0` = 不提前停 |
| 最大尝试次数 | 上限 |
| 并发数 | 同时进行的注册任务数 |
| 并发间隔 | 启动下一个任务前等待的秒数 |
| OCR 重试 / 邮件超时 | 图片码与收信等待 |
| 自动 OCR | 批量注册必须开启 |

### 3. 单次注册

点 **单次注册** → 自动创建邮箱 → OCR 过图 → 收信验证 → 登录拿 token → 写入账号池。

### 4. 批量注册（重要）

点 **批量注册**：

1. 服务端**立即返回** `job_id`（后台执行，避免 Cloudflare/Nginx **504**）  
2. 前端每 2.5s 轮询 `/api/account/auto-register-batch/{job_id}`  
3. 达到成功目标或用尽尝试次数后展示结果  

若看到 `Unexpected token '<'`，说明收到了 HTML（常见于旧版同步批量超时 504）。请部署包含「异步批量」的版本。

### 5. 相关 API

```http
POST /api/temp-mail/config          # 保存邮箱 / 注册参数（merge）
POST /api/temp-mail/test            # 测试邮箱
POST /api/account/auto-register     # 单次注册
POST /api/account/auto-register-batch   # 启动批量（异步）
GET  /api/account/auto-register-batch/{job_id}  # 查询进度
```

管理接口均需 HTTP Basic：`admin` + `admin_password`。

---

## 配置说明

`config.example.json` 示例：

```json
{
  "api_keys": "sk-mimo",
  "admin_password": "admin",
  "tools_passthrough": false,
  "temp_mail": {
    "api_base": "https://apimail.example.com",
    "admin_password": "YOUR_ADMIN_PASSWORD",
    "domain": "",
    "site_password": "",
    "register_region": "RANDOM",
    "batch_count": 5,
    "success_target": 3,
    "concurrent": 2,
    "concurrent_interval": 3.0,
    "captcha_retries": 10,
    "otp_timeout": 120,
    "auto_captcha": true
  },
  "mimo_accounts": []
}
```

| 字段 | 含义 |
|------|------|
| `api_keys` | 调用 `/v1/*` 的 Bearer Key，逗号分隔 |
| `admin_password` | 管理面板密码 |
| `temp_mail.*` | 邮箱 + 注册默认参数 |
| `mimo_accounts` | 已导入/注册的账号（含 `mail_jwt` 时支持自动取码） |

`config.json` 含密钥，请勿提交到 Git。

---

## OpenAI 兼容 API

```bash
# 列模型
curl http://localhost:8080/v1/models \
  -H "Authorization: Bearer sk-mimo"

# 对话
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{"model":"mimo-v2.5-pro","messages":[{"role":"user","content":"你好"}]}'
```

更多：流式、多模态、工具调用、Anthropic Messages、TTS/ASR 等与上游 MiMo 能力一致，详见 `/docs`（Swagger）。

---

## 账号保活

- 后台定时用 **passToken** 换新的 aistudio `serviceToken`  
- passToken 失效且账号存有 `email + password + mail_jwt` 时：  
  密码登录 → 发邮件验证码 → **从临时邮箱自动取码** → 重新登录  

环境变量（可选）：

| 变量 | 说明 |
|------|------|
| `PORT` | 监听端口，默认 8080 |
| `MIMO2API_CONFIG_FILE` | 配置文件路径 |
| `MIMO2API_RENEW_INTERVAL_SECONDS` | 续期间隔，默认 6 小时 |

---

## 项目结构

```
MiMo2API/
├── main.py                 # 入口、续期循环
├── app/
│   ├── routes.py           # OpenAI / 管理 / 注册 API
│   ├── xiaomi_login.py     # 密码登录、OTP、passToken 换票
│   ├── xiaomi_register.py  # 注册 + OCR
│   ├── temp_mail.py        # Cloudflare 临时邮箱客户端
│   ├── mimo_client.py      # 上游聊天代理
│   ├── config.py           # 配置与账号
│   └── ...
├── web/index.html          # 中文管理面板
├── config.example.json
└── requirements.txt
```

---

## 依赖

见 `requirements.txt`，主要包括：

- FastAPI / Uvicorn / httpx  
- pycryptodome（小米注册 AES/RSA）  
- ddddocr + onnxruntime（图片验证码 OCR）  

---

## 常见问题

**Q: 批量注册报 `Unexpected token '<'`？**  
A: 旧版同步批量会触发网关 **504 HTML**。请使用当前版本（异步 job + 轮询），并重新部署。

**Q: 图片验证码过不去？**  
A: 确认已安装 `ddddocr`；自动 OCR 会多轮重试，失败可手动填码。

**Q: 注册地区？**  
A: 不要选中国；推荐 `RANDOM`，每个账号从安全国家列表中随机。

**Q: 临时邮箱创建失败？**  
A: 检查 API 地址、`x-admin-auth` 管理口令，以及部署是否允许 admin 创建地址。

**Q: 如何导入已有账号？**  
A: 管理面板 Cookie / cURL / 邮箱密码导入；自动注册成功的账号会直接进入列表。

---

## 许可

MIT
