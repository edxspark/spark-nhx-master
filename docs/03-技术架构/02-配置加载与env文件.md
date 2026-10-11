# 配置加载：`.env` 为什么「填了却没生效」

> 故障复盘 + 设计说明。对应代码：`src/nhx_app/env.py`、`src/nhx_app/config.py`。
> 测试：`src/nhx_app/test_env.py`（19 项）；验收段：`bash scripts/verify.sh` 第 9 段。

---

## 1. 现象（用户原样反馈）

```console
spark-nhx-master % python3 src/nhx_app/server.py
倪师 AI · Web 服务
  绑定：http://127.0.0.1:8790
  模型：deepseek-chat @ https://api.deepseek.com/v1
  API Key：❌ 未配置（对话将不可用）
```

而 `.env` 里明明写着：

```ini
NHX_HOST=0.0.0.0
NHX_PORT=18790
NHX_LLM_API_KEY=sk-……（35 字符）
NHX_LLM_MODEL=deepseek-v4-flash
```

## 2. 复现与根因

```console
$ python3 -c "import sys;sys.path.insert(0,'src/nhx_app');from config import Config;c=Config();print(c.port, c.llm_model, bool(c.llm_api_key))"
8790 deepseek-chat False
```

三个值全部是**代码默认值**，`.env` 一个都没读到。用 grep 全仓确认：

```console
$ grep -rn "dotenv" --include=*.py src/
（无任何匹配）
```

**根因：`.env` 从未被加载过。** `config.py` 只读 `os.environ`，而 Python
不会自动读 `.env`；只有 Docker 的 `env_file:` 与 systemd 的 `EnvironmentFile=`
会注入。于是：

| 启动方式 | `.env` 是否生效 | 原因 |
|---|---|---|
| `docker compose up` | ✅ | compose 的 `env_file: .env` |
| systemd | ✅ | 单元文件 `EnvironmentFile=/opt/nishi-ai/.env` |
| **`python3 src/nhx_app/server.py`** | ❌ | **没人加载它** |
| `python3 src/nhx_media/server.py` | ❌ | 同上 |

README（「`cp .env.example .env` → `vim .env`」）与 AGENTS.md §0
（「`export NHX_LLM_API_KEY=sk-xxxx`」）两处说法还不一致：
前者暗示 `.env` 生效，后者要求显式导出。**文档之间打架本身就是这个坑的一部分。**

## 3. 修复

### 3.1 新增 `.env` 加载器（纯标准库，零依赖）

`src/nhx_app/env.py`：

* `parse_env_text()` —— 支持 `KEY=VALUE`、`export`、单/双引号、`\n` 转义、
  行尾 ` # 注释`、值内含 `=`；非法行**记入 `invalid` 而不抛异常**
* `find_env_file()` —— `NHX_ENV_FILE` > 仓库根 `.env` > 当前目录 `.env`
* `load_dotenv()` —— 写入 `os.environ`，**已有变量绝不覆盖**（12-factor：真环境 > 文件）；
  幂等（进程内只读一次盘，`force=True` 例外）
* `NHX_NO_DOTENV=1` 只关「自动发现」，显式传路径时照读（测试需要）

### 3.2 `Config()` 构造时自动加载

```python
def __init__(self, use_dotenv=True):
    self.dotenv = env_mod.load_dotenv() if use_dotenv else env_mod.dotenv_info()
```

* 入口脚本（`nhx_app/server.py`、`nhx_media/server.py`）无需改动即可生效
* `Config(use_dotenv=False)` 供测试使用，保证用例不受开发机 `.env` 影响
* 新增 `source_of(name)` / `describe_source()`，让「值到底从哪来」可被打印

### 3.3 相对路径按仓库根解析

`NHX_DATA_DIR=./data` 原先按 **cwd** 解析：换个目录启动就把会话写到别处
（`cd /` 时甚至写 `/data` 而权限失败）。新增 `_env_path()` 统一按仓库根解析，
与默认值（`REPO_ROOT/data`）语义一致。

### 3.4 启动横幅与自检说清来源

```
  API Key：已配置（来自 .env）
  配置来源：/Users/…/spark-nhx-master/.env（已加载 17 项）
  认证：protected（1 个账号）
```

未配置时直接给出三条可执行动作（写 `.env` / 导出变量 / 跑 `--check`）。

### 3.5 顺带补上的防呆：没有账号 → 拒绝启动

`.env` 里 `NHX_AUTH_MODE=protected` 从此**真的会生效**。若此时一个账号都没有，
服务启动后没人能登录（AGENTS.md §6.5 防呆 #1 说的「把自己锁在门外」）。
现在启动时检查，命中即拒绝并给出补救命令：

