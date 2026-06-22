# GenericAgent 最佳实践使用手册

> 基于 v2026.04 版本，适用于 macOS / Windows / Linux

---

## 一、快速启动

### 1.1 三种运行模式

```bash
cd /Users/cuip/projects/GenericAgent

# 命令行模式（最轻量，推荐日常使用）
python3 agentmain.py

# GUI 模式（Streamlit Web + pywebview 窗口）
python3 launch.pyw

# Streamlit 独立 Web 模式（可远程访问）
streamlit run frontends/stapp.py
```

### 1.2 命令行模式基本操作

```
> 帮我在桌面创建一个项目清单文件       ← 直接输入任务
> /session.model=claude-opus-4-7      ← 运行时切换模型
> /session.temperature=0.3             ← 调低温度（更精确）
> /session.reasoning_effort=high       ← 调高推理强度
> /session.max_tokens=16384            ← 调大输出上限
> /resume                              ← 恢复上一次会话
> Ctrl+C                               ← 中断当前任务
```

### 1.3 启动参数

```bash
# 指定使用第 N 个 LLM 后端（从 0 开始）
python3 agentmain.py --llm_no 1

# 开启详细日志
python3 agentmain.py --verbose

# 一次性任务模式（适合自动化集成）
python3 agentmain.py --task my_task --input "统计桌面文件数量"

# 后台运行（输出日志到 temp/my_task/）
python3 agentmain.py --task my_task --input "任务内容" --bg

# 反射模式（定时任务触发）
python3 agentmain.py --reflect reflect/scheduler.py
```

---

## 二、配置详解（mykey.py）

### 2.1 配置原理

GenericAgent 通过变量命名约定自动识别 Session 类型：

| 变量名关键字 | Session 类型 | 适用场景 |
|-------------|-------------|---------|
| `native` + `claude` | NativeClaudeSession | Claude 模型（推荐） |
| `native` + `oai` | NativeOAISession | GPT / Gemini 等 OpenAI 兼容模型 |
| `claude`（无 native） | ClaudeSession | Claude 文本协议（已废弃） |
| `oai`（无 native） | LLMSession | OpenAI 兼容文本协议（已废弃） |
| `mixin` | MixinSession | 多模型故障转移（强烈推荐） |

### 2.2 推荐配置方案

**最佳实践：Mixin 故障转移**，Claude 主力 + GPT 兜底：

```python
# 故障转移：按 llm_nos 顺序尝试，第一个失败自动切下一个
mixin_config = {
    'llm_nos': ['claude-relay', 'gpt-native'],
    'max_retries': 10,
    'base_delay': 0.5,
    'spring_back': 300,   # 300 秒后尝试回到主节点
}

# Claude 中转
native_claude_config0 = {
    'name': 'claude-relay',
    'apikey': 'sk-xxx',
    'apibase': 'https://your-proxy.com',
    'model': 'claude-sonnet-4-6',
    'fake_cc_system_prompt': True,   # 中转渠道必须开启
    'thinking_type': 'adaptive',
    'max_retries': 3,
    'read_timeout': 180,
}

# GPT 兜底
native_oai_config = {
    'name': 'gpt-native',
    'apikey': 'sk-xxx',
    'apibase': 'https://your-proxy.com/v1',
    'model': 'gpt-5.4',
    'api_mode': 'chat_completions',
    'max_retries': 3,
    'read_timeout': 120,
}
```

### 2.3 关键参数调优

| 参数 | 默认值 | 建议 |
|------|--------|------|
| `temperature` | 1.0 | 精确任务降到 0.3-0.5；创意任务保持 1.0 |
| `max_tokens` | 8192 | 长输出任务（写文章/代码）调到 16384-32768 |
| `context_win` | 28000 | 控制历史裁剪阈值；增大会消耗更多 Token |
| `read_timeout` | 30 | 中转渠道建议 120-300 秒 |
| `thinking_type` | - | `adaptive`（模型自决）/ `enabled`（强制开启） |
| `reasoning_effort` | - | `low` / `medium` / `high` / `xhigh` |
| `stream` | True | 某些渠道不支持 SSE 时改 False |

### 2.4 运行时动态调参

无需重启，直接在对话中输入：

```
/session.temperature=0.3
/session.reasoning_effort=high
/session.thinking_type=enabled
/session.thinking_budget_tokens=32768
/session.max_tokens=16384
/session.model=claude-opus-4-7
```

---

## 三、9 个原子工具使用技巧

### 3.1 工具一览

