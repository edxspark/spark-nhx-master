# 倪师 AI · 部署指南

独立 Web 应用，**零第三方依赖**（纯 Python 标准库），可部署到任意 Linux 服务器。

---

## 0. 架构一览

```
浏览器 ──HTTPS──> Nginx ──HTTP──> Python 服务 (8790)
                     │                  │
                     │                  ├─ 红旗分诊（规则，确定性）
                     │                  ├─ 知识库检索（本地文本）
                     │                  ├─ 输出守门（后置校验）
                     │                  └─ LLM API（DeepSeek 等）
                     │
                静态前端            data/  会话与认证
```

**没有数据库、没有消息队列、没有 Node 构建**。一台 1 核 1G 的机器就能跑。

---

## 1. 最快路径：Docker

```bash
git clone <repo> nishi-ai && cd nishi-ai
cp .env.example .env
vim .env                       # 填 NHX_LLM_API_KEY

docker compose up -d
docker compose logs -f
```

访问 `http://127.0.0.1:8790`。

默认只映射到 `127.0.0.1`（不对外），由宿主机 Nginx 反代并提供 HTTPS。

> **已验证**：镜像构建成功，容器以非 root（uid 10001）运行，
> 健康检查 `healthy`，`data/` 权限 0700，镜像体积约 206 MB。

---

## 2. 传统部署：systemd

```bash
git clone <repo> /tmp/nishi-ai && cd /tmp/nishi-ai
sudo bash deploy/install.sh
```

脚本会：建系统用户 `nishi` → 拷文件到 `/opt/nishi-ai` → 生成 `.env`（权限 600）
→ 建 `data/`（权限 700）→ 安装 systemd 单元 → 自检。

然后：

```bash
sudo nano /opt/nishi-ai/.env          # 填 NHX_LLM_API_KEY

# 若用 protected/strict 模式，先建账号
sudo -u nishi python3 /opt/nishi-ai/src/nhx_auth/auth.py adduser admin

sudo systemctl enable --now nishi-ai
sudo systemctl status nishi-ai
journalctl -u nishi-ai -f
```

---

## 3. Nginx 与 HTTPS

```bash
sudo cp deploy/nginx.conf /etc/nginx/conf.d/nishi-ai.conf
sudo vim /etc/nginx/conf.d/nishi-ai.conf     # 改 server_name 与证书路径
sudo certbot --nginx -d ai.example.com       # 自动申请证书
sudo nginx -t && sudo systemctl reload nginx
```

### ⚠️ 必须保留的三行

```nginx
proxy_buffering off;
proxy_cache off;
proxy_set_header X-Accel-Buffering no;
```

**不关掉 buffering，对话就不是流式的**——用户要等模型全部生成完才看到内容，
体验会明显变差（看起来像卡住了）。

---

## 4. 环境变量

完整清单见 [`.env.example`](../.env.example)。最关键的几项：

| 变量 | 必填 | 说明 |
|---|---|---|
| `NHX_LLM_API_KEY` | ✅ | LLM 密钥。不填服务能启动，但对话会报错 |
| `NHX_LLM_BASE_URL` | | 默认 `https://api.deepseek.com/v1`，换供应商改这里 |
| `NHX_LLM_MODEL` | | 默认 `deepseek-chat` |
| `NHX_AUTH_MODE` | | `open` / `protected` / `strict`，**对外部署务必改** |
| `NHX_HOST` | | 容器里用 `0.0.0.0`；systemd 部署可用 `127.0.0.1` |
| `NHX_GUARD_ENABLED` | | 输出守门，**强烈建议保持 true** |

### 换模型供应商

本服务走 OpenAI 兼容协议，换供应商只改两个变量：

```bash
# 阿里通义
NHX_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
NHX_LLM_MODEL=qwen-plus

# 智谱
NHX_LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
NHX_LLM_MODEL=glm-4

# 本地 Ollama
NHX_LLM_BASE_URL=http://127.0.0.1:11434/v1
NHX_LLM_MODEL=qwen2.5:14b
NHX_LLM_API_KEY=ollama        # 占位，Ollama 不校验
```

---

## 5. 认证

| 模式 | 读接口 | 对话 | 适用 |
|---|---|---|---|
| `open`（默认） | 免登录 | 免登录 | 本机开发、演示 |
| `protected` | 免登录 | **需登录** | 对外服务（推荐） |
| `strict` | **需登录** | **需登录** | 内网严格管控 |

```bash
python3 src/nhx_auth/auth.py adduser <用户名>      # 建账号
python3 src/nhx_auth/auth.py users                  # 列出
python3 src/nhx_auth/auth.py passwd <用户名>         # 改密（会踢掉该用户全部会话）
python3 src/nhx_auth/auth.py sessions               # 活动会话
python3 src/nhx_auth/auth.py logoutall              # 全体下线
```

安全实现：PBKDF2-HMAC-SHA256（26 万次迭代 + 随机盐）、会话只存 token 的
SHA-256、常量时间比对、5 次失败锁 15 分钟、错误信息不泄露用户名是否存在。

> **注意**：`NHX_AUTH_MODE` 环境变量存在时，CLI 的 `mode` 切换会被**拒绝并报错**——
> 因为环境变量会覆盖配置文件，切换会静默无效。要用 CLI 切换就先注释掉该变量。

---

## 6. 数据与备份

```
data/
├── auth.json          # 用户与会话（含口令哈希，600 权限）
└── sessions/          # 会话记录，每会话一个 JSON（含用户健康信息）
```

**必须备份**：`data/` 目录、`.env`、`knowledge/`。

