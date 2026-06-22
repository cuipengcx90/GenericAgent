# GenericAgent x 微信数据集成技术方案

> 作者: cuip | 日期: 2026-05-18 | 版本: 1.0

---

## 1. 方案概述

本文档提出一套将 **GenericAgent**（自进化自主 Agent 框架）与**微信本地数据工具**集成的技术方案，构建 AI 驱动的微信数据智能平台。

方案实现：自然语言查询微信聊天记录、语义搜索、实时消息监控、智能分析——全部本地运行，零数据泄漏。

### 核心选型

| 维度 | 选择 | 理由 |
|------|------|------|
| 数据引擎 | **wechat-decrypt** | 支持无头运行、原生 MCP、持续维护、三平台 |
| Agent 框架 | **GenericAgent** | 自进化技能、极致省 Token、code_run 能力 |
| 日常浏览 | **WeFlow**（可选） | GUI 体验最佳，适合手动浏览 |
| 集成方式 | **HTTP API + code_run** | 双方零改动 |

---

## 2. 工具全景分析

### 2.1 四工具对比矩阵

| 能力维度 | wechat-decrypt | WeFlow | chatlog_alpha | wechat-cli |
|:---------|:-:|:-:|:-:|:-:|
| Stars | 3,437 | 10,088 | 887 | 956 |
| 最后更新 | 2026-05-17 | 2026-05-17 | 2026-04-26 | 2026-04-06 |
| 语言 | Python + C | Electron + React | Go | npm/Python |
| 许可证 | — | CC BY-NC-SA 4.0 | MIT | — |
| 微信版本 | 4.x（无上限） | 4.0+（无上限） | 4.x（无上限） | **<= 4.1.8.100** |
| macOS | arm64 + x64 | arm64 | arm64 + x64 | arm64 |
| Windows | x64 | x64 | x64 + arm64 | — |
| Linux | x64 | x64 | **不支持** | — |
| 无头模式 | **支持** | 不适用（桌面应用） | **不支持（依赖 TUI）** | 支持 |
| MCP Server | **原生支持** | 无 | 支持（绑定 TUI） | 无 |
| HTTP API | Web UI (:5678) | :5031 | :5030 | 无 |
| 数据库解密 | SQLCipher 4 | SQLCipher 4 | SQLCipher 4 | SQLCipher 4 |
| 图片解密 | XOR/AES-ECB | AES-128-ECB + XOR | dat2img + AES | 无 |
| 视频解密 | 无 | WASM + ffmpeg | XOR 128KB + WASM | 无 |
| 语音转录 | Whisper/OpenAI | SenseVoice（离线） | Silk 转 MP3 | 无 |
| 朋友圈 | 解析 + 导出 | **浏览 + 防删 + 破时限** | Feed/搜索/代理 | 无 |
| 防撤回 | 无 | **支持** | 无 | 无 |
| 实时监听 | WAL 轮询 + SSE | 桌面通知 | SSE + 多通道推送 | 无 |
| 语义搜索 | 无 | 无 | **Ollama 向量** | 无 |
| RAG 问答 | 无 | 无 | **流式 SSE** | 无 |
| 知识图谱 | 无 | 无 | **时间维度（实验）** | 无 |
| 数据分析 | 无 | **年度/双人报告** | 仪表盘 + 排行 | 无 |
| 导出格式 | JSON/CSV/HTML | JSON/HTML/TXT/Excel/CSV/PGSQL | YAML/JSON | JSON/Text/MD |
| 企业微信 | 实验性 | 无 | 部分 | 无 |

### 2.2 为什么选择 wechat-decrypt

1. **无头优先**：CLI / Web UI / MCP 均无需 TTY，可直接后台运行——这是后台服务的关键
2. **原生 MCP**：Claude Code 通过 `mcp_server.py` 自动管理进程生命周期
3. **持续维护**：2026-05-17 仍在更新，紧跟微信最新版本
4. **三平台**：macOS + Windows + Linux
5. **解耦架构**：解密一次 → 所有下游工具读取明文 SQLite
6. **Python 生态**：可直接导入 GenericAgent 的 `code_run` 环境