```console
$ NHX_AUTH_MODE=protected python3 src/nhx_app/server.py   # 且无账号时
  ❌ 认证模式 protected 需要账号，但当前一个都没有 —— 启动后没人能登录。
     先建账号： python3 src/nhx_auth/auth.py adduser <用户名>
     或改回免登录：在 .env 里设 NHX_AUTH_MODE=open
```

## 4. 附带发现的安全问题（已修）

排查过程中读 `.env.example` 时发现：

| # | 问题 | 处置 |
|---|---|---|
| S1 | `.env.example` **写着真实 API Key**（与 `.env` 逐字节相同），而它是入库模板 | 已改为占位符模板（`NHX_LLM_API_KEY=` 留空），真实 Key 只留在 `.env` |
| S2 | `.gitignore` **没有排除 `.env`**，与 `.env.example` 头部注释「已在 .gitignore 中排除」矛盾 | 已加 `.env`、`.env.local` |
| S3 | `data/sessions/` 未被忽略（内含用户健康信息，见 §4 R7） | 已整体忽略 `data/` |

**已核实该 Key 未进入 git 历史**（仓库仅 1 个提交、只跟踪 1 个文件，
`git log --all -- .env*` 与 `git grep` 全历史均无命中）。

> ⚠️ **建议轮换该 Key。** 它曾以明文存在于一个「准备入库」的模板文件里；
> 只要轮换一次，风险即归零。

## 5. 验收证据

```console
$ python3 src/nhx_app/server.py            # 用户原命令，修复后
倪师 AI · Web 服务
  绑定：http://0.0.0.0:18790               # ← .env 生效
  模型：deepseek-v4-flash @ https://api.deepseek.com/v1
  API Key：已配置（来自 .env）              # ← 故障点已消除
  配置来源：/Users/…/spark-nhx-master/.env（已加载 17 项）
  知识库：5 篇文档（/Users/…/spark-nhx-master/knowledge）   # 相对路径按仓库根解析
  认证：protected（1 个账号）
  守门：开启（action=block）
  契约：/Users/…/AGENT.md（3829 字符）
```

真实调用（不是 mock）：

```console
$ curl -s http://127.0.0.1:18790/api/config
{"ok": true, "data": {"model": "deepseek-v4-flash", "llm_ready": true,
 "auth_mode": "protected", "guard_enabled": true, "kb_enabled": true}}
```

```console
$ POST /api/chat  → SSE
event: triage  {"hit": false, …}
event: route   {"layer": "L1"}
event: delta   {"text": "你好。"}
…
```

普通对话与 L1 方剂提问均能流式返回，分诊 → 路由 → 检索 → 生成 → 守门 全链路通。

测试与全量验收：

```console
$ python3 src/nhx_app/test_env.py
Ran 19 tests in 0.005s
OK

$ bash scripts/verify.sh
汇总：11 项通过，0 项失败
```

关键回归断言（`test_env.py::TestConfigUsesDotenv`）：

* `test_config_reads_dotenv` —— 写进 `.env` 的 Key/模型/**端口**必须被 `Config` 读到
  （端口没生效就是当初那个 bug）
* `test_env_var_beats_dotenv_in_config` —— 真实环境变量优先
* `test_relative_paths_resolve_against_repo_root` —— `./data` 不随 cwd 漂移
* `test_use_dotenv_false_is_hermetic` —— 测试可完全隔离

## 6. 回滚

改动集中在 4 个文件，去掉 `Config.__init__` 里的 `load_dotenv()` 调用即可退回旧行为：

```bash
git diff src/nhx_app/config.py src/nhx_app/server.py src/nhx_media/server.py
rm src/nhx_app/env.py src/nhx_app/test_env.py     # 并移除 verify.sh 第 9 段
```

## 7. 遗留（本次未做）

1. **`.env` 与 `.env.example` 曾逐字节相同**，说明有人用编辑器把两者一起写了。
   模板已在 `.env.example` 顶部加了醒目警告，但惯例仍需靠人守。
2. `deploy/install.sh` 用 `cp .env.example .env` 生成配置 —— 现在模板里
   `NHX_LLM_API_KEY` 为空，install.sh 自检会提示未配置，行为不变。
3. `NHX_HOST` 模板值改为 `127.0.0.1`（与 `nginx.conf` 的 upstream 一致）；
   需要其他机器直连时自行改 `0.0.0.0`。原模板写的是 `0.0.0.0`，属更宽松的默认。
