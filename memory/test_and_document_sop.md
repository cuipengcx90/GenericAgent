# 实测记录 SOP（test_and_document_sop）

## 适用场景
- 新工具/接口首次集成
- 验证已有功能是否正常
- 新模块首次使用前的行为确认

---

## 标准步骤

1. **最小测试**: 写≤20行测试代码，覆盖happy path
2. **失败路径**: 主动测试异常情况（超时/权限/空返回）
3. **记录行为**: 实际输出 vs 预期，差异点重点标注
4. **提炼坑点**: ≥2个，格式：「现象→根因→解法」
5. **写SOP**: 到`memory/xxx_sop.md`，patch方式，同步L1 insight

---

## 坑点

- `notify_group`超时抛异常（需try/except）
- `notify_feishu`静默失败（需检查返回值）
- MCP需3步握手（initialize→notifications→工具调用）
- 禁全量读取大文件，抽样验证即可