| 工具 | 用途 | 最佳实践 |
|------|------|---------|
| `code_run` | 执行 Python/Bash 代码 | 复杂逻辑用 Python；系统操作用 Bash |
| `file_read` | 读取文件内容 | 支持行号定位和关键字搜索；修改前必读 |
| `file_write` | 创建/覆写/追加文件 | 新文件创建用 overwrite；日志追加用 append |
| `file_patch` | 精确文本替换 | 要求 old_content 唯一匹配；改代码首选 |
| `web_scan` | 获取页面简化 HTML | 自动精简 DOM 至 35K 字符；首次扫描如为空需稍等重试 |
| `web_execute_js` | 浏览器内执行 JS | 输入框用原生 setter + 事件链；点击前检查 disabled |
| `ask_user` | 询问用户确认 | 需要决策或不可逆操作时使用 |
| `update_working_checkpoint` | 更新短期工作记忆 | 关键信息及时存入；避免上下文丢失 |
| `start_long_term_update` | 蒸馏长期记忆 | 任务完成后自动提取经验到 L2/L3 层 |

### 3.2 高效任务表达

**直接说目标，不要说步骤**——Agent 会自己规划路径：

```
# 好的表达（目标驱动）
帮我把 ~/Documents 下所有 .csv 文件合并成一个 Excel，按文件名分 sheet

# 差的表达（步骤驱动）
先用 os.listdir 列出文件，然后用 pandas 读取每个 csv，再...
```

**复杂任务加约束**：

```
帮我重构 src/utils.py：
- 拆分成 3 个模块：string_utils, date_utils, file_utils
- 保持所有现有测试通过
- 不要改变公开 API 的函数签名
```

---

## 四、记忆系统最佳实践

### 4.1 四层记忆架构

```
memory/
  global_mem_insight.txt    ← L1 洞察索引（Agent 每次启动加载）
  global_mem.txt            ← L2 全局事实（长期稳定知识）
  *.md / *.py               ← L3 技能/SOP（可复用的任务流程）
  L4_raw_sessions/          ← L4 会话归档（已完成任务记录）
```

### 4.2 记忆工作原理

1. **每次对话启动**：Agent 加载 L1 索引 + L2 全局事实作为系统提示的一部分
2. **对话过程中**：通过 `update_working_checkpoint` 维护短期工作记忆
3. **任务完成后**：通过 `start_long_term_update` 自动蒸馏经验到 L2/L3
4. **L1 索引**是路由层——Agent 根据索引决定是否需要深入读取 L3 SOP

### 4.3 内置 SOP 说明

| SOP 文件 | 用途 |
|---------|------|
| `memory_management_sop.md` | L0 元规则：记忆读写的宪法约束 |
| `plan_sop.md` | 规划模式：3 步以上复杂任务的分解方法 |
| `tmwebdriver_sop.md` | 浏览器控制：文件上传/Cookie/跨域等特殊操作 |
| `ljqCtrl_sop.md` + `.py` | 键鼠控制（禁用 pyautogui，用 ljqCtrl） |
| `vision_sop.md` + `ocr_utils.py` | 截图/OCR/视觉识别 |
| `adb_ui.py` | Android 设备控制 |
| `scheduled_task_sop.md` | 定时任务配置 |
| `subagent.md` | 子代理委派：探索型任务用子代理节省主上下文 |
| `autonomous_operation_sop.md` | 自主运行模式 |

### 4.4 手动管理记忆

```
# 让 Agent 查看当前记忆状态
查看你的记忆系统，列出所有 L2 事实和 L3 技能

# 让 Agent 记住偏好
记住：我习惯用 VSCode 打开文件，终端用 iTerm2，偏好 dark 主题

# 让 Agent 清理过时记忆
整理你的记忆，删除过时或冗余的条目
```

---

## 五、浏览器控制（TMWebDriver）

### 5.1 安装 Chrome 扩展

1. 打开 Chrome，访问 `chrome://extensions/`
2. 开启"开发者模式"
3. 点击"加载已解压的扩展程序"
4. 选择 `GenericAgent/assets/tmwd_cdp_bridge/` 目录
5. 扩展加载成功后即可使用

### 5.2 浏览器任务示例

```
# 网页操作（保留你的登录态）
帮我打开淘宝，搜索"机械键盘"，按销量排序，截取前 5 个结果

# 信息采集
打开我已登录的 GitHub，统计我最近 30 天的提交数量

# 表单填写
打开 xxx 网站，帮我填写并提交这个表单：姓名张三，邮箱xxx@xx.com
```

### 5.3 注意事项

- `web_scan` 返回精简 HTML（35K 字符以内），不是完整 DOM
- 首次扫描页面可能为空，Agent 会自动等待后重试
- JS 注入的输入操作要用原生 setter + 事件链，避免直接赋值
- 点击按钮前 Agent 会自动检查 disabled 状态
- 复杂操作详见 `memory/tmwebdriver_sop.md`

