import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
import uvicorn
from config import config
from frp_handler import handler
from telegram_bot import tg_manager
from updater import updater

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("frps_auth.main")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 绑定 Telegram /update 命令触发更新
    async def trigger_update():
        await updater.check_and_update(notify_callback=tg_manager.send_simple_message)

    tg_manager.update_trigger_func = trigger_update

    # 启动后台任务：Telegram 轮询 & GitHub 自动更新
    tg_task = asyncio.create_task(tg_manager.start_polling())
    updater_task = asyncio.create_task(updater.start_updater_loop())

    logger.info(f"FRPS Telegram 审批服务已启动，监听: http://{config.listen_host}:{config.listen_port}")
    yield

    # 优雅退出
    await tg_manager.close()
    tg_task.cancel()
    updater_task.cancel()

app = FastAPI(title="FRPS Telegram Auth Service", lifespan=lifespan)

@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "frps-telegram-auth",
        "protected_proxies": config.protected_proxies,
        "bot_configured": bool(config.bot_token and config.admin_chat_id)
    }

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.post("/handler")
async def frp_webhook_handler(req: Request):
    """
    接收来自 FRPS 的 HTTP Plugin Webhook 回调
    """
    try:
        body = await req.json()
    except Exception:
        return {"reject": False, "unchange": True}

    op = body.get("op")
    content = body.get("content", {})

    logger.info(f"收到 FRPS 事件: op={op}")

    if op == "NewUserConn":
        return await handler.handle_new_user_conn(content)

    # 其他操作 (如 Login, NewProxy, Ping 等) 直接放行
    return {"reject": False, "unchange": True}

@app.post("/webhook/update")
async def github_webhook_handler():
    """支持接收 GitHub Webhook 推送触发即时更新"""
    asyncio.create_task(updater.check_and_update(notify_callback=tg_manager.send_simple_message))
    return {"status": "update_triggered"}

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=config.listen_host,
        port=config.listen_port,
        log_level="info"
    )
