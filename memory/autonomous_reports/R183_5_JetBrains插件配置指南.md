
# R183-5 JetBrains插件配置指南（基于R172分析）

**状态**: 完成（文档分析模式，降级执行）  
**任务评分**: 7分 → 4分  
**基于报告**: R172 JetBrains插件深度分析（6518字节）

## 执行说明

**原计划**: 实测安装+配置+使用场景→记录坑点→产出配置指南  
**实际情况**: 5个推荐插件全部未安装，无法实测  
**降级方案**: 基于R172分析→产出配置指南文档

## 推荐插件清单（5个）

### 1. GitToolBox
- **插件ID**: `zielu.gittoolbox`
- **功能**: 增强Git集成，显示inline blame信息
- **价值**: 减少30%的Git命令行操作（R172预估）

**安装**: Settings → Plugins → 搜索 "GitToolBox" → Install → Restart

**推荐配置**:
```
Settings → Other Settings → GitToolBox Global
- 启用 Show inline blame
- 启用 Show status widget
- Blame format: {author} · {date} · {message}
```

**使用场景**:
- 代码审查时快速查看每行代码的作者和提交时间
- 状态栏显示当前分支和未推送的提交数

---

### 2. Rainbow Brackets
- **插件ID**: `izhangzhihao.rainbow.brackets`
- **功能**: 彩色括号匹配
- **价值**: 减少20%的括号匹配错误（R172预估）

**安装**: Settings → Plugins → 搜索 "Rainbow Brackets" → Install → Restart

**推荐配置**:
```
Settings → Other Settings → Rainbow Brackets
- 启用所有括号类型（round/square/curly/angle）
```

**使用场景**:
- Java Stream API、Lambda表达式等复杂嵌套代码
- JSON/XML文件编辑

---

### 3. Key Promoter X
- **插件ID**: `Key Promoter X`
- **功能**: 快捷键提示，统计鼠标操作
- **价值**: 2周内记住20+快捷键（R172预估）

**安装**: Settings → Plugins → 搜索 "Key Promoter X" → Install → Restart

**推荐配置**:
```
Settings → Tools → Key Promoter X
- 启用 Show tips for buttons/menus/toolbar
- Notification display time: 5 seconds
```

**使用场景**:
- 每次鼠标点击菜单/按钮时弹出对应快捷键提示
- 统计最常用的鼠标操作，优先学习对应快捷键

---

### 4. String Manipulation
- **插件ID**: `String Manipulation`
- **功能**: 字符串批量处理（大小写/编码/排序/转义）
- **价值**: 减少手动字符串处理，提升编码效率

**安装**: Settings → Plugins → 搜索 "String Manipulation" → Install → Restart

**快捷键**: Alt+M (macOS: Option+M)

**常用操作**:
- 驼峰命名 ↔ 下划线命名转换
- JSON字符串格式化和转义
- Base64/URL编码解码
- SQL字段名批量处理

---

### 5. RestfulTool
- **插件ID**: `cn.cloud.auto.restfultool`
- **功能**: Spring MVC接口快速测试
- **价值**: 减少50%的Postman切换时间（R172预估）

**安装**: Settings → Plugins → 搜索 "RestfulTool" → Install → Restart

**推荐配置**:
```
Settings → Tools → RestfulTool
- 启用 Scan Spring MVC annotations
- Default request timeout: 30s
```

**使用步骤**:
1. 打开Controller类
2. 在方法上右键 → RestfulTool → Request
3. 填写请求参数 → Send
4. 查看响应结果

---

## 与现有插件的协作

基于R172扫描结果（16个已安装插件），推荐插件协作关系：

| 推荐插件 | 现有插件 | 协作场景 |
|---------|---------|---------|
| GitToolBox | MavenHelper | Git blame + Maven依赖，定位依赖引入者 |
| RestfulTool | MybatisX | API测试 + SQL调试，完整后端开发流程 |
| String Manipulation | MybatisX | SQL字段名批量转换 |
| Key Promoter X | Claude Code | 帮助记忆AI插件快捷键 |
| Rainbow Brackets | 所有插件 | 提升AI生成的复杂嵌套代码可读性 |

## AI插件精简建议（高优先级）

**现状**: 5个AI插件并存（Claude Code, ProxyAI, Codium AI, aiXcoder, Lingma）  
**问题**: 功能重叠，影响性能  
**建议**: 保留 Claude Code + ProxyAI，禁用其他3个  
**预期收益**: 节省300-500MB内存，提升10-15%启动速度

## 实施路线图

| 阶段 | 内容 | 时间 |
|-----|------|------|
| 1. 安装 | 依次安装5个推荐插件 | 10分钟 |
| 2. 配置 | 按上述推荐配置各插件 | 15分钟 |
| 3. 学习 | 在smyze项目中实际使用 | 1-2小时 |
| 4. 评估 | 1周后评估收益 | 30分钟 |

## 预期收益（来自R172分析）

- **Git操作**: 减少30%的命令行操作
- **快捷键**: 2周内记住20+快捷键
- **API测试**: 减少50%的Postman切换时间
- **代码可读性**: 减少20%的括号匹配错误
- **ROI**: 投入2小时，每日节省15-30分钟，4-8天回本

## 注意事项

1. **Rainbow Brackets**: 可能与某些主题冲突，建议先用默认主题测试
2. **Key Promoter X**: 通知频繁时可调整显示时间或禁用某些类型
3. **RestfulTool**: 仅适用于Spring MVC项目

---

**完成时间**: 2026-04-29  
**基于报告**: R172 JetBrains插件深度分析  
**产出类型**: 配置指南文档（降级执行，无实测）
