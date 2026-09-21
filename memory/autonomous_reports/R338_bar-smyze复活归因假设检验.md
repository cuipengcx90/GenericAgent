# R338 bar-smyze「仓库复活」归因假设检验（只读取证，结论：前提本身错了）

- 日期：2026-09-22｜类型：产出 / 假设检验（只读 git，零副作用）
- 任务来源：TODO#3（评审 6/10）「对 bar-smyze 9 月复活给归因假设检验」
- 硬约束遵守：全程 `git log/show/rev-list/rev-parse` 只读 + `os.path.exists` 探测，**未对任何业务仓库做写操作**（无 commit/checkout/fetch）

## 0. 一句话结论

**H1 成立的部分不在"复活"上，而在"工具链"上**：所谓"9 仓复活"是把 R334 的**仓组张冠李戴**了——R334 的 10 仓（`smyze/` 根级云服务组）**今天仍然冻结**（复现无误）；而看起来"复活"的 `smyze/bar-smyze/*` 11 个仓**从未冻结**（2026 全年 736 次提交，3 名真人开发），它们根本不在 R334 基线里。
唯一真实的"从 0 到有"是 **`smyze/bar-smyze` 根仓本身**（首提交 2026-06-15「初始化 CCB 多 Agent 协作框架」，Mar–May 为 0）——即 **CCB/编排链才是"新增物"**，这与 H1 的后半句（CCB 编排链）方向一致，但研究对象应是"工具链仓"，不是"产品仓"。

## 1. 前提纠错：这里有两个不同的仓组（关键发现）

| 仓组 | 路径 | R334(2026-05-11) 是否覆盖 | 今日状态（实测） |
|---|---|---|---|
| A：云服务组（R334 的"10 仓库/27 模块"） | `~/develop/WorkSpaces/idea/smyze/` 根级：bar-cloud、bar-service、bar-agent、Quokka251、bar-deploy-server、tools、ui、ruoyi-* 等 | ✅ 是 | **仍冻结**：bar-cloud 末次 2026-05-08、bar-deploy-server 2025-12-24、bar-service 2022-12-16、bar-agent 2024-07-30、Quokka251 2024-08-03、tools 2025-01-13 |
| B：bar 产品组（11 仓） | `smyze/bar-smyze/` 下的 opserver/op/mp/scm/deploy-server/deploy-webadmin/market-server/e2e/spec/maintenance_app/screen | ❌ 否（R334 未涉） | **一直活跃**：2026 年合计 736 次 Sept 提交、4522 次 opserver 全年提交 |

R334 原文（history.txt）：`近3月仅bar-cloud活跃(13commits集中bar-qmai-service)，其余9仓库冻结`。
**复现验证**：`bar-cloud` 在 `2026-03-01~2026-06-01` 窗口**恰好 13 次提交**（05-08 Jackson「fix:退款接口」… 03-10「fix:失败停机退款处理」）；同窗口 `bar-service/bar-agent/Quokka251/bar-deploy-server/tools/bar-smyze` **均为 0**。⇒ R334 的 13/9 计数口径被精确复现，证明其样本 = **仓组 A**。
⇒ TODO 里"9 仓复活"的比对（拿仓组 B 的 09-16/09-22 提交去对 R334 的仓组 A 结论）是**跨组误读**，前提不成立。

## 2. 证据链 B：仓组 B 从未冻结（月度提交直方图，Σ 已与 `rev-list --count` 逐仓校验 OK）

