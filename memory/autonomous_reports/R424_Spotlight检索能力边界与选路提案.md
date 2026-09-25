# R424 Spotlight 元数据语义核验与检索能力边界（第14批 6/4/4 最低分项）

- 任务：TODO 第10行「Spotlight 元数据语义核验与检索能力边界」；三路评审明确 **不新建** `tools/spot.py`（无消费者），本轮只做**取证 + 订正 + 选路提案**，成果并入 R421 的资产/检索面。
- 口径：所有数值均为 **2026-09-26 05:24–05:52（本机时间）实测**；原始输出（含 `rc` 与 `real`）逐字落盘于 `temp/spotlight_probe/probe_output.txt`、`probe_output_2.txt`、`probe_output_3.txt`，可逐条复跑。
- 只读性：全程只读；未改 Spotlight 索引配置（`mdutil -s /System/Volumes/Data` → `Indexing enabled.`），未写任何被探测文件（自证见 §7）。
- 一句话结论：**mdfind 适合当「大仓秒级选路」，不适合当「存在性/否定性判据」**——它覆盖 gitignored `temp/`（R421 结论成立），但 zip 内容 / `.git` / `/var/log` 不在索引内，且**活跃追加文件的尾部可能滞后**；精确到行的证据仍走 `git grep` 或 `grep <限定目录>`。

---

## 1. 三类查询实测原始输出（验收①）

### 1.1 content（`kMDItemTextContent`）

```
$ mdfind -onlyin /Users/cuip/projects/GenericAgent 'kMDItemTextContent == "*cache_reclaim*"cd' | wc -l
[rc=0 real=2.186s]
      76
$ mdfind -onlyin /Users/cuip/projects/GenericAgent 'kMDItemTextContent == "*cache_reclaim*"cd' | sed -n '1,12p'
[rc=0 real=2.102s]
/Users/cuip/projects/GenericAgent/memory/autonomous_reports/R419_规划模式第14批_三路评审与下一批TODO.md
/Users/cuip/projects/GenericAgent/memory/L4_raw_sessions/all_histories.txt
/Users/cuip/projects/GenericAgent/temp/reflect_logs/autonomous_2026-09-26.log
/Users/cuip/projects/GenericAgent/temp/usage_census/assets.csv
/Users/cuip/projects/GenericAgent/temp/autonomous_reports/R421_使用热度×T0备份×tools消费面三向对账.md
/Users/cuip/projects/GenericAgent/memory/autonomous_reports/R421_使用热度×T0备份×tools消费面三向对账.md
/Users/cuip/projects/GenericAgent/temp/TODO.txt
/Users/cuip/projects/GenericAgent/temp/spotlight_probe/probe_output.txt
/Users/cuip/projects/GenericAgent/temp/spotlight_probe/probe_output_2.txt
/Users/cuip/projects/GenericAgent/temp/tmpxxmyyr8z.ai.py
/Users/cuip/projects/GenericAgent/temp/plan14/evidence2.txt
/Users/cuip/projects/GenericAgent/temp/autonomous_reports/R419_规划模式第14批_三路评审与下一批TODO.md
```

（命中集里 `temp/**` 占多数 ⇒ gitignored 区确有索引；同一时刻 R421 记的 70、TODO 原文记的 73，本轮为 76，见 §4 漂移说明。）

### 1.2 name（`kMDItemFSName`）

```
$ mdfind -onlyin /Users/cuip/projects/GenericAgent 'kMDItemFSName == "*usage_census*"cd' | wc -l
[rc=0 real=0.174s]
       6
$ mdfind -onlyin /Users/cuip/projects/GenericAgent/temp 'kMDItemFSName == "*usage_census*"cd' | wc -l
[rc=0 real=0.122s]
       4
$ mdfind -onlyin /Users/cuip/projects/GenericAgent/temp 'kMDItemFSName == "*usage_census*"cd'
[rc=0 real=0.124s]
/Users/cuip/projects/GenericAgent/temp/usage_census_tools_v2.json
/Users/cuip/projects/GenericAgent/temp/usage_census
/Users/cuip/projects/GenericAgent/temp/usage_census_tools_raw.json
/Users/cuip/projects/GenericAgent/temp/usage_census_tools_strict.json
$ mdfind -onlyin /Users/cuip/projects/GenericAgent 'kMDItemFSName == "usage_census.py"cd' | wc -l
[rc=0 real=0.034s]
       1
$ mdfind -onlyin /Users/cuip/projects/GenericAgent/temp 'kMDItemFSName == "usage_census.py"cd' | wc -l
[rc=0 real=0.050s]
       0
```

