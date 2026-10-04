# R538 anyrouter 探针三态归因与台账证据留痕

- 日期：2026-10-04
- 对象：`tools/anyrouter_slot_watch.py`（87183 B / 1631 行 / sha256:16 `f20c84bdec1f5401`）+ `tools/anyrouter_slot_watchd.py`（27784 B / 593 行 / `b13f449cbcf843f0`）+ `memory/anyrouter_quota_sop.md`（23860 B / 215 行 / `71827880f470c86b`）
- 提交：`99257b9`（两形状路由 + LINK_DOWN 链路证据门）、`3f640a2`（`[link=]` 尾标）、`fdce34c`（台账补 `snippet`/`face`/`shape_retry`）
- selftest：119 → **155 passed / 0 failed**

## 1 起因

用户给出站点消费日志截图，同一批请求被站点写成三种不同的失败：

1. `请求上游地址失败`
2. `invalid codex request`
3. `not implemented`

而我的台账当时每条只存 `http` + `verdict`（`detail` 在内存里存在、落账时被丢弃）⇒ 事后只能靠用户截图才能区分「号池满」「请求形状不对」「网关未实现」。**证据留痕不足，等于判据不可复核。**

## 2 三态判读框架（本报告的核心口径）

| 网关侧现象 | 物理含义 | 是不是「没额度」 |
|---|---|---|
| 网关记录到了这条请求 | 传输层是通的（本机 → 站点 OK） | 否 |
| `invalid codex request` | **请求形状不对**（客户端形状不匹配，非额度） | 否 |
| `请求上游地址失败` | **上游供给态**（号池满 / 无可用上游） | 否 |

即：这三种失败**全都不是额度问题**；额度问题的唯一信号是 `402`。三者不可混判，关键在于把「传输层 / 形状层 / 供给层」分开留痕。

## 3 改动

1. **两形状路由**（`99257b9`）：按模型选请求形状 ——
   - claude 系 → CC 形状（`face=messages`）
   - gpt-6-astra → codex 形状（`face=codex`）
2. **LINK_DOWN 链路证据门**（`99257b9`/`3f640a2`）：`http=0` 时行尾打 `[link=ok/down]`，把「链路断」与「排队但无上游」分开（24 用例 / 5 负控）。
3. **台账证据留痕**（`fdce34c`）：新增纯函数 `_ledger_snippet(r)`，`results` 每条补三键 `snippet` / `face` / `shape_retry`：
   - `200` ⇒ `None`（**绝不落生成正文**）
   - 非 200 ⇒ `detail` 空白归一后**截断 160 字**
   - 无 `detail` / 全空白 ⇒ `None`（不造假证据）

## 4 实证（首轮新码，10:20:20 落账，台账 190 行）

形状分流生效、网关原文可事后回读：

- `claude-opus-4-8` → `face=messages`，`http=429`，`verdict=POOL_BUSY`，`shape_retry=False`
  - snippet（逐字，截断）：`{"error":{"message":"Service Unavailable","type":"error"},"type":"error"}`
- `gpt-6-astra` → `face=codex`，`http=500`，`verdict=POOL_BUSY`，`shape_retry=False`
  - snippet（逐字，截断）：`{"error":{"message":"当前模型 gpt-6-astra 负载已经达到上限，请稍后重试 (request id: 20261004102022888748526dlTFC6z3)","type":"ne`

即：7 个 claude 名走 `messages` 面、`gpt-6-astra` 走 `codex` 面，两条链路的失败原因**现在能各自回读原文**；`500 + 负载已经达到上限` 归 `POOL_BUSY`，与 R537 判据（上游供给态）一致。

## 5 边界与残余

- **改判据必须重启才生效**：watchd 只在启动时读代码（SOP §3.3 四步：停 watchd → 杀残留探测 → 起 watchd → 核锁与 START）。
- snippet 是**人工复核通道**，不参与 `verdict` 判定；判据仍以 `http` 为准（防站点文案漂移）。
- `face` / `shape_retry` 的价值在于事后区分「形状自愈成功」与「形状自愈重试仍失败」。
- 本轮无额度类事件（无 `402`），三态框架下的真值暂无实测样本，其判据由 selftest 负控覆盖。

## 6 可复跑命令

```bash
python3 tools/anyrouter_slot_watch.py --selftest              # 155 passed / 0 failed
tail -1 temp/anyrouter_watch/ledger.jsonl | python3 -c "import json,sys;r=json.load(sys.stdin);print([(x['model'],x['http'],x['verdict'],x['face'],x['snippet']) for x in r['results']])"
tail -3 temp/anyrouter_watch/watch.log
```
