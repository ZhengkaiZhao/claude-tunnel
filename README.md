# claude-tunnel

通过任意公网 VPS，将 Claude Code 的 API 访问能力从 C 端（有 API 权限的服务器）桥接到 A 端（本地开发机）。服务器无需预装任何东西，工具通过 SSH 自动部署中继、建立隧道、用完自动清理。

[English](#english) | 中文

---

## 使用场景

- **C 端**：能访问 Anthropic API 或私有模型端点的机器
- **A 端**：本地开发机，想用 Claude Code 但没有直接 API 访问权限

再加一台任意公网 VPS（只需 SSH 访问），claude-tunnel 自动完成剩下的一切。

## 架构

```
A 本地开发机                    公网 VPS（中继）                 C 模型服务器
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

## 安装

```bash
git clone https://github.com/ZhengkaiZhao/claude-tunnel.git
cd claude-tunnel
pip install -r requirements.txt
```

### 依赖

| 包 | 用途 | 必须 |
|---|---|---|
| `rich` | 终端 UI（彩色输出、表格、进度条） | 是 |
| `paramiko` | 纯 Python SSH（密码认证、无需外部 ssh 命令） | 推荐 |

> A 端还需安装 [Claude Code CLI](https://docs.anthropic.com/claude-code)：`npm install -g @anthropic-ai/claude-code`

## 快速开始

### C 端（模型服务器）

```bash
python claude_tunnel.py init    # 交互式配置：role=c，VPS 地址，API key
python claude_tunnel.py up      # 部署 relay → 启动 gateway → 反向隧道
```

### A 端（本地开发机）

```bash
python claude_tunnel.py init    # 交互式配置：role=a，VPS 地址，项目目录
python claude_tunnel.py up      # 部署 relay → 本地隧道 → 启动 Claude Code
```

### Web 管理面板

```bash
python claude_tunnel.py web     # 打开 http://localhost:8765
```

提供可视化配置、一键启停、实时日志、连接诊断。

## 认证方式

支持两种认证模式：

### API Key 模式（默认）

适用于有 Anthropic API key（`sk-ant-xxx`）的用户。

```bash
python claude_tunnel.py init
# auth_type 选择 api_key，填入 API key
```

### OAuth 账户模式

适用于通过 `claude auth login` 登录 Pro/Max/Team 订阅账户的用户，无需 API key。

**C 端操作：**

```bash
# 1. 先生成长期 OAuth token（有效期一年）
claude setup-token
# 按提示完成浏览器授权，复制输出的 token

# 2. 配置 claude-tunnel
python claude_tunnel.py init
# auth_type 选择 oauth_token，粘贴上一步获得的 token
```

A 端配置不变，正常 `init` + `up` 即可。

## 命令

| 命令 | 说明 |
|------|------|
| `init` | 交互式配置向导（含环境检测） |
| `check` | 运行环境依赖检测 |
| `up` | 一键全流程：deploy + 隧道 + Claude Code |
| `down` | 断开隧道 + 清理服务器 relay |
| `deploy` | 仅部署 relay 到服务器 |
| `c-start` | C 端：gateway + 反向隧道 |
| `a-start` | A 端：本地隧道 + Claude Code |
| `status` | 查看 relay 和房间状态 |
| `web` | 启动 Web 管理面板 |

支持 `--config <path>` 指定配置文件，`--skip-check` 跳过环境检测。

## 配置

配置保存在 `~/.claude-tunnel.json`，完整示例见 `config.example.json`。

```jsonc
{
  "role": "c",                    // "a" 或 "c"
  "server": {
    "host": "your-vps.example.com",
    "port": 22,
    "user": "root",
    "password": "",               // 密码认证
    "key_file": null              // 或 SSH key 路径（推荐）
  },
  "tunnel": {
    "relay_port": 8088,
    "forward_port": 19001
  },
  "gateway": {                    // C 端
    "port": 8787,
    "token": "shared-secret",     // A/C 两端必须一致
    "auth_type": "api_key",       // "api_key" 或 "oauth_token"
    "upstream_base_url": "https://api.anthropic.com",
    "upstream_auth_token": "sk-ant-xxx"  // OAuth 模式填 setup-token 输出的 token
  },
  "claude": {                     // A 端
    "local_port": 50000,
    "model": "claude-sonnet-4-6",
    "project_dir": "/path/to/project",
    "command": ""                  // 留空自动检测
  },
  "room": {
    "name": "default",            // A/C 两端必须一致
    "token": "change-me"          // A/C 两端必须一致
  }
}
```

## SSH 认证

- **SSH key（推荐）**：设置 `server.key_file`
- **密码**：设置 `server.password`，通过 paramiko 自动注入，无需手动输入

## 平台支持

| 平台 | 状态 | 备注 |
|------|------|------|
| Linux | 完全支持 | — |
| macOS | 完全支持 | 需 OpenSSH 8.4+（macOS 12+ 自带） |
| Windows | 支持 | 推荐 paramiko；如遇 claude.exe 问题，设置 `command` 为 `npx @anthropic-ai/claude-code` |

## 工作原理

1. `deploy`：通过 SSH 上传轻量 relay 脚本到 VPS `/tmp/`，后台启动
2. `c-start`：本地启动 API gateway，建立 SSH -R 反向隧道
3. `a-start`：建立 SSH -L 本地隧道，启动 Claude Code
4. relay 5 分钟无心跳自动退出；Ctrl+C 立即清理

## 项目结构

```
claude_tunnel.py   主程序（relay、gateway、隧道、CLI）
ct_web.py          Web 管理面板
ct_init.py         交互式配置向导
ct_ui.py           终端 UI 组件
```

---

## English

**claude-tunnel** bridges Claude Code API access from a server (C-side) to a local dev machine (A-side) through any public VPS via SSH tunneling.

### Install

```bash
git clone https://github.com/ZhengkaiZhao/claude-tunnel.git
cd claude-tunnel
pip install -r requirements.txt
```

### Dependencies

- `rich` — terminal UI
- `paramiko` — pure Python SSH (recommended for password auth)
- Claude Code CLI on A-side: `npm install -g @anthropic-ai/claude-code`

### Usage

```bash
# C-side (model server)
python claude_tunnel.py init   # role=c, VPS info, API key or OAuth token
python claude_tunnel.py up     # deploy relay + gateway + reverse tunnel

# A-side (dev machine)
python claude_tunnel.py init   # role=a, VPS info, project dir
python claude_tunnel.py up     # deploy relay + local tunnel + Claude Code

# Web UI
python claude_tunnel.py web    # http://localhost:8765
```

### Authentication Modes

**API Key mode** (default): Use your `sk-ant-xxx` API key.

**OAuth token mode**: For Pro/Max/Team subscribers without an API key:
```bash
# On C-side, generate a long-lived OAuth token (valid for 1 year)
claude setup-token
# Then run init and select auth_type = oauth_token, paste the token
python claude_tunnel.py init
```

### Requirements

- Python 3.10+
- Any public VPS with SSH access
- Claude Code CLI on A-side

## License

MIT