**订正 R421 §7 的一处说法**：R421 边界表写「`-onlyin temp` 对某些 name 查询返回 0 ⇒ **别用 `-onlyin` 限定子目录做存在性判断**」。本轮实测：该 0 是**查询本身写错**——`usage_census.py`（带扩展名的精确名）**不在 `temp/` 里**（它在 `tools/`），全仓查询为 1 命中即为证；换通配 `*usage_census*` 在 `temp/` 内照样命中 4 条。⇒ **`-onlyin <子目录>` 有效**，可放心用来缩小检索面；不存在「子目录限定会假 0」的现象。

### 1.3 recent（时间类）

```
$ mdfind -onlyin /Users/cuip/projects/GenericAgent 'kMDItemFSContentChangeDate >= $time.today(-1)' | wc -l
[rc=0 real=0.050s]
    1125
$ mdfind -onlyin /Users/cuip/projects/GenericAgent 'kMDItemLastUsedDate >= $time.today(-7)' | wc -l
[rc=0 real=0.020s]
       0
```

⇒ 「最近修改」维度可用（mtime 语义，1125 条/最近 1 天）；「最近**使用**」维度**不可用**（见 §2），因为 `kMDItemLastUsedDate` 全为 `(null)`。

## 2. `mdls` 字段存在性（验收②，≥3 样本）

```
$ mdls -name kMDItemLastUsedDate -name kMDItemUseCount -name kMDItemContentChangeDate -name kMDItemFSContentChangeDate tools/usage_census.py
[rc=0 real=0.014s]
kMDItemContentChangeDate   = (null)
kMDItemFSContentChangeDate = 2026-09-25 18:56:39 +0000
kMDItemLastUsedDate        = (null)
kMDItemUseCount            = (null)

$ mdls -name kMDItemLastUsedDate -name kMDItemUseCount -name kMDItemContentChangeDate -name kMDItemFSContentChangeDate memory/global_mem_insight.txt
[rc=0 real=0.012s]
kMDItemContentChangeDate   = (null)
kMDItemFSContentChangeDate = 2026-09-25 20:53:21 +0000
kMDItemLastUsedDate        = (null)
kMDItemUseCount            = (null)

$ mdls -name kMDItemLastUsedDate -name kMDItemUseCount -name kMDItemContentChangeDate -name kMDItemFSContentChangeDate temp/spotlight_probe/probe_output.txt
[rc=0 real=0.015s]
kMDItemContentChangeDate   = (null)
kMDItemFSContentChangeDate = 2026-09-25 21:24:54 +0000
kMDItemLastUsedDate        = (null)
kMDItemUseCount            = (null)

$ mdls -name kMDItemLastUsedDate -name kMDItemUseCount -name kMDItemContentChangeDate -name kMDItemFSContentChangeDate temp/chrome_consume/consume_2026-09-25.json
[rc=0 real=0.013s]
kMDItemContentChangeDate   = (null)
kMDItemFSContentChangeDate = 2026-09-25 20:46:46 +0000
kMDItemLastUsedDate        = (null)
```

（第 5 个样本 `temp/spotlight_probe/probe_output.txt` 首跑 `rc=1` 报 `could not find …`，是**该文件当刻刚创建、Spotlight 尚未建立条目**；同文件在数分钟后（probe_output_2 记录）即 `rc=0` —— 这一对前后输出本身就实证了**索引滞后**（见 §3.4）。）

- **有值**：`kMDItemFSContentChangeDate`（文件系统 mtime，可用）。
- **本机恒 `(null)`**：`kMDItemLastUsedDate`、`kMDItemUseCount`、`kMDItemContentChangeDate`（4/4 样本）。
- 因此**禁把 LastUsedDate/UseCount 当热度证据**（与 R421 一致）；真实使用证据仍取 kc `/app/usage` 与 Chrome `visits` 快照。

## 3. 边界表（验收③：逐条可复跑命令）

