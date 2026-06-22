# GenericAgent 使用手册索引

> 统一入口：所有模块的最佳实践文档

---

## 📚 手册列表

| 模块 | 文档 | 说明 |
|------|------|------|
| Reflect 监控触发 | [reflect_best_practices.md](./reflect_best_practices.md) | 6大应用场景：系统监控/邮件/定时报告/GitHub/文件归档/行情监控 |

---

## 📁 项目结构

```
GenericAgent/
├── docs/                        ← 📖 所有使用手册在此
│   ├── INDEX.md                 ← 本文件（总入口）
│   └── reflect_best_practices.md
├── reflect_scripts/             ← Reflect 模式脚本
├── memory/                      ← Agent 记忆与 SOP
└── temp/                        ← 工作目录
```

---

> 新增手册时，在上方表格追加一行即可。