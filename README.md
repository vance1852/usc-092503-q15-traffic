# 道路交通事故快处与处罚协同服务

本项目是一套可离线运行的 Python 后台，用于事故受理、道路风险研判、警力与拖车调度、结构化证据复核、责任认定、处罚执行和审计追溯。系统把同一事故从报警到结案的关键状态保存在 SQLite 中，角色权限覆盖接警员、调度员、事故处理民警、复核人员和审计人员。

## 目录

- `src/traffic_dispatch/`：事故风险指数、快处中心、道路走廊、应急资源、调度申请和响应情景；
- `src/evidence_review/`：采集设备、证据规范、结构化记录导入、一致性分析、复核租约和采信决定；
- `src/penalty_ops/`：事故案件、违法记录、风险告警、处置工单、处罚流转和审计；
- `src/point_ledger/`：驾驶证记分资格账本——追加式记分事件、滚动周期、规则版本生效区间、学习/暂扣/恢复措施的确定性重算与幂等物化；
- `fixtures/`：离线验收使用的证据规范与结构化事故记录；
- `tests/`：领域规则、事务边界、权限、HTTP API 和 CLI 验收测试。

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
PYTHONPATH=src python3 -m point_ledger.acceptance --workspace .
```

验收会建立临时 SQLite 数据库，登记事故风险记录、快处中心、道路走廊和应急资源，完成调度与证据复核，并输出 JSON 结果。命令不会访问公网，也不需要额外数据库、队列或常驻服务。

## HTTP API

```bash
PYTHONPATH=src python3 -m traffic_dispatch.api --database traffic.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m evidence_review.api --database evidence.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m penalty_ops.api --database penalties.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m point_ledger.api --database point_ledger.sqlite3 --host 127.0.0.1 --port 8083
```

四个服务均提供 `GET /health`，其余接口使用 JSON。SQLite 文件保存业务状态、幂等结果和审计记录，进程重启后可继续查询。

## 记分资格账本（point_ledger）

账本把"已生效的处罚决定"按**违法发生时间**和当时适用的规则版本追加为不可变记分事件：

- 撤销、申诉变更只追加冲销事件（`penalty_removed`），同一违法的**重复决定**不进入余额；
- **跨周期结转**追加清零事件（`cycle_reset`，周期按驾驶证初次领取日周年起算、北京时间零点为界），旧行与旧余额永不改写；
- 规则版本带 `[effective_from, effective_to)` 生效区间，登记新版本会自动把旧开放式版本截止到新生效日——规则调整只影响明确范围内的违法；
- `GET /drivers/{id}/ledger` 每次从事件流**确定性重算**：返回当前有效分值、每周期汇总、逐笔形成过程（`postings`）、已成立/已解除的阈值跨越（`crossings`）、已到期措施（`due_actions`）和距下一措施的差距（`upcoming`）；
- 学习完成（`/studies`）与限制期届满（`POST /advance`，按可注入业务时钟）生成新的资格事件；措施以稳定 `dedupe_key` 幂等物化（`point_measures`），任意回溯登记/撤销后重放都不会重复下发，撤销使满分不成立时措施标记为 `rescinded`；
- 所有写操作进入前向链接的 SHA-256 审计链（`GET /audit/chain` 可校验）。
