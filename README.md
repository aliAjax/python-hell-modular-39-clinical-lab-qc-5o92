# 临床实验室质量控制与结果拦截

只使用Python标准库和SQLite的模块化服务，默认端口`8339`。覆盖检测项目、质控品批次、质控规则、允许范围、仪器校准、连续偏差、趋势、失控、结果拦截、复测、调查、批次切换和历史更正。

## 模块结构

- `app.py`：参数解析、依赖组装和服务生命周期。
- `src/domain.py`：角色、领域异常和数据对象。
- `src/rules.py`：QC规则计算、状态机、校准与放行约束。
- `src/repository.py`：SQLite持久化、乐观锁、幂等和审计查询。
- `src/service.py`：用例编排、权限校验和版本控制。
- `src/http_api.py`：JSON接口和统一错误响应。
- `src/audit.py`：操作审计。
- `static/index.html`：最小演示页面。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8339
```

## 核心对象

`assay`为检测项目，`qc_lot`为质控品批次，`instrument`为仪器，`qc_run`为质控结果，`result_batch`为患者结果批次。

## 专业组归属

检验科各专业组共用同一套系统，权限按专业组隔离。`assay`、`qc_lot`、`result_batch` 带组归属；`qc_run` 通过其质控品批次继承组；`instrument` 为共用资源。

- 跨组操作（放行、创建、指派等）一律拒绝，并在审计中记录操作者、角色、来源组与目标组。
- 检测项目换组后，其下批次的组确认失效，需重新确认归属；批次重新确认时必须与检测项目当前组一致，杜绝“项目归一组、批次在另一组”。
- 历史数据缺组标记：升级时先落到总务组（`general`）并标记待指派，再逐条指派；单条失败不影响其余，可断点重试。

## 接口

- `GET /health`
- `GET /api/<kind>`，可用`?status=`过滤
- `GET /api/entities/<id>`
- `POST /api/<kind>`
- `POST /api/entities/<id>/actions`
- `POST /api/entities/<id>/group`（指派组归属）
- `GET /api/group-assignments`（待重新确认归属的对象）
- `POST /api/admin/migrate-groups`（升级：存量数据落到总务组）
- `POST /api/admin/group-assignments`（逐条指派，失败可重试）
- `GET /api/audit`

身份通过`X-User-Id`、`X-Role`和`X-Group`请求头传入。可选`Idempotency-Key`防止重复创建。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

质控规则为可运行的简化模型，包含1-3s、连续偏移和趋势检查，但不替代CLIA、ISO 15189、Westgard完整规则集或实验室信息系统接口。