### 2.3 为什么 chatlog_alpha 不作为主引擎

尽管其 AI 能力更强（语义搜索、RAG、知识图谱）：

- **TUI 锁定**：HTTP/MCP 服务只能从 TUI（`tview`）内部启动
- **无 `--headless` / `--daemon` 参数**：后台运行需要 tmux 包裹
- **单人开发**：全部 75 次提交来自一位贡献者
- **不支持 Linux**：平台覆盖不全

**结论**：持续关注其 headless 模式进展；若解决则升级为备选引擎。

---

## 3. 系统架构

### 3.1 整体架构图

```
+-------------------------------------------------------------------+
|                         用户界面层                                  |
|                                                                    |
|  [CLI]  [Streamlit Web]  [Qt 桌面]  [TUI]  [微信 Bot]             |
|    |         |               |        |         |                  |
+-------------------------------------------------------------------+
         |                                    |
         v                                    v
+-------------------+              +---------------------+
|   GenericAgent    |              |  微信 Bot 客户端     |
|   核心 (~3K 行)   |              |  (wechatapp.py)     |
|                   |              |  ilinkai API        |
| - agent_loop.py   |              +---------------------+
| - ga.py (9 工具)  |                       |
| - llmcore.py      |       +--------------+
| - memory/ (L0-L4) |       |
+-------------------+       |
         |                  |
    [code_run]              |
         |                  v
+-------------------------------------------------------------------+
|                        数据访问层                                   |
|                                                                    |
|  +-------------------+  +------------------+  +----------------+  |
|  | wechat-decrypt    |  | wechat-decrypt   |  | wechat-decrypt |  |
|  | HTTP API (:5678)  |  | MCP Server       |  | Python Import  |  |
|  |                   |  | (stdin/stdout)   |  | (direct call)  |  |
|  | GET /api/history  |  | Claude 自动管理   |  | 通过 code_run  |  |
|  | GET /api/sessions |  |                  |  |                |  |
|  | GET /stream (SSE) |  | 8+ MCP 工具      |  | decrypt_db.py  |  |
|  +-------------------+  +------------------+  +----------------+  |
|           |                     |                     |            |
+-------------------------------------------------------------------+
                          |
                          v
+-------------------------------------------------------------------+
|                         存储层                                      |
|                                                                    |
|  +--------------------+    +-------------------+                   |
|  | 微信 WCDB          |    | 解密后 SQLite      |                  |
|  | (SQLCipher 4)      | -> | (明文)             |                  |
|  | ~/Library/          |    | decrypted/*.db    |                  |
|  |   Containers/       |    +-------------------+                  |
|  |   WeChat/...        |            |                              |
|  +--------------------+    +-------------------+                   |
|                            | 媒体文件           |                   |
|  +--------------------+    | .dat -> JPEG/PNG  |                   |
|  | GenericAgent       |    | .silk -> MP3      |                   |
|  | 记忆系统 (L0-L4)   |    +-------------------+                   |
|  | memory/*.md        |                                            |
|  +--------------------+                                            |
+-------------------------------------------------------------------+
```

### 3.2 数据流

```
微信进程（运行中）
    |
    | [1] 内存扫描 (Mach VM API / C 语言)
    v
密钥提取 (find_all_keys_macos)
    |
    | [2] SQLCipher 4 解密 (AES-256-CBC + HMAC-SHA512)
    v
明文 SQLite 数据库 (decrypted/)
    |
    +---> [3a] HTTP API (monitor_web.py :5678)
    |         |
    |         +---> GenericAgent code_run (requests.get)
    |         +---> SSE 实时消息流 (/stream)
    |
    +---> [3b] MCP Server (mcp_server.py)
    |         |
    |         +---> Claude Code（自动管理）
    |
    +---> [3c] Python 直接导入
              |
              +---> GenericAgent code_run (import 调用)
```

---

## 4. 集成设计

### 4.1 集成方式选型

