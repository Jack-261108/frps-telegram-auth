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
        # 封禁黑名单: ip -> expire_timestamp
        self.ip_blacklist: Dict[str, float] = {}

    def is_proxy_protected(self, proxy_name: str) -> bool:
        if not config.protected_proxies:
            return True
        for pattern in config.protected_proxies:
            if pattern in proxy_name:
                return True
        return False

    def check_whitelist(self, ip: str, proxy_name: str) -> bool:
        self._clean_expired_lists()
        key = (ip, proxy_name)
        if key in self.ip_whitelist:
            if time.time() < self.ip_whitelist[key]:
                return True
            else:
                self.ip_whitelist.pop(key, None)
        return False

    def add_whitelist(self, ip: str, proxy_name: str, duration: int):
        self.ip_whitelist[(ip, proxy_name)] = time.time() + duration

    def check_blacklist(self, ip: str) -> bool:
        self._clean_expired_lists()
        if ip in self.ip_blacklist:
            if time.time() < self.ip_blacklist[ip]:
                return True
            else:
                self.ip_blacklist.pop(ip, None)
        return False

    def add_blacklist(self, ip: str, duration: int):
        self.ip_blacklist[ip] = time.time() + duration

    def remove_blacklist(self, ip: str) -> bool:
        if ip in self.ip_blacklist:
            self.ip_blacklist.pop(ip, None)
            return True
        return False

    def _clean_expired_lists(self):
        now = time.time()
        expired_white = [k for k, v in self.ip_whitelist.items() if v <= now]
        for k in expired_white:
            self.ip_whitelist.pop(k, None)
        expired_black = [k for k, v in self.ip_blacklist.items() if v <= now]
        for k in expired_black:
            self.ip_blacklist.pop(k, None)

    async def handle_new_user_conn(self, content: dict) -> dict:
        proxy_name = content.get("proxy_name", "")
        remote_addr = content.get("remote_addr", "")

        # 支持 IPv4 (1.2.3.4:5678) 和 IPv6 ([2001:db8::1]:5678)
        if remote_addr.startswith("[") and "]:" in remote_addr:
            remote_ip, port_str = remote_addr[1:].split("]:", 1)
            remote_port = int(port_str) if port_str.isdigit() else 0
        elif ":" in remote_addr:
            remote_ip, port_str = remote_addr.rsplit(":", 1)
            remote_port = int(port_str) if port_str.isdigit() else 0
        else:
            remote_ip, remote_port = remote_addr, 0

        # 1. 检查是否在黑名单中（被管理员封禁的 IP，静默拦截）
        if self.check_blacklist(remote_ip):
            logger.info(f"🚫 IP [{remote_ip}] 处于封禁黑名单中，静默拦截连接: {remote_addr} -> {proxy_name}")
            return {"reject": True, "reject_reason": "您的 IP 已被管理员封禁！"}

        # 2. 检查是否为受保护的代理
        if not self.is_proxy_protected(proxy_name):
            logger.info(f"代理 [{proxy_name}] 未在保护名单中，自动放行 {remote_addr}")
            return {"reject": False, "unchange": True}

        # 3. 检查是否在白名单中
        if self.check_whitelist(remote_ip, proxy_name):
            logger.info(f"IP [{remote_ip}] 在临时放行白名单中，放行连接到 [{proxy_name}]")
            return {"reject": False, "unchange": True}

        # 4. 同 IP 并发防抖：检查是否已有同一 IP 的审批卡片正在等待处理（避免扫描器并发刷屏）
        for pending in self.pending_conns.values():
            if pending.get("remote_ip") == remote_ip:
                logger.warning(f"⚠️ IP [{remote_ip}] 已有正在等待审批的连接，并发连接静默拦截: {remote_addr}")
                return {"reject": True, "reject_reason": "已有相同 IP 的连接正在等待审批，请勿并发发起"}

        # 5. 创建审批事件
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
            elif action == "ban":
                conn_info["decision"] = "ban"
                self.add_blacklist(remote_ip, config.ban_duration)
                ban_hours = config.ban_duration // 3600
                asyncio.create_task(tg_manager.answer_callback(cq_id, f"🚫 已将该 IP 封禁 {ban_hours} 小时！"))
            else: # deny
                conn_info["decision"] = "deny"
                asyncio.create_task(tg_manager.answer_callback(cq_id, "❌ 已拒绝并掐断该连接！"))

            event.set()

        tg_manager.register_decision_callback(conn_id, on_decision)

        # 6. 发送 Telegram 审批卡片
        logger.info(f"拦截到连接: IP={remote_addr} -> 目标代理={proxy_name}，正在向 Telegram 发送审批请求...")
        msg_id = await tg_manager.send_approval_card(conn_id, remote_ip, remote_port, proxy_name)
        conn_info["msg_id"] = msg_id

        if msg_id is None:
            logger.warning(f"Telegram 审批卡片发送失败（可能未配置 Bot 凭据或网络异常），快速阻断连接: {remote_addr}")
            tg_manager.unregister_decision_callback(conn_id)
            self.pending_conns.pop(conn_id, None)
            return {"reject": True, "reject_reason": "安全审批通道不可用或未配置，已自动拒绝连接！"}

        # 7. 异步等待管理员审批
        try:
            await asyncio.wait_for(event.wait(), timeout=float(config.approval_timeout))
        except asyncio.TimeoutError:
            conn_info["decision"] = "timeout"
            logger.warning(f"连接审批超时: IP={remote_addr}，自动执行拒绝。")

        # 8. 注销回调并清理
        tg_manager.unregister_decision_callback(conn_id)
        self.pending_conns.pop(conn_id, None)

        decision = conn_info["decision"]

        # 9. 更新或销毁 Telegram 审批卡片
        if msg_id:
            if decision == "timeout" and config.auto_delete_timeout:
                logger.info(f"审批超时，自动销毁 Telegram 卡片 (msg_id={msg_id})")
                asyncio.create_task(tg_manager.delete_message(msg_id))
            else:
                if decision in ("allow", "whitelist"):
                    status_text = "🟢 <b>已允许放行</b>" + (f" (并加入{config.whitelist_duration // 60}分钟白名单)" if decision == "whitelist" else "")
                elif decision == "ban":
                    status_text = f"🚫 <b>已手动拒绝并封禁 {config.ban_duration // 3600} 小时</b>"
                elif decision == "deny":
                    status_text = "🔴 <b>已手动拒绝</b>"
                else:
                    status_text = "⌛ <b>审批超时，已自动拦截</b>"

                asyncio.create_task(
                    tg_manager.update_message_result(msg_id, remote_ip, proxy_name, status_text)
                )

        # 10. 响应 FRPS
        if decision in ("allow", "whitelist"):
            logger.info(f"✅ 放行连接: {remote_addr} -> {proxy_name}")
            return {"reject": False, "unchange": True}
        else:
            logger.info(f"❌ 拒绝连接: {remote_addr} -> {proxy_name} (原因: {decision})")
            return {"reject": True, "reject_reason": "管理员拒绝、封禁或审批超时！"}

handler = FrpHandler()
