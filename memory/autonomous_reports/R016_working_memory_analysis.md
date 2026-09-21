
# R16 — WORKING MEMORY膨胀根因分析与修复方案

**执行时间**：2026-04-24  
**任务来源**：TODO R16（P0，R6发现成本4.7x）  
**验收标准**：定位膨胀根因，产出可落地修复方案

---

## 一、结论先行

> **R6的"无截断"结论有误**：系统已有多层截断机制，但存在设计缺陷导致中间轮次仍膨胀。真正的P0问题是`compress_history_tags`触发频率过低（每5轮一次），导致4轮内history标签全量累积。

---

## 二、现有截断机制（实测代码）

### 层1：LLM对话history截断（llmcore.py L77）
```python
def trim_messages_history(history, context_win):
    compress_history_tags(history)
    cost = sum(len(json.dumps(m)) for m in history)
    if cost > target:
        while len(history) > 5 and cost > target:
            history.pop(0)  # 删除最旧消息
```
- **触发条件**：每次LLM调用前（L484）
- **效果**：超context_win时删旧消息，防止OOM

### 层2：history标签压缩（llmcore.py L26）
```python
def compress_history_tags(messages, keep_recent=10, max_len=800, force=False):
    compress_history_tags._cd = getattr(compress_history_tags, '_cd', 0) + 1
    if compress_history_tags._cd % 5 != 0: return messages  # ⚠️ 每5轮才执行
    # 压缩<history>和<key_info>标签内容
```
- **触发条件**：每5轮一次（冷却计数器）
- **⚠️ 缺陷**：中间4轮不压缩，history标签全量注入

### 层3：WORKING MEMORY注入截断（ga.py L506）
```python
h_str = "\n".join(self.history_info[-20:])  # 只取最近20条
prompt = f"\n### [WORKING MEMORY]\n<history>\n{h_str}\n</history>"
```
- **效果**：注入prompt时已截断最近20条
- **✅ 此层设计正确**

---

## 三、真正的膨胀根因

```
轮次1: compress触发 → history压缩 ✅
轮次2: 跳过压缩 → history全量注入 ⚠️
轮次3: 跳过压缩 → history全量注入 ⚠️
轮次4: 跳过压缩 → history全量注入 ⚠️
轮次5: 跳过压缩 → history全量注入 ⚠️
轮次6: compress触发 → history压缩 ✅
```

**每个5轮周期内，4/5轮（80%）携带未压缩的完整history标签**，与R6实测"88.8%轮次包含`<history>`标签"高度吻合。

---

## 四、修复方案（需用户审批）

### 方案A：提高compress频率（最小改动，推荐）

**文件**：`llmcore.py` L30  
**改动**：`if compress_history_tags._cd % 5 != 0` → `if compress_history_tags._cd % 2 != 0`

```python
# 改前：每5轮压缩一次
if compress_history_tags._cd % 5 != 0: return messages

# 改后：每2轮压缩一次（或直接去掉冷却，每轮都压缩）
if compress_history_tags._cd % 1 != 0: return messages  # 即每轮都压缩
```

**预期效果**：history标签膨胀从80%轮次降至0%，成本节省估算50-60%

### 方案B：每轮强制压缩（彻底方案）

在`ga.py` L507注入前主动压缩：
```python
# ga.py L506附近，注入前先压缩
from llmcore import compress_history_tags
compress_history_tags(self.llmclient.backend.history, keep_recent=10, force=True)
h_str = "\n".join(self.history_info[-20:])
```

### 方案C：减小keep_recent（激进节省）

```python
compress_history_tags(messages, keep_recent=5, ...)  # 从10降至5
```

---

## 五、风险评估

| 方案 | 改动量 | 风险 | 预期节省 |
|------|--------|------|---------|
| A（提高频率） | 1行 | 低（仅频率变化） | ~50% |
| B（每轮强制） | 3行 | 低-中（需import） | ~60% |
| C（减keep_recent） | 1行 | 中（可能丢上下文） | ~30% |

**推荐方案A**：改动最小，风险最低，效果显著。

---

## 六、记忆更新建议

```
[L2更新] WORKING MEMORY膨胀根因（2026-04-24实测）：
- llmcore.py已有trim_messages_history+compress_history_tags双层截断
- 缺陷：compress_history_tags每5轮才触发，中间4轮history全量注入
- 修复：将%5改为%1（每轮压缩），预期节省50%+成本
- ga.py L506的history_info[-20:]截断设计正确，无需修改
```