---

## 六、规划模式（Plan Mode）

适用于 3 步以上的复杂任务，Agent 会自动进入规划模式。也可以主动触发：

```
# 主动触发规划模式
用规划模式帮我完成：将这个 Flask 项目重构为 FastAPI，包括路由迁移、中间件适配、测试更新

# Agent 的规划流程
1. 创建工作目录 plan_XXX/
2. 启动探索子代理（只读探测环境，不占主上下文）
3. 基于探索结果制定 checklist
4. 逐步执行，每步验证
5. 完成后蒸馏经验到记忆系统
```

规划模式下 `max_turns` 从 40 提升到 80，适合长程任务。

---

## 七、多前端接入

### 7.1 可用前端

| 前端 | 文件 | 启动方式 |
|------|------|---------|
| 命令行 | `agentmain.py` | `python3 agentmain.py` |
| Streamlit Web | `frontends/stapp.py` | `streamlit run frontends/stapp.py` |
| Qt 桌面 | `frontends/qtapp.py` | `python3 frontends/qtapp.py` |
| 桌面宠物 | `frontends/desktop_pet.pyw` | `python3 frontends/desktop_pet.pyw` |
| Telegram | `frontends/tgapp.py` | 需配置 Bot Token |
| QQ | `frontends/qqapp.py` | 需配置 QQ Bot |
| 微信 | `frontends/wechatapp.py` | 需配置微信接入 |
| 企业微信 | `frontends/wecomapp.py` | 需配置企微 Webhook |
| 飞书 | `frontends/fsapp.py` | 需配置飞书 Bot |
| 钉钉 | `frontends/dingtalkapp.py` | 需配置钉钉 Bot |

### 7.2 GUI 模式（推荐体验）

```bash
# 方式一：pywebview 窗口（Windows/macOS）
python3 launch.pyw

# 方式二：纯 Streamlit（可远程访问）
streamlit run frontends/stapp.py --server.port 8501
```

---

## 八、定时任务与自主运行

### 8.1 反射模式（Reflect）

通过 `--reflect` 启动定时监控脚本：

```bash
python3 agentmain.py --reflect reflect/scheduler.py
```

### 8.2 自定义监控脚本

创建 Python 脚本，实现 `check()` 函数：

```python
# my_monitor.py
INTERVAL = 60          # 每 60 秒检查一次
ONCE = False           # True = 执行一次后退出

def check():
    """返回任务字符串触发 Agent，返回 None 跳过"""
    import os
    inbox = os.path.expanduser('~/Desktop/inbox')
    files = os.listdir(inbox) if os.path.exists(inbox) else []
    if files:
        return f"桌面 inbox 文件夹有新文件：{files}，帮我分类整理到对应目录"
    return None

def on_done(result):
    """任务完成后的回调（可选）"""
    print(f"整理完成：{result[:100]}")
```

启动：

```bash
python3 agentmain.py --reflect my_monitor.py
```

### 8.3 内置调度器

`reflect/scheduler.py` 支持多种重复策略：

- `daily` — 每天执行
- `weekday` — 工作日执行
- `weekly` — 每周执行
- `monthly` — 每月执行
- `every_Nh` / `every_Nm` / `every_Nd` — 每 N 小时/分钟/天

任务配置文件放在 `reflect/sche_tasks/*.json`。

---

## 九、安全注意事项

### 9.1 核心风险

GenericAgent 的 `code_run` 工具可以执行任意代码，**没有沙箱隔离**。这意味着：

- Agent 拥有和当前用户相同的系统权限
- 误操作可能删除文件、修改系统配置
- 已有 Issue #28 报告"严重误删除"事件

### 9.2 安全最佳实践

1. **不要在 root/管理员权限下运行**
2. **重要文件先备份**，特别是首次使用时
3. **不可逆操作前 Agent 会调用 ask_user 确认**——认真阅读再确认
4. **敏感目录设限**：不要让 Agent 操作系统关键目录
5. **API Key 安全**：`mykey.py` 已在 `.gitignore` 中，不会被提交
6. **定时查看 `temp/` 目录**：Agent 的临时文件和执行日志都在这里

### 9.3 宪法约束（内置安全机制）

Agent 内置了行为约束规则：
- 修改自身源码需先请示用户
- 3 次失败后请求人工干预
- 密钥文件仅引用，不读取/移动
- 不可逆操作前自动询问

---

## 十、常见任务模板

### 10.1 文件与数据处理

```
把 ~/Downloads 下所有 PDF 按年份分类到子文件夹
合并 data/ 目录下所有 CSV 文件，去重后导出为 Excel
扫描项目中的 TODO 注释，生成一份待办清单
```

