
# R14 — notify_group vs notify_feishu 行为差异实测报告

**执行时间**：2026-04-24  
**任务来源**：R3发现P1通知推送盲区，无notify使用规范  
**验收标准**：用实测数据驱动，产出notify_sop.md

---

## 一、函数架构对比

| 维度 | notify_feishu | notify_group |
|------|--------------|--------------|
| 推送目标 | 个人（open_id） | 飞书群（Webhook） |
| 认证方式 | OAuth tenant_access_token | HMAC-SHA256签名 |
| 错误处理 | try/except，静默打印 | 无try/except，直接抛异常 |
| 返回值 | None | dict（含StatusCode） |
| 依赖 | llmcore.mykeys（keychain） | config/feishu_group.json |
| 网络调用数 | 2次（获取token+发消息） | 1次 |

---

## 二、实测结果（6个用例）

### 正常场景

| 测试 | 结果 | 耗时 |
|------|------|------|
| notify_feishu 正常推送 | ✅ 成功 | 0.60s |
| notify_group 正常推送 | ✅ 成功，响应`{'code':0,'msg':'success'}` | 0.29s |

**结论**：notify_group比notify_feishu快约2倍（少一次token请求）。

### 异常场景

| 测试 | notify_feishu | notify_group |
|------|--------------|--------------|
| 错误接收方ID | 静默失败（打印`[飞书推送失败] HTTP Error 400`），无异常 | N/A |
| 错误secret签名 | N/A | ⚠️ **返回success！飞书不校验签名错误** |
| 网络超时 | ✅ 静默处理，不抛异常 | ❌ **抛出URLError，调用方需自行捕获** |

---

## 三、关键发现

### 🔴 P0：notify_group超时会崩溃调用方
```python
# 危险：超时直接抛出，未被捕获会中断整个reflect脚本
notify_group("消息")  # URLError: timed out
```

### 🟡 P1：notify_feishu静默失败无感知
```python
notify_feishu("消息", open_id="错误ID")
# 只打印到stdout，调用方无法感知失败
# 返回None，无法判断是否成功
```

### 🟢 发现：飞书Webhook不校验签名
- 错误secret仍返回`{'code':0,'msg':'success'}`
- 说明签名校验在飞书服务端可能是可选的（或该群未开启）

---

## 四、notify_sop（使用规范）

### 场景选择

```
需要通知个人 → notify_feishu(text, open_id=FEISHU_USER)
需要通知群组 → notify_group(text)  ⚠️ 需包裹try/except
```

### 安全用法模板

```python
# notify_feishu：已有try/except，可直接调用
from reflect_common import notify_feishu
notify_feishu("消息内容")  # 失败静默，不影响主流程

# notify_group：必须自行包裹
from notify_feishu_group import notify_group
try:
    resp = notify_group("消息内容")
    if resp.get("code") != 0:
        print(f"[群通知失败] {resp}")
except Exception as e:
    print(f"[群通知异常] {e}")  # 超时/网络错误不崩溃
```

### 何时用哪个

| 场景 | 推荐 | 理由 |
|------|------|------|
| reflect脚本结果通知 | notify_feishu | 静默失败不影响脚本 |
| 重要告警（价格/监控） | notify_group（包裹try） | 群可见，响应快 |
| 需要确认送达 | notify_group（检查resp.code） | 有返回值可验证 |
| token可能失效时 | notify_group | 不依赖OAuth token |

### 已知坑点

1. **notify_group超时不捕获**：必须自行try/except
2. **notify_feishu失败无返回值**：无法程序化判断是否成功
3. **notify_feishu需要keychain**：离线/keychain损坏时完全失效
4. **飞书签名校验可能未开启**：错误secret仍能发送成功

---

## 五、修复建议（待用户审批）

### 修复notify_group（加try/except）

```python
def notify_group(text: str) -> dict:
    """推送纯文本到飞书群"""
    try:
        cfg = _load_cfg()
        # ... 原有逻辑 ...
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except Exception as e:
        print(f"[群通知失败] {e}")
        return {"code": -1, "msg": str(e)}
```

### 修复notify_feishu（返回成功状态）

```python
def notify_feishu(text: str, open_id: str = FEISHU_USER) -> bool:
    try:
        # ... 原有逻辑 ...
        return True
    except Exception as e:
        print(f"[飞书推送失败] {e}")
        return False
```