```
repo                       01   02   03   04   05   06   07   08   09   Σ校验
bar-deploy-server          54   27   51   29   45   19   52   51   61   389 OK
bar-deploy-webadmin        28    0   27   17   12    7   52   26   42   211 OK
bar-market-server           0    0    0    0    0    7    5   11   18    41 OK
bar_maintenance_app        11    6   14    3    1    3    1    0    0    39 OK
bar_smyze_e2e               0    0    0    0    0    5    4   12    4    25 OK
bar_smyze_mp               13   19    1  165  211   57   21   41   53   581 OK
bar_smyze_op                4   17  191  134  234  130  188  207   61  1166 OK
bar_smyze_opserver          3   23  818  767  612  423  603  891  382  4522 OK
bar_smyze_scm               0    0    1  100   12   54   93  119   85   464 OK
bar_smyze_screen            9    0    0   12   25   33    6   22    0   107 OK
smyze-spec                  0    0    9    0    2    0    0    6    4    21 OK
```
要点：
- opserver 3–8 月每月 423~891 次提交，**没有任何"冻结期"**；bar_smyze_op 4–8 月 130~234/月；scm 4 月起 12~119/月。
- 9 月全线**低于** 8 月（opserver 891→382、op 207→61、scm 119→85、mp 41→53 持平、deploy-server 51→61 略升）：若 9 月是"复活月"，则 8 月更该叫"复活月"。
- 真正静默的只有 2 个：`bar_maintenance_app`（末次 07-01）、`bar_smyze_screen`（末次 08-27）。
- 唯一"0 → 有"的跃迁发生在 **6 月的 `bar-smyze` 根仓**（03~05 全 0 → 06 月 10 → 07 月 54 → 08 月 32 → 09 月 26）。

## 3. 证据链 C：注入层是真的，且是"批量脚本化"的（H1 唯一被证实的机制）

**C1 同分钟跨仓同题提交**（同一条目出现在 N 个仓、commit 时间戳精确到分钟相同）：

| 注入提交（9/15–9/16） | 覆盖仓数 |
|---|---|
| `chore(dsh): 同步 DSH 适配工具脚本与通用技能`（09-15 21:00） | 7 |
| `chore(dsh): 清理 DSH 重复技能并同步工具脚本`（09-16 11:36） | 7 |
| `chore(dsh): 退役 Claude 记忆同步脚本，注入层改由 $DSH_HOME/AGENTS.md 承担`（09-16 13:18） | 7 |
| `chore(dsh): 合并 open-code-review 仓级评审规则`（09-16 21:53） | 4 |
| `feat(dsh): 铺开 DSH 侧 Trellis 引导层`（09-16 16:37，随后 merge 到 develop/dev） | 4 |
| `chore(dsh): DSH 适配（不改 Claude 树）`（09-15 18:42） | 6 |

覆盖到的仓：bar-smyze(root)、bar-deploy-server、bar-deploy-webadmin、bar-market-server、bar_smyze_e2e、bar_smyze_mp、bar_smyze_scm（+ op/scm 各自的 `feat/20260916-dsh-trellis-bootstrap` 分支）。
**同一分钟、同一文案、跨 4~7 仓** ⇒ 只能由脚本/编排一次性铺开，不可能是人手逐仓敲。这就是"注入层"的物理证据。

**C2 物理落地痕迹**（文件系统探测，非 git）：
- 9 个仓含 `.dsh/`；10 个仓含 `.trellis/`+`.claude/`+`.agents/`+`.grok/`+`.omp/`+`.codex/`+`.pi/` **六平台镜像**及 `AGENTS.md`（部分含 `CLAUDE.md`）。
- 未覆盖：`bar_maintenance_app`（空）、`smyze-spec`（空）、`bar_smyze_op`（无 `.dsh`）、`bar_smyze_screen`（仅 3 个）、`bar_smyze_e2e`（仅 2 个）。
- 提交正文自述（bar_smyze_scm/bar-deploy-server 等）：记忆真源迁至 `~/.dsh/memory/`（63 条习惯 + 81 条项目事实从 Claude Code 记忆桶一次性复制）、删除 `.dsh/tools/sync-claude-memory.py`、`CLAUDE_PROJECT_DIR`/`DSH_SESSION_ID` 会话键修复。

**C3 注入层对提交量的贡献很有限**（工具链 vs 业务/文档，9 月）：
opserver 102 vs 209、op 13 vs 40、scm 15 vs 33、mp 14 vs 26、deploy-server 26 vs 14、market 12 vs 3、webadmin 12 vs 16、e2e 4 vs 0。
⇒ 即便把工具链提交全算上，也解释不了 opserver 的 382 次/月——**活跃度主力是业务功能与文档，不是注入层**。

