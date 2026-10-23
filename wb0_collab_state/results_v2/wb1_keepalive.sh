#!/bin/bash
# WB1 常驻会话 keepalive 心跳守护
# 依据：WB0 运维指令 1792769600-2667d3（WB0-INSTR-20261023-KEEPALIVE-RESTART-WB1）
# 行为：每 1200s（20 分钟）向 Hub 发一次 GET /api/brief（带 sharecode），维持会话活跃，防沙箱空闲回收。
# 约束：不写台账、不执行任何研究实验、不伪造心跳（每次均为真实 HTTP 请求，状态码与字节数落日志）。

HUB="https://aa27d73086593e6b6.app.workbuddy.host"
CODE="V6GYQIUI7Z"
LOG="/workspace/semantickv-reorg/hub_state/keepalive.log"
COOKIE="/tmp/wb1_keepalive_cookie.txt"
INTERVAL=1200

echo "[$(date '+%Y-%m-%d %H:%M:%S')] keepalive 守护启动 pid=$$ interval=${INTERVAL}s" >> "$LOG"

while true; do
  TS=$(date '+%Y-%m-%d %H:%M:%S')
  OUT=$(curl -s -m 30 -c "$COOKIE" -b "$COOKIE" \
        "${HUB}/?sharecode=${CODE}" -o /dev/null -w "%{http_code}" 2>/dev/null)
  RES=$(curl -s -m 30 -b "$COOKIE" -c "$COOKIE" \
        "${HUB}/api/brief" -o /tmp/wb1_keepalive_brief.json -w "%{http_code}/%{size_download}" 2>/dev/null)
  echo "[${TS}] sharecode=${OUT} brief=${RES}" >> "$LOG"
  sleep "$INTERVAL"
done
