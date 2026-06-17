# keychain SOP - 密钥管理最佳实践

## 推荐方案：keyring（系统级钥匙串）

### 为什么选keyring
- ✅ **真加密**：macOS Keychain使用AES-256，系统级保护
- ✅ **访问控制**：需用户授权/Touch ID，进程隔离
- ✅ **跨平台**：支持macOS/Windows/Linux统一API
- ❌ **依赖**：需安装`pip install keyring`
- ❌ **首次慢**：写入166ms（vs keychain.py 1ms）

### 基本用法
```python
import keyring

# 写入（首次可能需Touch ID授权）
keyring.set_password("GenericAgent", "openai_key", "sk-xxx...")

# 读取
api_key = keyring.get_password("GenericAgent", "openai_key")

# 删除
keyring.delete_password("GenericAgent", "openai_key")
```

### 迁移示例（从keychain.py迁移到keyring）
```python
import keyring
from keychain import keys as old_keys

# 1. 读取旧keychain所有key
old_key_names = old_keys.ls()

# 2. 逐个迁移到keyring
for name in old_key_names:
    value = old_keys.__getattr__(name).use()
    keyring.set_password("GenericAgent", name, value)
    print(f"✅ 已迁移: {name}")

# 3. 验证迁移
for name in old_key_names:
    new_val = keyring.get_password("GenericAgent", name)
    old_val = old_keys.__getattr__(name).use()
    assert new_val == old_val, f"❌ {name} 迁移失败"
print("✅ 所有密钥迁移成功")
```

---

## Fallback方案：keychain.py（XOR混淆）

⚠️ **安全局限性**：
- 加密方式：XOR + SHA256混淆（**可逆**，非真加密）
- 存储位置：`~/ga_keychain.enc`（任何进程可读）
- 适用场景：零依赖环境、低价值密钥、快速原型

### 导入
```python
import keychain          # 直接 import（已在 PATH）
from keychain import keys
```

### 写入 key
```python
# 方式1：直接传值
keychain.keys.set("openai_key", "sk-xxx...")
# 方式2：从文件读（自动strip）
keychain.keys.set("openai_key", file="/path/to/key.txt")
```

### 读取 key
```python
# 获取 SecretStr 对象（不会打印明文）
secret = keychain.keys.openai_key
# 实际使用时调用 .use()
api_key = keychain.keys.openai_key.use()
```
⚠️ 禁止 `print(keychain.keys.openai_key.use())`，日志会泄露明文。

### 列出所有 key 名
```python
keychain.keys.ls()  # 返回 ['openai_key', 'feishu_token', ...]
```

---

## 选择指南

| 场景 | 推荐方案 |
|------|---------|
| 生产环境API Key | keyring |
| 高价值密钥（支付/银行） | keyring + 定期轮换 |
| CI/CD环境 | 环境变量（不持久化） |
| 零依赖快速原型 | keychain.py |
| 路径/端口等非敏感配置 | 环境变量或代码 |

## 坑点
- keyring首次写入需Touch ID授权，自动化脚本需提前手动授权一次
- keychain.py的`SecretStr`对象需`.use()`才能传给SDK
- 文件损坏时keychain.py自动备份为`.enc.bak`，重置为空