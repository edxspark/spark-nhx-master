#!/usr/bin/env bash
# 倪师 AI · 全量验收脚本
#
# 用法：bash scripts/verify.sh
# 退出码：0 = 全部通过；非 0 = 有失败项
#
# 这是「先证据后结论」（AGENT.md §7）的落地：任何声称"做完了"的结论，
# 都必须能由本脚本复现。

set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
REPO_ROOT="$(pwd)"

PASS=0
FAIL=0
FAILED_SECTIONS=()

hr() { printf '%s\n' "────────────────────────────────────────────────────────────"; }

run_section() {
  local title="$1"; shift
  hr
  printf '▶ %s\n' "$title"
  hr
  if "$@"; then
    printf '✅ 通过：%s\n\n' "$title"
    PASS=$((PASS + 1))
  else
    printf '❌ 失败：%s\n\n' "$title"
    FAIL=$((FAIL + 1))
    FAILED_SECTIONS+=("$title")
  fi
}

triage_tests()   { python3 src/nhx_triage/test_triage.py; }
validate_tests() { python3 src/nhx_schema/test_validate.py; }
validate_data()  { python3 src/nhx_schema/validate.py; }
media_tests()    { python3 src/nhx_media/test_store.py; }
auth_tests()     { python3 src/nhx_auth/test_auth.py; }
app_tests()      { python3 src/nhx_app/test_app.py; }
corpus_tests()   { python3 src/nhx_corpus/test_import.py; }
clinic_tests()   { python3 src/nhx_clinic/test_review.py; }
env_tests()      { python3 src/nhx_app/test_env.py; }

media_api_smoke() {
  # 起一个临时服务，跑通关键接口后关掉
  # 端口避开主应用(8790)与假 LLM(8799)
  local port=8787
  python3 src/nhx_media/server.py --port "$port" >/tmp/nhx-verify-media.log 2>&1 &
  local pid=$!
  local ok=0
  local up=0
  for _ in $(seq 1 40); do
    if curl -sf "http://127.0.0.1:$port/api/meta" >/dev/null 2>&1; then up=1; break; fi
    sleep 0.2
  done
  if [ "$up" -eq 0 ]; then
    printf '  ❌ 媒体服务未启动（端口 %s 可能被占用），日志：\n' "$port"
    sed 's/^/     /' /tmp/nhx-verify-media.log | head -8
    kill "$pid" 2>/dev/null
    return 1
  fi

  for ep in meta series stats validate items; do
    if curl -sf "http://127.0.0.1:$port/api/$ep" >/dev/null 2>&1; then
      printf '  ✅ GET /api/%s\n' "$ep"
    else
      printf '  ❌ GET /api/%s 失败\n' "$ep"
      ok=1
    fi
  done

  # 上线闸门：不完整条目必须被拒
  local gate
  gate=$(curl -s -X POST "http://127.0.0.1:$port/api/items/renji-zhenjiu-050/status" \
         -H 'Content-Type: application/json' -d '{"status":"published"}' 2>/dev/null)
  case "$gate" in
    *'"ok": false'*) printf '  ✅ 上线闸门生效（不完整条目不允上线）\n' ;;
    *) printf '  ❌ 上线闸门未生效：%s\n' "$gate"; ok=1 ;;
  esac

  # 目录穿越防护
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/%2e%2e/AGENT.md")
  if [ "$code" = "404" ] || [ "$code" = "403" ]; then
    printf '  ✅ 目录穿越已阻断（HTTP %s）\n' "$code"
  else
    printf '  ❌ 目录穿越未被阻断（HTTP %s）\n' "$code"
    ok=1
  fi

  kill "$pid" 2>/dev/null
  wait "$pid" 2>/dev/null
  return $ok
}

check_assets() {
  local ok=0
  for f in AGENT.md AGENTS.md README.md \
           src/nhx_triage/triage.py src/nhx_triage/red_flags.json \
           src/nhx_media/store.py src/nhx_media/server.py src/nhx_auth/auth.py \
           src/nhx_media/static/index.html src/nhx_media/static/app.js \
           src/nhx_clinic/review.py \
           src/nhx_app/server.py src/nhx_app/guard.py src/nhx_app/pipeline.py \
           src/nhx_app/env.py src/nhx_app/test_env.py \
           src/nhx_app/static/index.html src/nhx_app/static/app.js \
           deploy/nishi-ai.service deploy/nginx.conf deploy/install.sh \
           Dockerfile docker-compose.yml .env.example \
           src/nhx_corpus/import_classics.py \
           knowledge/01-人纪-中医经典/04-伤寒论/原文/伤寒论-条文.json \
           knowledge/01-人纪-中医经典/05-金匮要略/原文/金匮要略-条文.json \
           .dsh/skills/nhx-triage/SKILL.md .dsh/skills/nhx-consult/SKILL.md \
           .dsh/skills/nhx-bianzheng/SKILL.md .dsh/skills/nhx-fangji/SKILL.md; do
    if [ -e "$f" ]; then
      printf '  ✅ %s\n' "$f"
    else
      printf '  ❌ 缺失：%s\n' "$f"
      ok=1
    fi
  done

  # AGENTS.md 必须是符号链接——否则 AGENT.md 不会被 DSH 加载
  if [ -L AGENTS.md ]; then
    printf '  ✅ AGENTS.md 是符号链接 -> %s\n' "$(readlink AGENTS.md)"
  else
    printf '  ❌ AGENTS.md 不是符号链接；AGENT.md 将不会被 DSH 加载\n'
    printf '     修复：ln -sfn AGENT.md AGENTS.md\n'
    ok=1
  fi

  # 指令预算：DSH maxBytes = 65536
  local size
  size=$(wc -c < AGENT.md | tr -d ' ')
  if [ "$size" -lt 65536 ]; then
    printf '  ✅ AGENT.md 体积 %s 字节（预算 65536）\n' "$size"
  else
    printf '  ❌ AGENT.md 体积 %s 字节，超出 DSH 预算 65536\n' "$size"
    ok=1
  fi
  return $ok
}

printf '\n倪师 AI · 全量验收\n'
printf '仓库：%s\n' "$REPO_ROOT"
printf '时间：%s\n\n' "$(date '+%Y-%m-%d %H:%M:%S')"

run_section "红旗分诊测试（召回率必须 100%）" triage_tests
run_section "合规校验器对抗性测试" validate_tests
run_section "知识库结构化数据合规校验" validate_data
run_section "媒体管理数据层测试" media_tests
run_section "认证层测试（含绕过尝试）" auth_tests
run_section "媒体管理 API 冒烟测试" media_api_smoke
run_section "医师审核状态机测试（不变量）" clinic_tests
run_section "Web 应用测试（守门 / 管线 / 检索 / 会话）" app_tests
run_section "配置加载测试（.env 生效 / 环境变量优先）" env_tests
run_section "经典原文验收（R1 逐字 / 完整性 / 交叉验证）" corpus_tests
run_section "关键资产与行为契约检查" check_assets

hr
printf '汇总：%d 项通过，%d 项失败\n' "$PASS" "$FAIL"
if [ "$FAIL" -gt 0 ]; then
  printf '失败项：\n'
  for s in "${FAILED_SECTIONS[@]}"; do printf '  - %s\n' "$s"; done
fi
hr

[ "$FAIL" -eq 0 ] || exit 1
printf '✅ 全部通过\n'