| 方式 | 改动量 | 复杂度 | 可靠性 | 延迟 |
|------|:------:|:------:|:------:|:----:|
| **HTTP API + code_run** | 零 | 低 | 高 | ~10ms |
| MCP Client SDK | 需写适配层 | 中 | 中 | ~50ms |
| Python 直接导入 | 环境耦合 | 中 | 高 | ~5ms |
| Web UI 页面抓取 | 零 | 低 | 低 | ~500ms |

**最终选择：HTTP API + `code_run`**

理由：
- 双方零改动，不侵入任何一方代码
- GenericAgent 的 `code_run` 可执行任意 Python，包括 `requests`
- wechat-decrypt 的 Web UI 暴露完整 RESTful API
- 完全解耦——任一方独立升级互不影响

### 4.2 API 端点目录

#### 会话与联系人

| 端点 | 方法 | 参数 | 说明 |
|------|------|------|------|
| `/api/sessions` | GET | — | 最近会话列表 |
| `/api/contacts` | GET | `query`, `limit` | 搜索联系人 |
| `/api/contact_tags` | GET | — | 联系人标签列表 |

#### 消息查询

| 端点 | 方法 | 参数 | 说明 |
|------|------|------|------|
| `/api/history` | GET | `chat`, `limit`, `offset`, `start_time`, `end_time` | 聊天记录 |
| `/api/history?keyword=` | GET | `keyword`, `chat_name`, `limit`, `offset` | 消息搜索 |
| `/api/history?since=` | GET | `since`（时间戳） | 增量拉取 |

#### 实时

| 端点 | 方法 | 参数 | 说明 |
|------|------|------|------|
| `/stream` | GET (SSE) | — | 实时消息推送 |

#### 媒体

| 端点 | 方法 | 参数 | 说明 |
|------|------|------|------|
| `/api/voice` | GET | `chat_name`, `local_id` | 语音消息列表 |
| `/api/voice/decode` | GET | `chat_name`, `local_id` | 解码语音为 WAV |
| `/api/voice/transcribe` | GET | `chat_name`, `local_id` | 语音转文字 |

### 4.3 GenericAgent 技能（SOP）设计

GenericAgent 使用分层记忆系统（L0-L4）。集成通过在 L3 层放置技能 SOP 实现：

```
memory/
  wechat_data_query_sop.md      # L3 技能：微信数据查询
  global_mem_insight.txt         # L1：自动更新索引（将包含微信条目）
  global_mem.txt                 # L2：关于微信配置的持久化事实
```

#### L3 技能 SOP 内容

```markdown
# 微信数据查询 SOP

## 触发条件
用户询问：聊天记录、消息搜索、联系人查找、
微信监控、会话摘要、消息统计等相关问题。

## 前置条件
- wechat-decrypt HTTP 服务运行在 http://127.0.0.1:5678
- 微信桌面版正在运行（用于实时功能）
- 数据库已解密（decrypted/ 目录存在）

## 服务健康检查
每次查询前，先验证服务可用：
  requests.get('http://127.0.0.1:5678/api/sessions', timeout=3)
若失败：通知用户启动 wechat-decrypt 服务。

## 查询模式

### 模式 1：最近会话
import requests
r = requests.get('http://127.0.0.1:5678/api/sessions')
sessions = r.json()

### 模式 2：指定联系人聊天记录
params = {'chat': '{联系人名称}', 'limit': 50}
r = requests.get('http://127.0.0.1:5678/api/history', params=params)
messages = r.json()

### 模式 3：全局关键词搜索
params = {'keyword': '{关键词}', 'limit': 30}
r = requests.get('http://127.0.0.1:5678/api/history', params=params)

### 模式 4：时间范围查询
params = {
    'chat': '{联系人}',
    'start_time': '{YYYY-MM-DD}',
    'end_time': '{YYYY-MM-DD}',
    'limit': 100
}
r = requests.get('http://127.0.0.1:5678/api/history', params=params)

### 模式 5：实时监听（长期运行）
import sseclient
url = 'http://127.0.0.1:5678/stream'
client = sseclient.SSEClient(url)
for event in client.events():
    msg = json.loads(event.data)
    # 处理消息...

### 模式 6：语音转文字
params = {'chat_name': '{联系人}', 'local_id': '{消息ID}'}
r = requests.get('http://127.0.0.1:5678/api/voice/transcribe', params=params)
text = r.json()['text']

## 输出规范
- 用自然语言总结结果，不直接输出原始 JSON
- 大结果集只展示前 N 条，提供查看更多选项
- 敏感内容：不写入文件或发送到外部服务
- 时间格式：将时间戳转为人类可读格式
- 群消息：前缀标注发送者名称

## 异常处理
- 连接拒绝："wechat-decrypt 服务未运行，请启动"
- 空结果：尝试更宽泛的搜索词或检查联系人名称拼写
- SSE 超时：指数退避重连
```

