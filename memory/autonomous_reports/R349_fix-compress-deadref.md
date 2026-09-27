# R349 修复 helper.compress_and_archive() 死引用（TODO 第 44 行）

> 任务：`[ ] 工程 | helper.compress_and_archive() 的死引用缺陷（R348 顺带发现）`
> 结论：**已闭环，验收达标**。走 TODO 给出的「恢复脚本」路线（非删功能），
> 并用「夹具三层 + 真实链路字节级」验证无损；唯一的真实树改动 `tools/log_compress.py`
> 已入 git（`22e90c8`）。helper 第 29 行改指 `tools/` 的加严提案单列待批（§6）。

---

## 1. 现象

`helper.complete_task()` → `compress_and_archive()` 每轮收尾都输出：

```
[WARN] compress_log.py 不存在: /Users/cuip/projects/GenericAgent/temp/compress_log.py
```

即「日志压缩」功能**从未真正生效过**，只贡献了一条噪音警告。

## 2. 根因与证据链

| # | 事实 | 证据 |
|---|------|------|
| 1 | `helper.py:29` 写死 `_COMPRESS_SCRIPT = _TEMP_DIR / "compress_log.py"` | 源码第 29 行 |
| 2 | 该路径全仓不存在、**git 历史里也从未存在过** | 全仓 `find/grep` 为空；`git log --all -- '*compress_log.py'` 无记录；与 helper 同批引入的 commit `279ee88 (2026-06-08)` 即「写死引用 + 从未落盘」 |
| 3 | 同类名 `compress_session.py`（R220/R248）是 **llmcore 会话上下文压缩**，与日志无关 | 读该文件头 |
| 4 | 目标日志是 launchd plist `com.cuip.genericagent-autonomous` 的 `StandardOutPath/StandardErrorPath` | `~/Library/LaunchAgents/*.plist`；`lsof` 显示当前 reflect 进程持有 fd1/fd2 |
| 5 | 被压缩对象 12.10 MiB / 22 万行，长期只增不减 | 压缩前 `stat` |

## 3. 交付物

| 文件 | 性质 | 说明 |
|------|------|------|
| `tools/log_compress.py` | **受版本控制（tracked）**，commit `22e90c8` | 权威实现。copytruncate：历史前段 gzip 归档 → 尾部写回文件头 → `ftruncate` → 读回校验。参数 `--log/--archive-dir/--threshold/--keep/--force/--dry-run`，环境变量 `GA_LOG_COMPRESS_THRESHOLD`(默认 8 MiB) / `GA_LOG_COMPRESS_KEEP`(默认 1 MiB)。归档落 `logs/archive/`（已被 `.gitignore` 的 `archive/` 规则忽略，不会污染仓库） |
| `temp/compress_log.py` | 兼容 shim（`temp/` 被 gitignore） | 3 行 `runpy` 转发到 `tools/log_compress.py`。**用途=在不改动 memory/ 的 helper.py 的前提下，让现存引用立即指向真实现，WARN 当场消失** |
| `temp/test_log_compress.py` | 夹具回归 | T1 阈值跳过 / T2 无损性 / T3 并发 O_APPEND 写者下的安全性 |

## 4. 验证

### 4.1 夹具（`temp/test_log_compress.py`，`ALL PASS`）

- **T1 未达阈值**：rc=0、文件字节不变、不产生归档。
- **T2 无损压缩**：`gunzip(archive) + 新日志 == 原文`（arch 2,910,050 + new 49,950 == orig 2,960,000）；新日志是原文的**连续后缀**且从完整行首开始。
- **T3 并发写者（真 O_APPEND fd 持续追加）**：
  - 最终日志 **NUL 计数 = 0**（无稀疏空洞）；
  - `len(archive) + len(最终日志) == len(全部期望内容)` → **字节守恒，零丢失**；
  - 归档内容 == 期望内容的精确前缀。

### 4.2 真实链路（真日志、真 launchd fd、真 helper 调用）

```
BEFORE  size=12,684,816  nul=0
HELPER: ✅ 日志已压缩: 12.10MiB → 1023.99KiB (保留尾部 1023.99KiB, 释放 11.10MiB,
        归档 autonomous_reflect.log.20260922-131845.log.gz 1.82MiB)
AFTER   size=1,048,568   nul=0
archive 11,636,248B(raw) → 1.82MiB(gz)
archive + 新日志 == 原文  → True   （精确相等，连竞态窗口都未丢字节）
新日志以 (原文去掉归档前缀) 开头 → True
```

**WARN 已消失**，`✅ 日志已压缩: …` 正常回填到 complete_task 输出。

## 5. 关键环境发现：launchd 的 `StandardOutPath` 是 **O_APPEND**

压缩此类「进程持 fd 写、不能重启」的日志，唯一可行手法是 copytruncate；
其安全性完全取决于该 fd 是否 O_APPEND（若非 O_APPEND，截短后写入者仍从旧 offset 写，
会产生 **NUL 稀疏空洞**）。本次用三条独立证据闭合：

1. 日志跨 **82 次进程启动**、含 **2026-01-02 → 09-22 共 188 个不同日期** 的内容，
   且**文件最头部仍是最早的内容** ⇒ 既非 O_TRUNC（否则每次重启清空），
   也非无 APPEND 的原地覆写（否则新进程从 offset 0 覆盖，文件不会单调增长）。
