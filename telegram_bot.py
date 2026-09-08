import asyncio
import html
import ipaddress
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
        self._ip_geo_cache: Dict[str, str] = {}

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

    async def get_ip_info(self, ip: str) -> str:
        """异步查询 IP 物理归属地与运营商（带本地缓存）"""
        if not ip:
            return "未知归属地"
        if ip in self._ip_geo_cache:
            return self._ip_geo_cache[ip]

        try:
            addr = ipaddress.ip_address(ip)
            if addr.is_private or addr.is_loopback:
                res = "局域网 / 本地回环"
                self._ip_geo_cache[ip] = res
                return res
        except ValueError:
            pass

        try:
            data = None
            url = f"http://ip-api.com/json/{ip}?lang=zh-CN"
            # 优先直连，若遇网络波动则自动尝试通过本地代理兜底
            clients_to_try = [httpx.AsyncClient(timeout=3.5)]
            if config.telegram_proxy:
                clients_to_try.append(httpx.AsyncClient(proxy=config.telegram_proxy, timeout=3.5))

            for client in clients_to_try:
                try:
                    async with client:
                        resp = await client.get(url)
                        if resp.status_code == 200:
                            res_json = resp.json()
                            if res_json.get("status") == "success":
                                data = res_json
                                break
                except Exception:
                    continue

            if data:
                country = data.get("country", "")
                region = data.get("regionName", "")
                city = data.get("city", "")
                isp = data.get("isp", "")
                org = data.get("org", "")
                as_info = data.get("as", "")
                combined = f"{isp} {org} {as_info}".lower()

                if any(k in combined for k in ["联通", "unicom", "china169", "cnc group"]):
                    carrier = "中国联通 ✅"
                elif any(k in combined for k in ["移动", "mobile", "cmnet"]):
                    carrier = "中国移动 ✅"
                elif any(k in combined for k in ["电信", "telecom", "chinanet"]):
                    carrier = "中国电信 ✅"
                elif any(k in combined for k in ["广电", "cbn"]):
                    carrier = "中国广电 ✅"
                elif any(k in combined for k in ["教育网", "cernet"]):
                    carrier = "中国教育网 ✅"
                else:
                    carrier = isp or org

                loc_parts = [p for p in [country, region, city] if p]
                loc_str = " ".join(loc_parts)
                if country == "中国":
                    result = f"{loc_str} · {carrier}" if carrier else loc_str
                else:
                    result = f"{loc_str} · {carrier} (境外/可疑 ⚠️)" if carrier else f"{loc_str} (境外/可疑 ⚠️)"

                self._ip_geo_cache[ip] = result
                return result
        except Exception as e:
            logger.warning(f"获取 IP [{ip}] 归属地异常 ({type(e).__name__}): {e}")

        return "未知归属地"

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
        geo_info = await self.get_ip_info(remote_ip)
        safe_geo = html.escape(geo_info)

        timeout_str = (
            f"{config.approval_timeout // 60} 分钟"
            if config.approval_timeout >= 60 and config.approval_timeout % 60 == 0
            else f"{config.approval_timeout} 秒"
        )

        text = (
            f"🚨 <b>【FRPS 连接申请拦截】</b>\n\n"
            f"📍 <b>来源 IP:</b> <code>{safe_ip}:{remote_port}</code>\n"
            f"🌍 <b>物理位置:</b> {safe_geo}\n"
            f"🎯 <b>目标服务:</b> <code>{safe_proxy}</code>\n"
            f"⏰ <b>申请时间:</b> <code>{current_time}</code>\n"
            f"⏱ <b>有效时间:</b> <code>{timeout_str}</code>\n\n"
            f"<i>请在有效期内选择是否放行本次连接：</i>"
        )

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ 允许本次连接", "callback_data": f"allow:{conn_id}"},
                    {"text": "❌ 立即拒绝", "callback_data": f"deny:{conn_id}"}
                ],
                [
                    {"text": "⏱ 放行 30 分钟", "callback_data": f"whitelist:{conn_id}"},
                    {"text": "🚫 封禁此 IP 24小时", "callback_data": f"ban:{conn_id}"}
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
        geo_info = self._ip_geo_cache.get(remote_ip) or await self.get_ip_info(remote_ip)
        safe_geo = html.escape(geo_info)

        text = (
            f"🛡 <b>【FRPS 连接申请 - 审批完成】</b>\n\n"
            f"📍 <b>来源 IP:</b> <code>{safe_ip}</code>\n"
            f"🌍 <b>物理位置:</b> {safe_geo}\n"
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

    async def delete_message(self, message_id: int):
        """删除指定的 Telegram 消息（用于超时卡片自动销毁）"""
        if not self.client or not self.admin_chat_id:
            return
        try:
            await self.client.post(
                f"{self.api_base}/deleteMessage",
                json={
                    "chat_id": self.admin_chat_id,
                    "message_id": message_id
                }
            )
        except Exception as e:
            logger.error(f"删除 Telegram 消息失败: {e}")

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

            if text.startswith("/start") or text.startswith("/help"):
                reply = (
                    f"👋 <b>FRPS Telegram 审批机器人已就绪！</b>\n\n"
                    f"🆔 您的 Chat ID: <code>{chat_id}</code>\n"
                    f"👤 用户名: @{from_user.get('username', 'N/A')}\n\n"
                    f"📌 <b>常用指令:</b>\n"
                    f"• <code>/status</code>: 查看运行状态、黑白名单与等待连接\n"
                    f"• <code>/unban &lt;IP&gt;</code>: 解除指定 IP 的封禁\n"
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

            elif text.startswith("/unban"):
                if self.admin_chat_id and chat_id != self.admin_chat_id:
                    return
                parts = text.split()
                if len(parts) < 2:
                    await self.send_simple_message("⚠️ 请输入要解封的 IP，例如：<code>/unban 1.2.3.4</code>")
                else:
                    target_ip = parts[1].strip()
                    from frp_handler import handler
                    if handler.remove_blacklist(target_ip):
                        await self.send_simple_message(f"✅ 已成功将 IP <code>{target_ip}</code> 从黑名单移除！")
                    else:
                        await self.send_simple_message(f"ℹ️ IP <code>{target_ip}</code> 不在黑名单中。")

            elif text.startswith("/update"):
                if self.admin_chat_id and chat_id != self.admin_chat_id:
                    return
                await self.send_simple_message("🔄 正在触发从 GitHub 拉取更新并检查重启...")
                if self.update_trigger_func:
                    asyncio.create_task(self.update_trigger_func())

            elif text.startswith("/status"):
                from frp_handler import handler
                whitelist_count = len(handler.ip_whitelist)
                blacklist_count = len(handler.ip_blacklist)
                pending_count = len(handler.pending_conns)
                banned_ips_preview = ""
                if handler.ip_blacklist:
                    banned_ips_preview = "\n🚫 <b>当前封禁 IP:</b> " + ", ".join(f"<code>{ip}</code>" for ip in list(handler.ip_blacklist.keys())[:5])
                    if blacklist_count > 5:
                        banned_ips_preview += f" 等共 {blacklist_count} 个"

                reply = (
                    f"📊 <b>【系统运行状态】</b>\n\n"
                    f"🛡 <b>服务状态:</b> 🟢 正常运行\n"
                    f"⏳ <b>正在等待审批连接数:</b> {pending_count}\n"
                    f"📋 <b>临时白名单 IP 数量:</b> {whitelist_count}\n"
                    f"🚫 <b>黑名单封禁 IP 数量:</b> {blacklist_count}"
                    f"{banned_ips_preview}\n"
                    f"🎯 <b>拦截保护的代理列表:</b> <code>{', '.join(config.protected_proxies)}</code>"
                )
                await self.send_simple_message(reply)

tg_manager = TelegramManager()
