#!/usr/bin/env bash
set -e

echo "🚀 收到代码推送，开始执行部署..."
cd /home/ubuntu/frps-telegram-auth

git fetch -q origin main
git reset --hard origin/main

source venv/bin/activate
pip install -q -r requirements.txt

echo "🔄 正在重启 frps-tg-auth 服务..."
sudo systemctl restart frps-tg-auth.service
sleep 2

STATUS=$(systemctl is-active frps-tg-auth.service || true)
if [ "$STATUS" = "active" ]; then
  echo "=========================================="
  echo "🎉 部署验证成功！服务正常运行中 (状态: $STATUS)"
  echo "=========================================="
else
  echo "=========================================="
  echo "❌ 部署失败！服务状态异常 (状态: $STATUS)"
  echo "📋 正在提取服务最新日志供排查："
  echo "=========================================="
  journalctl -u frps-tg-auth.service -n 30 --no-pager
  exit 1
fi