### 4.4 自进化路径

GenericAgent 的核心特性是**自动技能结晶**。初始 SOP 部署后：

```
第 1 轮：用户问"张三上周说了什么"
  → Agent 读取 SOP → code_run 调 API → 返回结果
  → Agent 自动优化 SOP，记录实际 API 响应格式

第 5 轮：用户问"总结今天的群聊"
  → Agent 编排多步计划：列出会话 → 过滤群聊 → 拉取记录 → 摘要
  → Agent 将此保存为新的子技能

第 10 轮：用户问"监控所有群里提到'线上故障'的消息"
  → Agent 使用 SOP 中的 SSE 模式 → 添加关键词过滤逻辑
  → Agent 保存监控技能（支持可配置关键词）

第 20+ 轮：Agent 已构建完整的微信智能工具箱
  → 优化的 API 调用模式
  → 已学习用户的常见查询习惯
  → 自动缓存高频联系人名称
```

---

## 5. 部署指南

### 5.1 环境要求

| 组件 | 版本 | 用途 |
|------|------|------|
| macOS | >= 14.0 | 宿主系统 |
| Python | >= 3.10 | 运行时 |
| 微信 Mac 版 | 4.x | 数据源 |
| Xcode CLI Tools | 最新 | C 编译器（密钥扫描器） |
| GenericAgent | 最新 | Agent 框架 |
| wechat-decrypt | 最新 | 数据引擎 |

### 5.2 安装步骤

#### 步骤一：安装 wechat-decrypt

```bash
cd /Users/cuip/projects
git clone https://github.com/ylytdeng/wechat-decrypt.git
cd wechat-decrypt
pip install -r requirements.txt
```

#### 步骤二：提取密钥并解密（一次性操作，需微信运行中）

```bash
# 编译 macOS 密钥扫描器
cd wechat-decrypt
make keys    # 或: gcc -o find_keys find_all_keys_macos.c -framework Security

# 提取密钥（需 root 权限以使用 Mach VM API）
sudo ./find_keys
# 输出: all_keys.json

# 解密所有数据库
python main.py decrypt
# 输出: decrypted/ 目录，包含明文 SQLite 文件
```

#### 步骤三：启动 HTTP 服务

```bash
# 前台运行（测试用）
python monitor_web.py
# → http://127.0.0.1:5678

# 后台运行（生产用）
nohup python monitor_web.py > ~/wechat-decrypt.log 2>&1 &

# 验证
curl -s http://127.0.0.1:5678/api/sessions | python -m json.tool
```

#### 步骤四：部署 GenericAgent SOP

```bash
# 将 SOP 复制到 GenericAgent 记忆目录
cp /path/to/wechat_data_query_sop.md /Users/cuip/projects/GenericAgent/memory/

# 验证 GenericAgent 能访问 API
cd /Users/cuip/projects/GenericAgent
python agentmain.py
# 然后问："检查微信数据服务是否可用"
```

#### 步骤五：（可选）launchd 后台服务

```xml
<!-- /Library/LaunchDaemons/com.wechat-decrypt.server.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.wechat-decrypt.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/Users/cuip/projects/wechat-decrypt/monitor_web.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/cuip/projects/wechat-decrypt</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/var/log/wechat-decrypt.log</string>
    <key>StandardErrorPath</key>
    <string>/var/log/wechat-decrypt.err</string>
</dict>
</plist>
```

