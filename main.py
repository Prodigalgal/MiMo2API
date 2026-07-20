"""Mimo2API Python版本 - 主程序入口"""

import os
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path
from app.routes import router, _do_discover
from app.config import config_manager
from app.anthropic_routes import router as anthropic_router
from app.batch import init_batch_storage as init_anthropic_batches

# 创建FastAPI应用
app = FastAPI(
    title="Mimo2API",
    description="将小米 Mimo AI 转换为 OpenAI + Anthropic 兼容 API（Chat / Responses / Anthropic Messages）",
    version="2.4.0"
)

# 添加CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup_discover_models():
    import os as _anthropic_os
    from app.batch import init_batch_storage as _mimo_init_batch_storage
    _mimo_init_batch_storage(_anthropic_os.getenv(
        "MIMO2API_BATCH_DIR",
        _anthropic_os.path.join(_anthropic_os.path.dirname(_anthropic_os.path.abspath(__file__)), ".anthropic_batches"),
    ))
    """服务启动时预探测模型，避免首次请求返回3个硬编码模型"""
    try:
        await _do_discover()
        print("✅ 模型预探测完成")
    except Exception as e:
        print(f"⚠️ 模型预探测失败（不影响服务）: {e}")

    # 后台清理过期会话（避免风控）
    print("[启动] 后台清理过期会话...")
    import threading
    threading.Thread(target=_cleanup_old_sessions, daemon=True).start()

    # 账号自动续期（邮箱+密码 / passToken）
    print("[启动] 账号自动续期线程...")
    threading.Thread(target=_auto_renew_loop, daemon=True).start()


def _cleanup_old_sessions():
    """后台清理过期会话，每个删除间隔 10 秒。"""
    import time, asyncio
    async def _run():
        try:
            from app.session_store import get_expired_sessions, remove_session
            from app.mimo_client import MimoClient
            from app.config import config_manager
            expired = get_expired_sessions()
            if not expired:
                return
            print(f"[Cleanup] Found {len(expired)} expired sessions, deleting with 10s delay...")
            by_account = {}
            for account_label, conv_id, model, days_ago in expired:
                by_account.setdefault(account_label, []).append((conv_id, days_ago))
            deleted = 0
            for account_label, conv_items in by_account.items():
                acc = None
                for a in config_manager.config.mimo_accounts:
                    if a.user_id == account_label:
                        acc = a
                        break
                if not acc:
                    continue
                client = MimoClient(acc)
                for conv_id, days_ago in conv_items:
                    try:
                        if await client.delete_conversations([conv_id]):
                            remove_session(account_label, conv_id)
                            deleted += 1
                            print(f"[Cleanup] Deleted: {conv_id[:12]}... ({days_ago}d old)")
                    except Exception:
                        pass
                    time.sleep(10)
            print(f"[Cleanup] Done: {deleted}/{len(expired)}")
        except Exception as e:
            print(f"[Cleanup] Failed: {e}")
    asyncio.run(_run())


def _auto_renew_loop():
    """Periodically renew MiMo web tokens (keep-alive).

    1) passToken renew first
    2) if fail and account has email+password+mail_jwt and temp-mail configured:
       password login → send OTP → auto-read code from temp mail → re-login

    Interval from env MIMO2API_RENEW_INTERVAL_SECONDS (default 6h).
    First run delayed 60s after startup.
    """
    import time
    import asyncio
    import os

    interval = int(os.getenv("MIMO2API_RENEW_INTERVAL_SECONDS", str(6 * 3600)))
    time.sleep(60)

    async def _renew_all():
        from app.config import config_manager
        from app.routes import _renew_one_account

        accounts = list(config_manager.config.mimo_accounts)
        if not accounts:
            return
        print(f"[AutoRenew] checking {len(accounts)} account(s) (passToken + temp-mail OTP fallback)...")
        for i, acc in enumerate(accounts):
            if not getattr(acc, "auto_renew", True):
                continue
            has_pt = bool(getattr(acc, "pass_token", ""))
            has_mail_path = bool(
                getattr(acc, "email", "")
                and getattr(acc, "password", "")
                and getattr(acc, "mail_jwt", "")
            )
            if not has_pt and not has_mail_path:
                print(f"[AutoRenew] skip userId={acc.user_id}: no passToken and no mail_jwt path")
                continue
            try:
                result = await _renew_one_account(
                    i,
                    allow_password_fallback=False,
                    auto_temp_mail_otp=True,
                )
                if result.get("ok"):
                    print(f"[AutoRenew] ok userId={acc.user_id}")
                else:
                    print(f"[AutoRenew] fail userId={acc.user_id}: {result.get('error') or result.get('message')}")
            except Exception as e:
                print(f"[AutoRenew] error userId={getattr(acc, 'user_id', '?')}: {e}")
            time.sleep(3)

    while True:
        try:
            asyncio.run(_renew_all())
        except Exception as e:
            print(f"[AutoRenew] loop error: {e}")
        time.sleep(max(interval, 300))


# 注册路由
app.include_router(router)
app.include_router(anthropic_router)

# 初始化 Anthropic batch 存储
import os
_anthropic_batch_dir = os.getenv(
    "MIMO2API_BATCH_DIR",
    os.path.join(os.path.dirname(__file__), ".anthropic_batches"),
)
init_anthropic_batches(_anthropic_batch_dir)

# 静态文件目录
web_dir = Path(__file__).parent / "web"

# 管理页面由 routes.py 中的 router 处理（/ 和 /admin）


def main():
    """主函数"""
    # 获取端口配置
    port = int(os.getenv("PORT", "8080"))

    print(f"""
╔══════════════════════════════════════════════════════════╗
║                    Mimo2API Python                       ║
║          将小米 Mimo AI 转换为 OpenAI 兼容 API           ║
╚══════════════════════════════════════════════════════════╝

🚀 服务器启动中...
📍 地址: http://localhost:{port}
📊 管理界面: http://localhost:{port}
📡 API端点: http://localhost:{port}/v1/chat/completions
📖 API文档: http://localhost:{port}/docs

配置信息:
  - API Keys: {len(config_manager.config.api_keys.split(','))} 个
  - Mimo账号: {len(config_manager.config.mimo_accounts)} 个

按 Ctrl+C 停止服务器
""")

    # 启动服务器
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info"
    )


if __name__ == "__main__":
    main()
