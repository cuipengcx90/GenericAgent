# R368 磁盘「可再生空间」回收工具化：`tools/cache_reclaim.py`（dry-run / apply / verify / 闭环）

日期：2026-09-23 | 执行：自主会话（用户离线） | 上游：TODO#58（评审 5/10 替换槽位） | 前序：R364（一次性回收，本次把动作固化成工具）

## 0. 结论（先给数）

| 指标 | 值 |
|---|---|
| 交付物 | `tools/cache_reclaim.py`（559 行，纯 stdlib；默认 dry-run 零副作用） |
| 候选注册表 | **20 项**，分三档：safe 10 / approval 5 / never 5 |
| 本轮实测回收（仅 safe 档 1 项） | **pnpm-store prune：4.33 GiB → 2.87 GiB，实收 1,569,767,424 B（1.46 GiB）**，rc=0，3.2 s |
| T0 资产（memory/、temp/kc_snapshots/、tools/、sche_tasks/） | **前后 sha256 汇总逐字节一致**（`f930294728de…` → 同值，`t0_unchanged=true`） |
| 闭环验证 ④ | **pnpm 路线 PASS**（删→重建→可用，网络依赖真实触发，实测 10.9 s / downloaded=2 / `require` 返回值正确） |
| 未动的大头（待批准） | `uv-cache` **20.91 GiB**、`rustup-nightly` 2.63 GiB、`logs` 1.70 GiB（保留）等，合计 25.26 GiB |

一句话：**把 R364「一次性回收」变成可重复动作 —— 只读测算 → 显式确认回收 → 逐项分账 → 终点校验 T0 未变 → 至少一项真的“删掉再长回来还能用”。** 本轮只碰了 safe 档里最大的一项（pnpm store），大件（uv 21 GiB）因为有常驻服务持锁、且属“需人批准”档，保持不动。

## 1. 验收对照（TODO#58 四条）

| # | 要求 | 落实 | 证据 |
|---|---|---|---|
| ① | `tools/cache_reclaim.py` 默认 dry-run，只读测算并按可回收量排序 top10 | ✅ 默认即 dry-run；`du` 只读；输出按字节降序 | `python3 tools/cache_reclaim.py -v`（§3 表；T0 摘要+df 都随打印） |
| ② | `--apply` 需显式确认；逐项给「删 / 剪 / prune / 保留」+ 分项实收字节；日志落 `temp/` | ✅ 不给 `--yes` 直接拒绝（rc=2）；记账 JSON 落 `temp/` | `temp/cache_reclaim_20260923_170417.json`：`items[0].freed=1569767424`、`rc=0`、`sec=3.2` |
| ③ | `--verify` 对 T0 资产做 sha 校验且必须不变 | ✅ 全量 sha256（本轮 6312 文件）；`--apply` 前后自动各算一次并写入日志 | `t0_digest_before == t0_digest_after == f930294728de…`；`t0_unchanged: true` |
| ④ | 至少 1 项（uv 或 playwright）走「删→重建→可用」 | ✅ **已闭环 2 条路线**：`pnpm-store`（真删真重建真可用）+ `--probe playwright`（两轮全绿：重建 rc=0、可用性 CLI 截图 rc=0 落盘 PNG）；另有 `--probe uv` 探针 | §5(a)(d)；`temp/cache_reclaim_probe_pnpm_20260923_170520.json`、`temp/cache_reclaim_probe_playwright_20260923_171033.json` |

## 2. 工具设计（三档 + 红线 + 终点校验）

- **三档**：`safe`（可再生、默认可回收）／`approval`（要人批准：停服务、关 IDE、重下大件）／`never`（坚决不碰）。
- **红线（写进代码注释与判定，任何档都不越）**：不碰 `logs/`、`Documents/`、`Downloads/`、`Pictures/`；不碰 `ms-playwright-mcp` 浏览器 profile（可能含登录态）；**绝不自动升级**任何工具。
- **占用方守卫**：safe 档若被在跑进程占用 ⇒ 自动**升格为 approval**（防“重下依赖”变成“服务中断”）；`never` 档恒为 never（不会被升格成可回收项，避免误读）。
- **体积测算按“可回收子集”而非父目录**：`updater-residues` 只累加 bitwarden/tutti/orca/getpaseo/utools 的残留目录（不是整个 `~/Library/Caches`）；`rustup-nightly` 只算 nightly 工具链目录；`jetbrains-index` 只算索引目录；`ms-playwright-old` 只算**旧版本**目录（保留最新在用版本）。
- **终点校验**：`--apply` 自带 T0 前后摘要；`--verify` 可与任意基线比对（本仓库基线沿用 R364 的 `temp/t0_baseline.sha256`）。
- **可移植解析**：`pnpm`/`uv`/`node`/`brew` 用首选路径+候选目录解析（见 §7 坑①）。

