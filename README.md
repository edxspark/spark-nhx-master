# 倪师 AI（NiShi AI）

以倪海厦先生公开学术内容为知识内核的「中医经典研习 + 传统文化参考」AI 服务。

**独立可部署的 Web 应用** —— 纯 Python 标准库，零第三方依赖，1 核 1G 可运行。

> ⚠️ **本项目是 AI 助手，不是倪海厦本人。** 回答基于其公开学术内容构建，不代表其本人意见。
> 内容不构成医疗诊断或处方建议。若有急症，请立即就医。

---

## 快速开始

```bash
cp .env.example .env                  # 本地配置放这里（模板已入库，.env 不会）
vim .env                              # 填 NHX_LLM_API_KEY=sk-xxxx
python3 src/nhx_app/server.py         # → http://127.0.0.1:8790
```

打开浏览器即可对话。**不填 Key 也能启动**（服务可跑、急症分诊可用），只是普通对话会报错。

> 启动时 `Config` 会自动读取**仓库根目录的 `.env`**，所以不必先 `export`。
> **真实环境变量优先级高于 `.env`**（12-factor），临时覆盖直接 `NHX_PORT=9000 python3 src/nhx_app/server.py` 即可。
> 装了 Key 却仍显示「未配置」时，先跑 `python3 src/nhx_app/server.py --check`——
> 它会打印配置来源、`.env` 路径与无法解析的行。

### 部署到服务器

```bash
# 方式一：systemd
sudo bash deploy/install.sh

# 方式二：Docker
cp .env.example .env && vim .env
docker compose up -d
```

完整说明见 **[部署指南](deploy/README.md)**。

### 验收

```bash
bash scripts/verify.sh                # 10 个检查段
```

---

## 它做什么

| 能力层 | 处理什么 | 边界 |
|---|---|---|
| **L1 学习** | 经典讲解、方剂解析、本草穴位、医案复盘、学习路径 | 放开讲 |
| **L2 参考** | 自述症状、体质、调理诉求 | 给思路与依据，**不给确诊、不给剂量** |
| **L3 文化** | 命理、风水、占卜、易经 | 排盘靠算法，标注「文化研究 · 娱乐参考」 |

### 三条安全设计（这是产品的核心）

**1. 红旗分诊 —— 进闸门之前先筛急症**

命中胸痛、卒中、大出血、意识障碍、孕妇急症、婴幼儿高热惊厥、自伤意念等信号 →
**中断正常流程**，输出急症响应，不给辨证与方剂。

急症响应是**确定性模板，不调用 LLM**：零延迟、零风险（模型不可能自己加一句
「可能是心绞痛」）、且 **LLM 挂了也照常工作**。用笨办法换可靠。

**2. 输出守门 —— 不管模型是否遵守，结果都被检查**

提示词红线只降低违规「概率」；守门负责兜住漏网的那一次。

| 规则 | 检查 |
|---|---|
| G-DOSE | 面向用户的现代剂量（「你吃 9 克」）→ 抹去并替换为指引语 |
| G-R8 | 劝停化疗 / 降压药 / 胰岛素 → 拦下并追加澄清 |
| G-R3 | 确诊式断言（「你就是 X 病」）→ 标记 |
| G-R5 | 自称倪海厦本人 → 拦下 |
| G-DISC | L2/L3 缺合规定语 → 自动补上 |

**关键**：守门在**流式过程中**逐句生效，而不是等全文生成完再检查——
否则违规文本早已逐字发到用户屏幕，守门形同虚设。（这是实际踩过的坑。）

剂量检测按**语境**分级：`你吃 9 克` 高危拦截；`汉代一两约合 13.8 克` 是正当知识讲解，放行。

**3. 原文原样 —— 经典不由模型复述**

经典原文与方剂组成从结构化知识库**逐字取出**，不交给模型「复述」——
模型复述古文极易篡改字句。目录上 `原文/` 与 `讲义/` 物理分离，
因为实测发现第三方站点标注为「人纪五大经典」的内容实为**课程转录稿**，
与古籍条文并不逐字对应。

