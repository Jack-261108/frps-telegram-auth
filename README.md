# FRPS Telegram 实时审批与双因子认证系统 (frps-telegram-auth)

基于 **FRP Server Plugin (HTTP Webhook)** 与 **Telegram Bot API** 的实时交互式访问控制系统。

当有外部请求连接受保护的 FRP 穿透服务（例如手机 Termux SSH）时，系统会自动拦截连接并向管理员的 Telegram 发送审批卡片，管理员点击【允许】才放行，点击【拒绝】或超时则自动切断。

---

## ✨ 核心特性

- 🛡 **实时事前拦截**：基于 FRPS 官方 `NewUserConn` 插件机制，连接建立前拦截，杜绝未授权访问。
- 📱 **Telegram 交互式审批**：带 Inline 按钮的高颜值卡片，支持【允许单次】、【拒绝】和【临时放行 30 分钟】。
- 🔒 **纯内网交互**：Telegram 采用长轮询（Long Polling）机制，**无需公网域名或 HTTPS 证书**，开箱即用。
- ⚡ **自动部署（CI/CD）**：接入 GitHub Actions，代码一旦 push 到 `main` 分支即可自动 SSH 连接服务器热更新并重启；同时保留 Telegram `/update` 指令随时手动触发。
- 🐧 **Systemd 托管**：开机自启、故障自动拉起，可通过 `journalctl` 查看实时审计日志。

---

## 🚀 架构拓扑

```
[ 外部发起 SSH 连接 6022 端口 ]
               │
               ▼
        [ txyun: frps (v0.71+) ]
               │ (挂起连接)
               ▼ (HTTP POST /handler)
┌──────────────────────────────────────────────┐
│  frps-telegram-auth (FastAPI + Telegram Polling) │
└──────────────┬───────────────────────────────┘
               │ (推送审批卡片)
               ▼
       [ 管理员手机 Telegram ] ──(点击【✅ 允许】)──► [ 放行连接并连通手机 ]
```

---

## 🛠 安装与配置

### 1. 克隆代码与安装依赖

```bash
git clone https://github.com/Jack-261108/frps-telegram-auth.git
cd frps-telegram-auth

python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### 2. 修改配置文件

```bash
cp config.example.yaml config.yaml
nano config.yaml
```

填入你的 `bot_token` 和 `admin_chat_id`（可通过向机器人发送 `/start` 查看你的 Chat ID）。

### 3. 配置 FRPS 服务端 (`frps.toml`)

在 `/home/ubuntu/frp/frps.toml` 中追加：

```toml
[[httpPlugins]]
name = "telegram-approval"
addr = "127.0.0.1:8765"
path = "/handler"
ops = ["NewUserConn"]
```

重启 FRPS：
```bash
sudo systemctl restart frps
```

### 4. 注册 Systemd 服务

```bash
sudo cp frps-tg-auth.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now frps-tg-auth
```

查看运行状态与日志：
```bash
systemctl status frps-tg-auth
journalctl -u frps-tg-auth -f
```

---

## 🤖 Telegram 指令列表

- `/start`：测试机器人连通性并获取当前用户的 Chat ID
- `/status`：查看当前挂起的连接数与临时白名单 IP 列表
- `/update`：手动触发从 GitHub 拉取最新代码并热重启服务

---

## ⚙️ CI/CD 自动部署机制

本项目通过 `.github/workflows/deploy.yml` 实现了标准的 GitHub Actions 持续部署：
1. 本地代码 `git push` 到 `main` 分支；
2. GitHub Actions 自动触发并读取 GitHub Secrets 中的服务器主机、端口与 SSH 密钥；
3. 远程执行 `git fetch && git reset --hard origin/main`，安装更新的依赖；
4. 调用 `sudo systemctl restart frps-tg-auth.service` 完成平滑热重启并验证服务状态。

