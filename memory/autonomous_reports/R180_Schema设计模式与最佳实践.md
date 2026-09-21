
# R180 数据库Schema设计模式与最佳实践

## 执行时间
2026-04-29

## 数据来源
基于51个历史DB分析报告，重点分析5个典型案例：
- R139: 通用DB分析工具(Paste/Timing2/iStat Menus)
- R117: DingTalk本地DB深挖
- R119: sqlite快照任务诊断
- R121: 飞书残留DB真伪复核
- R129: Application Support未探索DB全量扫描

## Schema设计模式分类

### 1. 时间序列模式 (4个报告)

**典型案例: iStat Menus**
- **设计**: day_/hour_/week_/month_前缀表(64表中63个时间字段)
- **场景**: 系统监控数据(battery/cpu/disk/gpu/memory/network/sensor)
- **优点**: 
  - 按时间粒度分表，查询性能高
  - 历史数据归档方便
  - 支持多维度聚合分析

**设计要点**:
```sql
-- 按时间粒度分表
CREATE TABLE hour_networkhistory (
    timestamp INTEGER,
    bytes_in INTEGER,
    bytes_out INTEGER,
    ...
);

CREATE TABLE day_networkhistory (
    date TEXT,
    total_bytes_in INTEGER,
    total_bytes_out INTEGER,
    ...
);
```

**最佳实践**:
1. 时间字段使用INTEGER存储Unix timestamp(便于计算)
2. 按查询频率设计粒度(hour/day/week/month)
3. 定期归档历史数据(如保留最近3个月hour级别，1年day级别)
4. 索引时间字段: `CREATE INDEX idx_timestamp ON hour_networkhistory(timestamp)`

**反模式**:
- ❌ 所有粒度数据存一张表(查询慢)
- ❌ 时间字段用TEXT存储(无法高效范围查询)
- ❌ 无归档策略(数据无限增长)

### 2. KV存储模式 (2个报告)

**典型案例: Paste**
- **设计**: Z_PRIMARYKEY表(key-value结构)
- **场景**: 配置存储、元数据管理
- **优点**: 
  - 灵活扩展，无需ALTER TABLE
  - 适合异构数据存储

**设计要点**:
```sql
CREATE TABLE kv_store (
    key TEXT PRIMARY KEY,
    value TEXT,
    type TEXT,  -- 'string'/'int'/'json'
    updated_at INTEGER
);
```

**最佳实践**:
1. key使用命名空间前缀(如`app.config.theme`)
2. value统一用TEXT存储，type字段标记类型
3. 复杂对象用JSON序列化
4. 添加updated_at字段追踪变更

**反模式**:
- ❌ key无命名规范(冲突风险)
- ❌ 大对象直接存value(应拆分或用BLOB)
- ❌ 无类型标记(反序列化困难)

### 3. 外键关联模式 (1个报告)

**典型案例: Timing2**
- **设计**: 11个外键关联(Application→AppActivity, Project→TaskActivity等)
- **场景**: 应用活动追踪(300K条AppActivity记录)
- **优点**: 
  - 数据一致性保证
  - 支持级联删除
  - 便于JOIN查询

**设计要点**:
```sql
CREATE TABLE Application (
    id INTEGER PRIMARY KEY,
    name TEXT,
    bundle_id TEXT UNIQUE
);

CREATE TABLE AppActivity (
    id INTEGER PRIMARY KEY,
    app_id INTEGER,
    start_time INTEGER,
    duration INTEGER,
    FOREIGN KEY (app_id) REFERENCES Application(id) ON DELETE CASCADE
);

CREATE INDEX idx_app_activity_app_id ON AppActivity(app_id);
CREATE INDEX idx_app_activity_time ON AppActivity(start_time);
```

**最佳实践**:
1. 外键字段必须建索引(避免JOIN性能问题)
2. 明确ON DELETE策略(CASCADE/SET NULL/RESTRICT)
3. 高频JOIN的字段考虑冗余(空间换时间)
4. 使用EXPLAIN QUERY PLAN验证JOIN性能

**反模式**:
- ❌ 外键字段无索引(JOIN慢)
- ❌ 过度规范化(JOIN层级>3)
- ❌ 无ON DELETE策略(孤儿数据)

### 4. 大表设计 (5个报告)

**典型案例: Paste ACHANGE表(524K行)**
- **设计**: 记录每次剪贴板复制操作
- **挑战**: 
  - 插入频繁(每次复制触发)
  - 查询慢(全表扫描)
  - 存储膨胀(106MB DB)

**优化策略**:

**4.1 分区策略**
```sql
-- 按月分表
CREATE TABLE clipboard_2026_04 (...);
CREATE TABLE clipboard_2026_05 (...);

-- 或使用SQLite ATTACH实现逻辑分区
ATTACH DATABASE 'clipboard_archive.db' AS archive;
```

**4.2 索引优化**
```sql
-- 复合索引(覆盖常见查询)
CREATE INDEX idx_clipboard_time_type ON clipboard(timestamp, content_type);

-- 部分索引(只索引活跃数据)
CREATE INDEX idx_recent ON clipboard(timestamp) WHERE timestamp > strftime('%s', 'now', '-30 days');
```

**4.3 数据归档**
```sql
-- 定期归档旧数据
INSERT INTO archive.clipboard_old SELECT * FROM clipboard WHERE timestamp < ?;
DELETE FROM clipboard WHERE timestamp < ?;
VACUUM;  -- 回收空间
```

**最佳实践**:
1. 预估增长速度，设计分区策略
2. 高频查询字段必须建索引
3. 定期ANALYZE更新统计信息
4. 考虑使用WITHOUT ROWID表(节省空间)