---

## 医师闭环（可选升级）

```
L2 回答 → 用户选择「请医师确认」 → AI 结构化预问诊
        → 预问诊报告（标注未确认） → 医师审核 → 医师意见（署名）
        → 合并交付：AI 报告 + 医师意见
```

**七条不变量由代码强制**（`src/nhx_clinic/review.py`）：

| # | 不变量 |
|---|---|
| I1 | AI 不能把自己生成的报告置为 `reviewed` |
| I2 | 署名状态必须带 `physician_id` 与 `license_no` |
| I3 | 未确认不可交付用户 |
| I4 | 每次流转写审计记录 |
| I5 | `needs_more` 必须带具体问题 |
| I6 | 终态不可再流转 |
| I7 | 预问诊报告必须带免责说明 |

> ⚠️ **互联网诊疗不得首诊**。闭环定位为「复诊与健康管理」，
> 首诊只做就医引导。详见[医师闭环设计](docs/00-产品设计/02-医师闭环设计.md)。

---

## 目录结构

```
.
├── src/
│   ├── nhx_app/              # ★ Web 应用（服务 + 管线 + 守门 + 前端）
│   ├── nhx_triage/           # 红旗分诊（规则 + 引擎）
│   ├── nhx_auth/             # 共享认证（Web 应用 + 媒体后台）
│   ├── nhx_clinic/           # 医师审核状态机
│   ├── nhx_media/            # 媒体资源管理后台
│   └── nhx_schema/           # 结构化数据合规校验器
├── knowledge/                # 文本知识库
│   └── 01-人纪-中医经典/<科目>/{原文,讲义}/   # ★ 物理分离
├── media/                    # 媒体索引（本体在 OSS）
├── deploy/                   # systemd / nginx / Docker / 安装脚本
├── docs/                     # 设计与调研文档
├── data/                     # 会话与认证数据（gitignored，权限 700）
├── AGENT.md                  # ★ 行为契约（系统提示词的单一来源）
└── scripts/verify.sh         # 全量验收
```

---

## 配置

全部通过环境变量，或写进仓库根的 `.env`（启动时自动加载；环境变量优先）。
完整清单见 [`.env.example`](.env.example)。

| 来源 | 优先级 | 说明 |
|---|---|---|
| 真实环境变量 / systemd `EnvironmentFile` / Docker `env_file` | 高 | 与 `.env` 冲突时以此为准 |
| 仓库根 `.env` | 中 | 本地开发与 `python3 src/nhx_app/server.py` 直跑时自动加载 |
| 代码默认值 | 低 | 保证不配也能启动 |
| `NHX_ENV_FILE` | — | 指定其它路径的 env 文件 |
| `NHX_NO_DOTENV=1` | — | 关闭自动加载（测试 / CI） |

媒体后台（`src/nhx_media/server.py`）同样读取这份 `.env`，两个服务的认证模式不会打架。

| 变量 | 必填 | 说明 |
|---|---|---|
| `NHX_LLM_API_KEY` | ✅ | LLM 密钥 |
| `NHX_LLM_BASE_URL` | | 默认 `https://api.deepseek.com/v1` |
| `NHX_LLM_MODEL` | | 默认 `deepseek-chat` |
| `NHX_AUTH_MODE` | | `open`（默认免登录）/ `protected` / `strict` |
| `NHX_GUARD_ENABLED` | | 输出守门，**建议保持 true** |

### 换模型供应商

走 OpenAI 兼容协议，改两个变量即可：

```bash
NHX_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1   # 通义
NHX_LLM_BASE_URL=http://127.0.0.1:11434/v1                            # 本地 Ollama
```

### 认证

界面**默认免登录**，开箱即用。需要时：

```bash
python3 src/nhx_auth/auth.py adduser admin
# 然后在 .env 设 NHX_AUTH_MODE=protected
```