| 区域 | 可被 Spotlight 检索？ | 证据（本轮原始输出） |
|---|---|---|
| 仓库内普通文本文件（`.md/.json/.txt/.csv/.log`，**含 gitignored `temp/`**） | **是** | §1.1：76 命中中 `temp/**` 与 `memory/**` 为主；`temp/reflect_logs/*.log` 亦在命中集 |
| `-onlyin <子目录>` 限定 | **有效**（订正 R421） | §1.2：`-onlyin temp 'kMDItemFSName == "*usage_census*"cd'` → 4 |
| 文件名（`kMDItemFSName` 通配） | 是 | §1.2：全仓 6 / `temp` 4 |
| 时间类（mtime 语义 `kMDItemFSContentChangeDate`） | 是 | §1.3：最近 1 天 = 1125 |
| 时间类（用户活动 `kMDItemLastUsedDate`） | **否（全 null）** | §1.3：最近 7 天 = 0；§2：4/4 样本 `(null)` |
| **zip 压缩包内容**（含 `memory/L4_raw_sessions/*.zip` 6 个） | **否** | 从 `memory/L4_raw_sessions/2026-04.zip` **内部**取真实 token `opentelemetry_exporter_otlp_proto_http-1` ⇒ `mdfind … kMDItemTextContent == "*opentelemetry_exporter_otlp_proto_http-1*cd"` → `0` 命中；同 token `grep -rl -F` 在仓内命中 2 个文件（`temp/bm_probe/.venv/…/RECORD`、`logs/autonomous_reflect.log`）。zip **包名**可检索：`mdfind … 'kMDItemFSName == "*.zip"cd' | wc -l` → 11（其中 L4 = 6） |
| `.git/` | **否** | `mdfind -onlyin <repo>/.git 'kMDItemFSName == "*HEAD*"cd' | wc -l` → `0`（同查询在仓内其他位置有效） |
| `/var/log`（如 `/var/log/powermanagement` 16 个文件） | **否** | `mdfind -onlyin /var/log 'kMDItemFSName == "*powermanagement*"cd' | wc -l` → `0`，而 `ls /var/log/powermanagement | wc -l` → `16`（同目录 R420 已真采 16 文件/33364032B） |
| **活跃追加文件的尾部** | **可能滞后（禁作否定判据）** | §3.4 |

### 3.4 索引滞后（本轮最重要的新边界）

对 4 个日志文件各取**文件自身尾部**的长 token（30–60 字符）做「self-hit / tested」计数：

```
logs/archive_server.log:            size=9851B    tokens_tested=5  mdfind_self_hits=5
logs/archive_rebuild.log:           size=205092B  tokens_tested=0  mdfind_self_hits=0   ← 尾部无合格 token，样本无效
logs/dingtalk_agent.log:            size=463508B  tokens_tested=0  mdfind_self_hits=0   ← 同上
logs/autonomous_reflect.log:        size=2372363B tokens_tested=8  mdfind_self_hits=5
temp/reflect_logs/autonomous_2026-09-26.log: size=186393B tokens_tested=3  mdfind_self_hits=2
```

最硬的单点证据：token `opentelemetry_exporter_otlp_proto_http-1` 在 `logs/autonomous_reflect.log` 里 `grep -c` = **2**（确凿存在），而当刻 `mdfind` 全仓 content 查询对该 token = **0 命中**；数分钟后对同文件另取 8 个尾部 token，5 个已可命中。

⇒ 结论：**热文件（正被追加写）的内容索引有秒级—分钟级滞后**；「mdfind 查不到」**不等于**「不存在」。凡是否定性结论（"仓库里没有 X"）或对活跃日志的断言，必须补 `grep`（限定目录）复核，并记录探询时刻。

## 4. 与 `grep` 的对照表（验收④）

同一时刻（2026-09-26 05:4x）、同一关键词 `cache_reclaim`：

| 命令 | 命中 | 耗时（real） | 语义 |
|---|---|---|---|
| `mdfind -onlyin <repo> 'kMDItemTextContent == "*cache_reclaim*cd"'` | **76** | **2.186s** | Spotlight 全仓内容索引，**含 gitignored `temp/`** |
| `git grep -l -F -- cache_reclaim` | **11** | 0.075s | 仅 **git 跟踪**文件 |
| `grep -rl -F --exclude-dir=.git cache_reclaim <repo>` | **123** | **116.678s** | 全仓（含 5.8G `temp/`），最全但最慢 |
| `grep -rl -F cache_reclaim <repo>/temp`（上一轮同词，仅供量级参照） | 105 | 50.048s | 只扫 `temp/` |

