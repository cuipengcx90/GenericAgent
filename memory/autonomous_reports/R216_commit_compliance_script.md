
# R216 smyze commit合规率CI本地检查脚本

## 任务目标
基于R207分析（整体合规率29.4%，主要违规：中文开头40%+缺少冒号37%），编写 `check_commit_compliance.sh` 脚本，支持单仓库检查、多仓库扫描、pre-commit hook、CI模式，并对smyze 10仓库实测验证。

---

## 脚本功能

### 文件位置
`/Users/cuip/projects/GenericAgent/temp/check_commit_compliance.sh`

### 四种运行模式

| 模式 | 命令 | 用途 |
|------|------|------|
| 单仓库 | `./check_commit_compliance.sh [repo_path] [--count N]` | 检查单个仓库最近N条commit |
| 多仓库扫描 | `./check_commit_compliance.sh --scan-dir DIR [--count N]` | 批量扫描workspace下所有git仓库 |
| pre-commit hook | `./check_commit_compliance.sh --hook [msg_file]` | 拦截不合规commit，给出修正提示 |
| CI模式 | `./check_commit_compliance.sh --ci [base_branch] [--strict]` | 检查当前分支相对base的新增commits |

### 违规分类（6类）
1. **compliant** — 合规（`type(scope)?: subject`）
2. **chinese_colon** — 中文冒号（`fix：描述`）
3. **bracket_format** — 方括号格式（`[feat] 描述`）
4. **chinese_start** — 中文开头（无type前缀）
5. **no_colon** — 有type但缺冒号/空格
6. **other** — 其他格式（单词commit、Revert等）

### 技术实现
- 违规分类委托Python3处理（解决macOS BSD grep不支持`-P`的问题）
- 支持Unicode中文字符检测（`\u4e00-\u9fff`范围）
- 进度条可视化（`█░`字符，20格）
- 退出码：0=全部合规，1=存在违规（strict模式），2=参数错误

---

## 实测结果（2026-04-30，最近50条commits）

| 仓库 | 合规率 | 评级 | 主要违规类型 |
|------|--------|------|-------------|
| bar-deploy-server | **96%** (48/50) | ✅ 良好 | 中文开头×2 |
| tools | **56%** (28/50) | ⚠️ 待改进 | 其他格式×22（单词commit） |
| bar-cloud | **28%** (14/50) | ❌ 差 | 中文冒号×3+其他×27 |
| ruoyi-vue-pro-ee | **12%** (6/50) | ❌ 无规范 | 中文冒号×32（上游仓库） |
| bar-market-server | **5%** (2/38) | ❌ 无规范 | 中文开头×27 |
| bar-agent | **2%** (1/50) | ❌ 无规范 | 中文开头×9+缺冒号×9 |
| ruoyi-vue-pro-github | **0%** (0/1) | ❌ 无规范 | 中文冒号×1 |
| bar-service | **0%** (0/1) | ❌ 无规范 | 其他×1（Initial commit） |
| bar-deploy-server省市区 | **0%** (0/50) | ❌ 无规范 | 中文开头×50 |
| Quokka251 | **0%** (0/50) | ❌ 无规范 | 中文开头×16+方括号×3 |

**整体合规率：25%（99/390）**（R207基准29.4%，差异因扫描窗口不同）

---

## 合规率提升方案

### 第一优先级：部署pre-commit hook（立竿见影）

```bash
# 将脚本复制到各仓库并配置hook
cp check_commit_compliance.sh /path/to/repo/
# 在 .husky/commit-msg 中调用：
# bash check_commit_compliance.sh --hook "$1"
```

已通过 `deploy_git_hooks.py` 部署commit-msg hook的仓库（bar-deploy-server 96%）效果显著。**优先对 bar-market-server、bar-agent、bar-deploy-server省市区 补充部署**。

### 第二优先级：修复中文冒号（低成本高收益）

针对 bar-cloud（中文冒号×3）和 ruoyi-vue-pro-ee（中文冒号×32）：
```bash
# 一键修正历史commit中的中文冒号（仅供参考，需谨慎操作）
# 建议：在IDE中配置输入法，commit时自动切换英文
```

bar-cloud 若将中文冒号修正，合规率可从28%提升至**34%**。

### 第三优先级：团队规范培训

针对 bar-market-server（中文开头×27）、bar-deploy-server省市区（中文开头×50）：
- 在README中添加commit规范说明
- 提供常用type速查表：`feat/fix/docs/style/refactor/perf/test/chore`

### 预期效果

| 措施 | 影响仓库 | 预期合规率提升 |
|------|---------|--------------|
| 部署hook | bar-market-server/bar-agent/省市区 | 0%→80%+ |
| 修正中文冒号习惯 | bar-cloud/ruoyi-vue-pro-ee | +6~64% |
| 团队培训 | 全部 | +10~20% |
| **综合** | **全部** | **25%→60%+** |

---

## CI集成方式

```yaml
# .github/workflows/commit-check.yml
- name: Check commit compliance
  run: |
    bash check_commit_compliance.sh --ci main --strict
```

```bash
# pre-push hook（.husky/pre-push）
bash check_commit_compliance.sh --ci origin/main
```

---

## 坑点记录
- macOS BSD grep 不支持 `-P`（PCRE），需用 Python3 做正则分类
- `bar-service`/`ruoyi-vue-pro-github` 只有1条commit，样本量不足，合规率参考价值有限
- `tools` 仓库存在大量单词commit（`tls`/`fix`/`update`），属于个人工具仓库风格，可酌情豁免