```bash
sudo launchctl load /Library/LaunchDaemons/com.wechat-decrypt.server.plist
```

> 说明：wechat-decrypt 的 `monitor_web.py` 是标准 Flask/HTTP 进程，
> 不像 chatlog_alpha，它**没有 TUI 依赖**，可以在 launchd 下正常运行。

### 5.3 Claude Code MCP 集成（并行通道）

除 GenericAgent 外，wechat-decrypt 可同时通过 MCP 服务 Claude Code：

```json
// ~/.claude/settings.json 或项目配置
{
  "mcpServers": {
    "wechat": {
      "command": "python",
      "args": ["/Users/cuip/projects/wechat-decrypt/mcp_server.py"],
      "cwd": "/Users/cuip/projects/wechat-decrypt"
    }
  }
}
```

这提供**双通道访问**：
- **GenericAgent**：自进化 Agent，具备记忆、浏览器控制、多前端
- **Claude Code**：编码会话中直接调用 MCP 工具

两者读取同一套解密后的 SQLite 文件——互不冲突。

---

## 6. 使用场景

### 6.1 信息检索

| 场景 | 用户输入 | Agent 行为 |
|------|---------|-----------|
| 最近聊天 | "张三最近说了什么" | `GET /api/history?chat=张三&limit=20` → 摘要 |
| 关键词搜索 | "谁提过'周末聚餐'" | `GET /api/history?keyword=周末聚餐` → 列出匹配 |
| 时间范围 | "上周一的群聊" | 计算时间戳 → `GET /api/history?start_time=...` |
| 联系人查找 | "找一下李四的微信" | `GET /api/contacts?query=李四` |

### 6.2 分析与洞察

| 场景 | 用户输入 | Agent 行为 |
|------|---------|-----------|
| 聊天摘要 | "总结今天产品群的讨论" | 拉取今日全部消息 → LLM 摘要 |
| 决策追踪 | "我们关于 API 设计达成了什么共识" | 关键词搜索 → 过滤决策类消息 → 汇编 |
| 关系洞察 | "这个月和谁聊得最多" | 拉取会话统计 → 排名 → 可视化 |

### 6.3 实时监控

| 场景 | 用户输入 | Agent 行为 |
|------|---------|-----------|
| 关键词告警 | "有人提到'线上故障'就通知我" | SSE 流 → 关键词过滤 → 桌面通知 |
| 静默检测 | "值班群超过30分钟没人说话就提醒" | 周期性轮询 → 间隙检测 → 告警 |

### 6.4 跨工具协作流程（GenericAgent + 微信 Bot）

```
[微信用户] --发消息--> [微信 Bot (wechatapp.py)]
                                |
                                v
                        [GenericAgent 核心]
                                |
                        +-------+-------+
                        |               |
                [LLM 推理]      [wechat-decrypt API]
                        |               |
                        +-------+-------+
                                |
                                v
                        [组合回复]
                                |
                                v
[微信用户] <--回复---- [微信 Bot]
```

示例：同事问"上周会议我们达成了什么共识？"
→ Bot 收到消息 → Agent 搜索聊天记录 → 找到相关消息 → 组合回答 → 通过 Bot 发回

---

## 7. 安全考量

### 7.1 数据隐私

| 风险 | 缓解措施 |
|------|---------|
| 聊天数据泄漏 | 全部本地处理，HTTP 绑定 127.0.0.1 |
| API 密钥暴露 | wechat-decrypt 不使用外部 API |
| 记忆持久化 | GenericAgent L4 归档可定期清理 |
| LLM 上下文泄漏 | 敏感分析使用本地模型（Ollama） |

### 7.2 访问控制

```
wechat-decrypt HTTP: 仅绑定 127.0.0.1（无远程访问）
GenericAgent: 单用户本地进程
微信 Bot: Token 认证（ilinkai API）
解密数据库: chmod 600，仅用户可读
```

### 7.3 运维安全

- **禁止**将 `all_keys.json` 或解密数据库提交到 git
- **禁止**将 5678 端口暴露到网络（如需远程访问使用 SSH 隧道）
- **定期轮换**微信 Bot Token
- **审计** GenericAgent 的 `memory/` 目录，防止无意中持久化敏感数据

