#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

# ============================================================
#  LxOcConnector 一键启动脚本 (Linux / macOS)
# ============================================================

if [ ! -f .env ]; then
    echo "[!] 未找到 .env，正在从 .env.example 创建..."
    cp .env.example .env
    echo "[!] 请编辑 .env 填写蓝信凭证后重新运行本脚本。"
    exit 1
fi

export $(grep -v '^#' .env | xargs 2>/dev/null || true)

OC_PORT="${OPENCODE_BASE_URL##*:}"
OC_PORT="${OC_PORT:-4096}"

# 启动 opencode serve（如果端口没被占用）
if ! lsof -i :"$OC_PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
    echo "[*] 启动 opencode serve (端口 $OC_PORT)..."
    opencode serve --port "$OC_PORT" --hostname 127.0.0.1 >/dev/null 2>&1 &
    sleep 5
else
    echo "[*] opencode serve 已在端口 $OC_PORT 运行，跳过启动"
fi

echo "[*] 启动蓝信桥接服务..."
python3 -u main.py
