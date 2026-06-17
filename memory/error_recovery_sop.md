# Error Recovery SOP

## 适用场景
工具调用失败 / 网络超时 / 权限错误 / 路径错误 / 进程冲突

---

## 1. 工具调用失败

### 1.1 grep/shell命令转义失败
**症状**：`SyntaxWarning: invalid escape sequence`，输出为空
**根因**：Python字符串中`\|`等正则特殊字符未正确转义
**修复**：改用`file_read(keyword=...)`或Python原生`re`模块，避免shell grep

### 1.2 路径猜测错误
**症状**：`File not found`，但文件存在于其他路径
**根因**：凭印象猜路径（如`temp/sche_done`实为`sche_tasks/done`）
**修复**：先用`find`或`es`搜索文件名，禁止猜路径；读SOP确认标准路径

### 1.3 import失败
**症状**：`ModuleNotFoundError`
**根因**：路径未加入sys.path，或模块名有虚假前缀
**修复**：`sys.path.insert(0, '..')` 后直接import；memory模块无需前缀

---

## 2. 网络/API超时

### 2.1 飞书机器人context超时
**症状**：无[Output]响应，claude-relay无回复
**阈值**（R13实测）：context > 80k chars 或 > 17条消息
**修复**：检查Trim机制是否触发；手动清理history；降低compress_history_tags触发阈值

### 2.2 subagent超时
**症状**：等待60s无`[ROUND END]`标记
**根因**：subagent任务过重或LLM响应慢
**修复**：跳过subagent，主agent自行完成；或拆分任务减小输入

### 2.3 CoinGecko/外部API超时
**症状**：requests.exceptions.Timeout
**修复**：加`timeout=10`参数；失败时写入报告"API不可用"，不抛异常

---

## 3. 权限/进程错误

### 3.1 端口冲突
**症状**：`Address already in use`（如desktop_pet:41983）
**根因**：launchd版与手动版进程重复
**修复**：`lsof -i :41983`找PID → 确认是旧进程 → kill旧PID → 重启

### 3.2 scheduler端口锁冲突（45762）
**症状**：agentmain崩溃退出，无日志
**根因**：../reflect/scheduler.py bind 45762失败，说明已有实例运行
**修复**：`lsof -i :45762`确认PID，不要重复启动

### 3.3 文件权限错误
**症状**：`PermissionError: [Errno 13]`
**修复**：检查文件owner；避免操作`~/Library/`下系统文件；cwd内操作无需sudo

---

## 4. 配置/格式错误

### 4.1 scheduler任务格式错误
**症状**：`Invalid schedule format`或任务持续SKIP
**常见错误**：
- `schedule: "every_1h"` → 应为 `schedule: "00:00"` + `repeat: "every_1h"`
- `max_delay_hours`设置过小导致开机后SKIP
**修复**：参考`sche_tasks/price_alert.json`作为正确模板

### 4.2 JSON解析失败
**症状**：`JSON parse error for xxx.json`
**修复**：用`python -m json.tool`验证；检查trailing comma

---

## 5. 失败升级策略

| 次数 | 策略 |
|------|------|
| 第1次 | 读错误信息，理解根因 |
| 第2次 | 探测环境状态（路径/进程/日志） |
| 第3次 | 深度分析后切换方案，或请求用户干预 |

**禁止**：不读错误直接重试 / 猜路径 / 无新信息重复同一操作

---

## 6. 历史案例索引

| 案例 | 报告 | 根因 |
|------|------|------|
| folder_watcher沉默失效 | R12 | 从未配置scheduler，非watchdog问题 |
| 飞书context超时 | R13 | context>80k，Trim阈值86k |
| notify推送盲区 | R14 | notify_group无try/except，直接抛异常 |
| WORKING MEMORY膨胀 | R16 | compress_history_tags每5轮才触发 |
| bridge_autonomous路径误判 | R22 | done目录在sche_tasks/done非temp/sche_done |

---

## 7. 自主行动效率规则（R47实测）

### 7.1 安装包前环境预检
**触发**：准备pip install任何包
**操作**：先 `pip show <dep1> <dep2>` 检查版本兼容性，再安装
**示例**：安装llmlingua前先 `pip show numpy scipy`，确认无2.x冲突
**收益**：可节省3-5轮依赖冲突重试

### 7.2 搜索工具失败快速降级
**规则**：任何搜索工具（gh search/es/web）失败**1次**立即切换备用方案
**降级顺序**：gh search → gh api → web google搜索
**禁止**：同一工具重试超过2次（R44浪费4轮）

### 7.3 实验脚本先设计后实现
**规则**：写测试/实验脚本前先用注释写伪代码验证逻辑
**关键**：状态隔离用独立函数，禁用全局变量共享状态
**收益**：避免3次重写（R45浪费6轮）

### 7.4 单AUTO段单任务原则
**规则**：每次AUTO触发只完成**1个TODO**，完成后立即收尾（写报告+complete_task+标[x]）
**禁止**：连续执行多个TODO（轮数叠加易触发DANGER警告）
**例外**：当前任务需用户审批时，可跳过执行下一个
