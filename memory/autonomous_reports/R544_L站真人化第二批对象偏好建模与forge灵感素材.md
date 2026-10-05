# R544 L站真人化第二批：对象偏好建模 + forge 灵感素材 + 静默窗解包修复

- 日期：2026-10-06
- 提交：GA `c9d91ae`（父 `08cbdd3`）「L站真人化第二批: 对象偏好建模 + forge 灵感素材 + 静默窗解包修复」
- 变更 5 文件 `1216 insertions(+), 17 deletions(-)`（逐文件 numstat 见 §1）
- 承接：R543（去AI味闸门 + 发帖草稿锻造）与用户 2026-10-06 的「对象偏好建模 / 一、二、三要做」指示

## 1. 交付清单（numstat 原文）

```
15      0       memory/linuxdo_acts_sop.md
388     12      tools/linuxdo_acts.py
11      2       tools/linuxdo_antidetect.py
103     3       tools/linuxdo_post_forge.py
699     0       tools/linuxdo_prefs.py      (新增文件)
```

现况行数（当场 `wc -l` 口径）：`tools/linuxdo_acts.py` 1560 行、`tools/linuxdo_post_forge.py` 667 行、`tools/linuxdo_prefs.py` 699 行。

## 2. ①对象偏好建模：`tools/linuxdo_prefs.py`（新增，699 行）

- **定位**：把「像人一样有偏好」做成可复算的模型层——从**本地互动台账**学偏好，供执行器选题/选对象时加权，不再全靠随机。
- **自测**：`python3 tools/linuxdo_prefs.py --selftest` → **41/41 passed**（T1–T24c）。
  - 覆盖：去重/超窗/半衰期衰减（T5/T5b）、陌生 vs 熟人（T6/T6b/T6c/T12）、偏好板块优先选中（T8b `0.78>=0.70`）与**负控** T8d（无偏好模型必须无偏好 `0.49<=0.60`）、探索态非全 dev（T9）、当日已互动话题降权（T10）、排除自己（T11）、惩罚标记（T13）、空候选/空楼层/n=0（T14 系列）、坏行不崩（T15）、空模型 fresh 兜底（T16）、沙箱 ≠ 生产（T19）、只取 DONE 且去重（T20）。
  - **键正名（本件最值钱的一条）**：T22/T24c 明证权重键必须是 `category_id`，把**板块名当键**会把权重拆散（T24c 是负控）；名只进 `labels`，未知名回 `cat:<id>` 兜底不猜（T23/T24）。
- **接线**：`tools/linuxdo_acts.py` 中 `prefs` 出现 15 次（15 行），新增 `prefs_model()`(L594) / `prefs_rank_topics()`(L604) / `prefs_note_interaction()`(L618)，回复目标选择、互动回写均走模型；`fetch_self_user()`(L575) 供「排除自己」判定。
- **冷启动实况（读数，勿当失败）**：`python3 tools/linuxdo_prefs.py show` → `互动 0 次/对象 0 个 | 偏好板块 - | 熟人 - | 活跃时段 -`；`temp/linuxdo_prefs/state.json` 当前**不存在** ⇒ 模型为空是「没人互动过」的正常初始态，首批真实互动后才有内容。

## 3. ②forge 灵感素材：浏览过的帖子当「气口」

- 新增 `browse_inspiration()`（`tools/linuxdo_post_forge.py`，新增 defs：`browse_inspiration`、`_draft_prov`），复用对象偏好模块的 `read_browse_titles()` 从**浏览台账**取近期标题。
- **真跑读数（只读本地 jsonl，零站点请求）**：`browse_inspiration(limit=12)` → `n=12`，样例逐字：`测了下重置后日区pro的额度（20）`、`6.1 sol 巨慢（4）`、`小狼毫Rime输入法·中国色主题（第二弹）（20）`、`求PT馒头邀请码*1（83）`（应读作「社区在聊什么」，非我方素材）。
- **防洗稿边界（写进 prompt 与 SOP）**：浏览标题**只作气口/切口参考，不作素材本体**——正文仍必须来自真实素材正文（`provenance.material` + `material_sha` 不变）；prompt 明文「可以顺着这个气口选切口，但不是抄别人的帖子，内容仍必须来自上面那段记录；蹭不上就别硬蹭」。
- 可追溯：草稿 `provenance` 新增 `inspire_n`（本次注入几条灵感；0=未用），`--no-inspire` 可关、默认开。
- 自测：`python3 tools/linuxdo_post_forge.py selftest` → **28 checks, 0 failed**（含「灵感段只在 inspire=True 时入 prompt」的 sentinel 探针 + 读草稿断言 `inspire_n`）。

## 4. ③回访自己帖（revisit）接线口径

- 能力在 `tools/linuxdo_acts.py`：`revisit_due()` / `act_revisit()` / `revisit_new_posts()` / `revisit_own_posts()` / `cmd_revisit`，只读**新楼层**（进度记 `state.revisit[str(tid)].last_num`，幂等），默认 dry-run，`--apply` 才真动。
- **触发面与五动作一致**：①提示词临时触发；②daemon 随机段内消费。**不新增独立定时任务**（守「禁固定时刻+固定间隔」铁律）。
- **不做无条件周期回访**：不给 daemon 加「每 N 小时必回访」循环；发帖也不会自动标记回访（`act_post()` 不写 `revisit.last_ts`），避免「一发帖就必然产生一批可预测回访流量」。revisit 不进等级缺项路由。
- 落文：`memory/linuxdo_acts_sop.md` §11.7。