**反模式**:
- ❌ 无索引的大表(全表扫描)
- ❌ 无归档策略(数据无限增长)
- ❌ 频繁UPDATE大字段(碎片化)

## Schema演进管理

### 版本控制策略

**方案1: Migration文件**
```
migrations/
  001_create_users.sql
  002_add_email_index.sql
  003_add_avatar_column.sql
```

**方案2: 版本表**
```sql
CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY,
    description TEXT,
    applied_at INTEGER
);

-- 应用迁移时记录
INSERT INTO schema_version VALUES (3, 'add avatar column', strftime('%s', 'now'));
```

**最佳实践**:
1. 每次schema变更创建独立migration文件
2. migration文件只增不改(保证可重现)
3. 提供rollback脚本(回滚机制)
4. 生产环境变更前在副本上测试

### 兼容性设计

**向后兼容原则**:
1. **新增列**: 必须有默认值或允许NULL
   ```sql
   ALTER TABLE users ADD COLUMN avatar TEXT DEFAULT '';
   ```

2. **删除列**: 分两步(先停用，再删除)
   ```sql
   -- Step 1: 应用代码停止使用该列(部署后观察1周)
   -- Step 2: 删除列
   ALTER TABLE users DROP COLUMN old_field;
   ```

3. **重命名**: 使用视图过渡
   ```sql
   ALTER TABLE users RENAME COLUMN name TO full_name;
   CREATE VIEW users_compat AS SELECT full_name AS name, ... FROM users;
   ```

**反模式**:
- ❌ 直接删除列(破坏旧版本应用)
- ❌ 修改列类型(数据丢失风险)
- ❌ 无测试直接上生产

## 性能优化清单

### 查询优化
- [ ] 高频查询字段建索引
- [ ] 复合索引覆盖WHERE+ORDER BY
- [ ] 避免SELECT *(只查需要的列)
- [ ] 使用EXPLAIN QUERY PLAN分析

### 写入优化
- [ ] 批量INSERT使用事务
- [ ] 高频写入表减少索引数量
- [ ] 考虑使用WAL模式(并发写入)
- [ ] 定期VACUUM回收空间

### 存储优化
- [ ] TEXT字段长度限制(避免大对象)
- [ ] BLOB字段考虑外部存储
- [ ] 使用INTEGER PRIMARY KEY(自动ROWID)
- [ ] 归档历史数据

## 供应链SaaS场景建议

### 订单表设计
```sql
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    order_no TEXT UNIQUE NOT NULL,
    customer_id INTEGER,
    status TEXT CHECK(status IN ('pending','paid','shipped','completed','cancelled')),
    total_amount INTEGER,  -- 分为单位(避免浮点精度问题)
    created_at INTEGER,
    updated_at INTEGER,
    FOREIGN KEY (customer_id) REFERENCES customers(id)
);

CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_orders_status_time ON orders(status, created_at);
CREATE INDEX idx_orders_no ON orders(order_no);  -- 业务查询
```

### 库存表设计
```sql
CREATE TABLE inventory (
    product_id INTEGER PRIMARY KEY,
    quantity INTEGER NOT NULL DEFAULT 0,
    reserved INTEGER NOT NULL DEFAULT 0,  -- 已预留数量
    version INTEGER NOT NULL DEFAULT 0,   -- 乐观锁
    updated_at INTEGER,
    FOREIGN KEY (product_id) REFERENCES products(id)
);

-- 更新时使用乐观锁
UPDATE inventory 
SET quantity = quantity - ?, version = version + 1 
WHERE product_id = ? AND version = ?;
```

### 审计日志表设计
```sql
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY,
    table_name TEXT,
    record_id INTEGER,
    action TEXT,  -- 'INSERT'/'UPDATE'/'DELETE'
    old_value TEXT,  -- JSON
    new_value TEXT,  -- JSON
    user_id INTEGER,
    created_at INTEGER
);

CREATE INDEX idx_audit_table_record ON audit_log(table_name, record_id);
CREATE INDEX idx_audit_time ON audit_log(created_at);
```

## 工具推荐

### Schema管理工具
1. **Flyway/Liquibase**: Java生态migration工具
2. **Alembic**: Python生态(SQLAlchemy)
3. **golang-migrate**: Go生态
4. **db_analyzer.py**: 本项目通用DB分析工具(R139)

### 监控工具
1. **EXPLAIN QUERY PLAN**: SQLite内置查询分析
2. **sqlite3_analyzer**: 存储分析工具
3. **DB Browser for SQLite**: 可视化工具

## 总结

### 核心原则
1. **索引优先**: 高频查询字段必须建索引
2. **分区思维**: 大表按时间/业务维度分区
3. **演进兼容**: schema变更保证向后兼容
4. **定期维护**: ANALYZE/VACUUM/归档

### 常见陷阱
1. 无索引的大表(性能杀手)
2. 过度规范化(JOIN层级过深)
3. 无归档策略(数据无限增长)
4. 直接修改schema(破坏兼容性)

### 检查清单
- [ ] 每张表有PRIMARY KEY
- [ ] 外键字段有索引
- [ ] 时间字段用INTEGER存储
- [ ] 金额字段用INTEGER(分为单位)
- [ ] 枚举字段用CHECK约束
- [ ] 有schema_version表追踪版本
- [ ] 有定期VACUUM计划
- [ ] 有数据归档策略

---

**完成时间**: 2026-04-29  
**数据来源**: 51个DB分析报告(重点分析5个)  
**设计模式**: 4类(时间序列/KV存储/外键关联/大表设计)  
**最佳实践**: 15条  
**供应链SaaS**: 3个场景示例(订单/库存/审计日志)
