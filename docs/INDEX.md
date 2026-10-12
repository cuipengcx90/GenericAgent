# GenericAgent 使用手册索引

> 统一入口：所有模块的最佳实践文档

---

## 📚 手册列表

| 模块 | 文档 | 说明 |
|------|------|------|
| Reflect 监控触发 | [reflect_best_practices.md](./reflect_best_practices.md) | 6大应用场景：系统监控/邮件/定时报告/GitHub/文件归档/行情监控 |
| 本机成本与稳定性口径 | [LOCAL_COST_AND_STABILITY_GUIDE.md](./LOCAL_COST_AND_STABILITY_GUIDE.md) | 本机 GA 慢/费 token/老断/质量差的根因与解决方案；`context_win` 口径；13 个渠道与全部参数配置表；L1 规范；失败与费用口径；运维与自检手册（配套 `tools/ga_diag.py`、`plugins/ga_guard.py`、`tools/l1_compact.py`） |

---

## 📁 项目结构

```
GenericAgent/
├── docs/                        ← 📖 所有使用手册在此
│   ├── INDEX.md                 ← 本文件（总入口）
│   ├── reflect_best_practices.md
│   └── LOCAL_COST_AND_STABILITY_GUIDE.md  ← 本机成本与稳定性口径
├── reflect_scripts/             ← Reflect 模式脚本
├── memory/                      ← Agent 记忆与 SOP
└── temp/                        ← 工作目录
```

---

> 新增手册时，在上方表格追加一行即可。