**C4 作者维度**（9 月）：`崔朋 <peng.cui@smyze.cn>`（opserver 241 / op 34 / scm 44 / mp 17 / deployserver 33 …）、`yongqing.zhou`（opserver 79 / deployserver 28 / webadmin 23 / scm 15 / op 19 / mp 12）、`yiqun`（opserver 62 / mp 24 / scm 26 / op 8）。
⇒ **3 名真人开发者**的持续交付；author 与 committer 完全一致，无"机器人 committer"。
**agent 署名率**：11 仓 9 月 736 次提交中仅 2 次带 `Co-Authored-By: Claude Opus 4.6`（均在 bar_smyze_scm）= **0.27%**；`Auto`/`agent` 等词仅出现在正文描述 AI 工具链配置处。⇒ **AI 尚未成为这些仓的提交主力**，只是被接进了工具链。

**C5 编排链（release-marshal / Trellis）确有落地，但只在"工具链/流程"层**：
- `bar-smyze` 根仓 9 月 26 次提交中，`skills 49 次文件改动 / .dsh 33 / .trellis 21 / .claude 17 /.grok 15 /.omp 14 /.agents 13`，主题为 `feat(skills): release-marshal Stage 0-1/Stage 2 遗漏审计/Stage 7.5 自主诊断-修复闭环/G22 留痕`、`feat(release-marshal): 完成 GA/LoopX/Claude Code 编排入口 + 7.5 监控自动桥 + attest-deployed grant 校验`。
- `bar_smyze_opserver` 侧有对应的流程提交：`docs(task): release-marshal 发布编组 skill 的规划三件套`（09-03）、`feat(release-sql-audit): 实现 6 项检测门禁`（09-05）、`docs(release): 补记 Stage9 回流事后核实`（09-05）、本仓 `.trellis` 升级记录（`chore(trellis): 升级 Trellis 至 v0.6.16`，09-02；0.6.17 的升级在根仓，09-14）。
- 即：release-marshal/Trellis **被开发出来并开始在某几个仓落地**（治理层），但**不是提交量/活跃度的驱动**。

## 4. H1 判定表

| H1 命题拆解 | 判定 | 依据 |
|---|---|---|
| 存在"9 月复活"这一现象 | **假设不成立** | 仓组 B 全年活跃（3–8 月更高），9 月反而低于 8 月；仓组 A 至今仍冻结 |
| "复活"由 dsh/trellis 注入层驱动 | **研究对象错位**；就真实新增物（根仓 bar-smyze）而言方向成立 | 注入层只占 9 月提交 14%~44%（多数仓 ≤30%），且不触碰业务代码 |
| "复活"由 release-marshal/CCB 编排链驱动 | **仅治理层成立** | 根仓首提交 06-15「初始化 CCB 多 Agent 协作框架」，9 月全为 release-marshal/DSH/Trellis 工具链提交；但业务仓提交量与之无关 |
| 真正驱动仓组 B 活跃度的因素 | **3 名真人开发 + 业务需求流（feat/alan_0922 等交付分支）** | 作者分布 + 主题关键词（`fix(home)`/`feat(maintenance)`/`merge(feat/alan_*)`）|

## 5. 真因结论

1. **R334 的"冻结"结论今天依然有效**（仓组 A），无需修订；被误读的是"用哪组仓去比"。
2. **"bar 产品组从未冻结"**——这条此前只在我的会话记忆里，未落 L2；应固化，否则同类误读会重演（本次即由它引发）。
3. **2026 年的真实结构性变化是工具链**：06-15 起 CCB 多 Agent 框架仓（`smyze/bar-smyze` 根仓，无 remote、仅 main 分支、122 提交、全为 skills/.dsh/.trellis/.claude… 配置与 skill），09-14~09-16 完成 DSH/Trellis 注入层铺开（6 类提交 × 4~7 仓同分钟批量）+ Trellis 0.6.17 + open-code-review 仓级规则。
4. **AI 在业务仓的渗透率仍极低（0.27% 署名率）**：工具链已就绪，但业务提交仍由人主导——这是判断"AI 提效是否发生"的关键基线。

## 6. 记忆更新提案（**待审**，我未修改 memory 文件）