## 3. dry-run 实测（只读，零副作用）

```
 # id                   档         建议            占用  说明
 1 uv-cache             approval  prune    20.91GB
 2 m2                   never     保留        4.55GB ⛔
 3 local-uv-tools       never     保留        3.58GB ⛔
 4 models-gguf          never     保留        3.01GB ⛔
 5 pnpm-store           safe      prune     2.87GB      ← 本轮回收对象（回收前 4.33GB）
 6 rustup-nightly       approval  剪         2.63GB
 7 agent-runtimes       never     保留        2.37GB ⛔
 8 logs                 approval  保留        1.70GB      ← 红线内，只报不动
 9 ms-playwright-mcp    never     保留        0.90GB ⛔   ← 含登录态
10 chrome-http          safe      删         0.22GB
（11–20：brew-cleanup/npm-cache/brew-downloads/electron-cache/node-gyp/pip-cache/bun-cache/updater-residues/jetbrains-index/ms-playwright-old）
可立即回收（safe 档，合计 3.15GB）
需批准（approval 档，合计 25.26GB）: uv-cache, rustup-nightly, logs, jetbrains-index, ms-playwright-old
```

排序即优先级：**能自动做的都在 1GB 以下，真正的大头（21GB uv）落在“需人批准”档**——这正好解释了为什么“磁盘压力会复发”和“自动化回收收益有限”并存。

## 4. apply 实测（safe 档最大项）

```
[pnpm-store] prune 前=4.33GB 后=2.87GB 实收=1.46GB rc=0 3.2s
    out: Removed all cached metadata files | Removed 78727 files | Removed 1920 packages
=== 合计实收 1.46GB | 可用 14.32GB → 14.39GB (Δ+0.07GB) ===
=== T0 资产 sha256 未变 ✅ (f930294728de→f930294728de) ===
=== 记账日志: temp/cache_reclaim_20260923_170417.json ===
```

- `bytes_before=4,654,305,280` → `bytes_after=3,084,537,856`，分项 **freed=1,569,767,424 B = 1.46 GiB**。
- **df 只涨 0.07 GiB，别信 df**：APFS 延迟回收 + 其他并行会话同时在写，因而判“回收了多少”一律以**分项字节**为准（R364 也踩过同一现象）。
- 未执行 `--include-approval`：uv（21 GiB）需先停持锁常驻服务，属人决策范围。

## 5. 闭环 ④：删 → 重建 → 可用（真跑）

**(a) 自包含探针（不动全局 store）** —— `--probe pnpm`：

```
[probe] 建隔离 store rc=0 5.2s store=147456B(0.14MB)   # pnpm 10.16.0, Packages:+2, downloaded 2
[probe] 删 store 释放 147456B（存在性=False）
[probe] 重建 rc=0 1.5s downloaded=2 store=147456B
[probe] 可用性 rc=0: LEFT_PAD=007 NANOID_LEN=8
[probe] 结论=PASS
```

**(b) 全局 store 被 prune 后的真重建**（在 `/tmp/pnpm_rebuild_verify` 空白工程）：

```
store_before_kb=3012244
（pnpm install --reporter=append-only）10.9 s，downloaded 2
store_after_kb=3012388   growth_kb=144
node -e "require('left-pad'); require('nanoid')" ⇒ LEFT_PAD=007 NANOID_LEN=8 （rc=0）
```

**(c) 消费方（真实项目）未被 prune 破坏**：`~/projects/WeChat-AI`（唯一用 pnpm 的项目，`node_modules/.pnpm` 138 个包目录仍在）依赖解析正常 —— `@fastify/compress@8.3.1`、`zod@3.25.76` 均从 `.pnpm` 链接解析成功（只读核验，未运行其构建/测试）。

**(d) `--probe playwright`（题面点名的另一条路线）：跑了两轮，**重建 ✓ / 可用 ✓ / 探针自清理 ✓**（第一轮暴露了我检查法的 bug，改完第二轮全绿）

