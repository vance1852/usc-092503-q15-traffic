# 道路交通事故快处与处罚协同服务

本项目是一套可离线运行的 Python 后台，用于事故受理、道路风险研判、警力与拖车调度、结构化证据复核、责任认定、处罚执行和审计追溯。系统把同一事故从报警到结案的关键状态保存在 SQLite 中，角色权限覆盖接警员、调度员、事故处理民警、复核人员和审计人员。

## 目录

- `src/traffic_dispatch/`：事故风险指数、快处中心、道路走廊、应急资源、调度申请和响应情景；
- `src/evidence_review/`：采集设备、证据规范、结构化记录导入、一致性分析、复核租约和采信决定；
- `src/penalty_ops/`：事故案件、违法记录、风险告警、处置工单、处罚流转、审计，以及**驾驶记分资格账本**（`clock.py`/`rules.py`/`ledger.py`/`points.py`）；
- `fixtures/`：离线验收使用的证据规范与结构化事故记录；
- `tests/`：领域规则、事务边界、权限、HTTP API 和 CLI 验收测试。

## 驾驶记分资格账本

处罚执行人员需要判断驾驶人在滚动周期内的有效记分是否达到学习、暂扣或吊销（恢复资格）条件。账本采用事件溯源 + 纯函数折叠：

- **只追加事件，不改写旧余额**：已生效处罚按*违法发生时间*与当时适用规则写入 `penalty` 事件；撤销（`reversal`）、申诉变更（`appeal_adjustment`）均以新事件表达，旧事件行原样保留。折叠时被撤销处罚的有效贡献为 0（`status=voided`），被申诉变更的取变更后分值（`status=adjusted`）。
- **重复决定幂等**：同一处罚决定号重复登记直接返回既有事件，不产生第二条记分、不重复触发措施。
- **规则版本化**：`rules.py` 中每个规则版本有 `[effective_from, effective_to)` 生效区间，按违法发生时间选版；规则调整只影响其明确区间，旧违法永不按新规重算。
- **滚动 365 天窗口**：窗口开启时仍存活的上一期分值生成 `carryover` 结转分录；每条记分满 365 天生成 `expiry` 期满失效分录；满分学习完成（`study_completed`）与吊销重新资格（`requalified`）清零此前存活记分；暂扣届满（`restriction_lapsed`）只恢复驾驶资格、不清分。
- **措施是折叠的确定性投影**：余额向上跨越 12/24/36 时触发学习/暂扣/吊销，措施键绑定触发处罚事件（`kind|trigger_event_id`）。`reconcile` 按业务时钟确定性重算：新跨越幂等物化、不重复执行；回溯变化使跨越消失时措施置 `annulled`（行保留审计），届满/清零资格事件由折叠重新合成，不留幽灵记录。
- **业务时钟**：`SystemClock`/`FrozenClock` 可注入；`advance` 推进时钟，限制期届满自动完结措施并在查询中形成新的资格事件。

接口：`POST /drivers`、`POST /drivers/{id}/penalties`、`/reversals`、`/appeal-adjustments`、`/study-completions`、`/advance`，`GET /drivers/{id}/ledger`（当前有效分值、`upcoming` 即将触发措施、`active_measures`、`formation` 形成过程、全部措施）、`GET /drivers/{id}/events`、`GET /rules`。


## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅依赖 Python 标准库与 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -q
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m traffic_dispatch.acceptance --workspace .
PYTHONPATH=src python3 -m evidence_review.acceptance --workspace .
PYTHONPATH=src python3 -m penalty_ops.acceptance
```

验收会建立临时 SQLite 数据库，登记事故风险记录、快处中心、道路走廊和应急资源，完成调度与证据复核，并输出 JSON 结果。命令不会访问公网，也不需要额外数据库、队列或常驻服务。

## HTTP API

```bash
PYTHONPATH=src python3 -m traffic_dispatch.api --database traffic.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m evidence_review.api --database evidence.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m penalty_ops.api --database penalties.sqlite3 --host 127.0.0.1 --port 8082
```

三个服务均提供 `GET /health`，其余接口使用 JSON。SQLite 文件保存业务状态、幂等结果和审计记录，进程重启后可继续查询。