---

## 8. 可选增强：chatlog_alpha 作为备选引擎

如果 chatlog_alpha 未来添加 headless 模式，可作为语义搜索层：

```
GenericAgent
    |
    +---> wechat-decrypt HTTP (:5678)    # 主引擎：基础查询、实时监听
    |
    +---> chatlog_alpha HTTP (:5030)     # 备选：语义搜索、RAG、知识图谱
```

当前临时方案（基于 tmux）：

```bash
# 在 tmux 中启动 chatlog（需要 TUI）
tmux new-session -d -s chatlog 'sudo /path/to/chatlog'
# 在 chatlog TUI 中启用 HTTP 服务
# API 可用于 :5030，MCP 可用于 :5030/mcp
```

在 GenericAgent SOP 中的集成方式：

```python
# 语义搜索（如果 chatlog 在运行）
try:
    r = requests.get('http://127.0.0.1:5030/api/v1/semantic/search',
                     params={'q': '推荐过什么好用的 Mac 工具', 'limit': 10}, timeout=3)
    results = r.json()
except:
    # 降级到 wechat-decrypt 关键词搜索
    r = requests.get('http://127.0.0.1:5678/api/history',
                     params={'keyword': 'Mac 工具'})
```

---

## 9. 实施路线图

### 第一阶段：基础集成（第 1 周）

- [x] 工具全景调研分析
- [ ] 安装 wechat-decrypt，提取密钥，解密数据库
- [ ] 启动 HTTP 服务，验证 API 端点
- [ ] 部署 SOP 到 GenericAgent memory/
- [ ] 端到端测试：自然语言 → 聊天记录查询

### 第二阶段：实时能力（第 2 周）

- [ ] SSE 实时监听技能
- [ ] 关键词告警 + 桌面通知
- [ ] 语音转录集成
- [ ] 每日群聊自动摘要

### 第三阶段：微信 Bot 集成（第 3 周）

- [ ] 连接 wechatapp.py 前端与数据查询
- [ ] 实现"查询历史"的 Bot 对话流
- [ ] 添加上下文感知回复（回复前先查历史记录）

### 第四阶段：高级智能（第 4 周+）

- [ ] （可选）chatlog_alpha 语义搜索集成
- [ ] 定期自动分析报告
- [ ] 跨会话智能（跨多个对话串联信息）
- [ ] 技能库贡献（分享自进化的技能）

---

## 10. 附录

### A. 工具版本矩阵

| 工具 | 版本 | 仓库 | 安装方式 |
|------|------|------|---------|
| GenericAgent | 0.1.0 | github.com/user/GenericAgent | `pip install -e .` |
| wechat-decrypt | 最新 | github.com/ylytdeng/wechat-decrypt | `pip install -r requirements.txt` |
| WeFlow | v4.5.1 | github.com/hicccc77/WeFlow | 从 Releases 下载 .dmg |
| chatlog_alpha | ~v0.2 | github.com/teest114514/chatlog_alpha | 从 Releases 下载二进制 |

### B. 端口分配

| 服务 | 端口 | 协议 | 绑定地址 |
|------|------|------|---------|
| wechat-decrypt Web UI | 5678 | HTTP + SSE | 127.0.0.1 |
| chatlog_alpha（可选） | 5030 | HTTP + SSE + MCP | 127.0.0.1 |
| WeFlow（可选） | 5031 | HTTP + SSE | 127.0.0.1 |
| GenericAgent Streamlit | 8501 | HTTP | 127.0.0.1 |

### C. 参考资料

- GenericAgent: `/Users/cuip/projects/GenericAgent/README.md`
- wechat-decrypt: `https://github.com/ylytdeng/wechat-decrypt`
- WeFlow: `https://github.com/hicccc77/WeFlow`
- chatlog_alpha: `https://github.com/teest114514/chatlog_alpha`
- wechat-cli: `https://github.com/huohuoer/wechat-cli`（已落后，不推荐）
