# 认证 · Preset · 技能包 · 验收证据

生成时间: 2026-10-11 18:02:15

## 1. 全量验收（8 个检查段）

```
倪师 AI · 全量验收
▶ 红旗分诊测试（召回率必须 100%）
✅ 通过：红旗分诊测试（召回率必须 100%）
▶ 合规校验器对抗性测试
✅ 通过：合规校验器对抗性测试
▶ 知识库结构化数据合规校验
倪师 AI · 结构化数据合规校验
✅ 通过：知识库结构化数据合规校验
▶ 媒体管理数据层测试
✅ 通过：媒体管理数据层测试
▶ 认证层测试（含绕过尝试）
✅ 通过：认证层测试（含绕过尝试）
▶ 媒体管理 API 冒烟测试
✅ 通过：媒体管理 API 冒烟测试
▶ 医师审核状态机测试（不变量）
✅ 通过：医师审核状态机测试（不变量）
▶ 关键资产与 DSH 加载机制检查
✅ 通过：关键资产与 DSH 加载机制检查
汇总：8 项通过，0 项失败
```

## 2. 认证：默认免登录（开箱即用）

```console
$ curl /api/auth/status
{
  "label": "免登录",
  "read": false,
  "write": false,
  "mode": "open",
  "authenticated": false,
  "user": null,
  "has_users": true
}
```

## 3. 防自锁：没有用户时不允许开启登录

```console
$ python3 src/nhx_media/auth.py mode protected   # 仅在无用户时
✅ 已拦截: 无法切换到 `protected`：当前没有任何用户，切换后将无人能登录。
```

## 4. 安全属性实测

```console
1) 明文口令是否落盘: 否 ✅
2) 口令算法: pbkdf2_sha256 ✅
3) 文件权限: 600 ✅
4) 原始 token 是否落盘: 否 ✅
5) 有效 token 可解析: ✅
6) 伪造 token 被拒: ✅
7) 不泄露用户名存在性: ✅ 同样报错 '用户名或口令不正确'
8) 防爆破锁定: ✅ HTTP 429 — 尝试过于频繁，请 15 分钟后再试
```

## 5. 三层模式授权矩阵

```console
模式           GET(读)     POST(写)   
open         放行         放行        
protected    放行         需登录       
strict       需登录        需登录       
```

## 6. Preset 安装状态

```console
lrwxr-xr-x@ 1 moks  staff  63 Oct 11 17:59 /Users/moks/.dsh/.agent-presets/nihaisha -> /Users/moks/Desktop/codes/idea/spark-nhx-master/agents/nihaisha

name: 倪师 AI
description: 倪师 AI 中医与传统文化助手。三层能力闸门、红旗分诊、原文原样溯源、医师闭环。面向中医经典研习与传统文化参考服务。
order: 1
```

## 7. 技能包（DSH 热加载）

```console
.dsh/skills/nhx-bianzheng/SKILL.md
.dsh/skills/nhx-consult/SKILL.md
.dsh/skills/nhx-fangji/SKILL.md
.dsh/skills/nhx-triage/SKILL.md
```

本次会话中，nhx-triage / nhx-consult / nhx-bianzheng / nhx-fangji
四个技能均在写入后立即出现在 DSH 技能目录中，无需重启。
