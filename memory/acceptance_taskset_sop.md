# acceptance_taskset_sop —— 验收题集（真题）编写与判卷

**适用**：从真实历史提交挖题、做成"外部 oracle 判卷 + 反作弊"的验收题集（实例：`herdr-agents:control-plane/acceptance_v0`（v0 自建夹具）、`acceptance_v1`（v1 真题 2 道））。

## 交付面（题集目录结构）
`taskset_v1.json`(题面/夹具/oracle/期望/provenance/guard) + `fixtures/<id>/`(修之前、可运行) + `solutions/<id>/`(修之后) + `oracles/<id>/check.py`(**工作区外**判官) + `run_v1.py`(list/run/selfcheck/lock) + `runs/*.json`(每次判卷原始记录) + `taskset.lock.json`(哈希冻结) + `EVIDENCE.md`(真跑原文) + `README.md`。

## 铁律
1. oracle 必须住工作区之外；判卷时把 `oracles/` 挂 **chmod 555** 只读，跑前跑后哈希比对（防自改判分器）。
2. 题面必须是"能独立解出来"的规格，**禁止把真实 commit diff 当提示**；`source` 字段写 `repo@sha` + 原始 commit message（禁编造）。
3. 判分只判**行为**不判文本：oracle 直接调公开 API/函数喂向量（触发向量 + 负控向量成对出现），不 grep 源码。
4. **证据优先**：EVIDENCE.md 只贴真跑输出原文；跑不通就写"未跑通 + 卡在哪"。

## harness 三类真坑（都实测踩过）
- **占位符陷阱**：oracle 检测命令模板里若含 `<workdir>` 之类占位符，执行时必须**先剔除占位符再追加真实工作区路径**。曾因两者同时传入 → 判分器读到 `.../oracles/<id>/<workdir>` 目录 → 全题 0.2s 假 FAIL（异常快的全 FAIL 就是早退信号）。
- **删夹具文件必须拦**：oracle 若在工作区外独立判行为、不执行工作区单测，则"删掉夹具自带测试"不会被发现（实测删测后仍 rc=0）。必须加通用旗标 `fixture_file_removed:<relpath>`（初版快照存在、判卷时消失的文件即计入）。
- **`python -I` 隐含 `-E`**：会忽略 `PYTHONDONTWRITEBYTECODE`，`__pycache__` 一生成就污染"是否等于初版"的判定 → 必须显式 `-B`。另外 `-I` 会剥离 `PYTHONPATH`/`PYTHONSTARTUP` 等注入面（想吃掉注入就是好事）。

## 反作弊旗标清单（6 类，均需真跑演示）
`empty_work`（与初版逐字节相同）｜`forbidden_hook_file:*`（工作区出现 `sitecustomize.py`/`usercustomize.py`/`conftest.py`/`check.py`）｜`oracle_copy_in_workdir:*`（工作区里出现 oracle 副本，按哈希认）｜`oracle_modified_during_run` / `oracle_hash_changed_vs_frozen`（防改判分器）｜`required_artifact_missing_or_empty:<rel>`｜`fixture_file_removed:<rel>`。
判定 = `oracle_rc == 0` **且** 无任一旗标。

## 真题挖题要点（smyze 工作区实测）
- 题源仓库在 `~/develop/WorkSpaces/idea/smyze/**`（多含 `.git` 子工程）；找"有测试或可观测行为 + 改动可复现"的小提交。
- **`.claude/skills` 常是指向 `.agents/skills` 的符号链接** → `git show <rev>:.claude/skills/...` 报 `exists on disk, but not in <rev>`，取夹具/参考解必须走 `.agents/skills/...` 真实路径。
- 夹具要"修之前能运行"：若能跑真实测试框架最好；否则至少能 `javac`/`import` 到缺口那一层（v1 的 totp-001 初版刻意编译不过 = 期望 FAIL）。

## 判卷退出码语义
`rc=0`=全部判据过；`rc=1`=有判据不过（或编译失败）；`rc=2`=环境不满足（如缺 `javac/java`）⇒ 属"无法判定"，**绝不可当 PASS**。

## 加载"被测模块"的技巧（Python 夹具）
走 `importlib.util.spec_from_file_location` 时，若脚本里有 `dataclass`/自省，先 `sys.modules[名] = mod` 再 `exec_module(mod)`，否则装饰器解析模块名失败。

## 自检口径
`run_v1.py selfcheck` 必须打印四行全 `ok`（每题的 baseline=FAIL、solution=PASS）；`lock` 后重跑 selfcheck 仍应全绿（不得报 `oracle_hash_changed_vs_frozen`）。
