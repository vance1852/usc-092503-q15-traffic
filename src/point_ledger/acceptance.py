"""记分资格账本的离线贯通验收。

用冻结业务时钟演示：生效处罚记账、重复决定抑制、撤销冲销、
学习消分、满分暂扣与届满恢复、跨周期结转，以及任意时点的确定性重算。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .rules import (
    DriverProfile,
    RuleBook,
    digest,
    recompute as recompute_ledger,
)
from .service import PointLedgerService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 9, 25, 2, 0, tzinfo=timezone.utc))
    service = PointLedgerService(connection, clock)
    service.bootstrap()

    # 驾驶证初次领取日决定周期边界（周年日起算，12 个月滚动）。
    service.register_driver("officer", "D-DEMO", "2020-06-10")
    service.register_driver("officer", "D-REVOKE", "2019-03-02")
    service.register_driver("officer", "D-CARRY", "2021-09-01")

    # 当前周期内 3+3+3=9：触发学习通知，并预警距满分还差 3 分。
    for index, day in enumerate(("2026-06-20", "2026-07-18", "2026-08-05"), start=1):
        service.record_penalty(
            "officer", "D-DEMO",
            penalty_id=f"P-DEMO-{index}", violation_id=f"V-DEMO-{index}",
            points=3, occurred_at=f"{day}T03:00:00Z",
        )
    at_nine = service.ledger("auditor", "D-DEMO")
    assert at_nine["current_points"] == 9
    assert [item["measure"] for item in at_nine["measures"]] == ["study"]
    assert at_nine["upcoming"][0]["measure"] == "full_study"
    assert at_nine["upcoming"][0]["points_needed"] == 3

    # 同一违法的重复决定不得再次记分（余额仍为 9）。
    duplicate = service.record_penalty(
        "officer", "D-DEMO",
        penalty_id="P-DEMO-3-DUP", violation_id="V-DEMO-3",
        points=3, occurred_at="2026-08-05T03:00:00Z",
    )
    assert duplicate["duplicate"] is True
    assert service.ledger("auditor", "D-DEMO")["current_points"] == 9

    # 再记 3 分达到 12：满分学习、考试与扣留措施成立，释放日为 7 天后。
    service.record_penalty(
        "officer", "D-DEMO",
        penalty_id="P-DEMO-4", violation_id="V-DEMO-4",
        points=3, occurred_at="2026-09-10T01:00:00Z",
    )
    at_twelve = service.ledger("auditor", "D-DEMO")
    assert at_twelve["current_points"] == 12
    suspend = next(item for item in at_twelve["measures"] if item["measure"] == "suspend")
    assert suspend["status"] == "active"
    release_at = suspend["release_at"]

    # 完成满分学习与考试：记分清零、学习措施关闭；限制期未满仍不能恢复。
    clock.current = datetime(2026, 9, 14, 2, 0, tzinfo=timezone.utc)
    service.complete_study(
        "reviewer", "D-DEMO", study_record_id="STUDY-FULL-1",
        kind="full_study", occurred_at="2026-09-14T02:00:00Z",
    )
    after_study = service.ledger("auditor", "D-DEMO")
    assert after_study["current_points"] == 0
    assert all(item["measure"] != "restore" for item in after_study["measures"])

    # 时钟越过限制期届满点：生成恢复资格事件；重复推进不会重复执行。
    clock.current = datetime(2026, 9, 18, 2, 0, tzinfo=timezone.utc)
    service.advance("officer")
    measures = service.ledger("auditor", "D-DEMO")["measures"]
    assert any(item["measure"] == "restore" and item["status"] == "released" for item in measures)
    second = service.advance("officer")
    assert second["changed"] == []

    # 撤销/申诉变更：追加冲销事件，旧记分行保持不变，已发措施撤销。
    service.record_penalty("officer", "D-REVOKE", penalty_id="P-RV-1",
                           violation_id="V-RV-1", points=6,
                           occurred_at="2026-05-01T03:00:00Z")
    service.record_penalty("officer", "D-REVOKE", penalty_id="P-RV-2",
                           violation_id="V-RV-2", points=6,
                           occurred_at="2026-05-02T03:00:00Z")
    service.revoke_penalty("reviewer", "D-REVOKE", penalty_id="P-RV-2",
                           reason="appeal", occurred_at="2026-08-20T03:00:00Z")
    revoked = service.ledger("auditor", "D-REVOKE")
    assert revoked["current_points"] == 6
    assert all(item["status"] == "rescinded" for item in revoked["measures"]
               if item["measure"] in {"full_study", "suspend"})
    events_revoke = service.events("auditor", "D-REVOKE")
    assert all(item["points"] == 6 for item in events_revoke if item["kind"] == "penalty_added")

    # 跨周期结转：上周期余分在周期届满清零，绝不结转到新周期。
    clock.current = datetime(2026, 9, 25, 2, 0, tzinfo=timezone.utc)
    service.record_penalty("officer", "D-CARRY", penalty_id="P-OLD-1",
                           violation_id="V-OLD-1", points=3,
                           occurred_at="2026-03-01T03:00:00Z")
    service.advance("officer")
    carried = service.ledger("auditor", "D-CARRY")
    assert carried["current_cycle_start"] == "2026-09-01"
    assert carried["current_points"] == 0
    reset_event = next(item for item in service.events("auditor", "D-CARRY")
                       if item["kind"] == "cycle_reset")
    assert reset_event["points"] == 3
    # 周期边界按北京时间 2026-09-01 00:00 记录（即 UTC 2026-08-31T16:00Z）
    assert reset_event["occurred_at"] == "2026-08-31T16:00:00Z"

    # 确定性：同一事件流两次重算摘要完全一致。
    rulebook = service._load_rulebook()
    events = service._events("D-DEMO")
    profile = service._driver("D-DEMO")
    as_of = datetime(2026, 9, 25, tzinfo=timezone.utc)
    left = digest(recompute_ledger(events, profile, as_of=as_of, rulebook=rulebook))
    right = digest(recompute_ledger(list(events), profile, as_of=as_of, rulebook=rulebook))
    assert left == right

    chain = service.audit_chain("auditor")
    final = service.ledger("auditor", "D-DEMO")
    return {
        "status": "ok",
        "demo": {
            "points_at_nine": at_nine["current_points"],
            "upcoming_at_nine": at_nine["upcoming"],
            "duplicate_suppressed": duplicate,
            "points_after_full_study": after_study["current_points"],
            "suspend_release_at": release_at,
            "measures": [
                {"measure": item["measure"], "status": item["status"],
                 "cycle_start": item["cycle_start"]}
                for item in final["measures"]
            ],
            "postings": final["postings"],
        },
        "revocation": {"current_points": revoked["current_points"],
                       "event_count": len(events_revoke)},
        "carry_over": {"current_points": carried["current_points"],
                       "current_cycle_start": carried["current_cycle_start"],
                       "reset_event": reset_event},
        "recompute_digest": left,
        "audit_valid": chain["valid"],
        "audit_events": len(chain["events"]),
        "workspace": workspace.name,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行驾驶证记分资格账本离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
