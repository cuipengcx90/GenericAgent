# R439 macOS `shortcuts` CLI 单项能力实测（TODO#13）

- 日期：2026-09-27（快照时点 02:36–02:41，本机时间）
- 范围：只做 `shortcuts` 一项（按第16批评审要求砍掉 `log show`）
- 工具：`tools/shortcuts_cli_probe.py`（367 行 / 16876 B，纯 stdlib，只读优先）
- 原始输出落盘：`temp/shortcuts_probe/EVIDENCE.txt`、`tool_runs.txt`、`tool_runs2.txt`、`tool_runs3.txt`（均为带 `[ISO 时间戳]` 的原始记录）

## 0. 结论（TL;DR）

1. **三态判定 = 可用（AVAILABLE），但必须带硬护栏**：`/usr/bin/shortcuts run <name> -i … -o …` 在本机确实能跑通（有真执行痕迹），且**无需 Automation/TCC 授权**。
2. **`rc` 完全不可作为判据**：输入路径不存在时 **rc=0 且无 stderr、产物不落盘**（静默失败）。
3. **运行记录增量也只能证明「被调度」**，不能证明成功（坏输入那次同样落 1 条 `ZSHORTCUTRUNEVENT`）。⇒ 成功判据只能是**调用方要求的 `-o` 产物**。
4. **可无限挂起**：`Bob.Translate.v2` 收到 plist(字典) 输入后 workflow 真跑 10 个动作、全部 `error:false`，卡在需远程执行的 `WFTranslateTextAction`，>38 s 不返回、`-o` 不落盘 ⇒ 调用方必须自己设硬超时并清理进程组。
5. **本批不予立项**（验收④）：本机**唯一**用户快捷指令 `Bob.Translate.v2` 是翻译类，与在册数据源（`memory/*`、`R415/R430` 快照、`sche_tasks`）及现有待办**没有任何可对接关系**；按验收口径以负结论收尾，同时保留可复用调用示例（验收③）。

## 1. 环境与能力面（原始输出）

```
[2026-09-27T02:40:21] $ /usr/bin/shortcuts --version; echo rc=$?
rc=0
stdout='rc=64'
stderr="Error: Unknown option '--version'\nUsage: shortcuts <subcommand>\n  See 'shortcuts --help' for more information."
```

```
[2026-09-27T02:39:39] binary | /usr/bin/shortcuts exists=True
[2026-09-27T02:39:39] binary.stat | -rwxr-xr-x  1 root  wheel  386304 Aug 23  2024 /usr/bin/shortcuts
```

`help run` 给出的调用面（逐字）：

```
OVERVIEW: Run a shortcut.
USAGE: shortcuts run <shortcut-name-or-identifier> [--input-path <input-path> ...] [--output-path <output-path>] [--output-type <output-type>]
OPTIONS:
  -i, --input-path <input-path>   The input to provide to the shortcut.
  -o, --output-path <output-path> Where to write the shortcut output, if applicable.
```

清单（全机用户快捷指令只有 1 条）：

```
[2026-09-27T02:40:21] $ /usr/bin/shortcuts list --show-identifiers
rc=0
stdout='Bob.Translate.v2 (5E030E8A-9037-4D3C-AE67-8D7EA65C1D0B)'
```

## 2. 三态判定（验收②）

| 证据 | 命令 | 时间戳 | 结果 |
|---|---|---|---|
| 二进制存在 | `ls -l /usr/bin/shortcuts` | 02:39:39 | `exists=True`，386304 B |
| 无 TCC/Automation 拦截 | 直接 `run`（下节） | 02:36:33 起 | 未出现授权对话框，stderr 无 `-1743`/`not authorized` |
| 名字不存在 ⇒ 不可用 | `shortcuts run "__ga_nonexistent_9381"` | 02:40:21 | `rc=1`，stderr `Error: 未能完成该操作。找不到快捷指令` |
| 真执行 ⇒ 可用 | `shortcuts run "Bob.Translate.v2" -i in.txt -o out.txt` | 02:36:33/02:36:42 | `rc=0`；`ZSHORTCUTRUNEVENT` 计数 816→818 |

**判定：可用（AVAILABLE）**，档位说明：本工具额外区分 `NEEDS_AUTH`（命中 `-1743` / `not authorized` / 「需要授权」等特征时判「需授权」，且**不得**判可用）、`NOT_PROVEN`（rc=0 但零副作用/坏输入）、`HANG`（超时未返回）。本机实测未触发 NEEDS_AUTH 分支。

## 3. 「真执行」的判据，以及一个反例（重要）

