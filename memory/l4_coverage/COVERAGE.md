# L4 会话归档覆盖度审计

- 语料根：`/Users/cuip/projects/GenericAgent`
- 窗口：2026-04-03 .. 2026-09-29（180 天）
- 归档边界（权威面最大覆盖日）：`2026-09-28`
- 归档起点（权威面最小覆盖日）：`2026-04-22`

## 判据

覆盖仅由**权威面**授予：L4 zip 的会话成员名（`MMDD_HHMM-MMDD_HHMM.txt`）与
`all_histories.txt` 的 `SESSION:` 结构头。滚动面（`model_responses` / `desktop_sessions` /
`logs_live` / `logs_archive`）只作旁证——它们含跨日回声（命令输出、报告正文、记忆文本被回显），
实测能把 2020/2027 之类日期刷进命中集，故一律不授予覆盖。

## 汇总

| 指标 | 值 |
| --- | --- |
| 覆盖日 | 160 |
| 空洞日 | 19 |
| — 其中早于归档起点（无从严要求） | 19 |
| — 其中归档面内真空洞 | 0 |
| 待归档日 | 1 |
| 空洞日中有报告编号 | 0 |
| 单源薄覆盖日 | 45 |
| 跳过文件 | 1 |

## 各留存面区间

| 面 | 类型 | min | max |
| --- | --- | --- | --- |
| `all_histories` | 权威 | 2026-04-22 | 2026-09-28 |
| `l4_zip` | 权威 | 2026-04-22 | 2026-09-28 |
| `desktop_sessions` | 提示 | 2021-12-15 | 2026-09-25 |
| `logs_archive` | 提示 | 2001-01-01 | 2056-10-21 |
| `logs_live` | 提示 | 2001-01-01 | 2056-10-21 |
| `model_responses` | 提示 | 2001-01-01 | 2056-10-21 |

## 空洞清单

| 日 | 关联报告 | 提示面 |
| --- | --- | --- |
| 2026-04-03 | — | model_responses|logs_live|logs_archive |
| 2026-04-04 | — | model_responses|logs_live|logs_archive |
| 2026-04-05 | — | model_responses|logs_live|logs_archive |
| 2026-04-06 | — | model_responses|logs_live|logs_archive |
| 2026-04-07 | — | model_responses|logs_live|logs_archive |
| 2026-04-08 | — | model_responses|logs_live|logs_archive |
| 2026-04-09 | — | model_responses|logs_live|logs_archive |
| 2026-04-10 | — | model_responses|logs_live|logs_archive |
| 2026-04-11 | — | model_responses|logs_live|logs_archive |
| 2026-04-12 | — | model_responses|logs_live|logs_archive |
| 2026-04-13 | — | model_responses|logs_live|logs_archive |
| 2026-04-14 | — | model_responses|logs_live|logs_archive |
| 2026-04-15 | — | model_responses|logs_live|logs_archive |
| 2026-04-16 | — | model_responses|logs_live|logs_archive |
| 2026-04-17 | — | model_responses|logs_live|logs_archive |
| 2026-04-18 | — | model_responses|logs_live|logs_archive |
| 2026-04-19 | — | model_responses|logs_live|logs_archive |
| 2026-04-20 | — | model_responses|logs_live|logs_archive |
| 2026-04-21 | — | model_responses|logs_live|logs_archive |

## 待归档日（晚于边界，不计空洞）

2026-09-29

## 结论（由本函数机械生成，勿手改）

- 归档面窗口：`2026-04-22 .. 2026-09-28`（160 天）；窗口内覆盖 160 天、真空洞 0 天
- 窗口全幅 2026-04-03 .. 2026-09-29（180 天）：覆盖 160 天、空洞 19 天 = 早于归档起点 19 天（无从要求）+ 归档面内真空洞 0 天；待归档 1 天（L4 滚动滞后，不计空洞）
- 双权威面：`l4_zip` 2026-04-22 .. 2026-09-28、`all_histories` 2026-04-22 .. 2026-09-28
- 提示面（滚动日志/响应）区间：`logs_archive` 起 2001-01-01 .. `desktop_sessions` 止 2021-12-15 —— 含跨年回声，只作旁证，**不授予覆盖**

### 消费决策（每条给出「未来决策 ← 指标」映射）

1. 要回源某日**原话**：以 `l4_zip` 成员名（`MMDD_HHMM-MMDD_HHMM.txt`）定位 zip 内文件；
   `all_histories.txt` 的 `SESSION:` 头只证明「该日存在会话」，不证明正文可取。
2. 要判「是否该补归档」：看**归档面内真空洞**（0 天）与**单源薄覆盖**（45 天）两个数；
   早于归档起点的空洞不构成补归档诉求（该窗口本就没有归档面）。
3. 要判「有没有丢原话」：只有归档面内真空洞 + 该日有关联报告编号（0 天）才值得逐日追；
   本窗口该数为 0/非 0 即为判据落点。
4. 审计窗口起点应取归档起点；早于它的日期一律标 `before_archive_start`，避免读成「丢了 N 天会话」。

