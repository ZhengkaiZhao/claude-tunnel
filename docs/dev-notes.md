# claude-tunnel 开发文档

## 项目状态

**核心功能已全部验证通过。**

| 功能 | 状态 |
|------|------|
| `deploy` — 自动部署 relay 到服务器 | ✅ 通过 |
| `status` — 查询 relay 房间状态 | ✅ 通过 |
| `down` — 断开并清理服务器 relay | ✅ 通过 |
| `c-start` — C 端 gateway + 反向隧道 | 待实机测试 |
| `a-start` — A 端本地隧道 + Claude Code | 待实机测试 |
| `web` — 本地 Web UI | 待测试 |

## 已解决的技术问题

### 1. SSH 密码自动化
**方案：`SSH_ASKPASS` + `SSH_ASKPASS_REQUIRE=force`**

写一个临时 shell 脚本 `echo <password>`，设为 `SSH_ASKPASS`，SSH 会自动调用它获取密码，无需 sshpass、pty 或任何交互。

关键：必须同时加 `-o PubkeyAuthentication=no -o PreferredAuthentications=keyboard-interactive,password`，否则 SSH 先尝试 publickey 失败后不会再弹密码提示。

### 2. 文件上传（relay 脚本）
**方案：`scp` + `SSH_ASKPASS`**

relay 脚本约 3KB，base64 编码后超过 SSH 命令行长度限制（4096 字节）。改用 `scp` 上传临时文件，`scp` 同样支持 `SSH_ASKPASS`。

### 3. pkill 自杀问题
**方案：PID 文件**

`pkill -f /tmp/claude-tunnel-xxx/relay.py` 会匹配到 SSH 进程本身（因为路径在 SSH 命令参数里），导致 SSH 连接被杀死（RC 255）。改用 PID 文件：启动时 `echo $! | tee relay.pid`，停止时 `kill $(cat relay.pid)`。

### 4. relay 只监听 127.0.0.1
**方案：通过 SSH 在服务器上执行 curl**

relay 出于安全只监听 `127.0.0.1`，本地无法直接访问。`relay_post()` 和 `status` 命令通过 SSH 在服务器上执行 `curl` 来查询 relay。

## 文件清单

```
claude-tunnel/
├── claude_tunnel.py       # 主文件（~800行），所有功能
├── config.example.json    # 配置模板
├── README.md              # 项目说明
├── LICENSE                # MIT
└── docs/
    └── dev-notes.md       # 本文件
```

## claude_tunnel.py 结构

| 行范围（约） | 模块 | 说明 |
|-------------|------|------|
| 1-50 | Header | imports, version, config schema |
| 51-126 | Config | load/save/init wizard |
| 127-215 | SSH helpers | SSH_ASKPASS 密码自动化、scp 上传、tunnel 后台启动 |
| 216-300 | Embedded relay | 内嵌的 relay 脚本（字符串常量，~60行） |
| 300-380 | Deploy/Down | 自动部署/清理 relay |
| 380-470 | Gateway proxy | C 端 API 代理（内嵌） |
| 470-500 | Relay client | 心跳、状态查询（通过 SSH curl） |
| 500-600 | c-start / a-start | 核心启动逻辑 |
| 600-640 | up / status | 组合命令 |
| 640-740 | Web UI | 内嵌 HTML + localhost HTTP server |
| 740-end | Main + cleanup | argparse + dispatch + atexit |

## 测试步骤

```bash
# 1. 语法检查
python3 -m py_compile claude_tunnel.py

# 2. 部署 relay（需要服务器 SSH 访问）
python3 claude_tunnel.py deploy --config test-config.json

# 3. 查询状态
python3 claude_tunnel.py status --config test-config.json

# 4. 清理
python3 claude_tunnel.py down --config test-config.json

# 5. C 端全流程（需要真实 API key）
python3 claude_tunnel.py up --config test-config.json  # role=c

# 6. A 端全流程（需要 Claude Code 已安装）
python3 claude_tunnel.py up --config test-config.json  # role=a
```

## 配置说明

```jsonc
{
  "role": "c",              // "a" 或 "c"
  "server": {
    "host": "1.2.3.4",      // 公网 VPS IP
    "port": 22,
    "user": "root",
    "password": "xxx",      // 密码（与 key_file 二选一）
    "key_file": null        // SSH key 路径（推荐，完全静默）
  },
  "tunnel": {
    "relay_port": 8088,     // relay HTTP 端口（服务器上，127.0.0.1）
    "forward_port": 19001   // SSH 转发端口（服务器上）
  },
  "gateway": {              // C 端填写
    "host": "127.0.0.1",
    "port": 8787,
    "token": "shared-secret",
    "upstream_base_url": "https://api.anthropic.com",
    "upstream_auth_token": "sk-ant-xxx"
  },
  "claude": {               // A 端填写
    "local_port": 50000,
    "model": "claude-sonnet-4-6",
    "project_dir": "/path/to/project"
  },
  "room": {
    "name": "default",      // A/C 必须相同
    "token": "change-me"    // A/C 必须相同
  }
}
```

## 架构图

```
A (本地开发机)                    VPS (任何公网服务器)              C (模型服务器)
┌──────────────┐               ┌──────────────────┐           ┌──────────────┐
│ Claude Code  │               │ relay (auto)     │           │ Gateway Proxy│
│ localhost:   │──SSH -L──────▶│ :19001 (forward) │◀──SSH -R──│ :8787        │
│ 50000        │               │ :8088 (relay)    │           │              │
└──────────────┘               └──────────────────┘           └──────────────┘
                                                                     │
                                                              ┌──────┴──────┐
                                                              │ Upstream API│
                                                              │ anthropic   │
                                                              └─────────────┘
```

数据流：Claude Code → localhost:50000 → SSH -L → VPS:19001 → SSH -R → C:8787 → Anthropic API

