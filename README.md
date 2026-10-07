# 临床实验室质量控制与结果拦截

只使用Python标准库和SQLite的模块化服务，默认端口`8339`。覆盖检测项目、质控品批次、质控规则、允许范围、仪器校准、连续偏差、趋势、失控、结果拦截、复测、调查、批次切换和历史更正。

## 模块结构

- `app.py`：参数解析、依赖组装和服务生命周期。
- `src/domain.py`：角色、专业组常量、领域异常和数据对象。
- `src/rules.py`：QC规则计算、状态机、校准与放行约束。
- `src/repository.py`：SQLite持久化、乐观锁、幂等、schema升级、组归属与审计查询。
- `src/service.py`：用例编排、组归属校验、越权记录、改组级联和指派重试。
- `src/http_api.py`：JSON接口和统一错误响应。
- `src/audit.py`：操作审计。
- `static/index.html`：最小演示页面。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8339
```

首次启动自动完成schema升级（幂等，可重复执行）。

## 专业组归属与越权控制

系统不再是一套全局权限。检测项目（`assay`）、质控品批次（`qc_lot`）和患者结果批次（`result_batch`）都携带：

- `group_id`：所属专业组（对应`lab_group`）；
- `group_confirmed`：归属是否已确认。

规则：

- 请求通过`X-User-Id`、`X-Role`和`X-Group-Id`头携带身份；组必须已在`lab_group`中登记。
- 系统内置`general`（总务组）。质控品批次和患者结果批次的组永远等于其检测项目的组，创建时随项目落位，不能单独划组。
- 跨组创建或流转一律拒绝（HTTP 403），并写入`group_violations`：记录操作人、角色、来源组、目标组、动作和对象，可经`GET /api/group-violations`查询。例如免疫组分析员放行生化组的患者结果批次会被直接拒绝。
- `analyst`可以在**本组内**放行患者结果批次（放行仍要求质控通过、仪器就绪、校准有效、无未处理拦截）。
- `group_confirmed=false`的对象处于冻结状态，任何业务操作返回409，直到重新确认归属。
- 检测项目改组（`assign-group`）只能由当前组`supervisor`/`admin`执行；项目、相关质控品批次和患者结果批次在**同一事务**内重指到新组，批次置为待确认，旧组范围立即失效。任何时刻项目与批次都不会分属两组。
- 待确认批次只能指派（重新确认）到其检测项目所在组，由目标组`supervisor`/`admin`或总务组`coordinator`执行。

### 升级已有数据

旧数据没有组标记。schema v1升级时：

1. 自动创建总务组`general`；
2. 所有检测项目、质控品批次、患者结果批次补标为`group_id=general`、`group_confirmed=false`；
3. 同步进入组指派队列`group_assignment_queue`，等待逐条指派。

指派接口：

- `POST /api/entities/<id>/assign-group`，体：`{"group_id": "..."}`——单条指派/重新确认；
- `POST /api/group-assignments/process`，体：`{"targets": {"<实体id>": "<组id>"}}`——总务组逐条处理队列。只点名要处理的条目；未点名的批次可在其检测项目落位后，再用空目标跑一轮自动跟随；
- `POST /api/group-assignments/retry`——把上一轮失败（`failed`）的条目带着原目标重新入队，从未完成处继续逐条重试。单条失败只记录错误（`last_error`）不中断整批。
- `GET /api/group-assignments?status=pending|failed`查看队列。

## 核心对象

`lab_group`为专业组，`assay`为检测项目，`qc_lot`为质控品批次，`instrument`为仪器，`qc_run`为质控结果，`result_batch`为患者结果批次。

## 接口

- `GET /health`
- `GET /api/<kind>`，可用`?status=`过滤
- `GET /api/entities/<id>`
- `POST /api/<kind>`
- `POST /api/entities/<id>/actions`
- `POST /api/entities/<id>/assign-group`
- `POST /api/group-assignments/process`、`POST /api/group-assignments/retry`
- `GET /api/group-assignments`、`GET /api/group-violations`
- `GET /api/audit`

身份通过`X-User-Id`、`X-Role`和`X-Group-Id`请求头传入。可选`Idempotency-Key`防止重复创建。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

质控规则为可运行的简化模型，包含1-3s、连续偏移和趋势检查，但不替代CLIA、ISO 15189、Westgard完整规则集或实验室信息系统接口。
