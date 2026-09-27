# R452 · herdr-agents「revision-consistent snapshot」契约层最小子集

> 任务来源：`temp/TODO.txt` 第 18 批 C3（三路评审 codex/槽3/槽4 = 8/8/4 = 6.67）
> 落点：`herdr-agents:control-plane/snapshot_v0/` + `docs/product/F5_REVISION_SNAPSHOT_SPEC.md`
> 提交：`6b3f6ab`（6 文件 +881 / −7），已 push `main`（`1b3dc39..6b3f6ab`）

---

## 1. 做了什么（一句话）

给「投影视图」钉一条契约：**快照必须绑 `source_revision`，分页键是 `(revision, cursor)`；
跨 revision 续页一律拒绝，绝不返回物理上不存在的混合视图**。本批**零生产调用点**——
`board.py` / `kpi_real.json` / UI **一个字都没改**。

## 2. 先答评审分歧（槽4：触发条件未成立⇒只落契约不落代码）

TODO 里写死的前置动作是「执行前先量一次真实投影页数」。实测（2026-09-27）：

```bash
$ python3 control-plane/autonomy_v0/board.py --out /tmp/board_r452.md
已生成 /tmp/board_r452.md（14 条记录：真装置 9 / 模拟 5）
分列：等待人 7 · 阻塞 0 · 待复核 1 · 可跑 0 · 已认领 1 · 观察中 0 · 完成 5
$ wc -l /tmp/board_r452.md
     480 /tmp/board_r452.md
```

| 口径 | 实测 | 是否已越「一屏」 |
|---|---|---|
| `runs/` 的**记录**数 | 14 条 | 否（单屏内） |
| `board.py` **渲染**出的视图 | 480 行（38024 B） | **是，约 12× 一屏** |

即：**记录数还没越屏，但渲染出的整视图早就越屏了**——离线消费（人读 / 外部工具抓）
拿到的就是这一整块，中途没有任何 revision 概念。

裁决：**落契约 + 最小实现，但不接线**。理由是那条不变量约束的是「**读视图的人**」，
而越屏的是视图不是记录；且契约层落地不依赖 `board.py` 改造，可先被评审判。
（`--out` 给 `.md` 后缀也照样写 HTML——扩展名不决定内容。）

## 3. 交付物与实测

### 3.1 新模块 `control-plane/snapshot_v0/snapshot.py`（440 行 / 20243 B）

CLI 四子命令：`fingerprint`（打源当前 revision）/ `build`（生成快照）/ `read`（按 `(revision, cursor)` 取一页）/ `selftest`。

- 生成端：扫源目录 → 逐文件 sha256 → `source_revision` = 源指纹 → 落快照 JSON
  （`schema=ga.snapshot/v1`，另带 `records_digest` 自证）。
- 读取端三道闸门（任一不过 ⇒ **rc=2，`records==[]`，`completion=INCOMPLETE`**）：

| 闸门 | code | 触发 |
|---|---|---|
| 1 快照已过期 | `SNAPSHOT_STALE` | `--live-source` 现场重算的 revision ≠ 快照的（**源在翻页期间被写入**） |
| 2 revision 不符 | `REVISION_MISMATCH` | 调用方声明的 `--revision` ≠ 快照的 |
| 3 跨 revision 续页 | `CURSOR_REVISION_MISMATCH` | cursor 里封的 rev ≠ 本次请求的 rev |

另两条：`BAD_CURSOR`（cursor 解不开——**不静默从头开始读**）、`SNAPSHOT_TAMPERED` / `BAD_SNAPSHOT`
（自证不过 / 读不动，一律 rc=2，**不退化成裸异常 rc=1**）。

`--unsafe-skip-revision-check` 是**故意留着**的：让套件能**直接反证**「挡住混合视图的是那道闸门」——
关掉它，同一场景真的返回旧快照的行（一个当场读源得不到的视图）。

### 3.2 套件 `test_snapshot.py`（283 行 / 11164 B，19 个用例）

```bash
$ cd control-plane/snapshot_v0 && python3 -m pytest -q
19 passed in ...
$ python3 control-plane/snapshot_v0/snapshot.py selftest
...
[PASS] NC7 手改 records ⇒ 必须报 TAMPERED
[PASS] T3 read 只读（快照与源指纹前后一致）
selftest: 13/13 passed
```

三类**必测负控**（TODO 验收①②）全落：
①翻页期间源被写入 ⇒ 续页 rc=2 `SNAPSHOT_STALE`、`records==[]`；
②关掉闸门后同一场景**真的**返回旧行（证明是闸门在挡，不是恰好没数据）；
③陈旧快照的**首页**同样 rc=2（不是只在续页拒）。
另加：只改内容不新增文件也过期（灵敏度）/ revision 不符 / 跨 revision cursor / 坏 cursor /
手改 records / 快照缺失 / 缺必填参数 / 全部拒绝共享同一形状 / **read 只读**（快照与源指纹前后 sha256 一致）。
去掉 revision 校验即必红的对照由 NC1↔NC2（`test_nc_without_gate_the_mixed_view_really_happens`）承担。

### 3.3 规格 `docs/product/F5_REVISION_SNAPSHOT_SPEC.md`（134 行 / 8093 B）

含 §1 为什么只做子集（含上表）/ §2 数据形状 / §3 三道闸门 / §4 拒绝语义 /
**§5 `## Authority boundary`（逐字二级标题，供 `tools/authority_boundary_check.py` 机械校验）** /
§6 未接线清单 / §7 判据与负控 / §8 后续触发条件。