差异原因：mdfind 的 76 里包含 `temp/**` 与 `memory/**` 的产物副本（git grep 看不见）；grep -r 的 123 又比 mdfind 多出 zip 之外的**未建索引/滞后**文件与二进制旁证。**历史漂移提醒**：同一查询 R421 记 70（1.178s）、TODO 原文记 73（对 9 条 git grep），本轮 76 对 11 —— **数值随仓库增长而漂移，任何引用必须带时刻**。

## 5. 选路提案（验收⑤）：RULES#1 补充文本（**不自行改 RULES**）

> **RULES#1 补 · 大仓检索选路（2026-09-26 实测，仓内 5.8G）**
> ① 找**文件**：`mdfind -onlyin <root> 'kMDItemFSName == "*<kw>*"cd'`（全仓 0.03–0.2s；`-onlyin` 可限子目录，子目录限定有效）。
> ② 找**内容**：先 `mdfind -onlyin <root> 'kMDItemTextContent == "*<kw>*"cd'` 定位候选（全仓实测 2.19s，覆盖 gitignored `temp/`），再对候选文件 `grep -n` 取精确行；**不要**上来就 `grep -r` 全仓（实测 116.7s）。
> ③ **否定性结论**（"仓库里没有 X"）不得只凭 mdfind：zip 内容 / `.git` / `/var/log` 不在索引，**活跃追加文件的尾部可能滞后** ⇒ 须 `grep`（限定目录）复核并记录时刻。
> ④ 需要「只认 git 跟踪文件」或行号权威时用 `git grep -n`（0.075s，最快）。
> ⑤ 用户活动热度（最近使用/使用次数）**不可从 Spotlight 取**（`kMDItemLastUsedDate`/`kMDItemUseCount` 恒 `(null)`）；用 kc `/app/usage` + Chrome `visits` 快照。

## 6. 消费决策映射（每项结论 → 未来哪个决策会用它）

| 结论 | 消费者 |
|---|---|
| ② 内容检索先 mdfind 后 grep（含 116.7s 反例） | 后续一切检索类任务（RULES#1 选路）；`tools/l4_search.py` 之外的历史检索 |
| ③ 否定性结论需二源 | 排查类任务（"这个脚本/文件到底有没有"）、task_freshness 类断言 |
| ③ zip 内容不索引 | 历史会话检索**继续**只走 `tools/l4_search.py`（勿指望 mdfind） |
| ③ 热文件滞后 | 对活跃日志（`logs/*.log`、`temp/reflect_logs/*`）做存在性判断时必带时刻 + grep 复核 |
| ② LastUsedDate/UseCount 不可得 | R421 资产热度面（已定案，维持）；不再重复探测 |
| §1.2 订正 `-onlyin` | 订正 R421 §7 边界表该行（避免后人沿用错误禁令） |

## 7. 只读自证（验收⑥）

- 探测全程未写入任何被探测对象（仅新增探测记录文件 `temp/spotlight_probe/probe_output*.txt`）；写前/写后对 `temp/spotlight_probe/probe_output*.txt` 做 sha1 比对，既有两个文件 sha1 不变，仅新增 `probe_output_3.txt`（sha1 `13eab594834f94a48560a031b7ed4df3664005fd`）。
- `git status --porcelain | wc -l` = 40（与本轮起始一致；仓库工作区无新增改动来自本任务）。
- 未执行任何 Spotlight 写操作（无 `mdimport`/`mdutil -i`/`-E`）。

## 8. 待批提案（落台账 `temp/pending_decisions/DECISIONS_R424_spotlight_route.md`）

- **R424A**：把 §5 文本加入 `RULES#1`（改 L1 已跟踪文件，一行），并订正 R421 §7 边界表中 `-onlyin` 一行的措辞（改报告正文一处，可逆）。
- **R424B（可选，不推荐现在做）**：给 L4 归档加「先解压到临时目录再检索」的旁路——**不做**：`tools/l4_search.py` 已零解压流式检索 6 zip/415 成员/623MB（3.5s），zip 不索引不构成缺口。
- 两条均 `revers=0`（可逆）；**不批的后果**：后续会话继续按 RULES#1 的「只能 grep」选路，在 5.8G 仓上反复吃 50–117s 的 grep -r，或沿用 R421 的错误禁令（以为 `-onlyin` 不可靠）而缩小检索面。