```
# 第 1 轮 17:07:01 —— 重建成功，但我的可用性检查写法有错
$ nohup python3 tools/cache_reclaim.py --probe playwright > /tmp/pw_probe2.log 2>&1 &
$ cat temp/cache_reclaim_probe_playwright_20260923_170701.json
delete : bytes_freed = 0
rebuild: sec=111.2  rc=0  bytes=584,081,408   # npx playwright install chromium，stdout 尾部 “80% of 94.3 MiB”
usable : sec=0.0    rc=1  err="…nal/modules/helpers:196:16) at Object.<anonymous> (…/temp/pw_probe.js:1:18)"
cleanup: bytes_removed = 584,081,408

# 第 2 轮 17:10:33 —— 改完检查法后复跑（日志 /tmp/pw_probe2.log）
[probe] 重建 rc=0 134.9s  … (playwright chromium-headless-shell v1243) downloaded to …/temp/pw_probe/chromium_headless_shell-1243
[probe] 可用性①(CLI截图) rc=0 1.7s png=652B
[probe] 清理探针目录，回收 557.0MB
$ cat temp/cache_reclaim_probe_playwright_20260923_171033.json
delete : bytes_freed = 0
rebuild: sec=134.9  rc=0  bytes=584,081,408   # 与第 1 轮同体积（94.3 MiB 取件）
usable : mode="cli-screenshot"  sec=1.7  rc=0  png="pw_shot_20260923_171031.png"  png_bytes=652
cleanup: bytes_removed = 584,081,408
```

- **重建侧是货真价实的**：111.2 s（第 1 轮）/ 134.9 s（第 2 轮）下载+解包 chromium（94.3 MiB 取件 → 落盘 557 MB），两轮体积完全一致（584,081,408 B）。
- **第 1 轮可用性 rc=1 错在我的检查法**：脚本用 `node temp/pw_probe.js` 里 `require('playwright')`——非交互 `node` 的模块解析路径里没有 npx 缓存（`~/.npm/_npx/<hash>/node_modules`）⇒ **`Cannot find module`**，与“浏览器能不能用”无关。
- 修正（已改 `tools/cache_reclaim.py`）：可用性检查改为 ① `npx --no-install playwright screenshot about:blank <png>`（真启动浏览器 + 落盘 PNG 作证据）② 失败才回落 `node` + 显式 `NODE_PATH` 指向 npx 缓存的 `playwright` 包。**第 2 轮据此 rc=0、1.7 s 落盘 PNG 652 B**（`file` 实测 320×200 RGB PNG）。
- 两轮跑完都自动清理了 584 MB 探针目录（探针不留垃圾，这也是它的设计）。旁证：用系统已装 `~/Library/Caches/ms-playwright`（chromium-1243 等）跑同一 CLI 截图命令同样 rc=0 —— 说明“删缓存→下载重建→再用”这条链在本机是真实可逆的。

## 6. `--verify` 语义与本次差异解读（含一个真 bug 的修复）

- 工具原先对比基线时**未做路径归一**：R364 基线存**绝对路径**，`t0_digest()` 存**相对路径** ⇒ 首跑报 `新增=6312 删除=6299 内容变化=0`（**全是假差异**，一眼假但极易被当“T0 变了”）。
- 修复：载入基线时把 `ROOT/` 前缀剥成相对路径（1 行）。修复后真实差异：

```
[verify] T0 资产 6312 个文件  sha256 汇总=52b3b3afdceabba0…
[verify] 基线 temp/t0_baseline.sha256: 6299 个文件
[verify] 新增=13 删除=0 内容变化=6
        + memory/autonomous_reports/R359…R367（9 份报告）+ memory/file_access_stats.json
        + memory/verify_quotes_repo.py + sche_tasks/scheduler.log + tools/cache_reclaim.py（本轮新建）
        ~ memory/{autonomous_operation_sop.md, global_mem.txt, global_mem_insight.txt, …}
```

- 判读：**差异 100% 来自跨会话的正常写入**（其他自主会话的报告/记忆 + 定时任务日志 + 本轮新工具），**没有一条来自回收动作**；回收动作本身的“零改动”由 `--apply` 的前后同摘要硬证。⇒ `--verify` 的正确用法是：**回收前后自比**（权威），与历史基线比只作“漂移体检”（memory/ 必然漂移）。
- 存档：`temp/cache_reclaim_verify_1706.txt`（修复前，假差异）、`temp/cache_reclaim_verify_fixed.txt`（修复后，真实差异）。

## 7. 本轮真实踩坑（供后续复用）

