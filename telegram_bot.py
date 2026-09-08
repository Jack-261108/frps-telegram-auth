import asyncio
import html
import logging
import time
from typing import Dict, Optional, Callable, Any
import httpx
from config import config

logger = logging.getLogger("frps_auth.telegram")

class TelegramManager:
    def __init__(self):
        self.bot_token = config.bot_token
        self.admin_chat_id = config.admin_chat_id
        self.api_base = f"https://api.telegram.org/bot{self.bot_token}"
        self.client: Optional[httpx.AsyncClient] = None
        self.is_running = False
        self.last_update_id = 0

        # 回调处理器绑定
        self.decision_callbacks: Dict[str, Callable[[str, str], None]] = {}
        self.update_trigger_func: Optional[Callable[[], Any]] = None

    def is_configured(self) -> bool:
        """检查凭据是否已经填写真实值，而非占位符"""
        if not self.bot_token or self.bot_token == "YOUR_TELEGRAM_BOT_TOKEN":
            return False
        if not self.admin_chat_id or self.admin_chat_id in (0, 123456789):
            return False
        return True

    async def init_client(self):
        if not self.client:
            kwargs: dict = {"timeout": 35.0}
            if config.telegram_proxy:
                kwargs["proxy"] = config.telegram_proxy
                logger.info(f"Telegram 模块使用代理连接: {config.telegram_proxy}")
            self.client = httpx.AsyncClient(**kwargs)

    async def close(self):
        self.is_running = False
        if self.client:
            await self.client.aclose()
            self.client = None

    def register_decision_callback(self, conn_id: str, callback: Callable[[str, str], None]):
        self.decision_callbacks[conn_id] = callback

    def unregister_decision_callback(self, conn_id: str):
        self.decision_callbacks.pop(conn_id, None)

    async def send_approval_card(self, conn_id: str, remote_ip: str, remote_port: int, proxy_name: str) -> Optional[int]:
        """向 Telegram 发送带按钮的审批卡片"""
        if not self.is_configured():
            logger.warning("Telegram Bot Token 或 Admin Chat ID 尚未正确配置（仍为默认占位符），无法发送审批！请在 config.yaml 填写真实凭据。")
            return None

        await self.init_client()
        if not self.client:
            return None
        current_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        safe_ip = html.escape(str(remote_ip))
        safe_proxy = html.escape(str(proxy_name))

        text = (
            f"🚨 <b>【FRPS 连接申请拦截】</b>\n\n"
            f"📍 <b>来源 IP:</b> <code>{safe_ip}:{remote_port}</code>\n"
            f"🎯 <b>目标服务:</b> <code>{safe_proxy}</code>\n"
            f"⏰ <b>申请时间:</b> <code>{current_time}</code>\n"
            f"⏱ <b>有效时间:</b> <code>{config.approval_timeout} 秒</code>\n\n"
            f"<i>请在有效期内选择是否放行本次连接：</i>"
        )

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ 允许本次连接", "callback_data": f"allow:{conn_id}"},
                    {"text": "❌ 立即拒绝", "callback_data": f"deny:{conn_id}"}
                ],
                [
                    {"text": "⏱ 允许并放行 30 分钟", "callback_data": f"whitelist:{conn_id}"}
                ]
            ]
        }

        try:
            resp = await self.client.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": self.admin_chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "reply_markup": keyboard
                }
            )
            data = resp.json()
            if data.get("ok"):
                return data["result"]["message_id"]
            else:
                logger.error(f"发送 Telegram 消息失败: {data}")
        except Exception as e:
            logger.error(f"发送 Telegram 消息异常 ({type(e).__name__}): {e}")
        return None

    async def update_message_result(self, message_id: int, remote_ip: str, proxy_name: str, result_text: str):
        """审批完成后编辑原消息，防止重复点击"""
        if not self.client or not self.admin_chat_id:
            return

        safe_ip = html.escape(str(remote_ip))
        safe_proxy = html.escape(str(proxy_name))

        text = (
            f"🛡 <b>【FRPS 连接申请 - 审批完成】</b>\n\n"
            f"📍 <b>来源 IP:</b> <code>{safe_ip}</code>\n"
            f"🎯 <b>目标服务:</b> <code>{safe_proxy}</code>\n"
            f"📊 <b>处理结果:</b> {result_text}\n"
            f"⏰ <b>处理时间:</b> <code>{time.strftime('%Y-%m-%d %H:%M:%S')}</code>"
        )
        try:
            await self.client.post(
                f"{self.api_base}/editMessageText",
                json={
                    "chat_id": self.admin_chat_id,
                    "message_id": message_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "reply_markup": {"inline_keyboard": []}
                }
            )
        except Exception as e:
            logger.error(f"更新 Telegram 消息失败: {e}")

    async def answer_callback(self, callback_query_id: str, text: str):
        """给用户的点击弹窗提示 (Toast)"""
        if not self.client:
            await self.init_client()
        if not self.client:
            return
        try:
            await self.client.post(
                f"{self.api_base}/answerCallbackQuery",
                json={
                    "callback_query_id": callback_query_id,
                    "text": text
                }
            )
        except Exception as e:
            logger.error(f"响应 CallbackQuery 失败: {e}")

    async def send_simple_message(self, text: str):
        if not self.admin_chat_id:
            return
        await self.init_client()
        if not self.client:
            return
        try:
            await self.client.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": self.admin_chat_id,
                    "text": text,
                    "parse_mode": "HTML"
                }
            )
        except Exception as e:
            logger.error(f"发送简单消息失败: {e}")

    async def start_polling(self):
        """后台长轮询任务，接收用户点击和命令"""
        if not self.bot_token or self.bot_token == "YOUR_TELEGRAM_BOT_TOKEN":
            logger.warning("未配置有效的 BOT_TOKEN（仍为占位符），Telegram 轮询模块暂不启动。请在 config.yaml 中配置。")
            return

        self.is_running = True
        await self.init_client()
        logger.info("Telegram Bot 长轮询模块已启动...")
        error_backoff = 3

        while self.is_running:
            try:
                if not self.client:
                    await self.init_client()
                assert self.client is not None
                resp = await self.client.get(
                    f"{self.api_base}/getUpdates",
                    params={"offset": self.last_update_id + 1, "timeout": 25}
                )
                data = resp.json()
                if not data.get("ok"):
                    logger.error(f"Telegram getUpdates 响应失败: {data}")
                    await asyncio.sleep(5)
                    continue

                error_backoff = 3 # 成功收到响应，重置退避时间

                for update in data.get("result", []):
                    self.last_update_id = update["update_id"]
                    await self._handle_update(update)

            except httpx.TimeoutException:
                pass
            except Exception as e:
                logger.error(f"Telegram 轮询异常 ({type(e).__name__}: {e})，将在 {error_backoff} 秒后重试")
                if self.client:
                    try:
                        await self.client.aclose()
                    except Exception:
                        pass
                    self.client = None
                await asyncio.sleep(error_backoff)
                error_backoff = min(error_backoff * 2, 30)

    async def _handle_update(self, update: dict):
        # 1. 处理按钮点击 (callback_query)
        if "callback_query" in update:
            cq = update["callback_query"]
            from_user = cq.get("from", {})
            user_id = from_user.get("id")
            cq_id = cq["id"]
            data = cq.get("data", "")

            # 鉴权：只响应 Admin 的点击
            if self.admin_chat_id and user_id != self.admin_chat_id:
                await self.answer_callback(cq_id, "⚠️ 您无权审批此连接！")
                return

            if ":" in data:
                action, conn_id = data.split(":", 1)
                if conn_id in self.decision_callbacks:
                    self.decision_callbacks[conn_id](action, cq_id)
                else:
                    await self.answer_callback(cq_id, "⌛ 该连接审批已过期或已处理！")

        # 2. 处理文字命令 (message)
        elif "message" in update:
            msg = update["message"]
            text = msg.get("text", "").strip()
            chat_id = msg.get("chat", {}).get("id")
            from_user = msg.get("from", {})

            if text.startswith("/start"):
                reply = (
                    f"👋 <b>FRPS Telegram 审批机器人已就绪！</b>\n\n"
                    f"🆔 您的 Chat ID: <code>{chat_id}</code>\n"
                    f"👤 用户名: @{from_user.get('username', 'N/A')}\n\n"
                    f"📌 <b>常用指令:</b>\n"
                    f"• <code>/status</code>: 查看运行状态与当前白名单\n"
                    f"• <code>/update</code>: 检查并从 GitHub 自动更新代码\n"
                    f"• <code>/help</code>: 查看帮助信息"
                )
                if not self.client:
                    await self.init_client()
                if self.client:
                    await self.client.post(
                        f"{self.api_base}/sendMessage",
                        json={"chat_id": chat_id, "text": reply, "parse_mode": "HTML"}
                    )

            elif text.startswith("/update"):
                if self.admin_chat_id and chat_id != self.admin_chat_id:
                    return
                await self.send_simple_message("🔄 正在触发从 GitHub 拉取更新并检查重启...")
                if self.update_trigger_func:
                    asyncio.create_task(self.update_trigger_func())

            elif text.startswith("/status"):
                from frp_handler import handler
                whitelist_count = len(handler.ip_whitelist)
                pending_count = len(handler.pending_conns)
                reply = (
                    f"📊 <b>【系统运行状态】</b>\n\n"
                    f"🛡 <b>服务状态:</b> 🟢 正常运行\n"
                    f"⏳ <b>正在等待审批连接数:</b> {pending_count}\n"
                    f"📋 <b>当前临时白名单 IP 数量:</b> {whitelist_count}\n"
                    f"🎯 <b>拦截保护的代理列表:</b> <code>{', '.join(config.protected_proxies)}</code>\n"
                )
                await self.send_simple_message(reply)

tg_manager = TelegramManager()
