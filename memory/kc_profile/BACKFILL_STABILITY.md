# kc 迟写回填「稳定期」口径说明（R462 实测登记）

> 本文件是 TODO L173 的落点之一。**仅登记实测结论与引用规则，未改任何消费者代码**
> （把该规则写进 `tools/kc_app_usage.py` / `tools/sleep_source_crosscheck.py` 的窗口定义 = 待批台账
> `temp/pending_decisions/DECISIONS_R462_kc_stability.md` 的 D1；把 `--strict` 接进每日采集后置 = D2）。

## 1. 规则（可引用）

用 kc 的时长 / 熄屏量时：

1. **绑快照时点照旧** —— kc 任何度量只按其所属快照时点引用，**禁止跨源比**（pmset/睡眠量、R426 闸门值等一律不并入）。
2. **丢掉窗口最老 1 天** —— 评估窗口的最老一天会随快照变新而被按 ts 剔除（`EVICTION_HEAD`），
   它不是「回填」，但会让该日三点值不同；引用窗口起止时排除它即可得到三点逐字相同的稳定面。

## 2. 实测依据（2026-09-28，三份快照 2026-09-22 / 09-23 / 09-25）

- 输入：`temp/kc_snapshots/{2026-09-22,2026-09-23,2026-09-25}/knowledgeC.db`（均 13119488 B，含 `manifest.json`）。
- 评估面 24 天（`usable_obs ≥ 2`），**23 天**在三个时点的前台时长**逐字相同**（最大相对差 0.000%）。
- 唯一非零：**2026-09-01**，7.112 → 7.112 → 4.539 h（最大相对差 **36.2%**），
  判定 `EVICTION_HEAD`：窗口最老日的头部被按 ts 剔除，2.573 h 逐 app 对得上 Δfg。
- `changing_real = []`，总判定 `EVICTION_ONLY`。
- **同日的旧记录订正**：09-22 在 09-23 与 09-25 两份快照中逐字相同（7.600 h）；09-22 快照值为 0.844 h，
  因其采集于当日 10:49（`TRUNCATED` 截断面）而非回填。旧 L1 记的「同日 09-22 随快照 0.90→5.84→11.78 h」
  **在本轮三快照中不可复现**（无 11.78 对应值）；若确曾发生，其窗口早于现有快照面 ⇒ 本工具只做时点间比较，看不到。

## 3. 能回答 / 不能回答

- 能：在这三个时点之间，除窗口头剔除外**没有可见变化**（`T+k` 在本例中 k ≤ 2.72 d、`max_age_days = 23.95`）。
- 不能：给出「第 1 天到第 k 天的连续衰减曲线」（只有 3 个时点、09-01→09-22 之间 14 天无采样）；
  不能外推到 09-22 之前；8 个无效日（`usable_obs < 2`）不参与判定。

## 4. 复跑

```bash
python3 tools/kc_backfill_stability.py --selftest                     # 33/33（NC1–NC21）
python3 tools/kc_backfill_stability.py points    --root .
python3 tools/kc_backfill_stability.py stability --root . --readonly-proof \
        --json temp/r462/stability_2026-09-28.json
python3 tools/kc_backfill_stability.py stability --root . --strict     # 出现真变化才 rc=1
```

- 工具：`tools/kc_backfill_stability.py`（746 行 / 39504 B / sha256 `50f8ef0cbed88eb7bdd887492eb347f3d28584985f36cc20dcad4df4f67a3986`）。
- 只读自证：`--readonly-proof` 声明 3 份快照副本 sha256 前后一致；同参数复跑得到逐字节相同的
  `stability_2026-09-28.verify.json`（sha256 `d181b7c017d96882e3f2c87f9ce970466d5d4492e873f83ca555cad56b6a844c` / 33821 B）。
- 负控：篡改快照副本后复跑 ⇒ `INTEGRITY_FAIL` rc = 2（矩阵见报告 §5.3）。
- 完整报告：`memory/autonomous_reports/R462_kc迟写回填稳定期实测.md`。