真执行铁证（只读副本，按 RULE17 连 `-wal` 拷后 `mode=ro` 查询）：

```
[2026-09-27T02:40:21] runevent count=822 last=2026-09-27 02:40:21 sources=[('apple-event', 815), ('commandline', 7)] outcomes=[(0, 1), (1, 819), (2, 2)]
```

近期逐行明细（`snap2.sqlite` 只读）：

```
(822, '2026-09-27 02:40:21', 1, 'commandline', '7E3A6E22-2DCF-4620-A1A8-BF642366C909')
(821, '2026-09-27 02:39:54', 1, 'commandline', '84A0E8BA-B770-4599-97E5-03A63C6E9442')
(820, '2026-09-27 02:38:03', 1, 'commandline', '6986E9D8-2476-44AD-8162-4F87F24AED7E')
(815, '2026-09-25 16:19:38', 1, 'apple-event', '20AEB3E1-BB0C-4DF3-BC59-CB31A8260A31')
```

同时 `log show` 短窗内可见 `BackgroundShortcutRunner` 起停与 `Delivering workflow run result to client`。

**反例（决定了「增量不等于成功」）**：下面这次输入路径**根本不存在**，`shortcuts` 仍 `rc=0`、无 stderr、`-o` 不落盘，但**照样新增了一条运行事件**：

```
[2026-09-27T02:40:46] invoke | /usr/bin/shortcuts run Bob.Translate.v2 -i temp/shortcuts_probe/no_such_file.txt -o temp/shortcuts_probe/bad2.txt
[2026-09-27T02:40:46] result | rc=0 elapsed=0.16s timed_out=False out='' err=''
[2026-09-27T02:40:46] runevent | before=822 after=823 delta=1 last=2026-09-27 02:40:46
[2026-09-27T02:40:46] output | path=temp/shortcuts_probe/bad2.txt exists=False size=-
[2026-09-27T02:40:46] STATE | NOT_PROVEN（输入路径不存在（shortcuts 仍 rc=0 且无 stderr ⇒ 静默））
```

⇒ 落地到工具的判定顺序：`rc` 只用于识别「找不到指令」这类显式错误；**可用性必须由「所要求的 `-o` 产物存在」自证**，`runevent` 增量仅作辅助（且已知它证明不了成功）。

## 4. 硬风险：可无限挂起（必须由调用方设超时）

同一个快捷指令，输入换成 plist(字典) 后 workflow 真跑 10 个动作（全部 `error:false`），最终卡在需要远程执行的 `WFTranslateTextAction`：>38 s 不返回、`-o` 不落盘、rc 不返回。本工具用 8 s 硬超时复现并**同时**拿到证据：

```
[2026-09-27T02:39:54] invoke | /usr/bin/shortcuts run Bob.Translate.v2 -i temp/shortcuts_probe/in_dict.plist -o temp/shortcuts_probe/out.txt
[2026-09-27T02:40:02] result | rc=None elapsed=8.01s timed_out=True out='' err=''
[2026-09-27T02:40:02] runevent | before=820 after=821 delta=1 last=2026-09-27 02:39:54
[2026-09-27T02:40:02] output | path=temp/shortcuts_probe/out.txt exists=False size=-
[2026-09-27T02:40:02] STATE | HANG（调用超时未返回（已强杀进程组）⇒ 不可判可用）
```

清理后无残留（自有进程组 SIGTERM→SIGKILL）：

```
$ pgrep -fl 'BackgroundShortcutRunner|shortcuts run' || echo CLEAN
CLEAN
```

（更早一次同样的挂起已按精确 PID 清理；清理后未补记运行事件。）

## 5. 下次任务可直接调用的示例（验收③）

三条，均为真实可复跑命令（时间戳来自本次实测）：

**① 只读探活（零副作用，用于「这条通道还活着吗」）**

```
python3 tools/shortcuts_cli_probe.py probe
# 02:39:39 实测：binary exists=True；list rc=0 → Bob.Translate.v2 (5E030E8A-…)
#            runevent count=820 last=2026-09-27 02:38:03 copied_with_wal=True
```

**② 带硬护栏的调用（唯一被认可的用法）**

```
python3 tools/shortcuts_cli_probe.py run "<快捷指令名或 identifier>" \
        -i <输入文件> -o <输出文件> --timeout 15
# 判据只看最后一行 STATE：
#   AVAILABLE    ⇒ 可以当成功用（rc=0 ∧ runevent 有增量 ∧ -o 已落盘）
#   NOT_PROVEN   ⇒ 不得当成功（含坏输入 rc=0 静默这一实测形态）
#   HANG         ⇒ 已强杀进程组，重试或改用手工
#   UNAVAILABLE  ⇒ 名字/二进制问题；NEEDS_AUTH ⇒ 需人工授权
```