会话文件含用户健康信息（敏感个人信息），请：

- 限制服务器访问权限
- 备份文件加密存储
- 提供删除能力（`DELETE /api/session/<id>`）
- 会话默认 7 天过期（`NHX_SESSION_TTL`）

---

## 7. 监控与健康检查

```bash
curl -s http://127.0.0.1:8790/api/health
```

```json
{"ok": true, "data": {"status":"ok","model":"deepseek-chat",
                      "llm_ready":true,"auth_mode":"protected"}}
```

`llm_ready: false` 表示未配置 API Key——服务活着但对话不可用，**应当告警**。

建议监控：

| 指标 | 来源 | 告警条件 |
|---|---|---|
| 服务存活 | `/api/health` | 连续 3 次失败 |
| LLM 可用 | `/api/health` 的 `llm_ready` | 变为 false |
| 守门拦截率 | 应用日志中的 `guard` 事件 | 突增（说明模型行为异常） |
| 分诊命中 | 应用日志中的 `triage` 事件 | 突增（可能是内容问题） |

---

## 8. 上线前检查清单

- [ ] `.env` 已填 `NHX_LLM_API_KEY`，且文件权限 600
- [ ] `NHX_AUTH_MODE` 设为 `protected` 或 `strict`
- [ ] 已创建至少一个管理员账号
- [ ] Nginx 配置了 HTTPS（TLS 1.2+）
- [ ] Nginx 保留 `proxy_buffering off`（否则流式失效）
- [ ] `NHX_GUARD_ENABLED=true`
- [ ] 产品页面显著位置有「AI 助手，非倪海厦本人」声明
- [ ] 数据目录权限 700，且有备份策略
- [ ] 已确认内容来源合规（见[来源台账](../docs/02-资源管理/来源台账.md)）
- [ ] 若开放医师闭环：已有互联网诊疗资质与医师实名核验
- [ ] 红旗分诊回归测试纳入 CI（`bash scripts/verify.sh`）

---

## 9. 排障

### ⚠️ 访问 `http://0.0.0.0:端口` 页面全白

**这是最常见的坑，且极难自查。**

`0.0.0.0` 是「监听所有网卡」的**绑定地址**，不是可访问地址。
Chrome 128+ 出于防 DNS rebinding 的考虑，**直接屏蔽**对 `0.0.0.0` 的导航与 fetch：
页面完全空白、`document.body` 为空、连 `<title>` 都没有，
但服务器日志显示 `200` —— 看起来像前端坏了，其实请求根本没出去。

**正确做法**：用 `127.0.0.1`（本机）或局域网 IP。

```
❌ http://0.0.0.0:18790      ← 浏览器屏蔽
✅ http://127.0.0.1:18790    ← 本机访问
✅ http://192.168.x.x:18790  ← 同一局域网的其他设备
```

服务启动时会直接打印**可访问地址**，照着打开即可：

```
倪师 AI · Web 服务
  监听：0.0.0.0:18790（所有网卡）
  ★ 请在浏览器打开：http://127.0.0.1:18790
    ⚠️ 不要访问 http://0.0.0.0:18790 —— 那是绑定地址，现代浏览器会直接屏蔽（页面全白）
```

> 若确实需要外部访问，绑定 `0.0.0.0` 是对的（配 Nginx 反代或直接暴露），
> 但**从浏览器访问时永远用具体地址**，不要用 `0.0.0.0`。

### 其他常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 对话没反应，最后一次性出现 | Nginx 缓冲了 SSE | 加 `proxy_buffering off` |
| `未配置 LLM API Key` | `.env` 没填或没生效 | 跑 `python3 src/nhx_app/server.py --check`，看「配置来源」那一行；systemd 部署再查 `EnvironmentFile=/opt/nishi-ai/.env` 与 `systemctl show nishi-ai -p EnvironmentFiles` |
| `无法连接 LLM 服务` | 网络不通 / 需要代理 | 设 `NHX_LLM_PROXY`（注意本服务默认**不走系统代理**） |
| `LLM 认证失败（401）` | Key 错误或过期 | 换 Key |
| 登录后仍提示需登录 | token 过期 | 重新登录（默认 12 小时） |
| 无法开启登录模式 | 没有用户 | 先 `auth.py adduser` |
| 切换模式后没生效 | `NHX_AUTH_MODE` 覆盖了配置 | 注释掉该变量再切 |
| 启动报「端口已被占用」 | 有残留进程 | `kill $(lsof -t -iTCP:端口 -sTCP:LISTEN)` |

### 关于代理

LLM 客户端用 `http.client` 直连，**不读取操作系统代理设置**。
这是刻意的：macOS 上装了 Clash/Surge 时，`urllib` 会把包括 `127.0.0.1` 在内的
所有请求塞进本地代理，表现为莫名的 `RemoteDisconnected`，且 API Key 会经过第三方进程。

需要走代理时显式设置：

```bash
NHX_LLM_PROXY=http://10.0.0.1:8080
```

---

## 10. 升级

```bash
cd /opt/nishi-ai
sudo -u nishi git pull          # 或重新拷贝新版本
sudo systemctl restart nishi-ai
```

`data/` 不会被覆盖。知识库更新后**无需重启**（检索索引按文件时间自动重建）。

---

## 11. 媒体管理后台（可选，勿公开）

```bash
python3 src/nhx_media/server.py --host 127.0.0.1 --port 8788
```

可改写视频/音频索引，**不要暴露到公网**。远程访问用 SSH 隧道：

```bash
ssh -L 8788:127.0.0.1:8788 user@server
# 本地打开 http://127.0.0.1:8788
```
