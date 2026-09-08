import asyncio
import logging
import subprocess
import sys
from pathlib import Path
from config import config

logger = logging.getLogger("frps_auth.updater")

class AutoUpdater:
    def __init__(self):
        self.repo_dir = Path(__file__).parent.resolve()
        self.branch = config.git_branch
        self.interval = config.auto_update_interval
        self.is_running = False

    async def check_and_update(self, notify_callback=None) -> bool:
        """检查远程 Git 仓库是否有新提交，若有则拉取并重启 systemd 服务"""
        try:
            logger.info("正在检查 GitHub 仓库最新提交...")

            # 1. git fetch
            proc = await asyncio.create_subprocess_exec(
                "git", "fetch", "origin", self.branch,
                cwd=str(self.repo_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                msg = f"❌ Git fetch 失败: {stderr.decode()}"
                logger.error(msg)
                if notify_callback:
                    await notify_callback(msg)
                return False

            # 2. 比对本地 HEAD 与远程分支
            proc_local = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "HEAD",
                cwd=str(self.repo_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            local_hash, _ = await proc_local.communicate()

            proc_remote = await asyncio.create_subprocess_exec(
                "git", "rev-parse", f"origin/{self.branch}",
                cwd=str(self.repo_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            remote_hash, _ = await proc_remote.communicate()

            local_hash_str = local_hash.decode().strip()
            remote_hash_str = remote_hash.decode().strip()

            if local_hash_str == remote_hash_str:
                logger.info("代码已是最新版本，无需更新。")
                if notify_callback:
                    await notify_callback(f"✅ 当前代码已是最新版本 (commit: <code>{local_hash_str[:7]}</code>)")
                return False

            logger.info(f"发现新版本: {local_hash_str[:7]} -> {remote_hash_str[:7]}，开始执行拉取...")
            if notify_callback:
                await notify_callback(f"🚀 发现新版本 (<code>{remote_hash_str[:7]}</code>)，正在拉取更新并自动重启...")

            # 3. git pull
            proc_pull = await asyncio.create_subprocess_exec(
                "git", "pull", "origin", self.branch,
                cwd=str(self.repo_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            pull_out, pull_err = await proc_pull.communicate()
            if proc_pull.returncode != 0:
                logger.error(f"Git pull 失败: {pull_err.decode()}")
                if notify_callback:
                    await notify_callback(f"❌ Git pull 失败: {pull_err.decode()}")
                return False

            # 4. 安装可能更新的 Python 依赖
            req_file = self.repo_dir / "requirements.txt"
            if req_file.exists():
                pip_proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "pip", "install", "-r", "requirements.txt",
                    cwd=str(self.repo_dir),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
                await pip_proc.communicate()

            # 5. 调用 systemctl 重启自身服务
            logger.info("更新完成，正在重启 frps-tg-auth 服务...")
            await asyncio.sleep(1) # 留时间发送通知

            # 使用 sudo systemctl restart
            subprocess.Popen(["sudo", "systemctl", "restart", "frps-tg-auth.service"])
            return True

        except Exception as e:
            logger.error(f"自动更新异常: {e}")
            if notify_callback:
                await notify_callback(f"⚠️ 自动更新出错: {e}")
            return False

    async def start_updater_loop(self):
        """后台定时轮询任务"""
        if not config.auto_update_enabled:
            logger.info("自动更新未启用。")
            return

        self.is_running = True
        logger.info(f"自动更新轮询已启动，每隔 {self.interval} 秒检查一次 GitHub 更新...")
        while self.is_running:
            await asyncio.sleep(self.interval)
            try:
                await self.check_and_update()
            except Exception as e:
                logger.error(f"定时更新检查出错: {e}")

updater = AutoUpdater()