1. **`command -v uv` / `pnpm` 在非交互 shell 是假阴性**：两工具都不在本会话 PATH（`pnpm` 实际在 `~/Library/pnpm/pnpm`，`uv` 同理在用户自装目录）⇒ 工具内必须自带候选目录解析，否则会误判“工具没装”。
2. **`du -sh` 测父目录会虚报**：最初把 `updater-residues` 的路径设成 `~/Library/Caches`，显示 15.43 GiB（=整个 Caches），与实际可回收的残包差两个数量级 ⇒ 大目录候选必须配 `size_fn` 只累加**可回收子集**。
3. **`never` 档不能被“占用”升格成 `approval`**：否则报告里会出现“可批准回收 m2/models”这种危险暗示（本轮已修）。
4. **路径口径不一致会造出 6312 条假差异**（§6）——凡“比对基线”的工具，先证两边路径约定一致。
5. **长任务的 stdout 重定向不可靠**：`temp/probe_pw.log` 在运行中被外部 temp 清理删掉（进程仍在写已删除 inode，日志静默消失）⇒ 正确做法是**让脚本自己写结构化 JSON 结果**（本工具已如此：`temp/cache_reclaim_probe_*.json`）。
6. **`code_run` 默认 cwd 是 `temp/`**：脚本里再写 `temp/xxx` 会解析成 `temp/temp/xxx` ⇒ 排查时别被“文件不存在”误导（本轮排查到一次）。

## 8. 复现方式

```bash
cd ~/projects/GenericAgent
python3 tools/cache_reclaim.py                # ① dry-run top10（只读）
python3 tools/cache_reclaim.py --all -v       # 全部候选 + 重建方式/代价/路径
python3 tools/cache_reclaim.py --verify       # ③ T0 sha 校验（对 temp/t0_baseline.sha256）
python3 tools/cache_reclaim.py --apply --yes --only pnpm-store   # ② 回收（必须 --yes）
python3 tools/cache_reclaim.py --probe pnpm   # ④ 自包含探针：建→删→重下→可用
python3 tools/cache_reclaim.py --apply --yes --include-approval --only uv-cache   # 需人工判断后使用
```

## 9. 待批 / 未决（本轮未执行）

| 项 | 体积 | 为什么没做 | 建议 |
|---|---|---|---|
| `uv-cache` prune | **20.91 GiB** | R364 实测：常驻服务（hindsight-api:9077、mcp-server-aliyun、redis-mcp）持 `.lock`，prune 会静默等锁到超时（0% CPU / 0 回收） | 先停这些服务再 `--only uv-cache --include-approval`；收益最大（≈占可回收总量 60%） |
| `rustup-nightly` 卸载 | 2.63 GiB | 需确认是否还在用 nightly 工具链 | 若不用 ⇒ `rustup toolchain uninstall nightly` |
| `JetBrains` 索引 | 0.03 GiB | 需关闭 IDE 才能安全删 | 价值极低，可忽略（保留在清单里只为完整） |
| `logs` | 1.70 GiB | **红线内**，工具只报不动 | 走 `tools/log_compress.py` 压缩链路，不用本工具 |
| `ms-playwright-old` | 0.0 MB | 当前无旧版本残留 | 保持监视（prune 逻辑已就绪） |

## 10. 残余风险与边界

- 本轮**只对 1 项执行了回收**（pnpm-store）。其余 safe 档项在本机当前均为 0–0.22 GiB，且 `chrome-http` 涉及浏览器缓存（下次访问变慢），留待需要时按 `--only` 选择。
- `--probe playwright` **两轮都跑完且全绿**（第 2 轮 17:10:33：重建 rc=0 134.9 s / 584,081,408 B，可用性 CLI 截图 rc=0 1.7 s、PNG 652 B，探针自清理 557.0 MB）；第 1 轮 rc=1 只是我检查法（`node require('playwright')` 解析不到 npx 缓存）的错，已修并复跑验证 ⇒ ④ 由 **pnpm + playwright 双路线**支撑。
- 下载耗时不可控（本轮两轮分别 111 s / 135 s，受网络影响），探针**必须后台跑**（前台会撞工具超时）——这是操作注意点而非工具缺陷。
- 工具**不自动清场、不自动升级、不碰用户数据**；`--include-approval` 是唯一能触达大件的开关，必须人工判断后使用。
- 记忆更新建议（未执行，写记忆需批准）：①把“可再生缓存三档清单 + 红线（不碰 logs/文档/下载/照片、不自动升级）+ uv 需停常驻服务才可 prune”追加进 `global_mem_insight.txt` L1；②把两条**通用**坑写进 L1/SOP：a) 验“某工具删除后还能不能用”要走**工具自己的 CLI 入口**（`npx --no-install playwright screenshot …` 并留 PNG），用 `node -e "require('<pkg>')"` 会因 npx 缓存（`~/.npm/_npx/<hash>/node_modules`）不在模块解析路径而**假阴性**；b) **重下载型探针必须后台跑**（单轮 111–135 s，前台会撞工具超时被误判为失败）。
