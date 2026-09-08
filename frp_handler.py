import asyncio
import logging
import time
import uuid
from typing import Dict, Tuple
from config import config
from telegram_bot import tg_manager

logger = logging.getLogger("frps_auth.handler")

class FrpHandler:
    def __init__(self):
        # 正在等待审批的连接: conn_id -> {"event": Event, "decision": str, "remote_ip": str, "proxy_name": str, "msg_id": Optional[int]}
        self.pending_conns: Dict[str, dict] = {}
        # 临时白名单: (ip, proxy_name) -> expire_timestamp
        self.ip_whitelist: Dict[Tuple[str, str], float] = {}

    def is_proxy_protected(self, proxy_name: str) -> bool:
        if not config.protected_proxies:
            return True
        for pattern in config.protected_proxies:
            if pattern in proxy_name:
                return True
        return False

    def check_whitelist(self, ip: str, proxy_name: str) -> bool:
        self._clean_expired_whitelist()
        key = (ip, proxy_name)
        if key in self.ip_whitelist:
            if time.time() < self.ip_whitelist[key]:
                return True
            else:
                self.ip_whitelist.pop(key, None)
        return False

    def add_whitelist(self, ip: str, proxy_name: str, duration: int):
        self.ip_whitelist[(ip, proxy_name)] = time.time() + duration

    def _clean_expired_whitelist(self):
        now = time.time()
        expired = [k for k, v in self.ip_whitelist.items() if v <= now]
        for k in expired:
            self.ip_whitelist.pop(k, None)

    async def handle_new_user_conn(self, content: dict) -> dict:
        proxy_name = content.get("proxy_name", "")
        remote_addr = content.get("remote_addr", "") # 格式: "1.2.3.4:56789"

        remote_ip = remote_addr.split(":")[0] if ":" in remote_addr else remote_addr
        try:
            remote_port = int(remote_addr.split(":")[1]) if ":" in remote_addr else 0
        except Exception:
            remote_port = 0

        # 1. 检查是否为受保护的代理
        if not self.is_proxy_protected(proxy_name):
            logger.info(f"代理 [{proxy_name}] 未在保护名单中，自动放行 {remote_addr}")
            return {"reject": False, "unchange": True}

        # 2. 检查是否在白名单中
        if self.check_whitelist(remote_ip, proxy_name):
            logger.info(f"IP [{remote_ip}] 在临时放行白名单中，放行连接到 [{proxy_name}]")
            return {"reject": False, "unchange": True}

        # 3. 创建审批事件
        conn_id = uuid.uuid4().hex[:12]
        event = asyncio.Event()
        conn_info = {
            "event": event,
            "decision": "pending",
            "remote_ip": remote_ip,
            "remote_port": remote_port,
            "proxy_name": proxy_name,
            "msg_id": None
        }
        self.pending_conns[conn_id] = conn_info

        # 定义回调闭包
        def on_decision(action: str, cq_id: str):
            if conn_id not in self.pending_conns:
                return

            if action == "allow":
                conn_info["decision"] = "allow"
                asyncio.create_task(tg_manager.answer_callback(cq_id, "✅ 已放行本次连接！"))
            elif action == "whitelist":
                conn_info["decision"] = "whitelist"
                self.add_whitelist(remote_ip, proxy_name, config.whitelist_duration)
                asyncio.create_task(tg_manager.answer_callback(cq_id, f"⏱ 已放行，并对该 IP 白名单 {config.whitelist_duration // 60} 分钟！"))
            else: # deny
                conn_info["decision"] = "deny"
                asyncio.create_task(tg_manager.answer_callback(cq_id, "❌ 已拒绝并掐断该连接！"))

            event.set()

        tg_manager.register_decision_callback(conn_id, on_decision)

        # 4. 发送 Telegram 审批卡片
        logger.info(f"拦截到连接: IP={remote_addr} -> 目标代理={proxy_name}，正在向 Telegram 发送审批请求...")
        msg_id = await tg_manager.send_approval_card(conn_id, remote_ip, remote_port, proxy_name)
        conn_info["msg_id"] = msg_id

        # 5. 异步等待管理员审批
        try:
            await asyncio.wait_for(event.wait(), timeout=float(config.approval_timeout))
        except asyncio.TimeoutError:
            conn_info["decision"] = "timeout"
            logger.warning(f"连接审批超时: IP={remote_addr}，自动执行拒绝。")

        # 6. 注销回调并清理
        tg_manager.unregister_decision_callback(conn_id)
        self.pending_conns.pop(conn_id, None)

        decision = conn_info["decision"]

        # 7. 更新 Telegram 消息状态
        if msg_id:
            if decision in ("allow", "whitelist"):
                status_text = "🟢 <b>已允许放行</b>" + (" (并加入30分钟白名单)" if decision == "whitelist" else "")
            elif decision == "deny":
                status_text = "🔴 <b>已手动拒绝</b>"
            else:
                status_text = "⌛ <b>审批超时，已自动拦截</b>"

            asyncio.create_task(
                tg_manager.update_message_result(msg_id, remote_ip, proxy_name, status_text)
            )

        # 8. 响应 FRPS
        if decision in ("allow", "whitelist"):
            logger.info(f"✅ 放行连接: {remote_addr} -> {proxy_name}")
            return {"reject": False, "unchange": True}
        else:
            logger.info(f"❌ 拒绝连接: {remote_addr} -> {proxy_name} (原因: {decision})")
            return {"reject": True, "reject_reason": "管理员拒绝或审批超时！"}

handler = FrpHandler()