## 5. ④静默窗解包修复（真缺陷，凌晨必崩）

- `tools/linuxdo_antidetect.py`(+11/−2)：`next_wait()` 的 `quiet_window` 分支原把 **4 元组当 2 元组解包** ⇒ 01:00–07:30 之间真调用必 `ValueError`（源码注释已标「见 selftest T23b 负控」）。已修为 `_qh, _qm, eh, em = p["quiet"]`。
- 新增回归 **T21d/T21e**（T21d 静默窗内必须给正等待且 `why=wait_quiet_end`；T21e 窗右边界外不得走该分支）。自测 **58/58 PASS**。
- 修复后当场探针（`st` 空态、kind=browse）：
  - `next_wait @03:00` → `(16575.588936714455, 'wait_quiet_end')`
  - `next_wait @08:00` → `(11214.579772964178, 'wait_next_segment')`
  - `quiet_now 03:00` → `True`；`quiet_now 08:00` → `False`
- 因此本批重启 daemon 落在静默窗内，日志行为「静默窗内，睡 30min」属**预期**（旧码在此时刻会直接异常退出）。

## 6. SOP 订正与新增（`memory/linuxdo_acts_sop.md`，+15/−0）

- 新增 §11.6「选题灵感来源：浏览过的帖子」——含台账**真实路径订正**：`temp/linuxdo_browse_daemon/browse_log.jsonl`（旧文写作 `linuxdo_browse/…`，与实际落点不符）；零站点边界、防洗稿边界、`inspire_n`、实测 12 条与 `28 checks / 0 failed`。
- 新增 §11.7「回访自己帖（revisit）的接线口径」——默认 dry-run、触发面两条、无条件周期回访**不做**的理由。

## 7. daemon 运行态与台账实况（当场读数）

- 进程：`python3 tools/linuxdo_browse_daemon.py watch` pid **32134**，`lstart = Tue Oct  6 01:02:46 2026`（检查时 `etime=10:43`）；`tools/bg_task.py status --name linuxdo_browse` → `RUNNING  pid=32132  log=47B 运行中`（32132 为 bg_task 监督壳，32134 为真身）。
- 台账（`temp/linuxdo_browse_daemon/`）：
  - `browse_log.jsonl` 22984 B / 118 行，状态计数 `DONE 105 / LOITER 9 / FAIL 4`，末行 `{"ts": "2026-10-06 00:15:14", "st": "FAIL", "id": "2984919", "cat": 4, ...}`
  - `state.json`：`daily = {"2026-10-05": 102, "2026-10-06": 3}`
  - `risk_log.jsonl` 1 行：`{"ts": "2026-10-05 19:29:29", "st": "RISK", "kind": "FORBIDDEN", "detail": "page=8 status=403 err=None", "count_today": 1}`
- **读数解读（不计入本次代码缺陷）**：403 那次已被风控保险丝识别并留痕，当日仍完成 102 次浏览 ⇒ 属站点对个别页面的一次拒绝，非封禁（无 BANNED/RATE_LIMIT 记录）；4 条 FAIL 亦为个别挑帖失败。两者均作为**观察项**登记，不改判据。

## 8. 验证矩阵（全部当场复跑）

| 项 | 命令 | 结果 |
|---|---|---|
| 偏好模型 | `python3 tools/linuxdo_prefs.py --selftest` | 41/41 passed |
| 执行器 | `python3 tools/linuxdo_acts.py --selftest` | 65/65 PASS |
| 随机内核 | `python3 tools/linuxdo_antidetect.py --selftest` | 58/58 PASS |
| 锻造器 | `python3 tools/linuxdo_post_forge.py selftest`（**位置参数**，写 `--selftest` 报 rc=2） | 28 checks / 0 failed |
| 灵感读真台账 | `browse_inspiration(limit=12)` | n=12 |
| 静默窗探针 | `next_wait(st, 03:00, "browse")` | `(16575.59, 'wait_quiet_end')` |
| 提交卫生 | `git diff --numstat HEAD^ HEAD` | 5 文件 1216+/17−，与预期逐项相符 |
| 工作区残留 | `git diff --numstat` | 仅他会话 4 文件（`.ga/l2/index.json`、`.ga/l2/index.md`、`project_update_monitor/state.json`、`reflect_scripts/.price_history.json`） |

## 9. 边界与未接线（诚实清单）

1. **偏好模型当前为空**（`show` = 0 次互动）：效果要在首批真实回帖/点赞之后才能观测；本批只保证模型层可算、可测、键正确。
2. **灵感只作切口**：若某期素材与社区话题完全无关，`inspire_n` 可为 0 且属正常。
3. **revisit 无自动触发**：只有提示词或 daemon 随机段能起；不做周期回访。
4. **本轮未接任何定时任务**，也未新增 sche_tasks；唯一在册 L 站定时仍为 `linuxdo_trust_level_check.json`（纯本地保活、零站点）。
5. **未做**：CDP 可信输入（用户已裁「先不做」）、单元级 pytest 套件（工具自带 selftest 已覆盖，未进 run_tests）。

## 10. 复现命令

```bash
cd /Users/cuip/projects/GenericAgent
python3 tools/linuxdo_prefs.py --selftest && python3 tools/linuxdo_prefs.py show
python3 tools/linuxdo_acts.py --selftest
python3 tools/linuxdo_antidetect.py --selftest
python3 tools/linuxdo_post_forge.py selftest
python3 tools/bg_task.py status --name linuxdo_browse
git log -1 --format='%h %s' && git diff --numstat HEAD^ HEAD
```