| # | 目标 | 现值/缺口 | 提案 |
|---|---|---|---|
| M1 | L2「smyze/bar代码库架构」 | 仅记 "其余9仓冻结" 一类信息，未区分仓组 | 补一句组归属：**R334 的"10 仓/27 模块"= `smyze/` 根级云服务组（bar-cloud 等），与 `smyze/bar-smyze/` 下属 11 个 bar 产品仓是两组无关仓；后者 2026 全年活跃、从未冻结** |
| M2 | L2 新增 | 无"工具链仓"条目 | 记：**`smyze/bar-smyze` 根仓 = CCB 多 Agent 协作框架/工具链仓（首提交 2026-06-15，无 remote，仅 main，内容=skills/.dsh/.trellis/.claude/.grok/.omp/.codex/.pi 六平台镜像 + AGENTS.md）** |
| M3 | L2 或 L3 | 无"注入层批量铺开"打法与覆盖清单 | 记：**09-14~09-16 DSH 注入层滚动：6 类同题提交 × 4~7 仓同分钟完成，物理落点 `.dsh/`+`AGENTS.md`（$DSH_HOME 为记忆真源）；未覆盖：bar_maintenance_app/smyze-spec/bar_smyze_op** |
| M4 | L2 度量基线 | 无 | 记：**bar 产品仓 2026 月度提交基线（opserver 3–8 月 423~891/月，9 月 382）、作者 3 人（崔朋/yongqing.zhou/yiqun）、AI 署名率 0.27%（2/736）** —— 供后续"AI 是否提效"对比 |

## 7. 残余风险与未覆盖面（诚实边界）

- **仓库归属未经"官方"确认**：仓组 A/B 的划分是我从 R334 计数口径反推（13 次完美复现）+ 目录结构推得，非读 R334 原始脚本；若 R334 的 10 仓另有其表，M1 措辞需相应调整（结论方向不变：两组仓都会被混）。
- **"冻结"仅按 git 提交判定**：未查 A 组是否有未推送的本地改动/其他远端活动；B 组工作树是否有长期未提交改动也未查（避免破坏，未跑 `git status` 汇总统计外的东西）。
- **AI 署名率是下限**：只统计 `Co-Authored-By: Claude` / `Generated with Claude Code` trailer；若有人在 AI 协助下手工提交且不留痕，则真实渗透率被低估。
- **9 月未结束**（今天 09-22）：9 月数据为 01–22 日窗口，"低于 8 月"结论在月末可能变化（趋势明显，风险低）。
- 直方图脚本首版有 `%` 运算符优先级 bug（`A % B if c else C` 被解析成 `A % (B if c else C)`，9 月列全 0），已修正并加 **Σ月度 == rev-list 总数** 的机械校验（11/11 OK），避免再犯。

## 8. 附录：取证命令（可复现）

```
ROOT=~/develop/WorkSpaces/idea/smyze/bar-smyze
# 仓清单（动态，勿硬编码）
for d in $ROOT/*/; do [ -d "$d/.git" ] && echo ${d##*/}; done
# 最后提交 + 月度量
git -C $ROOT/<repo> log -1 --format='%ad|%an|%s' --date=format:'%Y-%m-%d %H:%M'
git -C $ROOT/<repo> rev-list --count --since=2026-09-01T00:00:00+08:00 --until=2026-10-01T00:00:00+08:00 HEAD
# 作者 / committer
git -C $ROOT/<repo> log --since=2026-09-01T00:00:00+08:00 --format='%an <%ae>|%cn <%ce>'
# 注入层同题提交覆盖矩阵
git -C $ROOT/<repo> log --since=2026-09-01T00:00:00+08:00 --format='%s' | grep -c 'chore(dsh)'
# 工具链 vs 业务（按改动路径分类）
git -C $ROOT/<repo> log --since=2026-09-01T00:00:00+08:00 --name-only --format=$'\x02%h\t%s'
# R334 仓组复现
git -C ~/develop/WorkSpaces/idea/smyze/bar-cloud log --since=2026-03-01T00:00:00+08:00 --until=2026-06-01T00:00:00+08:00 --oneline | wc -l   # → 13
```

> 注：时间口径必须带 `T00:00:00+08:00`（裸日期会丢当天提交）；跨仓批量比较务必用**同一窗口常量**并做 Σ 校验。