---

## 已实现 vs 待实现

| 模块 | 状态 | 验收 |
|---|---|---|
| Web 对话应用（流式 + 引用 + 分诊告警） | ✅ | 71 项测试 |
| 红旗分诊 | ✅ | 21 例召回 **100%**，误报 0 |
| 输出守门（流式逐句生效） | ✅ | 含误报/漏报双向断言 |
| 知识库检索（原文精确 + 关键词） | ✅ | 二元组切分；经典原文可精确取用 |
| 认证与授权 | ✅ | 37 项测试，含绕过尝试 |
| 医师审核状态机 | ✅ | 31 项测试，7 条不变量 |
| 媒体资源管理后台 | ✅ | 24 项测试 + API 冒烟 |
| 部署产物（systemd / Docker / Nginx） | ✅ | 容器实测构建运行，非 root |
| 经典原文（伤寒论 398 条 / 金匮 22 篇） | ✅ | 37 项测试；R1 逐字验收通过 |
| 确定性排盘（紫微 / 八字 / 六爻） | ⏳ | L3 层唯一硬技术依赖 |
| 辨证卡 / 方剂卡结构化渲染 | ⏳ | — |
| 医师侧界面 | ⏳ | — |

---

## 文档

| 文档 | 内容 |
|---|---|
| [`AGENT.md`](AGENT.md) | ★ **行为契约**（系统提示词的来源） |
| [产品设计书](docs/00-产品设计/01-产品设计书.md) | 定位、能力闸门、场景、路线图、资源复用策略 |
| [医师闭环设计](docs/00-产品设计/02-医师闭环设计.md) | 预问诊→确认的状态机、责任边界、合规要求 |
| [功能清单](docs/01-功能梳理/01-功能清单与优先级.md) | 六大能力域 90+ 功能项与优先级 |
| [资源分类与OSS方案](docs/02-资源管理/01-资源分类与OSS方案.md) | 目录、结构化 Schema、视频 OSS、命名规范 |
| [来源台账](docs/02-资源管理/来源台账.md) | 版权状态与决策记录 |
| [部署指南](deploy/README.md) | systemd / Docker / Nginx / 排障 |
| [交付说明](docs/04-交付说明.md) | 产出清单与后续计划 |
| [`docs/research/`](docs/research/) | 三个原始资源的实测调研 |

---

## 合规提示

- [ ] `.env` 权限 600，`data/` 权限 700
- [ ] 对外部署设 `NHX_AUTH_MODE=protected` 并启用 HTTPS
- [ ] Nginx 保留 `proxy_buffering off`（否则流式失效）
- [ ] 页面显著位置声明「AI 助手，非倪海厦本人」
- [ ] 内容来源已登记（见[来源台账](docs/02-资源管理/来源台账.md)）
- [ ] 用户健康数据按敏感个人信息处理（PIPL）

---

## 开发说明

**改行为 → 改 `AGENT.md`。** 它是系统提示词的单一来源，
`src/nhx_app/prompts.py` 从中提取运行时章节（§1–§4、§8）。
改了契约，线上行为立即跟着变——不存在「文档写了但代码没跟上」的漂移。

跑测试：

```bash
python3 src/nhx_app/test_app.py        # 应用（守门 / 管线 / 检索 / 会话 / LLM）
python3 src/nhx_triage/test_triage.py  # 分诊
python3 src/nhx_auth/test_auth.py      # 认证
python3 src/nhx_clinic/test_review.py  # 医师状态机
python3 src/nhx_media/test_store.py    # 媒体数据层
bash scripts/verify.sh                 # 全部
```

无 Key 调试对话：用内置假 LLM。

```bash
python3 src/nhx_app/mock_llm.py --port 8799 &
NHX_LLM_BASE_URL=http://127.0.0.1:8799/v1 NHX_LLM_API_KEY=test \
  python3 src/nhx_app/server.py
```
