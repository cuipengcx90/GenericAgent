# WeChat Signals SOP

适用: 每日00:01按 `groups.json` 当前配置的全部微信群抓取消息，分桶分类，生成Obsidian日报+飞书推送。
周报: 每周一09:00自动聚合上周日报生成周报（`sche_tasks/wechat_weekly.json`）。
环境配置见 L2 `[WeChat Signals]`。

## 执行架构 (解密DB方案 + subagent并行模式)
- **调度入口**: `sche_tasks/wechat_signals.json`(日报) / `wechat_weekly.json`(周报) → scheduler.py → GA主agent
- **数据源**: wechat-decrypt解密后的SQLite DB (纯sqlite3, 无需sqlcipher)
- **执行方式**: 主agent调度:
  1. 翻群同步: `temp/wechat_auto_sync.py` (AppleScript切换群聊触发Mac微信同步)
  2. 解密DB: `cd temp/wechat-decrypt && python decrypt.py` (需sudo, 每次翻群后重新解密)
  3. 提取+日报: `temp/wechat_daily_auto.py --date YYYY-MM-DD` (解密DB→JSON+Obsidian MD)
  4. AI摘要: 主agent对提取结果进行LLM分类汇总 (大群用subagent并行)
- **一键管道**: `temp/wechat_pipeline.py` (翻群→解密→提取→日报)
- **Key管理**: GA通过 mykey.py 内置LLM配置，subagent继承主agent环境
- **⚠️ 废弃**: chatlog API方案已废弃; `wechat_classify.py` / `wechat_fetch.py` 已归档至 `temp/archive/`

## 分类规则

### SaaS groups (category: saas)
| Bucket | Icon | Keywords |
|---|---|---|
| seo | 🔍 | Google, Ahrefs, keyword, ranking, SEO, content, backlink |
| pricing | 💰 | Stripe, Lemon Squeezy, Paddle, MRR, ARR, pricing, tax |
| infra | 🌐 | domain, Cloudflare, Vercel, deploy, CDN, DNS, hosting |
| growth | 📈 | ProductHunt, Twitter/X, growth, acquisition, conversion |
| risks | ⚠️ | ban, compliance, GDPR, risk, lawsuit, policy change |

### AI Coding groups (category: coding)
| Bucket | Icon | Keywords |
|---|---|---|
| tools | 🔧 | Claude, GPT, Gemini, SDK, API, release, model |
| workflow | 🔄 | CI/CD, spec, workflow, lint, git, testing |
| multimodel | 🤖 | agent, MCP, orchestration, multi-model, token |
| architecture | 📐 | architecture, design pattern, monorepo, microservice |
| pitfalls | ⚠️ | debug, performance, cost, OOM, timeout, pitfall |

### Signal strength
- **strong**: Actionable advice, verified experience, important tool release, concrete data
- **medium**: Valuable discussion, opinion clash, trend signal, tool recommendation
- **weak**: Casual mention, unverified rumor, personal preference

## Obsidian 输出格式

### 文件路径
- SaaS: `SaaS出海/Daily/{YYYY-MM-DD}-{slug}.md`
- Coding: `AICoding/Daily/{YYYY-MM-DD}-{slug}.md`
- slug: group name前10字符，特殊字符替换为`-`

### YAML frontmatter (mandatory)
```yaml
---
date: YYYY-MM-DD          # ⚠️ 不加引号，Dataview需裸日期
category: saas | coding
group: "full group name"
group_id: "xxxxx@chatroom"
signals_count: N
top3:
  - "signal 1"
  - "signal 2"
  - "signal 3"
tags: [tag1, tag2, tag3]
signal_strength:
  strong: N
  medium: N
  weak: N
---
```

### Wikilinks convention
- People: [[nickname]]  · Tools: [[Stripe]], [[Claude Code]]  · Concepts: [[SEO]], [[MCP]]

## 手动触发 (自定义范围 + subagent并行)

用户可随时指定任意群+任意日期范围生成日报/周报，使用Map模式subagent并行处理。

### 口头触发语义规范
- **单日 + 日报**：生成 **1份日报**
  - 例：`帮我生成 XX群 2026-05-18 的日报`
- **日期范围 + 日报**：按天生成，**范围内每天各1份日报**
  - 例：`帮我生成 XX群 2026-05-12~2026-05-18 的日报` → 共7份日报
- **日期范围 + 周报/汇总/总结**：生成 **1份聚合报告**
  - 例：`帮我生成 XX群 2026-05-12~2026-05-18 的周报`
  - 例：`帮我生成 XX群 2026-05-12~2026-05-18 的汇总`
- **多个群 + 日期范围**：默认对每个群分别输出；若用户明确要求“合并”，则生成跨群聚合版
- **未说明类型**：默认按“汇总”理解，并先向用户复述确认

### 工具
- **`temp/wechat_pipeline.py`** — 一键管道(翻群→解密→提取→日报)
- **`temp/wechat_daily_auto.py`** — 解密DB提取+Obsidian日报生成
- **`temp/wechat_auto_sync.py`** — AppleScript自动翻群触发微信同步
- **`temp/wechat-decrypt/decrypt.py`** — DB解密(需sudo)
- **`temp/wechat-decrypt/monitored_groups.json`** — 监控群列表(29群)
- 用法: `python temp/wechat_pipeline.py --skip-sync --skip-decrypt --date 2026-05-19`

### Map模式流程 (主agent执行)
```
1. [Fetch阶段] 主agent调 code_run 执行 wechat_fetch_helper.py → 生成 temp/wechat_batch/{date}/{slug}.yaml + manifest.json
2. [分发阶段] 读 manifest.json，按群启动subagent (每群独立上下文，避免大群消息污染):
   - 每个subagent的 context.json 包含: 消息文件绝对路径、群信息、SOP分类规则、输出路径
   - 启动: python ../agentmain.py --task wc_{slug} --bg
   - input.txt: "读context.json，按wechat_signals_sop分类规则分析消息，生成Obsidian日报写入指定路径"
3. [监控阶段] 主agent轮询各subagent output.txt，等待 [ROUND END]
4. [汇总阶段] 收集所有日报路径，推送飞书通知
```

### 单群少量消息 → 主agent直接处理
群数≤3 或 单群消息<500条时，无需subagent，主agent inline处理更高效。

## 关键坑点 (实测验证)

1. **解密DB方案(当前)** — wechat-decrypt解密后的DB用普通sqlite3直接查询，无需sqlcipher
2. **DB结构**: message_*.db含Name2Id表(chatroom_id→usrName) + Msg_{MD5(usrName)}表(消息); contact.db含contact表(wxid→nick_name)
3. **消息格式**: 群聊消息content格式为"wxid_xxx:\n实际内容"，需解析分离sender和content
4. **翻群同步必要**: Mac微信不会主动同步所有群历史，必须AppleScript切换群聊触发同步，翻群后重新解密
5. **解密需sudo**: `cd temp/wechat-decrypt && sudo python decrypt.py` 读取进程内存获取key
6. **WAL flush延迟** — WeChat SQLite WAL模式，消息可能滞后。翻群后等几秒再解密
7. **link/appmsg内容是XML** — 用regex提取: `re.search(r'<title>(.*?)</title>', content)`
8. **chatlog API已废弃** — 不再使用chatlog HTTP API方案