### 10.2 代码开发

```
帮我写一个 Python 脚本，监控指定目录的文件变化，变化时自动执行 git commit
阅读 src/ 目录的代码，画一张模块依赖关系图
这个函数有 bug，帮我定位并修复：[粘贴代码或指定文件路径]
```

### 10.3 网页自动化

```
打开 GitHub Trending，把今天的 Top 10 仓库信息整理成表格保存到桌面
帮我在已登录的 Notion 里创建一个本周工作计划页面
每天早上 9 点帮我检查邮箱，把未读邮件标题整理发到飞书
```

### 10.4 系统运维

```
检查我的 Mac 磁盘使用情况，找出占用空间最大的 10 个文件夹
帮我配置一个 crontab 任务，每天凌晨 2 点备份数据库
查看当前运行的 Docker 容器，停掉所有非必要的
```

### 10.5 信息检索与分析

```
搜索"2026 年 AI Agent 市场规模"，整理成一份简报
帮我对比 React 和 Vue 在 2026 年的生态数据，生成对比表格
阅读这篇论文 [路径]，提取核心观点并写一段 500 字的总结
```

---

## 十一、性能优化技巧

### 11.1 Token 节省

- GenericAgent 已内置极致 Token 优化（30K 上下文窗口 + HTML 精简）
- **短任务用短指令**：不需要写详细上下文，Agent 会自己探测
- **长任务用规划模式**：避免在单轮对话中堆积过多上下文
- **子代理处理探测**：耗 Token 的环境探测交给子代理，保护主上下文

### 11.2 速度优化

- 中转渠道设置合理的 `read_timeout`（120-300 秒）
- 简单任务用更快的模型（如 GPT 4o-mini）
- `stream: True` 开启流式输出（默认已开启）
- 关闭不需要的 thinking：`/session.thinking_type=disabled`

### 11.3 质量优化

- 复杂推理任务调高 reasoning_effort：`/session.reasoning_effort=high`
- 代码生成任务调低温度：`/session.temperature=0.3`
- 使用更强模型（Claude Opus / GPT 5.4）处理关键任务

---

## 十二、故障排查

| 症状 | 可能原因 | 解决方案 |
|------|---------|---------|
| 启动报 `ModuleNotFoundError` | 缺少依赖 | 让 Agent 自装：`请查看你的代码，安装所有依赖` |
| API 返回 403 | 中转渠道拒绝请求格式 | 检查 `fake_cc_system_prompt` 是否正确设置 |
| API 返回 429 | 频率限制 | Mixin 会自动切换备用节点；也可增加 `base_delay` |
| Agent 陷入循环 | 连续失败未升级方案 | Ctrl+C 中断，换个表述重试 |
| 浏览器控制无响应 | Chrome 扩展未加载 | 检查 `chrome://extensions/` 中扩展是否启用 |
| 中文乱码 | 编码问题 | 确认终端支持 UTF-8；Windows 用 `chcp 65001` |
| `EOFError` | 管道模式正常退出 | 非交互模式的正常行为，可忽略 |
| 记忆未生效 | L1 索引未更新 | 让 Agent 执行 `start_long_term_update` |

---

## 附录：目录结构速查

```
GenericAgent/
├── agentmain.py         # 主入口：Agent 类 + CLI / 任务 / 反射三种模式
├── agent_loop.py        # 核心循环：工具调度 + 上下文管理 + 失败升级
├── ga.py                # 9 个原子工具实现 + Handler
├── llmcore.py           # LLM 通信层：5 种 Session + SSE 解析 + 故障转移
├── simphtml.py          # HTML 简化引擎：DOM → 35K 字符精简文本
├── TMWebDriver.py       # 浏览器控制：WebSocket + Chrome 扩展
├── mykey.py             # 你的 API 配置（不会被 git 提交）
├── launch.pyw           # GUI 启动器（pywebview + Streamlit）
├── assets/
│   ├── sys_prompt.txt       # 系统提示词
│   ├── tools_schema.json    # 工具 Schema 定义
│   └── tmwd_cdp_bridge/     # Chrome 扩展（浏览器控制）
├── memory/
│   ├── global_mem_insight.txt   # L1 洞察索引
│   ├── global_mem.txt           # L2 全局事实
│   ├── *.md / *.py              # L3 技能/SOP
│   └── L4_raw_sessions/         # L4 会话归档
├── frontends/               # 多前端入口
├── reflect/                 # 定时调度 + 自主运行
├── plugins/                 # 可选插件
├── temp/                    # 运行时临时文件
└── tests/                   # 测试
```