2. 压缩后 **前后 NUL 计数均为 0**，且 `archive + 新日志 == 原文` 精确相等
   （若产生空洞，此等式必不成立）— 实测闭环。
3. 附带反例（记录以免再踩）：`proc_pidinfo` 的 `PROC_PIDFD*INFO` 在本机 macOS
   **不可靠**——用「已知 O_APPEND / 已知只读」两个 fd 自校验时返回同一常量
   （flavor 2 → `0x404010`，flavor 9 → `0x100000f`），**不可作为判据**；
   判定 fd 标志应改用「文件内容跨重启单调累积 + 文件头保留最早内容」这类**间接证据**。

## 6. 边界与待批（未自行落地）

1. **helper 第 29 行改指（建议批准，1 行）** —— 当前靠 `temp/`（被 gitignore、可能被清理）
   的 shim 维持。若要根治复发（temp 一被清，WARN 就回来），改：

   ```diff
   -_COMPRESS_SCRIPT = _TEMP_DIR / "compress_log.py"
   +_COMPRESS_SCRIPT = _AGENT_DIR / "tools" / "log_compress.py"
   ```

   回滚：`git checkout -- memory/autonomous_operation_sop/helper.py`。
   批准后 `temp/compress_log.py` 即可删除（shim 使命结束）。
   未自行落地原因：与 R347（plist 注册出提案不安装）、R348（补丁沙箱验证、真树落地待批）
   同一约定——**真树/memory 改动待用户批准**。
2. **保留窗口是价值取舍**：默认「超过 8 MiB 才压、保留最近 1 MiB」。
   若希望保留更长现场（如最近 7 天）或更激进（如 2 MiB 阈值），改 `--keep/--threshold`
   或对应环境变量即可，不需要改代码。
3. **归档会长期累积**：`logs/archive/*.log.gz` 每次约 1.8 MiB 且无上限回收；
   是否需要保留期/上限，属于策略选择，未擅自加删。
4. **copytruncate 的固有代价**：读快照→追读→截断之间存在微秒级窗口，
   窗口内的新行可能既不入归档也不留原位（与 logrotate 相同；本次实测未丢任何字节）。
   运行日志属可再生数据，判定可接受。
5. 本次只执行了 TODO 要求的「恢复脚本」一条路线，**未删除任何死代码、未改 SOP 文字**。

## 7. 复现命令

```bash
# 夹具回归（不碰真实日志）
/opt/anaconda3/bin/python3 temp/test_log_compress.py

# 预览（不改磁盘）
/opt/anaconda3/bin/python3 temp/compress_log.py --dry-run

# 手动压缩真实日志（helper 会在 complete_task 时自动调用）
/opt/anaconda3/bin/python3 tools/log_compress.py

# 体检：最新归档可解压 + 当前日志无 NUL 空洞
python3 - <<'PY'
import gzip,glob,os
p=max(glob.glob('logs/archive/*.gz'), key=os.path.getmtime)
arch=gzip.decompress(open(p,'rb').read())
cur=open('logs/autonomous_reflect.log','rb').read()
print('archive:', os.path.basename(p), len(arch), 'B，解压成功')
print('当前日志 NUL 数(应为 0):', cur.count(b'\x00'))
print('当前日志首行完整:', cur.split(b'\n',1)[0][:80])
PY
```
```

---

## 附录 A. 记忆更新（本任务实际落地 / 待审 清单）

**已落地（随 commit `3d8a054` 入库）**
- `memory/global_mem_insight.txt`（L1，agent 自身索引）在「定时/自主/反射」段追加一条：
  `日志压缩 tools/log_compress.py(copytruncate:归档gzip+尾部写回+ftruncate;helper收尾自动调;launchd StandardOutPath=O_APPEND已实证⇒截断安全;temp/compress_log.py仅临时兼容shim,根治需helper第29行改指tools/,提案待批;proc_pidinfo读fd标志在本机不可靠勿用)`
  —— 依据：L1 是 agent 自身的极简索引，本仓惯例（`605fbf4`「memory: …L1指针」、`cc57f72`「…+L1补knowledgeC」）即为「patch + 同批提交」。

**建议但未落地（待用户批准）**
1. `memory/autonomous_operation_sop/helper.py` 第 29 行改指（见 §6.1）—— 1 行，是唯一能防复发的一步。
2. `memory/autonomous_operation_sop.md`（L2 操作 SOP）增补「日志压缩」小节（该文档自身「权限边界」写明 *修改 memory 下 SOP 需写入报告待审*，故只提议不落地）：
   - 内容建议：`complete_task()` 收尾会自动调用 `tools/log_compress.py` 压缩 `logs/autonomous_reflect.log`（阈值 8 MiB / 保留 1 MiB，可用 `GA_LOG_COMPRESS_THRESHOLD`/`GA_LOG_COMPRESS_KEEP` 调整）；归档落 `logs/archive/*.log.gz`；现经 `temp/compress_log.py` shim 生效。
3. `logs/archive/*.log.gz` 的保留期/上限策略（§6.3）。

**明确未动**
- 未删除任何死代码、未改 SOP 正文、未动 `sche_tasks/` 等与本任务无关的工作区脏状态。
