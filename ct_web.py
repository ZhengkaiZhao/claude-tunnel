"""Web management panel for claude-tunnel.

Provides a full-featured web UI with:
- Environment detection + install guidance
- Configuration wizard
- Real-time console with SSE log streaming
- Connection info with copy buttons
"""
from __future__ import annotations

import json
import os
import platform
import queue
import shutil
import subprocess
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse, parse_qs

# Log queue for SSE streaming
_log_queue: queue.Queue = queue.Queue(maxsize=500)
_log_history: list = []


def log_emit(msg: str, level: str = "info") -> None:
    """Emit a log message to the SSE stream."""
    entry = {"ts": time.time(), "msg": msg, "level": level}
    _log_history.append(entry)
    if len(_log_history) > 200:
        _log_history.pop(0)
    try:
        _log_queue.put_nowait(entry)
    except queue.Full:
        pass


WEB_HTML = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>claude-tunnel</title>
<script src="https://cdn.tailwindcss.com"></script>
<script>
tailwind.config = {
  theme: { extend: { colors: {
    brand: { 50:'#FEF4EE',100:'#FDDCCC',400:'#E8845F',500:'#D97757',600:'#C4623F' },
    sand: { 50:'#FDFCFB',100:'#FAF9F7',200:'#F3F1EE',300:'#E8E4DF',400:'#D4CEC7',500:'#A39E96',600:'#6B6560',700:'#524D47',800:'#3D3833',900:'#2C2824' },
  }}}
}
</script>
<style>
body { background: #FAF9F7; color: #2C2824; font-family: 'Inter', -apple-system, system-ui, sans-serif; }
.tab-btn.active { border-color: #D97757; color: #D97757; background: #FEF4EE; }
.tab-btn { color: #6B6560; border-bottom: 2px solid transparent; }
.tab-btn:hover:not(.active) { color: #3D3833; background: #F3F1EE; }
.status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex-shrink: 0; }
.status-dot.green { background: #16a34a; box-shadow: 0 0 0 3px #dcfce7; }
.status-dot.red { background: #dc2626; box-shadow: 0 0 0 3px #fee2e2; }
.status-dot.yellow { background: #d97706; box-shadow: 0 0 0 3px #fef3c7; }
.log-line { font-family: 'JetBrains Mono', 'Fira Code', monospace; font-size: 12px; line-height: 1.6; }
.card { background: #fff; border: 1px solid #E8E4DF; border-radius: 16px; box-shadow: 0 1px 3px rgba(44,40,36,0.04), 0 4px 12px rgba(44,40,36,0.02); }
.section-label { font-size: 11px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; color: #A39E96; }
.field-label { display: block; font-size: 13px; color: #6B6560; margin-bottom: 5px; font-weight: 500; }
.field-label .req { color: #dc2626; font-weight: 700; margin-left: 2px; }
.field-input { width: 100%; border: 1px solid #E8E4DF; border-radius: 8px; padding: 9px 12px; font-size: 13.5px; color: #2C2824; background: #FDFCFB; transition: border-color 0.2s, box-shadow 0.2s; }
.field-input:focus { outline: none; border-color: #D97757; box-shadow: 0 0 0 3px rgba(217,119,87,0.1); }
.field-input:hover:not(:focus) { border-color: #D4CEC7; }
.btn-primary { background: linear-gradient(135deg, #D97757 0%, #C4623F 100%); color: white; border-radius: 8px; padding: 9px 20px; font-size: 13.5px; font-weight: 600; transition: all 0.15s; box-shadow: 0 1px 2px rgba(217,119,87,0.3); }
.btn-primary:hover { transform: translateY(-1px); box-shadow: 0 3px 8px rgba(217,119,87,0.3); }
.btn-primary:active { transform: translateY(0); }
.btn-primary:disabled { opacity: 0.6; cursor: not-allowed; transform: none; box-shadow: none; }
.btn-secondary { background: #fff; color: #3D3833; border-radius: 8px; padding: 8px 14px; font-size: 13.5px; font-weight: 500; border: 1px solid #E8E4DF; transition: all 0.15s; }
.btn-secondary:hover { background: #F3F1EE; border-color: #D4CEC7; }
.btn-danger { background: linear-gradient(135deg, #dc2626 0%, #b91c1c 100%); color: white; border-radius: 8px; padding: 8px 14px; font-size: 13.5px; font-weight: 600; transition: all 0.15s; box-shadow: 0 1px 2px rgba(220,38,38,0.3); }
.btn-danger:hover { transform: translateY(-1px); box-shadow: 0 3px 8px rgba(220,38,38,0.3); }
.btn-ghost { background: transparent; color: #6B6560; border-radius: 6px; padding: 6px 10px; font-size: 12px; transition: all 0.15s; }
.btn-ghost:hover { background: #F3F1EE; color: #3D3833; }
.role-badge { display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px; border-radius: 20px; font-size: 12px; font-weight: 600; }
.role-badge.a { background: #EFF6FF; color: #1d4ed8; }
.role-badge.c { background: #FEF4EE; color: #C4623F; }
@keyframes pulse-dot { 0%,100%{opacity:1} 50%{opacity:0.3} }
.pulse { animation: pulse-dot 1.5s infinite; }
@keyframes fadeIn { from{opacity:0;transform:translateY(4px)} to{opacity:1;transform:translateY(0)} }
.fade-in { animation: fadeIn 0.3s ease-out; }
#log-container { max-height: 380px; overflow-y: auto; scroll-behavior: smooth; }
#log-container::-webkit-scrollbar { width: 4px; }
#log-container::-webkit-scrollbar-track { background: transparent; }
#log-container::-webkit-scrollbar-thumb { background: #524D47; border-radius: 2px; }
.divider { border: none; border-top: 1px solid #E8E4DF; margin: 24px 0; }
.inline-test { transition: all 0.2s; }
.inline-test:hover { background: #F3F1EE; border-color: #D4CEC7; }
.suite-step { transition: all 0.3s ease; }
</style>
</head>
<body class="min-h-screen">

<!-- Header -->
<header class="border-b border-sand-300 px-6 py-3.5 flex items-center justify-between bg-white sticky top-0 z-10" style="box-shadow:0 1px 3px rgba(44,40,36,0.03)">
  <div class="flex items-center gap-3">
    <div class="w-7 h-7 rounded-lg bg-brand-500 flex items-center justify-center">
      <svg class="w-3.5 h-3.5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M13 10V3L4 14h7v7l9-11h-7z"/>
      </svg>
    </div>
    <span class="text-base font-semibold text-sand-900 tracking-tight">claude-tunnel</span>
    <span class="text-xs text-sand-400 border border-sand-300 px-2 py-0.5 rounded-full font-medium">v0.1.0</span>
  </div>
  <div class="flex items-center gap-3">
    <span id="role-badge" class="role-badge hidden">—</span>
    <div class="flex items-center gap-2 text-sm text-sand-600">
      <span class="status-dot red" id="global-dot"></span>
      <span id="global-status-text" class="font-medium">Disconnected</span>
    </div>
  </div>
</header>

<!-- Tabs -->
<nav class="border-b border-sand-300 px-4 flex bg-white">
  <button class="tab-btn active px-5 py-3 text-sm font-medium rounded-t-lg" data-tab="env">Environment</button>
  <button class="tab-btn px-5 py-3 text-sm font-medium rounded-t-lg" data-tab="config">Configuration</button>
  <button class="tab-btn px-5 py-3 text-sm font-medium rounded-t-lg" data-tab="console">Console</button>
  <button class="tab-btn px-5 py-3 text-sm font-medium rounded-t-lg" data-tab="connect">Connection</button>
</nav>

<!-- Tab: Environment -->
<div id="tab-env" class="tab-content p-6 max-w-3xl mx-auto">
  <div class="card p-6 mb-4">
    <div class="flex items-center justify-between mb-5">
      <div>
        <h2 class="text-base font-semibold text-sand-900">Environment Check</h2>
        <p class="text-xs text-sand-500 mt-0.5">Verify all required tools are installed</p>
      </div>
      <div class="flex gap-2">
        <button onclick="testSSH()" class="btn-secondary text-sm">Test SSH</button>
        <button onclick="runEnvCheck()" class="btn-primary text-sm">Re-check</button>
      </div>
    </div>
    <div id="env-results" class="space-y-1">
      <p class="text-sand-500 text-sm">Scanning environment...</p>
    </div>
  </div>
  <div id="env-install" class="card p-6 mb-4 hidden">
    <p class="section-label mb-4">Install Missing Dependencies</p>
    <div id="env-install-list" class="space-y-2"></div>
  </div>
  <div id="env-warnings" class="card p-5 hidden" style="border-color:#fde68a;background:#fffbeb;">
    <p class="text-xs font-semibold text-amber-700 uppercase tracking-wide mb-3">Recommendations</p>
    <div id="env-warnings-list" class="space-y-2"></div>
  </div>
</div>

<!-- Tab: Configuration -->
<div id="tab-config" class="tab-content p-6 max-w-3xl mx-auto hidden">
  <div class="card p-6">
    <!-- Role selector -->
    <div class="flex items-center justify-between mb-6">
      <div>
        <h2 class="text-base font-semibold text-sand-900">Configuration</h2>
        <p class="text-xs text-sand-500 mt-0.5">Settings are saved to ~/.claude-tunnel.json</p>
      </div>
      <div class="flex rounded-lg border border-sand-300 overflow-hidden">
        <button id="role-btn-a" onclick="_userSetRole=true;setRole('a')" class="px-4 py-2 text-sm font-medium transition-colors">
          A-side <span class="text-xs opacity-60">(dev)</span>
        </button>
        <button id="role-btn-c" onclick="_userSetRole=true;setRole('c')" class="px-4 py-2 text-sm font-medium transition-colors border-l border-sand-300">
          C-side <span class="text-xs opacity-60">(server)</span>
        </button>
      </div>
    </div>

    <form id="config-form" class="space-y-6">
      <!-- VPS / Relay Server — always shown -->
      <div>
        <p class="section-label mb-3">VPS / Relay Server</p>
        <div class="grid grid-cols-2 gap-3">
          <div class="col-span-2">
            <label class="field-label">Host<span class="req">*</span></label>
            <input id="cfg-host" type="text" placeholder="your-vps.example.com" class="field-input" required>
          </div>
          <div>
            <label class="field-label">SSH Port</label>
            <input id="cfg-port" type="number" value="22" class="field-input">
          </div>
          <div>
            <label class="field-label">SSH User<span class="req">*</span></label>
            <input id="cfg-user" type="text" value="root" class="field-input">
          </div>
          <div>
            <label class="field-label">SSH Password</label>
            <input id="cfg-password" type="password" placeholder="leave empty if using key" class="field-input">
          </div>
          <div>
            <label class="field-label">SSH Key File <span class="text-sand-400 font-normal">(optional)</span></label>
            <input id="cfg-key-file" type="text" placeholder="/path/to/key" class="field-input">
          </div>
        </div>
        <!-- Inline test: SSH -->
        <div class="mt-4 flex items-center gap-3 p-3 rounded-xl bg-sand-50 border border-sand-200 inline-test">
          <div class="flex-1">
            <span class="text-xs font-semibold text-sand-700">SSH Connection</span>
            <span class="text-xs text-sand-500 ml-2">Verify credentials and connectivity to VPS</span>
          </div>
          <span id="inline-ssh-dot" class="status-dot yellow"></span>
          <span id="inline-ssh-text" class="text-xs text-sand-500 min-w-[80px] text-right">not tested</span>
          <button type="button" onclick="inlineTestSSH()" class="btn-secondary text-xs whitespace-nowrap">Test SSH</button>
        </div>
      </div>

      <hr class="divider">

      <!-- Tunnel — always shown -->
      <div>
        <p class="section-label mb-3">Tunnel</p>
        <div class="grid grid-cols-3 gap-3">
          <div>
            <label class="field-label">Relay Port</label>
            <input id="cfg-relay-port" type="number" value="8088" class="field-input">
          </div>
          <div>
            <label class="field-label">Forward Port</label>
            <input id="cfg-forward-port" type="number" value="19001" class="field-input">
          </div>
          <div>
            <label class="field-label">Room Token<span class="req">*</span></label>
            <input id="cfg-room-token" type="text" value="change-me" class="field-input">
          </div>
        </div>
        <div class="mt-3">
          <label class="field-label">Room Name<span class="req">*</span></label>
          <input id="cfg-room-name" type="text" value="default" class="field-input" style="max-width:200px">
        </div>
        <!-- Inline test: Relay -->
        <div class="mt-4 flex items-center gap-3 p-3 rounded-xl bg-sand-50 border border-sand-200 inline-test">
          <div class="flex-1">
            <span class="text-xs font-semibold text-sand-700">Relay Server</span>
            <span class="text-xs text-sand-500 ml-2">Test if relay is reachable on the configured port</span>
          </div>
          <span id="inline-relay-dot" class="status-dot yellow"></span>
          <span id="inline-relay-text" class="text-xs text-sand-500 min-w-[80px] text-right">not tested</span>
          <button type="button" onclick="inlineTestRelay()" class="btn-secondary text-xs whitespace-nowrap">Test Relay</button>
        </div>
        <!-- Rooms viewer -->
        <div class="mt-3">
          <div class="flex items-center justify-between mb-2">
            <span class="text-xs font-semibold text-sand-600 uppercase tracking-wide">Active Rooms</span>
            <button type="button" onclick="loadRooms()" class="btn-ghost text-xs flex items-center gap-1">
              <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/>
              </svg>
              Refresh
            </button>
          </div>
          <div id="rooms-panel" class="rounded-xl border border-sand-200 overflow-hidden">
            <div class="px-4 py-3 text-xs text-sand-400 bg-sand-50">Click Refresh to load rooms from relay server</div>
          </div>
        </div>
      </div>

      <hr class="divider">

      <!-- C-side: Gateway section -->
      <div id="section-gateway">
        <div class="flex items-center justify-between mb-3">
          <p class="section-label">Gateway <span class="text-brand-500">(C-side only)</span></p>
          <button type="button" onclick="autoDetectCredentials()" class="btn-secondary text-xs flex items-center gap-1.5">
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/>
            </svg>
            Auto-detect from ~/.claude
          </button>
        </div>
        <div id="auto-detect-status" class="hidden mb-3 text-xs px-3 py-2 rounded-lg"></div>
        <div class="grid grid-cols-2 gap-3">
          <div>
            <label class="field-label">Gateway Port<span class="req">*</span></label>
            <input id="cfg-gw-port" type="number" value="8787" class="field-input">
          </div>
          <div>
            <label class="field-label">Gateway Token<span class="req">*</span></label>
            <input id="cfg-gw-token" type="text" value="change-me" class="field-input">
          </div>
          <div class="col-span-2">
            <label class="field-label">Upstream API Base URL<span class="req">*</span></label>
            <input id="cfg-upstream-url" type="text" value="https://api.anthropic.com" class="field-input">
          </div>
          <div class="col-span-2">
            <label class="field-label">Upstream API Key<span class="req">*</span></label>
            <input id="cfg-upstream-token" type="password" placeholder="sk-ant-..." class="field-input">
          </div>
        </div>
        <!-- Inline test: API Key -->
        <div class="mt-4 flex items-center gap-3 p-3 rounded-xl bg-sand-50 border border-sand-200 inline-test">
          <div class="flex-1">
            <span class="text-xs font-semibold text-sand-700">API Key</span>
            <span class="text-xs text-sand-500 ml-2">Verify upstream API key is valid</span>
          </div>
          <span id="inline-apikey-dot" class="status-dot yellow"></span>
          <span id="inline-apikey-text" class="text-xs text-sand-500 min-w-[80px] text-right">not tested</span>
          <button type="button" onclick="inlineTestApiKey()" class="btn-secondary text-xs whitespace-nowrap">Test Key</button>
        </div>
        <!-- Inline test: Gateway port listening -->
        <div class="mt-2 flex items-center gap-3 p-3 rounded-xl bg-sand-50 border border-sand-200 inline-test">
          <div class="flex-1">
            <span class="text-xs font-semibold text-sand-700">Gateway Port</span>
            <span class="text-xs text-sand-500 ml-2">Check if gateway is listening locally</span>
          </div>
          <span id="inline-gwport-dot" class="status-dot yellow"></span>
          <span id="inline-gwport-text" class="text-xs text-sand-500 min-w-[80px] text-right">not tested</span>
          <button type="button" onclick="inlineTestGwPort()" class="btn-secondary text-xs whitespace-nowrap">Test Port</button>
        </div>
      </div>

      <!-- A-side: Claude Code section -->
      <div id="section-claude">
        <p class="section-label mb-3">Claude Code <span class="text-blue-500">(A-side only)</span></p>
        <div class="grid grid-cols-2 gap-3">
          <div>
            <label class="field-label">Local Port</label>
            <input id="cfg-local-port" type="number" value="50000" class="field-input">
          </div>
          <div>
            <label class="field-label">Model</label>
            <input id="cfg-model" type="text" value="claude-sonnet-4-6" class="field-input">
          </div>
          <div class="col-span-2">
            <label class="field-label">Gateway Token<span class="req">*</span> <span class="text-sand-400 font-normal">(same as C-side)</span></label>
            <input id="cfg-gw-token-a" type="text" value="change-me" class="field-input">
          </div>
          <div class="col-span-2">
            <label class="field-label">Project Directory <span class="text-sand-400 font-normal">(optional)</span></label>
            <div class="flex gap-2">
              <input id="cfg-project-dir" type="text" placeholder="auto-detect (current directory)" class="field-input flex-1">
              <button type="button" onclick="openDirPicker()" class="btn-secondary text-xs whitespace-nowrap">Browse</button>
            </div>
          </div>
        </div>
        <!-- Inline test: Local port / Gateway port -->
        <div class="mt-4 flex items-center gap-3 p-3 rounded-xl bg-sand-50 border border-sand-200 inline-test">
          <div class="flex-1">
            <span class="text-xs font-semibold text-sand-700">Tunnel Port</span>
            <span class="text-xs text-sand-500 ml-2">Check if SSH tunnel is forwarding correctly</span>
          </div>
          <span id="inline-port-dot" class="status-dot yellow"></span>
          <span id="inline-port-text" class="text-xs text-sand-500 min-w-[80px] text-right">not tested</span>
          <button type="button" onclick="inlineTestPort()" class="btn-secondary text-xs whitespace-nowrap">Test Port</button>
        </div>
      </div>

      <!-- Dir picker modal -->
      <div id="dir-picker" class="hidden fixed inset-0 bg-black bg-opacity-40 z-50 flex items-center justify-center">
        <div class="bg-white rounded-2xl shadow-xl w-full max-w-md mx-4">
          <div class="flex items-center justify-between px-5 py-4 border-b border-sand-200">
            <span class="text-sm font-semibold text-sand-900">Select Directory</span>
            <button onclick="closeDirPicker()" class="btn-ghost text-lg leading-none">×</button>
          </div>
          <div class="px-4 py-2 bg-sand-100 border-b border-sand-200">
            <code id="dir-current" class="text-xs text-sand-700 break-all"></code>
          </div>
          <div id="dir-entries" class="overflow-y-auto" style="max-height:300px;"></div>
          <div class="flex gap-2 px-5 py-4 border-t border-sand-200">
            <button onclick="selectCurrentDir()" class="btn-primary text-sm flex-1">Select This Folder</button>
            <button onclick="closeDirPicker()" class="btn-secondary text-sm">Cancel</button>
          </div>
        </div>
      </div>

      <hr class="divider">

      <div class="flex gap-3">
        <button type="button" onclick="saveConfig()" class="btn-primary">Save Configuration</button>
        <button type="button" onclick="loadConfig()" class="btn-secondary">Reload</button>
      </div>
    </form>
  </div>

  <!-- Full Test Suite -->
  <div class="card p-6 mt-4">
    <div class="flex items-center justify-between mb-1">
      <div>
        <h3 class="text-base font-semibold text-sand-900">Full Test Suite</h3>
        <p class="text-xs text-sand-500 mt-0.5">End-to-end connectivity check — runs all tests in sequence</p>
      </div>
      <button onclick="runFullSuite()" id="suite-run-btn" class="btn-primary flex items-center gap-2">
        <svg id="suite-spinner" class="w-4 h-4 hidden animate-spin" fill="none" viewBox="0 0 24 24">
          <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
          <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"/>
        </svg>
        Run All Tests
      </button>
    </div>
    <div id="suite-progress" class="hidden mt-4 mb-3">
      <div class="h-1.5 bg-sand-200 rounded-full overflow-hidden">
        <div id="suite-progress-bar" class="h-full bg-brand-500 rounded-full transition-all duration-300" style="width:0%"></div>
      </div>
    </div>
    <div id="suite-steps" class="mt-4 space-y-2">
      <!-- Steps rendered by JS -->
      <div class="text-xs text-sand-400 px-1">Press "Run All Tests" to begin. Some tests require the tunnel to be running (Start).</div>
    </div>
    <div id="suite-summary" class="hidden mt-4 p-4 rounded-xl border">
      <!-- Summary rendered by JS -->
    </div>
  </div>
</div>

<!-- Tab: Console -->
<div id="tab-console" class="tab-content p-6 max-w-3xl mx-auto hidden">
  <div class="card p-6 mb-4">
    <div class="flex items-center justify-between mb-5">
      <h2 class="text-base font-semibold text-sand-900">Control Panel</h2>
      <div class="flex gap-2">
        <button onclick="doAction('up')" class="btn-primary">Start</button>
        <button onclick="doAction('down')" class="btn-danger">Stop</button>
        <button onclick="doAction('status')" class="btn-secondary">Status</button>
      </div>
    </div>
    <div class="grid grid-cols-3 gap-3">
      <div class="bg-sand-100 rounded-xl p-4">
        <p class="text-xs text-sand-500 mb-2 font-medium">Relay Server</p>
        <div class="flex items-center gap-2">
          <span class="status-dot red" id="st-relay"></span>
          <span class="text-sm font-semibold text-sand-800" id="st-relay-text">offline</span>
        </div>
        <p class="text-xs text-sand-400 mt-1" id="st-relay-hint">Not reachable</p>
      </div>
      <div class="bg-sand-100 rounded-xl p-4">
        <p class="text-xs text-sand-500 mb-2 font-medium">Local Tunnel</p>
        <div class="flex items-center gap-2">
          <span class="status-dot red" id="st-tunnel"></span>
          <span class="text-sm font-semibold text-sand-800" id="st-tunnel-text">disconnected</span>
        </div>
        <p class="text-xs text-sand-400 mt-1" id="st-tunnel-hint">SSH not active</p>
      </div>
      <div class="bg-sand-100 rounded-xl p-4">
        <p class="text-xs text-sand-500 mb-2 font-medium">Role</p>
        <span class="text-sm font-semibold text-sand-800" id="st-role">—</span>
        <p class="text-xs text-sand-400 mt-1" id="st-role-hint">Not configured</p>
      </div>
    </div>
  </div>
  <div class="card overflow-hidden">
    <div class="flex items-center justify-between px-4 py-3 border-b border-sand-200">
      <span class="text-xs font-semibold text-sand-600 uppercase tracking-wide">Live Log</span>
      <button onclick="clearLog()" class="btn-ghost">Clear</button>
    </div>
    <div id="log-container" class="p-4 font-mono text-xs space-y-0.5" style="background:#1c1917;min-height:120px;">
      <div class="text-gray-500">Waiting for events...</div>
    </div>
  </div>

  <!-- Diagnostics -->
  <div class="card p-6 mt-4">
    <div class="flex items-center justify-between mb-4">
      <div>
        <h3 class="text-sm font-semibold text-sand-900">Diagnostics</h3>
        <p class="text-xs text-sand-500 mt-0.5">Run individual connectivity tests</p>
      </div>
      <button onclick="runAllDiagnostics()" class="btn-primary text-xs">Run All</button>
    </div>
    <div class="space-y-2" id="diag-list">
      <div class="flex items-center justify-between py-2 px-3 rounded-lg bg-sand-100">
        <div>
          <span class="text-sm font-medium text-sand-800">SSH Connection</span>
          <span class="text-xs text-sand-500 ml-2">Connect to VPS</span>
        </div>
        <div class="flex items-center gap-2">
          <span id="diag-ssh-dot" class="status-dot yellow"></span>
          <span id="diag-ssh-text" class="text-xs text-sand-600">not tested</span>
          <button onclick="diagSSH()" class="btn-ghost text-xs">Test</button>
        </div>
      </div>
      <div class="flex items-center justify-between py-2 px-3 rounded-lg bg-sand-100">
        <div>
          <span class="text-sm font-medium text-sand-800">Relay Server</span>
          <span class="text-xs text-sand-500 ml-2">HTTP reachability</span>
        </div>
        <div class="flex items-center gap-2">
          <span id="diag-relay-dot" class="status-dot yellow"></span>
          <span id="diag-relay-text" class="text-xs text-sand-600">not tested</span>
          <button onclick="diagRelay()" class="btn-ghost text-xs">Test</button>
        </div>
      </div>
      <div class="flex items-center justify-between py-2 px-3 rounded-lg bg-sand-100">
        <div>
          <span class="text-sm font-medium text-sand-800">Local Tunnel Port</span>
          <span id="diag-port-label" class="text-xs text-sand-500 ml-2">port 50000</span>
        </div>
        <div class="flex items-center gap-2">
          <span id="diag-port-dot" class="status-dot yellow"></span>
          <span id="diag-port-text" class="text-xs text-sand-600">not tested</span>
          <button onclick="diagPort()" class="btn-ghost text-xs">Test</button>
        </div>
      </div>
      <div class="flex items-center justify-between py-2 px-3 rounded-lg bg-sand-100">
        <div>
          <span class="text-sm font-medium text-sand-800">Room Handshake</span>
          <span class="text-xs text-sand-500 ml-2">Relay room registration</span>
        </div>
        <div class="flex items-center gap-2">
          <span id="diag-room-dot" class="status-dot yellow"></span>
          <span id="diag-room-text" class="text-xs text-sand-600">not tested</span>
          <button onclick="diagRoom()" class="btn-ghost text-xs">Test</button>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Tab: Connection -->
<div id="tab-connect" class="tab-content p-6 max-w-3xl mx-auto hidden">
  <div class="card p-6">
    <h2 class="text-base font-semibold text-sand-900 mb-1">Connection Commands</h2>
    <p class="text-sm text-sand-500 mb-5">Run these in a terminal on the A-side machine to connect Claude Code through the tunnel.</p>
    <div class="space-y-4">
      <div class="rounded-xl overflow-hidden border border-sand-300">
        <div class="flex items-center justify-between px-4 py-2.5 bg-sand-100 border-b border-sand-300">
          <span class="text-xs font-semibold text-sand-700">PowerShell (Windows)</span>
          <button onclick="copyBlock('ps')" id="copy-ps" class="btn-ghost text-xs">Copy</button>
        </div>
        <pre id="cmd-ps" class="log-line text-green-300 whitespace-pre-wrap p-4 text-xs" style="background:#1c1917;"></pre>
      </div>
      <div class="rounded-xl overflow-hidden border border-sand-300">
        <div class="flex items-center justify-between px-4 py-2.5 bg-sand-100 border-b border-sand-300">
          <span class="text-xs font-semibold text-sand-700">Bash / Zsh / WSL</span>
          <button onclick="copyBlock('bash')" id="copy-bash" class="btn-ghost text-xs">Copy</button>
        </div>
        <pre id="cmd-bash" class="log-line text-green-300 whitespace-pre-wrap p-4 text-xs" style="background:#1c1917;"></pre>
      </div>
    </div>
  </div>
</div>

<script>
// ── Role switching ──────────────────────────────────────────────
let currentRole = 'a';
let _userSetRole = false;
function setRole(role) {
  currentRole = role;
  document.getElementById('role-btn-a').className =
    'px-4 py-2 text-sm font-medium transition-colors' + (role==='a' ? ' bg-blue-50 text-blue-700' : ' text-sand-600 hover:bg-sand-100');
  document.getElementById('role-btn-c').className =
    'px-4 py-2 text-sm font-medium transition-colors border-l border-sand-300' + (role==='c' ? ' bg-brand-50 text-brand-600' : ' text-sand-600 hover:bg-sand-100');
  document.getElementById('section-gateway').style.display = role === 'c' ? '' : 'none';
  document.getElementById('section-claude').style.display = role === 'a' ? '' : 'none';
  const badge = document.getElementById('role-badge');
  badge.className = 'role-badge ' + role;
  badge.textContent = role === 'a' ? 'A-side' : 'C-side';
  badge.classList.remove('hidden');
}

// ── Tab switching ───────────────────────────────────────────────
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.add('hidden'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.remove('hidden');
  });
});

// ── SSE log stream ──────────────────────────────────────────────
let evtSource = null;
function startSSE() {
  if (evtSource) evtSource.close();
  evtSource = new EventSource('/api/logs');
  evtSource.onmessage = (e) => {
    const data = JSON.parse(e.data);
    appendLog(data.msg, data.level);
  };
  evtSource.onerror = () => { setTimeout(startSSE, 3000); };
}
startSSE();

function appendLog(msg, level) {
  const container = document.getElementById('log-container');
  const colors = { info:'text-gray-300', warn:'text-amber-400', error:'text-red-400', success:'text-green-400' };
  const div = document.createElement('div');
  div.className = 'log-line ' + (colors[level] || 'text-gray-300');
  const ts = new Date().toLocaleTimeString();
  div.textContent = `[${ts}] ${msg}`;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
  if (container.children.length > 300) container.removeChild(container.firstChild);
}

function clearLog() {
  document.getElementById('log-container').innerHTML = '<div class="text-gray-500">Log cleared.</div>';
}

// ── Actions ─────────────────────────────────────────────────────
async function doAction(action) {
  appendLog('Running ' + action + '...', 'info');
  try {
    const r = await fetch('/api/' + action, { method: 'POST' });
    const d = await r.json();
    if (action === 'up') {
      appendLog(d.ok ? 'Starting tunnel in background...' : ('Error: ' + (d.error||d.msg)), d.ok ? 'success' : 'error');
    } else {
      appendLog(JSON.stringify(d), d.ok ? 'success' : 'error');
    }
    setTimeout(refreshStatus, 2000);
  } catch(e) { appendLog('Error: ' + e.message, 'error'); }
}

// ── Environment check ───────────────────────────────────────────
async function runEnvCheck() {
  const el = document.getElementById('env-results');
  el.innerHTML = '<p class="text-sand-500 text-sm">Checking...</p>';
  try {
    const r = await fetch('/api/env-check');
    const d = await r.json();
    let html = '';
    d.checks.forEach(c => {
      const dot = c.status==='ok' ? 'green' : c.status==='warn' ? 'yellow' : 'red';
      const bg = c.status==='ok' ? '' : c.status==='warn' ? 'bg-amber-50' : 'bg-red-50';
      html += `<div class="flex items-center gap-3 px-3 py-2 rounded-lg ${bg}">
        <span class="status-dot ${dot}"></span>
        <span class="text-sm font-medium text-sand-800 w-28">${c.name}</span>
        <span class="text-sm text-sand-600 flex-1">${c.detail}</span>
      </div>`;
    });
    el.innerHTML = html || '<p class="text-sand-400 text-sm">No results</p>';
    const iEl = document.getElementById('env-install');
    const iList = document.getElementById('env-install-list');
    const failedDeps = d.checks.filter(c => c.status !== 'ok' && c.install_id);
    if (failedDeps.length > 0) {
      iEl.classList.remove('hidden');
      iList.innerHTML = failedDeps.map(c =>
        `<div class="flex items-center justify-between bg-sand-100 border border-sand-300 rounded-lg px-4 py-3">
          <div>
            <span class="text-sm font-medium text-sand-800">${c.name}</span>
            <code class="text-xs text-sand-500 ml-2 bg-sand-200 px-1.5 py-0.5 rounded">${c.install_cmd||''}</code>
          </div>
          <button onclick="installDep('${c.install_id}')" class="btn-primary text-xs">Install</button>
        </div>`
      ).join('');
    } else { iEl.classList.add('hidden'); }
    const wEl = document.getElementById('env-warnings');
    const wList = document.getElementById('env-warnings-list');
    if (d.warnings && d.warnings.length) {
      wEl.classList.remove('hidden');
      wList.innerHTML = d.warnings.map(w =>
        `<div class="flex items-start gap-2 text-sm text-amber-800"><span>⚠</span><span>${w}</span></div>`
      ).join('');
    } else { wEl.classList.add('hidden'); }
  } catch(e) { el.innerHTML = `<p class="text-red-500 text-sm">Error: ${e.message}</p>`; }
}

async function installDep(dep) {
  appendLog('Installing ' + dep + '...', 'info');
  try {
    const r = await fetch('/api/install/' + dep, { method: 'POST' });
    const d = await r.json();
    if (d.manual) {
      appendLog(d.name + ': manual install — ' + d.info, 'warn');
      alert('Manual install required:\n' + d.info);
    } else if (d.ok) {
      appendLog(dep + ' installed!', 'success');
      setTimeout(runEnvCheck, 1000);
    } else {
      appendLog('Install failed: ' + d.error, 'error');
    }
  } catch(e) { appendLog('Install error: ' + e.message, 'error'); }
}

async function testSSH() {
  appendLog('Testing SSH connection...', 'info');
  try {
    const r = await fetch('/api/test-ssh', { method: 'POST' });
    const d = await r.json();
    appendLog(d.ok ? 'SSH OK: ' + d.output : 'SSH failed: ' + d.error, d.ok ? 'success' : 'error');
  } catch(e) { appendLog('SSH test error: ' + e.message, 'error'); }
}

// ── Auto-detect C-side credentials ─────────────────────────────
async function autoDetectCredentials() {
  const statusEl = document.getElementById('auto-detect-status');
  statusEl.className = 'mb-3 text-xs px-3 py-2 rounded-lg bg-sand-100 text-sand-600';
  statusEl.textContent = 'Searching: env vars → ~/.claude/settings.json → project .claude/...';
  statusEl.classList.remove('hidden');
  try {
    const r = await fetch('/api/detect-credentials');
    const d = await r.json();
    if (d.base_url) {
      document.getElementById('cfg-upstream-url').value = d.base_url;
    }
    if (d.auth_token) {
      document.getElementById('cfg-upstream-token').value = d.auth_token;
    }
    if (d.base_url || d.auth_token) {
      const src = d.source ? ' (from ' + d.source + ')' : '';
      statusEl.className = 'mb-3 text-xs px-3 py-2 rounded-lg bg-green-50 text-green-700';
      statusEl.textContent = '✓ Credentials loaded' + src;
    } else {
      statusEl.className = 'mb-3 text-xs px-3 py-2 rounded-lg bg-amber-50 text-amber-700';
      statusEl.textContent = 'No credentials found in env, ~/.claude/settings.json, or project settings — enter manually';
    }
  } catch(e) {
    statusEl.className = 'mb-3 text-xs px-3 py-2 rounded-lg bg-red-50 text-red-600';
    statusEl.textContent = 'Error: ' + e.message;
  }
}

// ── Status refresh ──────────────────────────────────────────────
async function refreshStatus() {
  try {
    const r = await fetch('/api/info');
    const d = await r.json();
    const relayDot = document.getElementById('st-relay');
    const tunnelDot = document.getElementById('st-tunnel');
    relayDot.className = 'status-dot ' + (d.relay_ok ? 'green' : 'red');
    tunnelDot.className = 'status-dot ' + (d.tunnel_ok ? 'green' : 'red');
    document.getElementById('st-relay-text').textContent = d.relay_ok ? 'online' : 'offline';
    document.getElementById('st-relay-hint').textContent = d.relay_ok
      ? ('Running on server' + (d.relay_detail === 'via SSH' ? '' : ' · ' + d.relay_detail))
      : 'Not deployed — click Start';
    document.getElementById('st-tunnel-text').textContent = d.tunnel_ok ? 'connected' : 'disconnected';
    document.getElementById('st-tunnel-hint').textContent = d.tunnel_ok ? 'Port ' + (d.local_port||50000) + ' active' : 'SSH tunnel not active';
    document.getElementById('st-role').textContent = d.role ? (d.role === 'a' ? 'A-side' : 'C-side') : '—';
    document.getElementById('st-role-hint').textContent = d.role ? (d.role === 'a' ? 'Local dev machine' : 'Model server') : 'Not configured';
    const gDot = document.getElementById('global-dot');
    const gText = document.getElementById('global-status-text');
    if (d.relay_ok && d.tunnel_ok) {
      gDot.className = 'status-dot green pulse';
      gText.textContent = 'Connected';
    } else if (d.relay_ok) {
      gDot.className = 'status-dot yellow';
      gText.textContent = 'Relay only';
    } else {
      gDot.className = 'status-dot red';
      gText.textContent = 'Disconnected';
    }
    const port = d.local_port || 50000;
    const token = d.gw_token || 'change-me';
    const model = d.model || 'claude-sonnet-4-6';
    document.getElementById('cmd-ps').textContent =
      `$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:${port}"\n$env:ANTHROPIC_AUTH_TOKEN = "${token}"\nclaude --model ${model}`;
    document.getElementById('cmd-bash').textContent =
      `export ANTHROPIC_BASE_URL=http://127.0.0.1:${port}\nexport ANTHROPIC_AUTH_TOKEN=${token}\nclaude --model ${model}`;
    if (d.role && !_userSetRole) setRole(d.role);
  } catch(e) {}
}

// ── Config load/save ────────────────────────────────────────────
async function loadConfig() {
  try {
    const r = await fetch('/api/config');
    const d = await r.json();
    if (d.role && !_userSetRole) setRole(d.role);
    if (d.server) {
      document.getElementById('cfg-host').value = d.server.host || '';
      document.getElementById('cfg-port').value = d.server.port || 22;
      document.getElementById('cfg-user').value = d.server.user || 'root';
      document.getElementById('cfg-password').value = d.server.password || '';
      document.getElementById('cfg-key-file').value = d.server.key_file || '';
    }
    if (d.tunnel) {
      document.getElementById('cfg-relay-port').value = d.tunnel.relay_port || 8088;
      document.getElementById('cfg-forward-port').value = d.tunnel.forward_port || 19001;
    }
    if (d.room) {
      document.getElementById('cfg-room-name').value = d.room.name || 'default';
      document.getElementById('cfg-room-token').value = d.room.token || 'change-me';
    }
    if (d.gateway) {
      document.getElementById('cfg-gw-port').value = d.gateway.port || 8787;
      document.getElementById('cfg-gw-token').value = d.gateway.token || 'change-me';
      document.getElementById('cfg-gw-token-a').value = d.gateway.token || 'change-me';
      document.getElementById('cfg-upstream-url').value = d.gateway.upstream_base_url || 'https://api.anthropic.com';
      document.getElementById('cfg-upstream-token').value = d.gateway.upstream_auth_token || '';
    }
    if (d.claude) {
      document.getElementById('cfg-local-port').value = d.claude.local_port || 50000;
      document.getElementById('cfg-model').value = d.claude.model || 'claude-sonnet-4-6';
      document.getElementById('cfg-project-dir').value = d.claude.project_dir || '';
    }
  } catch(e) {}
}

async function saveConfig() {
  // Validate required fields
  const host = document.getElementById('cfg-host').value.trim();
  const user = document.getElementById('cfg-user').value.trim();
  const roomName = document.getElementById('cfg-room-name').value.trim();
  const roomToken = document.getElementById('cfg-room-token').value.trim();
  const missing = [];
  if (!host) missing.push('Host');
  if (!user) missing.push('SSH User');
  if (!roomName) missing.push('Room Name');
  if (!roomToken) missing.push('Room Token');
  if (currentRole === 'c') {
    if (!document.getElementById('cfg-gw-token').value.trim()) missing.push('Gateway Token');
    if (!document.getElementById('cfg-upstream-url').value.trim()) missing.push('Upstream API URL');
  }
  if (currentRole === 'a') {
    if (!document.getElementById('cfg-gw-token-a').value.trim()) missing.push('Gateway Token');
  }
  if (missing.length > 0) {
    appendLog('Missing required fields: ' + missing.join(', '), 'error');
    // Highlight empty required fields
    missing.forEach(name => {
      const map = {'Host':'cfg-host','SSH User':'cfg-user','Room Name':'cfg-room-name','Room Token':'cfg-room-token','Gateway Token':'cfg-gw-token','Upstream API URL':'cfg-upstream-url'};
      const el = document.getElementById(map[name]);
      if (el) { el.style.borderColor = '#dc2626'; setTimeout(() => el.style.borderColor = '', 3000); }
    });
    return;
  }

  const gwToken = currentRole === 'c'
    ? document.getElementById('cfg-gw-token').value
    : document.getElementById('cfg-gw-token-a').value;
  const cfg = {
    role: currentRole,
    server: {
      host: document.getElementById('cfg-host').value,
      port: parseInt(document.getElementById('cfg-port').value) || 22,
      user: document.getElementById('cfg-user').value,
      password: document.getElementById('cfg-password').value,
      key_file: document.getElementById('cfg-key-file').value || null,
    },
    tunnel: {
      relay_port: parseInt(document.getElementById('cfg-relay-port').value) || 8088,
      forward_port: parseInt(document.getElementById('cfg-forward-port').value) || 19001,
    },
    room: {
      name: document.getElementById('cfg-room-name').value,
      token: document.getElementById('cfg-room-token').value,
    },
    gateway: {
      port: parseInt(document.getElementById('cfg-gw-port').value) || 8787,
      token: gwToken,
      upstream_base_url: document.getElementById('cfg-upstream-url').value,
      upstream_auth_token: document.getElementById('cfg-upstream-token').value,
    },
    claude: {
      local_port: parseInt(document.getElementById('cfg-local-port').value) || 50000,
      model: document.getElementById('cfg-model').value,
      project_dir: document.getElementById('cfg-project-dir').value,
    },
  };
  try {
    const r = await fetch('/api/config', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(cfg) });
    const d = await r.json();
    appendLog(d.ok ? 'Config saved' : 'Save failed: ' + d.error, d.ok ? 'success' : 'error');
    if (d.ok) {
      const btn = document.querySelector('[onclick="saveConfig()"]');
      const orig = btn.textContent;
      btn.textContent = '✓ Saved';
      setTimeout(() => btn.textContent = orig, 1500);
    }
  } catch(e) { appendLog('Error: ' + e.message, 'error'); }
}

function copyBlock(type) {
  const el = document.getElementById('cmd-' + type);
  const btn = document.getElementById('copy-' + type);
  navigator.clipboard.writeText(el.textContent).then(() => {
    btn.textContent = '✓ Copied';
    setTimeout(() => btn.textContent = 'Copy', 1500);
  });
}

// ── Diagnostics ─────────────────────────────────────────────────
function setDiag(id, ok, text) {
  const dot = document.getElementById('diag-' + id + '-dot');
  const txt = document.getElementById('diag-' + id + '-text');
  if (dot) dot.className = 'status-dot ' + (ok === null ? 'yellow' : ok ? 'green' : 'red');
  if (txt) txt.textContent = text;
}

async function diagSSH() {
  setDiag('ssh', null, 'testing...');
  try {
    const r = await fetch('/api/test-ssh', { method: 'POST' });
    const d = await r.json();
    setDiag('ssh', d.ok, d.ok ? 'connected' : (d.error || 'failed'));
  } catch(e) { setDiag('ssh', false, e.message); }
}

async function diagRelay() {
  setDiag('relay', null, 'testing...');
  try {
    const r = await fetch('/api/test-relay', { method: 'POST' });
    const d = await r.json();
    setDiag('relay', d.ok, d.ok ? ('reachable (' + (d.method||'') + ')') : (d.error || 'unreachable'));
  } catch(e) { setDiag('relay', false, e.message); }
}

async function diagPort() {
  setDiag('port', null, 'testing...');
  const portEl = document.getElementById('cfg-local-port');
  const port = portEl ? parseInt(portEl.value) || 50000 : 50000;
  document.getElementById('diag-port-label').textContent = 'port ' + port;
  try {
    const r = await fetch('/api/test-port', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({port}) });
    const d = await r.json();
    if (d.state === 'listening') setDiag('port', true, 'active (tunnel running)');
    else if (d.state === 'available') setDiag('port', true, 'available (ready)');
    else setDiag('port', false, 'occupied by another process');
  } catch(e) { setDiag('port', false, e.message); }
}

async function diagRoom() {
  setDiag('room', null, 'testing...');
  try {
    const r = await fetch('/api/test-room', { method: 'POST' });
    const d = await r.json();
    if (d.ok) {
      const rm = d.data && d.data.room ? d.data.room : d.data;
      const peers = rm ? (rm.a_alive ? 1 : 0) + (rm.c_alive ? 1 : 0) : 0;
      setDiag('room', true, 'active' + (peers ? ' (' + peers + ' peers)' : ''));
    } else {
      setDiag('room', false, d.error || 'no response');
    }
  } catch(e) { setDiag('room', false, e.message); }
}

async function runAllDiagnostics() {
  await diagSSH();
  await diagRelay();
  await diagPort();
  await diagRoom();
}

// ── Directory picker ─────────────────────────────────────────────
let _dirCurrent = '';
async function openDirPicker() {
  const cur = document.getElementById('cfg-project-dir').value || '';
  document.getElementById('dir-picker').classList.remove('hidden');
  await browseTo(cur || '~');
}
function closeDirPicker() {
  document.getElementById('dir-picker').classList.add('hidden');
}
function selectCurrentDir() {
  document.getElementById('cfg-project-dir').value = _dirCurrent;
  closeDirPicker();
}
async function browseTo(path) {
  try {
    const r = await fetch('/api/browse-dir', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path}) });
    const d = await r.json();
    if (!d.ok) return;
    _dirCurrent = d.current;
    document.getElementById('dir-current').textContent = d.current;
    const el = document.getElementById('dir-entries');
    el.innerHTML = d.entries.map(e =>
      `<div onclick="browseTo('${e.path.replace(/'/g,"\\'")}') " class="flex items-center gap-3 px-5 py-2.5 hover:bg-sand-100 cursor-pointer border-b border-sand-100 last:border-0">
        <svg class="w-4 h-4 text-sand-400 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
          <path d="M2 6a2 2 0 012-2h5l2 2h5a2 2 0 012 2v6a2 2 0 01-2 2H4a2 2 0 01-2-2V6z"/>
        </svg>
        <span class="text-sm text-sand-800">${e.name}</span>
      </div>`
    ).join('') || '<p class="text-sand-400 text-sm px-5 py-4">Empty directory</p>';
  } catch(e) {}
}

// ── Inline section tests ────────────────────────────────────────
function setInline(id, ok, text) {
  const dot = document.getElementById('inline-' + id + '-dot');
  const txt = document.getElementById('inline-' + id + '-text');
  if (dot) dot.className = 'status-dot ' + (ok === null ? 'yellow pulse' : ok ? 'green' : 'red');
  if (txt) txt.textContent = text;
}

async function inlineTestSSH() {
  setInline('ssh', null, 'connecting...');
  const payload = {
    host: document.getElementById('cfg-host').value,
    port: parseInt(document.getElementById('cfg-port').value) || 22,
    user: document.getElementById('cfg-user').value,
    password: document.getElementById('cfg-password').value,
    key_file: document.getElementById('cfg-key-file').value,
  };
  try {
    const r = await fetch('/api/test-ssh', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    const d = await r.json();
    setInline('ssh', d.ok, d.ok ? 'connected' : (d.error || 'failed').slice(0, 40));
  } catch(e) { setInline('ssh', false, e.message.slice(0, 40)); }
}

async function inlineTestRelay() {
  setInline('relay', null, 'checking...');
  try {
    const r = await fetch('/api/test-relay', { method: 'POST' });
    const d = await r.json();
    setInline('relay', d.ok, d.ok ? 'reachable (' + (d.method||'') + ')' : (d.error || 'unreachable').slice(0, 40));
  } catch(e) { setInline('relay', false, e.message.slice(0, 40)); }
}

async function inlineTestApiKey() {
  setInline('apikey', null, 'verifying...');
  const token = document.getElementById('cfg-upstream-token').value;
  const baseUrl = document.getElementById('cfg-upstream-url').value || 'https://api.anthropic.com';
  try {
    const r = await fetch('/api/test-api-key', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ token, base_url: baseUrl })
    });
    const d = await r.json();
    setInline('apikey', d.ok, d.ok ? 'valid' : (d.error || 'invalid').slice(0, 40));
  } catch(e) { setInline('apikey', false, e.message.slice(0, 40)); }
}

async function inlineTestPort() {
  setInline('port', null, 'checking...');
  const port = parseInt(document.getElementById('cfg-local-port').value) || 50000;
  try {
    const r = await fetch('/api/test-port', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ port })
    });
    const d = await r.json();
    if (d.state === 'listening') {
      setInline('port', true, 'port ' + port + ' active (tunnel running)');
    } else if (d.state === 'available') {
      setInline('port', true, 'port ' + port + ' available (ready for tunnel)');
    } else {
      setInline('port', false, 'port ' + port + ' occupied by another process');
    }
  } catch(e) { setInline('port', false, e.message.slice(0, 40)); }
}

async function inlineTestGwPort() {
  setInline('gwport', null, 'checking...');
  const port = parseInt(document.getElementById('cfg-gw-port').value) || 8787;
  try {
    const r = await fetch('/api/test-port', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ port })
    });
    const d = await r.json();
    if (d.state === 'listening') {
      setInline('gwport', true, 'port ' + port + ' active (gateway running)');
    } else if (d.state === 'available') {
      setInline('gwport', true, 'port ' + port + ' available (ready)');
    } else {
      setInline('gwport', false, 'port ' + port + ' occupied by another process');
    }
  } catch(e) { setInline('gwport', false, e.message.slice(0, 40)); }
}

// ── Rooms viewer ─────────────────────────────────────────────────
async function loadRooms() {
  const panel = document.getElementById('rooms-panel');
  panel.innerHTML = '<div class="px-4 py-3 text-xs text-sand-400 bg-sand-50">Loading rooms...</div>';
  try {
    const r = await fetch('/api/relay-rooms', { method: 'POST' });
    const d = await r.json();
    if (!d.ok) {
      panel.innerHTML = `<div class="px-4 py-3 text-xs text-red-500 bg-red-50">${d.error || 'Failed to load rooms'}</div>`;
      return;
    }
    const rooms = d.rooms || [];
    if (rooms.length === 0) {
      panel.innerHTML = '<div class="px-4 py-3 text-xs text-sand-400 bg-sand-50">No active rooms found</div>';
      return;
    }
    panel.innerHTML = rooms.map(rm => `
      <div class="flex items-center justify-between px-4 py-2.5 border-b border-sand-100 last:border-0 bg-white hover:bg-sand-50">
        <div class="flex items-center gap-2">
          <span class="status-dot green"></span>
          <span class="text-sm font-medium text-sand-800">${rm.name}</span>
        </div>
        <div class="flex items-center gap-3 text-xs text-sand-500">
          <span>${rm.peers} peer${rm.peers !== 1 ? 's' : ''}</span>
          ${rm.last_seen ? '<span>last seen ' + rm.last_seen + '</span>' : ''}
        </div>
      </div>`).join('');
  } catch(e) {
    panel.innerHTML = `<div class="px-4 py-3 text-xs text-red-500 bg-red-50">Error: ${e.message}</div>`;
  }
}

// ── Full Test Suite ──────────────────────────────────────────────
const SUITE_STEPS = [
  { id: 'ssh',    label: 'SSH Connection',    desc: 'Connect to VPS with saved credentials' },
  { id: 'relay',  label: 'Relay Server',      desc: 'HTTP reachability on relay port' },
  { id: 'rooms',  label: 'Relay Rooms',       desc: 'List active rooms on relay' },
  { id: 'port',   label: 'Local Tunnel Port', desc: 'SSH tunnel forwarding local port' },
  { id: 'room',   label: 'Room Handshake',    desc: 'Register heartbeat in configured room' },
  { id: 'apikey', label: 'API Key',           desc: 'Validate upstream API key (C-side only)' },
];

let _suiteRunning = false;

function renderSuiteSteps(results) {
  const el = document.getElementById('suite-steps');
  el.innerHTML = SUITE_STEPS.map((s, i) => {
    const res = results[s.id];
    let dot = 'yellow', statusText = 'pending';
    if (res === 'running') { dot = 'yellow pulse'; statusText = 'running...'; }
    else if (res && res.ok === true) { dot = 'green'; statusText = res.detail || 'passed'; }
    else if (res && res.ok === false) { dot = 'red'; statusText = res.detail || 'failed'; }
    else if (res === 'skip') { dot = 'yellow'; statusText = 'skipped'; }
    return `<div class="flex items-center gap-3 px-3 py-2.5 rounded-lg suite-step ${res && res.ok === false ? 'bg-red-50' : res && res.ok === true ? 'bg-green-50' : 'bg-sand-50'}">
      <span class="text-xs font-medium text-sand-500 w-4 text-right">${i+1}</span>
      <span class="status-dot ${dot}"></span>
      <div class="flex-1">
        <span class="text-sm font-medium text-sand-800">${s.label}</span>
        <span class="text-xs text-sand-500 ml-2">${s.desc}</span>
      </div>
      <span class="text-xs ${res && res.ok === false ? 'text-red-600' : res && res.ok === true ? 'text-green-700' : 'text-sand-500'} max-w-[200px] text-right truncate">${statusText}</span>
    </div>`;
  }).join('');
}

async function runFullSuite() {
  if (_suiteRunning) return;
  _suiteRunning = true;
  const btn = document.getElementById('suite-run-btn');
  const spinner = document.getElementById('suite-spinner');
  const progress = document.getElementById('suite-progress');
  const bar = document.getElementById('suite-progress-bar');
  const summary = document.getElementById('suite-summary');
  btn.disabled = true;
  spinner.classList.remove('hidden');
  progress.classList.remove('hidden');
  summary.classList.add('hidden');
  bar.style.width = '0%';

  const results = {};
  const role = currentRole;
  const total = SUITE_STEPS.length;

  const setStep = (id, ok, detail) => {
    results[id] = { ok, detail };
    const idx = SUITE_STEPS.findIndex(s => s.id === id);
    bar.style.width = Math.round(((idx + 1) / total) * 100) + '%';
    renderSuiteSteps(results);
  };

  // 1. SSH
  results['ssh'] = 'running'; renderSuiteSteps(results);
  try {
    const r = await fetch('/api/test-ssh', { method: 'POST' });
    const d = await r.json();
    setStep('ssh', d.ok, d.ok ? 'connected' : (d.error || 'failed').slice(0, 50));
  } catch(e) { setStep('ssh', false, e.message.slice(0, 50)); }

  // 2. Relay
  results['relay'] = 'running'; renderSuiteSteps(results);
  try {
    const r = await fetch('/api/test-relay', { method: 'POST' });
    const d = await r.json();
    setStep('relay', d.ok, d.ok ? 'reachable via ' + (d.method||'?') : (d.error || 'unreachable').slice(0, 50));
  } catch(e) { setStep('relay', false, e.message.slice(0, 50)); }

  // 3. Rooms
  results['rooms'] = 'running'; renderSuiteSteps(results);
  try {
    const r = await fetch('/api/relay-rooms', { method: 'POST' });
    const d = await r.json();
    if (d.ok) {
      setStep('rooms', true, (d.rooms||[]).length + ' room(s) found');
    } else {
      setStep('rooms', false, (d.error || 'failed').slice(0, 50));
    }
  } catch(e) { setStep('rooms', false, e.message.slice(0, 50)); }

  // 4. Local port (A-side only)
  if (role === 'a') {
    results['port'] = 'running'; renderSuiteSteps(results);
    const port = parseInt(document.getElementById('cfg-local-port').value) || 50000;
    try {
      const r = await fetch('/api/test-port', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({port}) });
      const d = await r.json();
      if (d.state === 'listening') setStep('port', true, 'port ' + port + ' active (tunnel running)');
      else if (d.state === 'available') setStep('port', true, 'port ' + port + ' available (ready for tunnel)');
      else setStep('port', false, 'port ' + port + ' occupied by another process');
    } catch(e) { setStep('port', false, e.message.slice(0, 50)); }
  } else {
    results['port'] = 'skip'; renderSuiteSteps(results);
    setStep('port', true, 'skipped (C-side)');
  }

  // 5. Room handshake
  results['room'] = 'running'; renderSuiteSteps(results);
  try {
    const r = await fetch('/api/test-room', { method: 'POST' });
    const d = await r.json();
    if (d.ok) {
      const peers = d.data ? Object.keys(d.data).length : 0;
      setStep('room', true, 'room active' + (peers ? ', ' + peers + ' peer(s)' : ''));
    } else {
      setStep('room', false, (d.error || 'no response').slice(0, 50));
    }
  } catch(e) { setStep('room', false, e.message.slice(0, 50)); }

  // 6. API key (C-side only)
  if (role === 'c') {
    results['apikey'] = 'running'; renderSuiteSteps(results);
    const token = document.getElementById('cfg-upstream-token').value;
    const baseUrl = document.getElementById('cfg-upstream-url').value || 'https://api.anthropic.com';
    try {
      const r = await fetch('/api/test-api-key', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({token, base_url: baseUrl}) });
      const d = await r.json();
      setStep('apikey', d.ok, d.ok ? 'valid' : (d.error || 'invalid').slice(0, 50));
    } catch(e) { setStep('apikey', false, e.message.slice(0, 50)); }
  } else {
    setStep('apikey', true, 'skipped (A-side)');
  }

  bar.style.width = '100%';

  // Summary
  const passed = SUITE_STEPS.filter(s => results[s.id] && results[s.id].ok === true).length;
  const failed = SUITE_STEPS.filter(s => results[s.id] && results[s.id].ok === false).length;
  const allOk = failed === 0;
  summary.className = 'mt-4 p-4 rounded-xl border ' + (allOk ? 'bg-green-50 border-green-200' : 'bg-red-50 border-red-200');
  summary.innerHTML = `<div class="flex items-center gap-3">
    <span class="text-2xl">${allOk ? '✓' : '✗'}</span>
    <div>
      <p class="text-sm font-semibold ${allOk ? 'text-green-800' : 'text-red-800'}">${allOk ? 'All tests passed' : failed + ' test(s) failed'}</p>
      <p class="text-xs ${allOk ? 'text-green-600' : 'text-red-600'} mt-0.5">${passed} passed · ${failed} failed · ${total - passed - failed} skipped</p>
    </div>
  </div>`;
  summary.classList.remove('hidden');

  btn.disabled = false;
  spinner.classList.add('hidden');
  _suiteRunning = false;
}

// ── Init ────────────────────────────────────────────────────────
setRole('a');
runEnvCheck();
refreshStatus();
loadConfig();
setInterval(refreshStatus, 5000);
</script>
</body>
</html>'''


# ═══════════════════════════════════════════════════════════════════════════════
# Web Handler
# ═══════════════════════════════════════════════════════════════════════════════

class WebHandler(BaseHTTPRequestHandler):
    server_version = "claude-tunnel-web/0.2"

    def log_message(self, fmt, *args):
        log_emit(fmt % args, "info")

    def do_GET(self):
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            body = WEB_HTML.encode("utf-8")
            self._send(200, body, "text/html; charset=utf-8")
            return

        if path == "/api/info":
            self._handle_info()
            return

        if path == "/api/env-check":
            self._handle_env_check()
            return

        if path == "/api/config":
            self._handle_get_config()
            return

        if path == "/api/detect-credentials":
            self._handle_detect_credentials()
            return

        if path == "/api/logs":
            self._handle_sse()
            return

        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("content-length", "0") or "0")
        body = self.rfile.read(length) if length else b""

        if path == "/api/config":
            self._handle_save_config(body)
            return

        if path == "/api/up":
            self._handle_action("up")
            return

        if path == "/api/down":
            self._handle_action("down")
            return

        if path == "/api/status":
            self._handle_action("status")
            return

        if path.startswith("/api/install/"):
            dep = path.split("/api/install/", 1)[1]
            self._handle_install(dep, body)
            return

        if path == "/api/test-ssh":
            self._handle_test_ssh(body)
            return

        if path == "/api/test-relay":
            self._handle_test_relay()
            return

        if path == "/api/test-port":
            self._handle_test_port(body)
            return

        if path == "/api/test-room":
            self._handle_test_room()
            return

        if path == "/api/relay-rooms":
            self._handle_relay_rooms()
            return

        if path == "/api/test-api-key":
            self._handle_test_api_key(body)
            return

        if path == "/api/browse-dir":
            self._handle_browse_dir(body)
            return

        self._json(404, {"error": "not found"})

    def _handle_info(self):
        from claude_tunnel import load_config, ssh_exec
        cfg = load_config()
        relay_ok = False
        relay_detail = ""
        relay_port = cfg.get("tunnel", {}).get("relay_port", 8088)
        # Try direct HTTP to relay (works if relay is local or SSH -L forwards relay port)
        try:
            import http.client as _hc
            conn = _hc.HTTPConnection("127.0.0.1", relay_port, timeout=2)
            conn.request("GET", "/health")
            resp = conn.getresponse()
            resp.read(128)
            conn.close()
            relay_ok = resp.status < 500
            relay_detail = f"HTTP {resp.status}"
        except Exception:
            pass
        # Fallback: SSH exec curl (only if server configured, with short timeout)
        if not relay_ok and cfg.get("server", {}).get("host"):
            ssh_result = [False, ""]
            def _ssh_check():
                try:
                    rc, stdout, _ = ssh_exec(cfg, f"curl -sf --max-time 2 http://127.0.0.1:{relay_port}/health")
                    if rc == 0 and stdout.strip():
                        ssh_result[0] = True
                        ssh_result[1] = "via SSH"
                except Exception:
                    ssh_result[1] = "unreachable"
            t = threading.Thread(target=_ssh_check, daemon=True)
            t.start()
            t.join(timeout=5)
            if ssh_result[0]:
                relay_ok = True
                relay_detail = ssh_result[1]
            elif not t.is_alive():
                relay_detail = ssh_result[1] or "unreachable"

        tunnel_ok = False
        lp = cfg.get("claude", {}).get("local_port", 50000)
        try:
            s = socket.create_connection(("127.0.0.1", lp), timeout=1)
            s.close()
            tunnel_ok = True
        except Exception:
            pass
        self._json(200, {
            "role": cfg.get("role", ""),
            "relay_ok": relay_ok,
            "relay_detail": relay_detail,
            "tunnel_ok": tunnel_ok,
            "local_port": lp,
            "gw_token": cfg.get("gateway", {}).get("token", "change-me"),
            "model": cfg.get("claude", {}).get("model", "claude-sonnet-4-6"),
        })

    def _handle_env_check(self):
        from claude_tunnel import (
            _check_ssh_tool, _get_ssh_version, _check_scp_tool,
            _check_node, _check_npx, _check_claude_code,
            HAS_PARAMIKO, IS_WINDOWS
        )
        checks = []
        warnings = []

        ssh_ok, ssh_tool = _check_ssh_tool()
        checks.append({"name": "SSH Client", "status": "ok" if ssh_ok else "fail",
                       "detail": ssh_tool or "NOT FOUND"})

        ssh_ver_str, ssh_ver = _get_ssh_version()
        if ssh_ver != (0, 0):
            checks.append({"name": "SSH Version", "status": "ok",
                           "detail": ssh_ver_str.split(",")[0] if "," in ssh_ver_str else ssh_ver_str})

        scp_ok, scp_tool = _check_scp_tool()
        checks.append({"name": "File Transfer", "status": "ok" if scp_ok else "fail",
                       "detail": scp_tool or "NOT FOUND",
                       "install_id": "paramiko" if not scp_ok else None,
                       "install_cmd": "pip install paramiko"})

        checks.append({"name": "paramiko", "status": "ok" if HAS_PARAMIKO else "warn",
                       "detail": "installed" if HAS_PARAMIKO else "not installed",
                       "install_id": None if HAS_PARAMIKO else "paramiko",
                       "install_cmd": "pip install paramiko"})

        node_ok, node_ver = _check_node()
        checks.append({"name": "Node.js", "status": "ok" if node_ok else "fail",
                       "detail": node_ver or "NOT FOUND",
                       "install_id": "nodejs" if not node_ok else None,
                       "install_cmd": "https://nodejs.org/"})

        npx_ok = _check_npx()
        checks.append({"name": "npx", "status": "ok" if npx_ok else "warn",
                       "detail": "available" if npx_ok else "not found (comes with Node.js)"})

        claude_ok, _, claude_detail = _check_claude_code()
        checks.append({"name": "Claude Code", "status": "ok" if claude_ok else "fail",
                       "detail": claude_detail or "NOT FOUND",
                       "install_id": "claude-code" if not claude_ok else None,
                       "install_cmd": "npm install -g @anthropic-ai/claude-code"})

        if not ssh_ok:
            warnings.append("Install OpenSSH or PuTTY for SSH access")
        if not HAS_PARAMIKO and IS_WINDOWS:
            warnings.append("pip install paramiko — recommended for Windows password auth")
        if not node_ok:
            warnings.append("Install Node.js: https://nodejs.org/")
        if not claude_ok:
            warnings.append("npm install -g @anthropic-ai/claude-code")

        self._json(200, {"checks": checks, "warnings": warnings})

    def _handle_get_config(self):
        from claude_tunnel import load_config
        cfg = load_config()
        if cfg.get("server", {}).get("password"):
            cfg["server"]["password"] = "***"
        if cfg.get("gateway", {}).get("upstream_auth_token"):
            token = cfg["gateway"]["upstream_auth_token"]
            cfg["gateway"]["upstream_auth_token"] = token[:8] + "..." if len(token) > 8 else "***"
        self._json(200, cfg)

    def _handle_detect_credentials(self):
        """Detect API credentials from multiple sources."""
        result = {"base_url": "", "auth_token": "", "source": ""}

        # Source 1: Environment variables (highest priority)
        env_url = os.environ.get("ANTHROPIC_BASE_URL", "")
        env_token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "") or os.environ.get("ANTHROPIC_API_KEY", "")
        if env_url or env_token:
            result["base_url"] = env_url
            result["auth_token"] = env_token
            result["source"] = "environment variables"
            self._json(200, result)
            return

        # Source 2: ~/.claude/settings.json
        settings_path = Path.home() / ".claude" / "settings.json"
        try:
            with settings_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            env = data.get("env", {})
            url = env.get("ANTHROPIC_BASE_URL", "")
            token = env.get("ANTHROPIC_AUTH_TOKEN", "") or env.get("ANTHROPIC_API_KEY", "")
            if url or token:
                result["base_url"] = url
                result["auth_token"] = token
                result["source"] = "~/.claude/settings.json"
                self._json(200, result)
                return
        except Exception:
            pass

        # Source 3: Project-level .claude/settings.json (current directory)
        local_settings = Path.cwd() / ".claude" / "settings.json"
        try:
            with local_settings.open("r", encoding="utf-8") as f:
                data = json.load(f)
            env = data.get("env", {})
            url = env.get("ANTHROPIC_BASE_URL", "")
            token = env.get("ANTHROPIC_AUTH_TOKEN", "") or env.get("ANTHROPIC_API_KEY", "")
            if url or token:
                result["base_url"] = url
                result["auth_token"] = token
                result["source"] = ".claude/settings.json (project)"
                self._json(200, result)
                return
        except Exception:
            pass

        self._json(200, result)

    def _handle_save_config(self, body: bytes):
        from claude_tunnel import load_config, save_config
        try:
            new_data = json.loads(body.decode("utf-8"))
        except Exception as e:
            self._json(400, {"ok": False, "error": str(e)})
            return
        cfg = load_config() or {}
        if "role" in new_data:
            cfg["role"] = new_data["role"]
        if "server" in new_data:
            if "server" not in cfg:
                cfg["server"] = {}
            for k in ("host", "port", "user", "key_file"):
                if k in new_data["server"]:
                    cfg["server"][k] = new_data["server"][k]
            if new_data["server"].get("password") and new_data["server"]["password"] != "***":
                cfg["server"]["password"] = new_data["server"]["password"]
        if "tunnel" in new_data:
            if "tunnel" not in cfg:
                cfg["tunnel"] = {}
            for k in ("relay_port", "forward_port"):
                if k in new_data["tunnel"]:
                    cfg["tunnel"][k] = new_data["tunnel"][k]
        if "room" in new_data:
            if "room" not in cfg:
                cfg["room"] = {}
            cfg["room"].update(new_data["room"])
        if "gateway" in new_data:
            if "gateway" not in cfg:
                cfg["gateway"] = {}
            for k in ("port", "token", "upstream_base_url"):
                if k in new_data["gateway"]:
                    cfg["gateway"][k] = new_data["gateway"][k]
            if new_data["gateway"].get("upstream_auth_token"):
                cfg["gateway"]["upstream_auth_token"] = new_data["gateway"]["upstream_auth_token"]
        if "claude" in new_data:
            if "claude" not in cfg:
                cfg["claude"] = {}
            for k in ("local_port", "model", "project_dir"):
                if k in new_data["claude"]:
                    cfg["claude"][k] = new_data["claude"][k]
        save_config(cfg)
        log_emit("Configuration saved", "success")
        self._json(200, {"ok": True})

    def _handle_action(self, action: str):
        from claude_tunnel import load_config, cmd_up, cmd_down, relay_post
        cfg = load_config()
        if not cfg:
            self._json(400, {"ok": False, "error": "No config found"})
            return
        if action == "up":
            log_emit("Starting tunnel...", "info")
            def _run():
                try:
                    # Force tunnel-only mode from web (don't launch Claude in terminal)
                    if cfg.get("role") == "a":
                        if "claude" not in cfg:
                            cfg["claude"] = {}
                        cfg["claude"]["command"] = "tunnel-only"
                    rc = cmd_up(cfg)
                    log_emit(f"Up completed (rc={rc})", "success" if rc == 0 else "error")
                except Exception as e:
                    log_emit(f"Up failed: {e}", "error")
            threading.Thread(target=_run, daemon=True).start()
            self._json(200, {"ok": True, "msg": "starting..."})
        elif action == "down":
            from claude_tunnel import cmd_down
            rc = cmd_down(cfg)
            log_emit("Tunnel stopped", "success")
            self._json(200, {"ok": rc == 0})
        elif action == "status":
            data = relay_post(cfg, "heartbeat", cfg.get("role", ""))
            self._json(200, data or {"error": "relay unreachable"})

    def _handle_install(self, dep: str, body: bytes):
        """Install a dependency. Returns install command output."""
        INSTALL_COMMANDS = {
            "paramiko": {"cmd": [sys.executable, "-m", "pip", "install", "paramiko"],
                         "name": "paramiko"},
            "rich": {"cmd": [sys.executable, "-m", "pip", "install", "rich"],
                     "name": "rich"},
            "claude-code": {"cmd": ["npm", "install", "-g", "@anthropic-ai/claude-code"],
                           "name": "Claude Code"},
            "nodejs": {"info": "Download from https://nodejs.org/", "name": "Node.js"},
        }
        # Platform-specific adjustments
        system = platform.system()
        if system == "Windows":
            if dep == "claude-code":
                INSTALL_COMMANDS["claude-code"]["cmd"] = ["npm.cmd", "install", "-g", "@anthropic-ai/claude-code"]

        if dep not in INSTALL_COMMANDS:
            self._json(400, {"ok": False, "error": f"Unknown dependency: {dep}"})
            return

        info = INSTALL_COMMANDS[dep]
        if "info" in info:
            self._json(200, {"ok": False, "manual": True, "info": info["info"], "name": info["name"]})
            return

        log_emit(f"Installing {info['name']}...", "info")
        try:
            proc = subprocess.run(
                info["cmd"], capture_output=True, timeout=120
            )
            stdout = proc.stdout.decode(errors="replace")
            stderr = proc.stderr.decode(errors="replace")
            if proc.returncode == 0:
                log_emit(f"{info['name']} installed successfully", "success")
                self._json(200, {"ok": True, "output": stdout[-500:] if stdout else "Done"})
            else:
                log_emit(f"Install failed: {stderr[:200]}", "error")
                self._json(200, {"ok": False, "error": stderr[-500:]})
        except subprocess.TimeoutExpired:
            log_emit("Install timed out", "error")
            self._json(200, {"ok": False, "error": "Installation timed out (120s)"})
        except Exception as e:
            log_emit(f"Install error: {e}", "error")
            self._json(200, {"ok": False, "error": str(e)})

    def _handle_test_ssh(self, body: bytes = b""):
        """Test SSH connectivity — uses request body credentials if provided, else saved config."""
        from claude_tunnel import load_config, ssh_exec, HAS_PARAMIKO
        try:
            req = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            req = {}

        cfg = load_config() or {}
        if "server" not in cfg:
            cfg["server"] = {}
        # Override server section with form values if provided
        if req.get("host"):
            cfg["server"] = {
                "host": req["host"],
                "port": int(req.get("port", 22)),
                "user": req.get("user", "root"),
                "password": req.get("password", ""),
                "key_file": req.get("key_file", "") or None,
            }

        if not cfg.get("server", {}).get("host"):
            self._json(200, {"ok": False, "error": "No host configured"})
            return

        srv = cfg["server"]
        log_emit(f"Testing SSH to {srv['user']}@{srv['host']}:{srv.get('port', 22)}...", "info")

        # Use a thread with timeout to prevent hanging
        result = {"ok": False, "error": "Connection timed out (15s)"}
        def _do_test():
            try:
                # Prefer paramiko for password auth (no terminal needed)
                if HAS_PARAMIKO and srv.get("password"):
                    import paramiko
                    client = paramiko.SSHClient()
                    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    client.connect(
                        hostname=srv["host"],
                        port=int(srv.get("port", 22)),
                        username=srv.get("user", "root"),
                        password=srv["password"],
                        timeout=10,
                        look_for_keys=False,
                        allow_agent=False,
                        auth_timeout=10,
                        banner_timeout=10,
                    )
                    _, stdout_ch, _ = client.exec_command("echo ok && uname -a", timeout=10)
                    out = stdout_ch.read().decode(errors="replace").strip()
                    client.close()
                    result["ok"] = True
                    result["output"] = out
                    result.pop("error", None)
                elif HAS_PARAMIKO and srv.get("key_file"):
                    import paramiko
                    client = paramiko.SSHClient()
                    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    client.connect(
                        hostname=srv["host"],
                        port=int(srv.get("port", 22)),
                        username=srv.get("user", "root"),
                        key_filename=srv["key_file"],
                        timeout=10,
                        auth_timeout=10,
                        banner_timeout=10,
                    )
                    _, stdout_ch, _ = client.exec_command("echo ok && uname -a", timeout=10)
                    out = stdout_ch.read().decode(errors="replace").strip()
                    client.close()
                    result["ok"] = True
                    result["output"] = out
                    result.pop("error", None)
                else:
                    rc, stdout, stderr = ssh_exec(cfg, "echo ok && uname -a")
                    if rc == 0:
                        result["ok"] = True
                        result["output"] = stdout.strip()
                        result.pop("error", None)
                    else:
                        result["error"] = stderr.strip() or "Connection failed"
            except Exception as e:
                err_msg = str(e)
                if "Authentication" in err_msg or "auth" in err_msg.lower():
                    result["error"] = "Authentication failed — check password/key"
                elif "timed out" in err_msg or "timeout" in err_msg.lower():
                    result["error"] = "Connection timed out — check host/port"
                elif "refused" in err_msg.lower():
                    result["error"] = "Connection refused — check host/port"
                elif "resolve" in err_msg.lower() or "getaddrinfo" in err_msg.lower():
                    result["error"] = "Cannot resolve hostname"
                else:
                    result["error"] = err_msg[:100]

        t = threading.Thread(target=_do_test, daemon=True)
        t.start()
        t.join(timeout=15)

        if t.is_alive():
            result = {"ok": False, "error": "Connection timed out (15s) — check host/port/credentials"}

        log_emit(
            f"SSH {'OK' if result['ok'] else 'failed'}: {result.get('output', result.get('error', ''))}",
            "success" if result["ok"] else "error"
        )
        self._json(200, result)

    def _handle_test_relay(self):
        """Test relay HTTP reachability via SSH tunnel (direct HTTP to relay port)."""
        from claude_tunnel import load_config, ssh_exec
        cfg = load_config()
        if not cfg or not cfg.get("server", {}).get("host"):
            self._json(400, {"ok": False, "error": "No server configured"})
            return
        relay_port = cfg.get("tunnel", {}).get("relay_port", 8088)
        log_emit(f"Testing relay on server port {relay_port}...", "info")
        # Try direct HTTP first (if SSH -L tunnel is active)
        try:
            import http.client as _hc
            conn = _hc.HTTPConnection("127.0.0.1", relay_port, timeout=3)
            conn.request("GET", "/health")
            resp = conn.getresponse()
            body = resp.read(200).decode(errors="replace")
            conn.close()
            if resp.status < 500:
                log_emit(f"Relay reachable via local tunnel: HTTP {resp.status}", "success")
                self._json(200, {"ok": True, "method": "local", "status": resp.status, "body": body})
                return
        except Exception:
            pass
        # Fallback: SSH exec curl on server
        rc, stdout, stderr = ssh_exec(cfg, f"curl -sf --max-time 3 http://127.0.0.1:{relay_port}/health || echo FAIL")
        if rc == 0 and "FAIL" not in stdout:
            log_emit(f"Relay reachable via SSH: {stdout.strip()}", "success")
            self._json(200, {"ok": True, "method": "ssh", "body": stdout.strip()})
        else:
            log_emit(f"Relay not reachable on port {relay_port}", "error")
            self._json(200, {"ok": False, "error": f"Relay not responding on port {relay_port}. Is it deployed?"})

    def _handle_test_port(self, body: bytes):
        """Test a local TCP port — checks if it's listening (tunnel active) or available (for binding)."""
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            data = {}
        port = int(data.get("port", 50000))
        host = data.get("host", "127.0.0.1")
        log_emit(f"Testing port {host}:{port}...", "info")

        # Check if something is listening (tunnel active)
        listening = False
        try:
            s = socket.create_connection((host, port), timeout=2)
            s.close()
            listening = True
        except Exception:
            pass

        if listening:
            log_emit(f"Port {port} is active (listening)", "success")
            self._json(200, {"ok": True, "port": port, "host": host, "state": "listening"})
            return

        # Port not listening — check if it's available for binding (not occupied by another process)
        available = False
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
            s.close()
            available = True
        except OSError:
            available = False

        if available:
            log_emit(f"Port {port} is available (not in use, ready for tunnel)", "info")
            self._json(200, {"ok": True, "port": port, "host": host, "state": "available"})
        else:
            log_emit(f"Port {port} is occupied by another process", "warn")
            self._json(200, {"ok": False, "port": port, "host": host, "state": "occupied", "error": f"Port {port} is already in use by another process"})

    def _handle_test_room(self):
        """Test room handshake on relay — tries direct HTTP first, then SSH."""
        from claude_tunnel import load_config, ssh_exec
        import http.client as _hc
        cfg = load_config()
        if not cfg or not cfg.get("server", {}).get("host"):
            self._json(200, {"ok": False, "error": "No server configured"})
            return
        room_name = cfg.get("room", {}).get("name", "default")
        room_token = cfg.get("room", {}).get("token", "change-me")
        relay_port = cfg.get("tunnel", {}).get("relay_port", 8088)
        role = cfg.get("role", "a")
        log_emit(f"Testing room handshake for room '{room_name}'...", "info")

        body_data = json.dumps({
            "room": room_name,
            "token": room_token,
            "role": role,
            "action": "heartbeat",
        })

        # Try direct HTTP to relay (works if SSH -L tunnel or local relay is active)
        try:
            conn = _hc.HTTPConnection("127.0.0.1", relay_port, timeout=5)
            conn.request("POST", "/", body=body_data, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            resp_body = resp.read(2048).decode(errors="replace")
            conn.close()
            if resp.status == 200:
                data = json.loads(resp_body)
                log_emit(f"Room '{room_name}' handshake OK (direct)", "success")
                self._json(200, {"ok": True, "room": room_name, "data": data})
                return
            elif resp.status == 401:
                log_emit(f"Room handshake: bad room/token", "error")
                self._json(200, {"ok": False, "room": room_name, "error": "Authentication failed — check room name and token"})
                return
        except Exception:
            pass

        # Fallback: SSH curl (in thread with timeout)
        escaped = body_data.replace("'", "'\\''")
        ssh_cmd = f"curl -s --max-time 5 -X POST http://127.0.0.1:{relay_port}/ -H 'Content-Type: application/json' -d '{escaped}'"
        ssh_result = [None]
        def _ssh_room():
            try:
                rc, stdout, _ = ssh_exec(cfg, ssh_cmd)
                if rc == 0 and stdout.strip():
                    data = json.loads(stdout)
                    ssh_result[0] = data
            except Exception:
                pass
        t = threading.Thread(target=_ssh_room, daemon=True)
        t.start()
        t.join(timeout=10)

        if ssh_result[0] is not None:
            data = ssh_result[0]
            if data.get("ok"):
                log_emit(f"Room '{room_name}' handshake OK (via SSH)", "success")
                self._json(200, {"ok": True, "room": room_name, "data": data})
                return
            elif data.get("error"):
                log_emit(f"Room handshake: {data['error']}", "error")
                self._json(200, {"ok": False, "room": room_name, "error": data["error"]})
                return

        if t.is_alive():
            log_emit("Room handshake: SSH timeout", "error")
            self._json(200, {"ok": False, "room": room_name, "error": "SSH timeout — try again"})
        else:
            log_emit("Room handshake failed — relay not responding", "error")
            self._json(200, {"ok": False, "room": room_name, "error": "No response — click Start to deploy relay"})

    def _handle_relay_rooms(self):
        """List active rooms on the relay server."""
        from claude_tunnel import load_config, ssh_exec
        import http.client as _hc
        cfg = load_config()
        if not cfg or not cfg.get("server", {}).get("host"):
            self._json(200, {"ok": False, "error": "No server configured"})
            return
        relay_port = cfg.get("tunnel", {}).get("relay_port", 8088)

        # Method 1: Try GET /rooms via direct HTTP (local tunnel)
        try:
            conn = _hc.HTTPConnection("127.0.0.1", relay_port, timeout=3)
            conn.request("GET", "/rooms")
            resp = conn.getresponse()
            body = resp.read(4096).decode(errors="replace")
            conn.close()
            if resp.status == 200:
                data = json.loads(body)
                if "error" not in data:
                    rooms = self._parse_rooms(data)
                    self._json(200, {"ok": True, "rooms": rooms, "method": "local"})
                    return
        except Exception:
            pass

        # Method 2+3 via SSH (in thread to avoid blocking)
        room_name = cfg.get("room", {}).get("name", "default")
        room_token = cfg.get("room", {}).get("token", "change-me")
        role = cfg.get("role", "c")
        result = [None]

        def _ssh_fetch():
            # Combined: try GET /rooms first, if it fails try heartbeat POST — single SSH connection
            hb = json.dumps({"room": room_name, "token": room_token, "role": role, "action": "heartbeat"})
            escaped = hb.replace("'", "'\\''")
            # Single command: try /rooms, if 404 then do heartbeat
            combined_cmd = (
                f"ROOMS=$(curl -s --max-time 4 http://127.0.0.1:{relay_port}/rooms) && "
                f"echo \"ROOMS:$ROOMS\" || "
                f"echo \"ROOMS:FAIL\"; "
                f"HB=$(curl -s --max-time 4 -X POST http://127.0.0.1:{relay_port}/ "
                f"-H 'Content-Type: application/json' -d '{escaped}') && "
                f"echo \"HB:$HB\""
            )
            rc, stdout, _ = ssh_exec(cfg, combined_cmd)
            if rc != 0 and not stdout.strip():
                return

            lines = stdout.strip()
            # Parse ROOMS response
            for line in lines.split("\n"):
                if line.startswith("ROOMS:") and line != "ROOMS:FAIL":
                    raw = line[6:]
                    try:
                        data = json.loads(raw)
                        if isinstance(data, dict) and "error" not in data:
                            result[0] = {"ok": True, "rooms": self._parse_rooms(data), "method": "ssh"}
                            return
                    except (json.JSONDecodeError, ValueError):
                        pass
                elif line.startswith("HB:"):
                    raw = line[3:]
                    try:
                        data = json.loads(raw)
                        if data.get("ok") and data.get("room"):
                            rm = data["room"]
                            peers = (1 if rm.get("a_alive") else 0) + (1 if rm.get("c_alive") else 0)
                            age = rm.get("age", 0)
                            age_str = f"{int(age)}s ago" if age < 60 else f"{int(age/60)}m ago"
                            result[0] = {"ok": True, "rooms": [{"name": rm.get("name", room_name), "peers": peers, "last_seen": age_str}], "method": "ssh-heartbeat"}
                            return
                        elif data.get("error"):
                            result[0] = {"ok": False, "error": f"Relay: {data['error']}"}
                            return
                    except (json.JSONDecodeError, ValueError):
                        pass

        t = threading.Thread(target=_ssh_fetch, daemon=True)
        t.start()
        t.join(timeout=12)

        if result[0] is not None:
            self._json(200, result[0])
        elif t.is_alive():
            self._json(200, {"ok": False, "error": "SSH timeout — try again"})
        else:
            self._json(200, {"ok": False, "error": f"Relay not responding on port {relay_port}. Click Start to deploy."})

    def _parse_rooms(self, data) -> list:
        """Normalize relay /rooms response into [{name, peers, last_seen}]."""
        rooms = []
        if isinstance(data, dict):
            for name, info in data.items():
                if isinstance(info, dict):
                    # Relay snapshot format: {a_alive, c_alive, switch_open, age}
                    peers = sum([
                        1 if info.get("a_alive") else 0,
                        1 if info.get("c_alive") else 0,
                    ])
                    age = info.get("age", 0)
                    age_str = f"{int(age)}s ago" if age < 60 else f"{int(age/60)}m ago"
                    rooms.append({"name": name, "peers": peers, "last_seen": age_str})
                else:
                    rooms.append({"name": name, "peers": 0, "last_seen": ""})
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    rooms.append({"name": item.get("name", "?"), "peers": item.get("peers", 0), "last_seen": str(item.get("last_seen", ""))[:19]})
        return rooms

    def _handle_test_api_key(self, body: bytes):
        """Verify an upstream API key by making a lightweight request."""
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            data = {}
        token = data.get("token", "")
        base_url = data.get("base_url", "https://api.anthropic.com").rstrip("/")
        if not token:
            # Try loading from saved config
            from claude_tunnel import load_config
            cfg = load_config()
            token = cfg.get("gateway", {}).get("upstream_auth_token", "") if cfg else ""
        if not token:
            self._json(200, {"ok": False, "error": "No API key provided"})
            return
        log_emit(f"Testing API key against {base_url}...", "info")
        try:
            parsed = urlparse(base_url)
            host = parsed.hostname
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            use_ssl = parsed.scheme == "https"
            if use_ssl:
                import http.client as _hc
                conn = _hc.HTTPSConnection(host, port, timeout=8)
            else:
                import http.client as _hc
                conn = _hc.HTTPConnection(host, port, timeout=8)
            headers = {
                "x-api-key": token,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            # Use /v1/models as a lightweight probe (no billing)
            conn.request("GET", "/v1/models", headers=headers)
            resp = conn.getresponse()
            resp.read(512)
            conn.close()
            if resp.status in (200, 206):
                log_emit("API key valid", "success")
                self._json(200, {"ok": True, "status": resp.status})
            elif resp.status == 401:
                log_emit("API key invalid (401 Unauthorized)", "error")
                self._json(200, {"ok": False, "error": "Invalid API key (401)"})
            elif resp.status == 403:
                log_emit("API key forbidden (403)", "error")
                self._json(200, {"ok": False, "error": "Forbidden (403) — check key permissions"})
            else:
                log_emit(f"API key test: HTTP {resp.status}", "warn")
                self._json(200, {"ok": resp.status < 500, "status": resp.status, "error": f"HTTP {resp.status}"})
        except Exception as e:
            log_emit(f"API key test error: {e}", "error")
            self._json(200, {"ok": False, "error": str(e)})

    def _handle_browse_dir(self, body: bytes):
        """List directories for project dir picker."""
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            data = {}
        base = data.get("path", str(Path.home()))
        try:
            p = Path(base).expanduser().resolve()
            if not p.exists():
                p = Path.home()
            entries = []
            # Parent dir
            if p != p.parent:
                entries.append({"name": "..", "path": str(p.parent), "type": "dir"})
            for child in sorted(p.iterdir()):
                if child.is_dir() and not child.name.startswith("."):
                    entries.append({"name": child.name, "path": str(child), "type": "dir"})
            self._json(200, {"ok": True, "current": str(p), "entries": entries[:100]})
        except Exception as e:
            self._json(200, {"ok": False, "error": str(e), "entries": []})

    def _handle_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        # Send history first
        for entry in _log_history[-20:]:
            self._sse_write(entry)
        # Stream new events
        try:
            while True:
                try:
                    entry = _log_queue.get(timeout=30)
                    self._sse_write(entry)
                except queue.Empty:
                    # Send keepalive
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _sse_write(self, entry: dict):
        data = json.dumps({"msg": entry["msg"], "level": entry["level"]})
        self.wfile.write(f"data: {data}\n\n".encode())
        self.wfile.flush()

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json")

    def _send(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def cmd_web(port: int = 8765) -> int:
    """Start the web management panel."""
    from ct_ui import ui
    server = None
    actual_port = port
    for p in range(port, port + 10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), WebHandler)
            actual_port = p
            break
        except OSError:
            continue
    if server is None:
        ui.error(f"Cannot bind to ports {port}-{port+9}, all in use")
        return 1
    ui.success(f"Web UI starting on http://127.0.0.1:{actual_port}")
    ui.info("Open this URL in your browser")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


