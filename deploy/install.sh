#!/usr/bin/env bash
# 倪师 AI · 一键部署脚本（Linux 服务器）
#
# 用法：
#   sudo bash deploy/install.sh                    # 安装到 /opt/nishi-ai
#   sudo bash deploy/install.sh --dir /srv/nishi   # 自定义目录
#   sudo bash deploy/install.sh --no-service       # 只部署文件，不装 systemd
#
# 做的事：建用户 → 拷文件 → 建 .env → 建数据目录 → 装 systemd → 自检
#
# 不做的事（需你手动完成）：
#   - 填写 .env 里的 NHX_LLM_API_KEY
#   - 配置 Nginx 与 HTTPS（见 deploy/nginx.conf）

set -euo pipefail

APP_DIR="/opt/nishi-ai"
APP_USER="nishi"
INSTALL_SERVICE=1

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) APP_DIR="$2"; shift 2 ;;
    --no-service) INSTALL_SERVICE=0; shift ;;
    -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：$1"; exit 2 ;;
  esac
done

SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "❌ 需要 root 权限运行（sudo）"
  exit 1
fi

echo "▸ 源目录：$SRC_DIR"
echo "▸ 目标目录：$APP_DIR"
echo

# ── 1. 检查 Python ──────────────────────────────────────────────────────────
PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  echo "❌ 未找到 python3，请先安装：apt install python3  /  yum install python3"
  exit 1
fi
PYVER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "▸ Python：$PY ($PYVER)"
if "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)'; then
  echo "  ✅ 版本满足（需 ≥3.8）"
else
  echo "  ❌ 需要 Python 3.8 及以上"
  exit 1
fi
echo

# ── 2. 建用户 ───────────────────────────────────────────────────────────────
if id "$APP_USER" >/dev/null 2>&1; then
  echo "▸ 用户 $APP_USER 已存在，跳过"
else
  useradd -r -m -s /usr/sbin/nologin "$APP_USER" 2>/dev/null \
    || useradd -r -m -s /sbin/nologin "$APP_USER"
  echo "▸ 已创建系统用户 $APP_USER"
fi
echo

# ── 3. 拷文件 ───────────────────────────────────────────────────────────────
mkdir -p "$APP_DIR"
echo "▸ 复制文件…"
for item in src knowledge media docs deploy scripts AGENT.md README.md \
            .env.example Dockerfile docker-compose.yml; do
  if [ -e "$SRC_DIR/$item" ]; then
    cp -R "$SRC_DIR/$item" "$APP_DIR/" 2>/dev/null || true
  fi
done

mkdir -p "$APP_DIR/data"
chmod 700 "$APP_DIR/data"
echo "  ✅ 文件已就位"
echo

# ── 4. .env ─────────────────────────────────────────────────────────────────
if [ -f "$APP_DIR/.env" ]; then
  echo "▸ .env 已存在，保留不动"
else
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  # 生成随机会话密钥无关，但把默认改成需登录更安全
  sed -i 's/^NHX_AUTH_MODE=.*/NHX_AUTH_MODE=protected/' "$APP_DIR/.env" 2>/dev/null || true
  chmod 600 "$APP_DIR/.env"
  echo "▸ 已生成 $APP_DIR/.env（权限 600）"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
echo

# ── 5. systemd ──────────────────────────────────────────────────────────────
if [ "$INSTALL_SERVICE" -eq 1 ] && command -v systemctl >/dev/null 2>&1; then
  sed "s#/opt/nishi-ai#$APP_DIR#g" "$APP_DIR/deploy/nishi-ai.service" \
    > /etc/systemd/system/nishi-ai.service
  systemctl daemon-reload
  echo "▸ 已安装 systemd 单元 nishi-ai.service"
  echo "  启用： systemctl enable --now nishi-ai"
else
  echo "▸ 跳过 systemd 安装"
fi
echo

# ── 6. 自检 ─────────────────────────────────────────────────────────────────
echo "▸ 自检…"
if (cd "$APP_DIR" && "$PY" src/nhx_app/server.py --check); then
  echo "  ✅ 自检通过"
else
  echo "  ⚠️  自检未通过（通常是还没填 NHX_LLM_API_KEY，属正常）"
fi

cat <<EOF

════════════════════════════════════════════════════════════════
部署完成

下一步（必做）：
  1. 填写 API Key：
       sudo nano $APP_DIR/.env
       # 设置 NHX_LLM_API_KEY=sk-xxxx

  2. 创建管理账号（若 NHX_AUTH_MODE 为 protected / strict）：
       sudo -u $APP_USER python3 $APP_DIR/src/nhx_auth/auth.py adduser admin

  3. 启动服务：
       sudo systemctl enable --now nishi-ai
       sudo systemctl status nishi-ai

  4. 本机验证：
       curl -s http://127.0.0.1:8790/api/health

  5. 配置 Nginx + HTTPS（见 $APP_DIR/deploy/nginx.conf）
       ⚠️ 务必保留 proxy_buffering off，否则流式输出会被缓冲

查看日志：
  journalctl -u nishi-ai -f
════════════════════════════════════════════════════════════════
EOF