**③ 未来接入模板（当用户新建一条「产出文件」型快捷指令时）**

```bash
# 该快捷指令必须满足：①接受 -i 文件路径 ②用 -o 写产物 ③不依赖远程服务（本次挂起即因远程动作）
OUT="$GA_ROOT/temp/shortcuts_probe/<name>_$(date +%Y%m%d_%H%M%S).txt"
python3 tools/shortcuts_cli_probe.py run "<NAME>" -i "$IN" -o "$OUT" --timeout 30
# 只有 STATE=AVAILABLE 且 OUT 非空才进入下游流程；否则按 UNAVAILABLE/NOT_PROVEN/HANG 分类处理
```

## 6. 为什么本批「不予立项」（验收④）

- 本机 `list --show-identifiers` 只有 1 条：`Bob.Translate.v2`，功能=翻译；与在册数据源（`memory/**`、`temp/chrome_snapshots`、`temp/kc_snapshots`、`temp/pmset_snapshots`、`sche_tasks/*.json`）**无对接点**。
- 现存待办（`temp/TODO.txt`）中没有任何一项需要「调用用户自建快捷指令」；反过来也没有哪条快捷指令能消费现有产物。
- 因此按验收口径以负结论收尾：**本批不予立项**；但通道能力（三态判定 + 硬护栏 + 调用模板）已固化为工具，供未来用户新增「产出文件型」快捷指令时**零调研成本**接入（示例见 §5③）。

## 7. 工具与自检（验收⑤）

```
$ python3 tools/shortcuts_cli_probe.py selftest | tail -20
[2026-09-27T02:40:46] selftest.ts | probe
-- selftest 14/14 PASS
   PASS T1 rc0+有增量+产物在 ⇒ AVAILABLE
   PASS NC1 不存在快捷指令 ⇒ UNAVAILABLE
   PASS NC1b 英文 not found ⇒ UNAVAILABLE
   PASS NC2 无 Automation 权限 ⇒ NEEDS_AUTH（不得 AVAILABLE）
   PASS NC2b 中文授权文案 ⇒ NEEDS_AUTH（不得 AVAILABLE）
   PASS NC3 rc=0 零副作用 ⇒ NOT_PROVEN（不得 AVAILABLE）
   PASS NC3b rc=0 但读不到运行记录 ⇒ NOT_PROVEN
   PASS NC4 超时 ⇒ HANG（不得 AVAILABLE）
   PASS NC5 二进制缺失 ⇒ UNAVAILABLE
   PASS NC8 输入路径不存在 ⇒ NOT_PROVEN（不得 AVAILABLE）
   PASS T2 未要产物但真执行 ⇒ AVAILABLE
   PASS T3 连 -wal 副本读数正确
   PASS NC6 缺 -wal ⇒ 假空（必须少于真值，证明 RULE17 必要）  got=True nowal=0 full=2
   PASS NC7 原始输出行须带 [ISO 时间戳]
```

- 负控 7 条（NC1/NC1b/NC2/NC2b/NC3/NC3b/NC4/NC5/NC6/NC8 中，按「不得判可用」计 6 条 + 语料类 2 条），全部覆盖验收点名的三条：不存在名⇒不可用、无授权⇒需授权而**不得**可用、原始输出带时间戳。
- 只读边界：查询用户快捷指令库一律走**副本 + `file:…?mode=ro`**，连 `-wal/-shm` 一起拷（NC6 自证：缺 `-wal` 时读数 `nowal=0` vs `full=2`，即 RULE17 的「假空」）；`--selftest` 只用 `TemporaryDirectory` 内的合成库，不触碰真库。

## 8. 边界与残余

1. **结论只代表当次环境**：清单仅 1 条快捷指令、无 TCC 拦截；用户新增快捷指令或系统更新后须复跑 `probe`。
2. **`shortcuts` 无版本号**（`--version` rc=64），无法做版本级对账；只能记录二进制 mtime（2024-08-23 构建）与 `ls -l` 指纹。
3. **挂起根因属快捷指令自身**（远程动作 `WFTranslateTextAction`），不是 CLI 缺陷；本工具只能保证「不把调用方拖死」。
4. `ZOUTCOME` 语义未完全解码（本次全部 commandline 行为 1，另有 1 条 0、2 条 2 的古老 apple-event 行）⇒ 未用它做判据，仅记录分布。
5. 未接线定时/未接 `complete_task`（一次性实测，无需常驻）。
