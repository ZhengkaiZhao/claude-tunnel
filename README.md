# claude-tunnel

一条命令，通过任意公网 VPS 把 Claude Code 的模型能力从 C 端（有 API 访问权限的服务器）桥接到 A 端（本地开发机）。**服务器无需预装任何东西**，工具通过 SSH 自动部署中继、建立隧道、用完自动清理。

[English](#english) | 中文

---

## 使用场景

你有两台机器：

- **C 端**：公司服务器 / 有 VPN 的机器，能访问 Anthropic API 或私有模型端点
- **A 端**：本地开发机，想用 Claude Code 编辑本地文件，但没有直接的 API 访问权限

再加一台任意公网 VPS（只需 SSH 访问），claude-tunnel 自动完成剩下的一切。

## 架构

```
A 本地开发机                    公网 VPS（任意）                C 模型服务器
┌──────────────┐            ┌──────────────────────┐       ┌──────────────────┐
│  Claude Code │            │  relay（自动部署）     │       │  API Gateway     │
│  localhost:  │──SSH -L───▶│  :19001（转发端口）   │◀─SSH -R│  :8787           │
│  50000       │            │  :8088（relay HTTP）  │       │                  │
└──────────────┘            └──────────────────────┘       └────────┬─────────┘
                                                                     │
                                                            ┌────────▼─────────┐
                                                            │  Anthropic API   │
                                                            │  / 私有端点       │
                                                            └──────────────────┘
```

数据流：`Claude Code → localhost:50000 → SSH -L → VPS:19001 → SSH -R → C:8787 → API`

## 快速开始

```bash
git clone https://github.com/ZhengkaiZhao/claude-tunnel.git
cd claude-tunnel
```

### C 端（模型服务器）

```bash
python3 claude_tunnel.py init
# 交互式填写：role=c，VPS 地址/密码，API key 等

python3 claude_tunnel.py up
# 自动：部署 relay → 启动 gateway → 建立反向隧道
# Ctrl+C 退出时自动清理服务器
```

### A 端（本地开发机）

```bash
python3 claude_tunnel.py init
# 交互式填写：role=a，VPS 地址/密码，项目目录等

python3 claude_tunnel.py up
# 自动：部署 relay → 建立本地隧道 → 启动 Claude Code
# Claude Code 退出时自动断开
```

## 命令

| 命令 | 说明 |
|------|------|
| `init` | 交互式配置向导，保存到 `~/.claude-tunnel.json` |
| `up` | 一键全流程：deploy + 启动隧道 + 启动 Claude Code |
| `down` | 断开隧道 + 清理服务器上的 relay |
| `deploy` | 仅部署 relay 到服务器 |
| `c-start` | C 端：启动 gateway + 反向隧道 |
| `a-start` | A 端：本地隧道 + 启动 Claude Code |
| `status` | 查看 relay 和房间状态 |
| `web` | 启动本地 Web UI（http://localhost:8765） |

所有命令都支持 `--config <path>` 指定配置文件路径。

## 配置文件

配置保存在 `~/.claude-tunnel.json`，完整示例见 `config.example.json`。

```jsonc
{
  "role": "c",                    // "a" 或 "c"
  "server": {
    "host": "your-vps.example.com",
    "port": 22,
    "user": "root",
    "password": "xxx",            // 密码认证
    "key_file": null              // 或 SSH key 路径（推荐）
  },
  "tunnel": {
    "relay_port": 8088,           // relay 在服务器上监听的端口
    "forward_port": 19001         // SSH 转发端口
  },
  "gateway": {                    // C 端填写
    "host": "127.0.0.1",
    "port": 8787,
    "token": "shared-secret",     // A/C 两端必须一致
    "upstream_base_url": "https://api.anthropic.com",
    "upstream_auth_token": "sk-ant-xxx"
  },
  "claude": {                     // A 端填写
    "local_port": 50000,
    "model": "claude-sonnet-4-6",
    "project_dir": "/path/to/project"
  },
  "room": {
    "name": "default",            // A/C 两端必须一致
    "token": "change-me"          // A/C 两端必须一致
  }
}
```

## SSH 认证

支持密码和 SSH key 两种方式：

- **SSH key（推荐）**：设置 `server.key_file`，完全静默无交互
- **密码**：设置 `server.password`，工具通过 `SSH_ASKPASS` 机制自动注入，无需手动输入，也不依赖 sshpass

## Web UI

有浏览器的机器可以用 Web 模式：

```bash
python3 claude_tunnel.py web
# 打开 http://localhost:8765
```

提供 up / down / status 按钮，实时显示连接状态。

## 环境要求

- Python 3.10+
- 纯 stdlib，零外部依赖
- A 端需要安装 [Claude Code CLI](https://docs.anthropic.com/claude-code)
- VPS 只需 SSH 访问权限（无需预装任何东西）

## 工作原理

1. `deploy` 通过 SSH + scp 把一个轻量 relay 脚本（~60 行）上传到 VPS 的 `/tmp/claude-tunnel-<room>/`，后台启动
2. `c-start` 在本地启动 API gateway 代理，然后建立 SSH `-R` 反向隧道，让 VPS 能访问 C 端的 gateway
3. `a-start` 建立 SSH `-L` 本地隧道，把 `localhost:50000` 转发到 VPS 的转发端口，然后带着正确的环境变量启动 Claude Code
4. relay 在 5 分钟无心跳后自动退出；`down` 或 Ctrl+C 也会立即清理

---

## English

**claude-tunnel** bridges Claude Code's model capability from a server with API access (C-side) to a local dev machine (A-side) through any public VPS. The server needs no pre-installation — everything is deployed automatically via SSH.

### Quick Start

```bash
git clone https://github.com/ZhengkaiZhao/claude-tunnel.git
cd claude-tunnel

# On C-side (model server)
python3 claude_tunnel.py init   # role=c, fill VPS info + API key
python3 claude_tunnel.py up     # auto-deploy relay + gateway + reverse tunnel

# On A-side (dev machine)
python3 claude_tunnel.py init   # role=a, fill VPS info + project dir
python3 claude_tunnel.py up     # auto-deploy relay + local tunnel + Claude Code
```

### Requirements

- Python 3.10+, pure stdlib, no dependencies
- Any public VPS with SSH access
- Claude Code CLI on A-side

## License

MIT