Authority boundary 三条要点：**快照不是权威状态**（权威仍在装置事件账本）；
**`OK` 不等于「源没变过」**（只在给 `--live-source` 时才判过期）；
**未接线 ≠ 已生效**（零生产调用点，现在没有任何看板/UI 行为因此改变）。

### 3.4 接线面与门禁

- 零生产调用点（grep 自证）：`grep -rn 'snapshot_v0' --include='*.py' --include='*.sh' . | grep -v 'snapshot_v0/'` ⇒ **0 行**。
- 进 CI：`run_tests.sh` 按 `control-plane/*/` **glob 自动收录**，新套件无需改脚本。
  `PYTHON=/opt/anaconda3/bin/python3 bash run_tests.sh` ⇒ **`suites run: 23 / ALL SUITES PASSED`**。
- `bash control-plane/stage_gate_check.sh` ⇒ **`steps: 22  FAIL: 0`**。
- `python3 tools/doc_state_check.py --strict` ⇒ rc=0（`stage-markers fails=0 hints=4`）。
- `python3 tools/authority_boundary_check.py` ⇒ `fails=0 specs=22 missing=20 grandfathered=20`（F5 自带该节）。
- `python3 tools/residual_registry_check.py` ⇒ `fails=0 rows=10`。
- `python3 control-plane/derive_guard_v0/derive_guard.py check` ⇒ `MANUAL=2 OK=4`
  （`l1_index` 与 183 个输入均未变 / `board_html` 与 60 个输入均未变）。

## 4. 提交与仓库卫生

```
$ git add -- control-plane/snapshot_v0 docs/product/F5_REVISION_SNAPSHOT_SPEC.md \
    control-plane/README.md docs/product/FUTURE_CAPABILITIES.md control-plane/derive_guard_v0/manifest.json
$ git commit -m "F5: snapshot_v0 revision-consistent 分页契约层最小子集（…）" -- <同上 5 条路径>
[main 6b3f6ab] 6 files changed, 881 insertions(+), 7 deletions(-)
$ git show HEAD --numstat --format=
1       0       control-plane/README.md
9       4       control-plane/derive_guard_v0/manifest.json
440     0       control-plane/snapshot_v0/snapshot.py
283     0       control-plane/snapshot_v0/test_snapshot.py
134     0       docs/product/F5_REVISION_SNAPSHOT_SPEC.md
14      3       docs/product/FUTURE_CAPABILITIES.md
```

行数与预期逐项相符（README +1 / manifest 9-4 / 440 / 283 / 134 / 14-3）。
带 pathspec 提交是刻意的：当时共享 index 里有**他会话已 staged 的
`control-plane/*/runs/*.json`** 与 `.gitignore`/`AGENTS.md`/`CHANGELOG.md`/`stage_gate_check.sh`/
`ROADMAP_MASTER.md`/`STAGE2_DECLARATION.md` 的工作区改动，一条都没被吞（提交后 `git status` 仍在）。
另：`l1_map` 输入变了 ⇒ 重建索引后**只对 `l1_index` 重新盖章**（`manifest.json` 9+/4−），
否则 `derive_guard check` 报 RESYNC。

## 5. 踩到的坑（可复用）

1. **量渲染行数会污染 tracked 派生件**：`board.py` 不带 `--out` 时**默认写 `control-plane/autonomy_v0/board.html`**。
   我为了量行数跑了一次默认路径，`derive_guard check` 立刻报
   `[TAMPER] board_html 产物被手改（上游输入无变化可解释）` ⇒ 做这类**只读测量必须带 `--out /tmp/...`**。
   已 `git checkout -- control-plane/autonomy_v0/board.html` 还原，复跑 `MANUAL=2 OK=4`、阶段门 `FAIL: 0`。
2. `--out x.md` 不改变输出格式（板子仍是 HTML）⇒ 别按扩展名判断产物类型。
3. 新文件（未跟踪）必须先 `git add` 才能被 `git commit -- <pathspec>` 匹配（R446 同坑）。
4. `git push` 首跑 `Connection closed by 198.18.0.60 port 22`（Clash TUN 假 IP 瞬断），**重试即成功**，
   不是权限/远端问题（`1b3dc39..6b3f6ab main -> main`）。
5. `selftest` 子命令**不带横线**（`python3 snapshot.py selftest`）；`--selftest` 会 rc=2（argparse 拒绝）。
6. 本机默认 `python3` 无 pytest ⇒ 全仓套件必须 `PYTHON=/opt/anaconda3/bin/python3`。

## 6. 未做（别当成做了）

`board.py`/`kpi_real.json` 接 revision+分页、UI 翻页控件、增量游标、
快照与 `teamctl` 事件面的一致性校验（需装置侧订阅改造）、多进程并发 `build` 的锁、
定时重建/缓存目录约定 —— 六项均列在 F5 §6，**本批一律不碰**。

**触发条件**（F5 §8）：记录数真正越屏，或看板开始被外部按页消费。届时按依赖顺序做三件：
①`board.py` 输出加 `source_revision`+cursor（输出契约变更，需评审）；
②`kpi_real.json` 写快照带 revision（**复用本模块的 `source_revision` 口径，别另发明一套**）；
③负控「跨 revision 续页必拒」进 `board` 的套件（本模块已有同形负控可搬）。

## 7. 残余风险

- 契约层**无生产调用点** ⇒ 现在它挡不住任何真实混合视图（也正因如此它零风险）。
- `build` 单写者假设（临时文件 + `os.replace` 原子落盘）；并发写未定义，接线时须补锁。
- 「源没变」的判断只在显式传 `--live-source` 时生效；不传时是「相对快照自身一致」，
  规格 §5 已写明这条**不等于**新鲜度承诺。
