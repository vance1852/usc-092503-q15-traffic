"""记分资格账本的事务用例。

写入侧只做一件事：向 point_ledger_events 追加不可变事实。
读取侧每次都从事件流确定性重算；run_due 按业务时钟把“已成立的措施”
幂等物化到 point_measures（dedupe_key 稳定），因此：
- 撤销、申诉变更、重复决定、跨周期结转都不会改写旧行；
- 规则版本只在明确生效区间内适用；
- 学习完成、限制期届满等时钟事件可随时重放且不会重复执行措施。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .rules import (
    CLEAR_KIND_FULL,
    CLEAR_KIND_PERIOD,
    DEFAULT_RULES,
    EVENT_CYCLE_RESET,
    EVENT_PENALTY_ADDED,
    EVENT_PENALTY_REMOVED,
    EVENT_POINTS_CLEARED,
    MEASURE_FULL_STUDY,
    MEASURE_ORDER,
    MEASURE_RESTORE,
    MEASURE_STUDY,
    MEASURE_SUSPEND,
    LedgerEvent,
    Rule,
    RuleBook,
    DriverProfile,
    canonical_json,
    cycle_start_for,
    digest,
    integer_points,
    next_cycle_start,
    parse_event_time,
    recompute,
)
from .storage import initialize, transaction

SHANGHAI_OFFSET = timezone(timedelta(hours=8))

ROLE_PERMISSIONS = {
    "officer": {"driver.write", "penalty.write", "ledger.read", "clock.advance"},
    "reviewer": {"penalty.revoke", "study.write", "ledger.read", "clock.advance"},
    "auditor": {"ledger.read", "audit.read"},
    "admin": {
        "driver.write", "penalty.write", "penalty.revoke", "study.write",
        "ledger.read", "audit.read", "rule.write", "clock.advance",
    },
}

REMOVE_REASONS = {"revoked", "appeal"}


class PointLedgerService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ---------------------------------------------------------------- 基础

    def _now(self) -> datetime:
        return self.clock.now().astimezone(timezone.utc)

    def _now_text(self) -> str:
        return utc_text(self.clock.now())

    def _user_row(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM ledger_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user_row(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM ledger_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now_text(),
            "previous_hash": previous_hash,
        }
        event_hash = digest(body)
        self.connection.execute(
            "INSERT INTO ledger_audit_events(entity_type,entity_id,event_type,actor_id,"
            "payload_json,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type, entity_id, event_type, actor_id,
                canonical_json(payload), previous_hash, event_hash, body["created_at"],
            ),
        )

    def create_user(self, actor_id: str | None, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if actor_id is not None:
            self._require(actor_id, "driver.write")
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO ledger_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now_text()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def bootstrap(self) -> None:
        """种子化账号与默认规则版本（幂等）。"""
        for user_id, display_name, role in (
            ("officer", "执勤民警", "officer"),
            ("reviewer", "复核人员", "reviewer"),
            ("auditor", "审计人员", "auditor"),
            ("admin", "系统管理员", "admin"),
        ):
            try:
                self.create_user(None, user_id, display_name, role)
            except Conflict:
                pass
        for rule in DEFAULT_RULES:
            if not self.connection.execute(
                "SELECT 1 FROM point_rule_versions WHERE version=?", (rule.version,)
            ).fetchone():
                self.connection.execute(
                    "INSERT INTO point_rule_versions(version,effective_from,effective_to,"
                    "thresholds_json,suspend_days,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        rule.version, rule.effective_from, rule.effective_to,
                        canonical_json(dict(rule.thresholds)), rule.suspend_days,
                        "officer", self._now_text(),
                    ),
                )

    # ---------------------------------------------------------------- 规则

    def register_rule(
        self,
        actor_id: str,
        version: str,
        effective_from: str,
        thresholds: Mapping[str, int],
        suspend_days: int | None,
        effective_to: str | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "rule.write")
        if not version.strip():
            raise ValidationFailed("规则版本不能为空")
        try:
            start = date.fromisoformat(effective_from)
            end = date.fromisoformat(effective_to) if effective_to else None
        except ValueError as exc:
            raise ValidationFailed("生效日期必须是 YYYY-MM-DD") from exc
        if end is not None and end <= start:
            raise ValidationFailed("生效结束日必须晚于生效日")
        cleaned = {
            measure: integer_points(value, f"thresholds.{measure}")
            for measure, value in thresholds.items()
            if measure in MEASURE_ORDER
        }
        if not cleaned:
            raise ValidationFailed("至少要提供一个措施阈值")
        if suspend_days is not None and suspend_days <= 0:
            raise ValidationFailed("扣留时长必须为正整数")

        # 先在内存中校验全部版本构成不重叠、至多一个开放式区间。
        existing = self.connection.execute(
            "SELECT * FROM point_rule_versions"
        ).fetchall()
        candidate = [
            Rule(
                version=row["version"],
                effective_from=row["effective_from"],
                effective_to=row["effective_to"],
                thresholds=json.loads(row["thresholds_json"]),
                suspend_days=row["suspend_days"],
            )
            for row in existing
        ]
        superseded_open = next((rule for rule in candidate if rule.effective_to is None
                                and rule.effective_from < start.isoformat()), None)
        if superseded_open is not None:
            # 内存中先把旧版本截止到新生效日，再校验不重叠区间。
            candidate = [
                Rule(rule.version, rule.effective_from,
                     start.isoformat() if rule.version == superseded_open.version else rule.effective_to,
                     dict(rule.thresholds), rule.suspend_days)
                for rule in candidate
            ]
        candidate.append(Rule(version, start.isoformat(),
                              end.isoformat() if end else None, dict(cleaned), suspend_days))
        try:
            RuleBook(candidate)
        except ValueError as exc:
            raise ValidationFailed(f"规则区间不合法: {exc}") from exc

        try:
            with transaction(self.connection, immediate=True):
                if self.connection.execute(
                    "SELECT 1 FROM point_rule_versions WHERE version=?", (version,)
                ).fetchone():
                    raise Conflict("规则版本已经存在")
                if superseded_open is not None:
                    # 新版本生效日即为旧版本明确的截止日；旧区间内的违法仍适用旧规则。
                    self.connection.execute(
                        "UPDATE point_rule_versions SET effective_to=? WHERE version=?",
                        (start.isoformat(), superseded_open.version),
                    )
                self.connection.execute(
                    "INSERT INTO point_rule_versions(version,effective_from,effective_to,"
                    "thresholds_json,suspend_days,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (version, start.isoformat(), end.isoformat() if end else None,
                     canonical_json(cleaned), suspend_days, actor_id, self._now_text()),
                )
                self._audit("rule", version, "registered", actor_id, {
                    "effective_from": start.isoformat(),
                    "effective_to": end.isoformat() if end else None,
                    "thresholds": cleaned, "suspend_days": suspend_days,
                    "superseded": superseded_open.version if superseded_open else None,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("规则版本已经存在") from exc
        return {"version": version, "effective_from": start.isoformat(),
                "effective_to": end.isoformat() if end else None,
                "thresholds": cleaned, "suspend_days": suspend_days}

    def _load_rulebook(self) -> RuleBook:
        rows = self.connection.execute(
            "SELECT * FROM point_rule_versions ORDER BY effective_from, version"
        ).fetchall()
        rules = [
            Rule(
                version=row["version"],
                effective_from=row["effective_from"],
                effective_to=row["effective_to"],
                thresholds=json.loads(row["thresholds_json"]),
                suspend_days=row["suspend_days"],
            )
            for row in rows
        ]
        return RuleBook(rules)

    # ---------------------------------------------------------------- 驾驶人

    def register_driver(self, actor_id: str, driver_id: str, license_issued_on: str) -> dict[str, Any]:
        self._require(actor_id, "driver.write")
        try:
            issued = date.fromisoformat(license_issued_on)
        except ValueError as exc:
            raise ValidationFailed("初次领证日期必须是 YYYY-MM-DD") from exc
        if issued >= self._now().date():
            raise ValidationFailed("初次领证日期必须早于今天")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO point_drivers(driver_id,license_issued_on,created_by,created_at)"
                    " VALUES(?,?,?,?)",
                    (driver_id, issued.isoformat(), actor_id, self._now_text()),
                )
                self._audit("driver", driver_id, "registered", actor_id,
                            {"license_issued_on": issued.isoformat()})
        except sqlite3.IntegrityError as exc:
            raise Conflict("驾驶人已经登记") from exc
        return {"driver_id": driver_id, "license_issued_on": issued.isoformat()}

    def _driver(self, driver_id: str) -> DriverProfile:
        row = self.connection.execute(
            "SELECT * FROM point_drivers WHERE driver_id=?", (driver_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"驾驶人 {driver_id} 不存在")
        return DriverProfile(driver_id, date.fromisoformat(row["license_issued_on"]))

    # ---------------------------------------------------------------- 事件

    def _events(self, driver_id: str) -> list[LedgerEvent]:
        rows = self.connection.execute(
            "SELECT * FROM point_ledger_events WHERE driver_id=? ORDER BY seq", (driver_id,)
        ).fetchall()
        return [
            LedgerEvent(
                seq=row["seq"], driver_id=row["driver_id"], kind=row["kind"],
                points=row["points"], occurred_at=row["occurred_at"],
                recorded_at=row["recorded_at"], cycle_start=row["cycle_start"],
                rule_version=row["rule_version"], penalty_id=row["penalty_id"],
                violation_id=row["violation_id"], clear_kind=row["clear_kind"],
                reason=row["reason"], dedupe_key=row["dedupe_key"],
                ref_event_seq=row["ref_event_seq"],
            )
            for row in rows
        ]

    def _append_event(self, event_fields: Mapping[str, Any]) -> int:
        columns = (
            "driver_id", "kind", "points", "occurred_at", "recorded_at", "cycle_start",
            "rule_version", "penalty_id", "violation_id", "clear_kind", "reason",
            "dedupe_key", "ref_event_seq",
        )
        placeholders = ",".join("?" for _ in columns)
        cursor = self.connection.execute(
            f"INSERT INTO point_ledger_events({','.join(columns)}) VALUES({placeholders})",
            tuple(event_fields.get(name) for name in columns),
        )
        return int(cursor.lastrowid)

    def record_penalty(
        self,
        actor_id: str,
        driver_id: str,
        *,
        penalty_id: str,
        violation_id: str,
        points: int,
        occurred_at: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """登记已经生效的处罚决定，按违法发生时间写入记分事件。"""
        self._require(actor_id, "penalty.write")
        driver = self._driver(driver_id)
        try:
            points = integer_points(points)
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if points == 0:
            raise ValidationFailed("生效记分为 0 时不需要记账")
        if not penalty_id.strip() or not violation_id.strip():
            raise ValidationFailed("处罚编号与违法编号不能为空")
        incident = parse_utc(occurred_at, "occurred_at")
        if incident > self._now():
            raise ValidationFailed("违法发生时间不能晚于当前业务时钟")

        with transaction(self.connection, immediate=True):
            if idempotency_key:
                cached = self._idempotency_cached("penalty", idempotency_key)
                if cached is not None:
                    return cached
            existing = self.connection.execute(
                "SELECT seq FROM point_ledger_events WHERE driver_id=? AND penalty_id=? AND kind=?",
                (driver_id, penalty_id, EVENT_PENALTY_ADDED),
            ).fetchone()
            if existing is not None:
                # 同一处罚决定重放：幂等返回，不产生第二笔记账
                response = {"penalty_id": penalty_id, "duplicate": True, "seq": existing["seq"]}
                self._idempotency_store("penalty", idempotency_key,
                                        {"penalty_id": penalty_id, "violation_id": violation_id,
                                         "points": points, "occurred_at": occurred_at}, response)
                return response
            # 重复决定：同一违法已有生效记分、且原决定未被撤销/申诉推翻时，
            # 不再记账；撤销后就同一违法重新作出决定则不受此限。
            active = self.connection.execute(
                "SELECT pe.seq FROM point_ledger_events pe "
                "WHERE pe.driver_id=? AND pe.violation_id=? AND pe.kind=? "
                "AND NOT EXISTS (SELECT 1 FROM point_ledger_events r "
                "WHERE r.ref_event_seq=pe.seq AND r.kind=?) LIMIT 1",
                (driver_id, violation_id, EVENT_PENALTY_ADDED, EVENT_PENALTY_REMOVED),
            ).fetchone()
            if active is not None:
                response = {"penalty_id": penalty_id, "duplicate": True,
                            "seq": active["seq"], "reason": "violation_already_scored"}
                self._idempotency_store("penalty", idempotency_key,
                                        {"penalty_id": penalty_id, "violation_id": violation_id,
                                         "points": points, "occurred_at": occurred_at}, response)
                return response
            rulebook = self._load_rulebook()
            try:
                rule = rulebook.for_incident(incident)
            except LookupError as exc:
                raise ValidationFailed(str(exc)) from exc
            cycle = cycle_start_for(incident, driver.license_issued_on).isoformat()
            seq = self._append_event({
                "driver_id": driver_id,
                "kind": EVENT_PENALTY_ADDED,
                "points": points,
                "occurred_at": utc_text(incident),
                "recorded_at": self._now_text(),
                "cycle_start": cycle,
                "rule_version": rule.version,
                "penalty_id": penalty_id,
                "violation_id": violation_id,
                "clear_kind": None,
                "reason": "penalty_effective",
                "dedupe_key": f"add:{driver_id}:{penalty_id}",
                "ref_event_seq": None,
            })
            self._audit("penalty", penalty_id, "points_recorded", actor_id, {
                "driver_id": driver_id, "violation_id": violation_id, "points": points,
                "occurred_at": utc_text(incident), "cycle_start": cycle,
                "rule_version": rule.version, "seq": seq,
            })
            materialized = self._run_due(driver, rulebook, self._now(), actor_id)
            response = {"penalty_id": penalty_id, "duplicate": False, "seq": seq,
                        "cycle_start": cycle, "rule_version": rule.version,
                        "materialized": materialized}
            self._idempotency_store("penalty", idempotency_key,
                                    {"penalty_id": penalty_id, "violation_id": violation_id,
                                     "points": points, "occurred_at": occurred_at}, response)
            return response

    def revoke_penalty(
        self,
        actor_id: str,
        driver_id: str,
        *,
        penalty_id: str,
        reason: str,
        occurred_at: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """撤销或申诉变更：追加冲销事件，绝不修改原始记分行。"""
        self._require(actor_id, "penalty.revoke")
        driver = self._driver(driver_id)
        if reason not in REMOVE_REASONS:
            raise ValidationFailed("原因必须是 revoked 或 appeal")
        effective_at = parse_utc(occurred_at, "occurred_at") if occurred_at else self._now()

        with transaction(self.connection, immediate=True):
            target = self.connection.execute(
                "SELECT * FROM point_ledger_events WHERE driver_id=? AND penalty_id=? AND kind=?",
                (driver_id, penalty_id, EVENT_PENALTY_ADDED),
            ).fetchone()
            if target is None:
                raise NotFound(f"处罚 {penalty_id} 没有记分事件")
            prior = self.connection.execute(
                "SELECT seq FROM point_ledger_events WHERE ref_event_seq=? AND kind=?",
                (target["seq"], EVENT_PENALTY_REMOVED),
            ).fetchone()
            if prior is not None:
                return {"penalty_id": penalty_id, "duplicate": True, "seq": prior["seq"]}
            rulebook = self._load_rulebook()
            seq = self._append_event({
                "driver_id": driver_id,
                "kind": EVENT_PENALTY_REMOVED,
                "points": target["points"],
                "occurred_at": utc_text(effective_at),
                "recorded_at": self._now_text(),
                "cycle_start": target["cycle_start"],
                "rule_version": target["rule_version"],
                "penalty_id": penalty_id,
                "violation_id": target["violation_id"],
                "clear_kind": None,
                "reason": reason,
                "dedupe_key": f"rm:{driver_id}:{penalty_id}",
                "ref_event_seq": target["seq"],
            })
            self._audit("penalty", penalty_id, "points_reversed", actor_id, {
                "driver_id": driver_id, "points": target["points"], "reason": reason,
                "ref_event_seq": target["seq"], "occurred_at": utc_text(effective_at),
                "note": note, "seq": seq,
            })
            materialized = self._run_due(driver, rulebook, self._now(), actor_id)
            return {"penalty_id": penalty_id, "duplicate": False, "seq": seq,
                    "ref_event_seq": target["seq"], "materialized": materialized}

    def complete_study(
        self,
        actor_id: str,
        driver_id: str,
        *,
        study_record_id: str,
        kind: str = CLEAR_KIND_PERIOD,
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        """登记学习完成（业务时钟），追加消分事件并推动后续资格事件。"""
        self._require(actor_id, "study.write")
        driver = self._driver(driver_id)
        if kind not in {CLEAR_KIND_PERIOD, CLEAR_KIND_FULL}:
            raise ValidationFailed("学习类型必须是 period_study 或 full_study")
        if not study_record_id.strip():
            raise ValidationFailed("学习记录编号不能为空")
        completed_at = parse_utc(occurred_at, "occurred_at") if occurred_at else self._now()

        with transaction(self.connection, immediate=True):
            if self.connection.execute(
                "SELECT 1 FROM point_ledger_events WHERE dedupe_key=?",
                (f"clear:{driver_id}:{study_record_id}",),
            ).fetchone():
                raise Conflict("该学习记录已经消分")
            rulebook = self._load_rulebook()
            snapshot = recompute(self._events(driver_id), driver, as_of=completed_at,
                                 rulebook=rulebook)
            cycle_start = snapshot["current_cycle_start"]
            crossing = next((
                item for item in snapshot["crossings"]
                if item["cycle_start"] == cycle_start and item["active"]
                and item["measure"] == (MEASURE_FULL_STUDY if kind == CLEAR_KIND_FULL else MEASURE_STUDY)
            ), None)
            if crossing is None:
                raise InvalidState("当前周期没有成立的对应学习措施，不能登记消分")
            if kind == CLEAR_KIND_PERIOD and any(
                item["cycle_start"] == cycle_start
                and item["measure"] == MEASURE_FULL_STUDY and item["active"]
                for item in snapshot["crossings"]
            ):
                raise InvalidState("满分措施已成立，必须完成满分学习与考试，不能按普通学习消分")
            balance = next(item["closing"] for item in snapshot["cycles"]
                           if item["cycle_start"] == cycle_start)
            clear_points = balance if kind == CLEAR_KIND_FULL else min(6, balance)
            if clear_points <= 0:
                raise InvalidState("当前周期没有可消减的记分")
            seq = self._append_event({
                "driver_id": driver_id,
                "kind": EVENT_POINTS_CLEARED,
                "points": clear_points,
                "occurred_at": utc_text(completed_at),
                "recorded_at": self._now_text(),
                "cycle_start": cycle_start,
                "rule_version": crossing["rule_version"],
                "penalty_id": None,
                "violation_id": None,
                "clear_kind": kind,
                "reason": f"study_completed:{kind}",
                "dedupe_key": f"clear:{driver_id}:{study_record_id}",
                "ref_event_seq": crossing["trigger_event_seq"],
            })
            self._audit("study", study_record_id, "points_cleared", actor_id, {
                "driver_id": driver_id, "cycle_start": cycle_start, "kind": kind,
                "points": clear_points, "ref_event_seq": crossing["trigger_event_seq"],
                "occurred_at": utc_text(completed_at), "seq": seq,
            })
            materialized = self._run_due(driver, rulebook, self._now(), actor_id)
            return {"study_record_id": study_record_id, "seq": seq,
                    "cycle_start": cycle_start, "cleared_points": clear_points,
                    "materialized": materialized}

    # ------------------------------------------------------- 时钟驱动的物化

    def advance(self, actor_id: str) -> dict[str, Any]:
        """按业务时钟推进所有驾驶人的周期结转与措施到期。"""
        self._require(actor_id, "clock.advance")
        drivers = self.connection.execute("SELECT * FROM point_drivers").fetchall()
        rulebook = self._load_rulebook()
        now = self._now()
        changed: list[dict[str, Any]] = []
        with transaction(self.connection, immediate=True):
            for row in drivers:
                driver = DriverProfile(row["driver_id"], date.fromisoformat(row["license_issued_on"]))
                before = self.connection.execute(
                    "SELECT COUNT(*) FROM point_ledger_events WHERE driver_id=?", (driver.driver_id,)
                ).fetchone()[0]
                materialized = self._run_due(driver, rulebook, now, actor_id)
                after = self.connection.execute(
                    "SELECT COUNT(*) FROM point_ledger_events WHERE driver_id=?", (driver.driver_id,)
                ).fetchone()[0]
                if materialized or after != before:
                    changed.append({"driver_id": driver.driver_id, "actions": materialized,
                                    "events_added": after - before})
        return {"as_of": utc_text(now), "processed": len(drivers), "changed": changed}

    def _run_due(self, driver: DriverProfile, rulebook: RuleBook, now: datetime,
                 actor_id: str) -> list[dict[str, Any]]:
        """把截至 now 已成立的事实幂等物化；可在任意时刻安全重放。"""
        events = self._events(driver.driver_id)
        snapshot = recompute(events, driver, as_of=now, rulebook=rulebook)
        current_cycle = snapshot["current_cycle_start"]

        # 1) 跨周期结转：上一周期余分清零（满分未处理的除外），追加而非改写。
        for cycle in snapshot["cycles"]:
            if cycle["cycle_start"] >= current_cycle or cycle["closing"] <= 0:
                continue
            reset_key = f"reset:{driver.driver_id}:{cycle['cycle_start']}"
            if self.connection.execute(
                "SELECT 1 FROM point_ledger_events WHERE dedupe_key=?", (reset_key,)
            ).fetchone():
                continue
            active_full = any(
                item["cycle_start"] == cycle["cycle_start"]
                and item["measure"] == MEASURE_FULL_STUDY and item["active"]
                for item in snapshot["crossings"]
            )
            if active_full:
                # 满分学习考试未完成，余分不随周期届满清除
                continue
            boundary = datetime.combine(
                next_cycle_start(date.fromisoformat(cycle["cycle_start"])),
                datetime.min.time(), tzinfo=SHANGHAI_OFFSET,
            ).astimezone(timezone.utc)
            self._append_event({
                "driver_id": driver.driver_id,
                "kind": EVENT_CYCLE_RESET,
                "points": cycle["closing"],
                "occurred_at": utc_text(boundary),
                "recorded_at": self._now_text(),
                "cycle_start": cycle["cycle_start"],
                "rule_version": None,
                "penalty_id": None,
                "violation_id": None,
                "clear_kind": None,
                "reason": "cycle_boundary_reset",
                "dedupe_key": reset_key,
                "ref_event_seq": None,
            })
            self._audit("driver", driver.driver_id, "cycle_reset", actor_id, {
                "cycle_start": cycle["cycle_start"], "points": cycle["closing"],
                "boundary_at": utc_text(boundary), "dedupe_key": reset_key,
            })

        # 追加结转后重新重算，得到权威的跨越状态
        snapshot = recompute(self._events(driver.driver_id), driver, as_of=now, rulebook=rulebook)
        crossing_by_key = {item["dedupe_key"]: item for item in snapshot["crossings"]}
        actions: list[dict[str, Any]] = []

        # 2) 已到期措施：study/full_study 通知、suspend 生效（幂等插入）。
        for due in snapshot["due_actions"]:
            executed = utc_text(parse_event_time(due["due_at"]))
            release_at = None
            status = "notified"
            if due["measure"] == MEASURE_SUSPEND and due["suspend_days"]:
                status = "active"
                release_at = utc_text(
                    parse_event_time(due["due_at"]) + timedelta(days=due["suspend_days"])
                )
            inserted = self.connection.execute(
                "INSERT OR IGNORE INTO point_measures(dedupe_key,driver_id,measure,cycle_start,"
                "rule_version,trigger_event_seq,status,total_points,due_at,release_at,"
                "executed_at,detail_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (due["dedupe_key"], driver.driver_id, due["measure"], due["cycle_start"],
                 due["rule_version"], due["trigger_event_seq"], status, due["total"],
                 executed, release_at, executed, canonical_json({}),
                 self._now_text(), self._now_text()),
            )
            if inserted.rowcount:
                actions.append({"op": "issued", "measure": due["measure"],
                                "dedupe_key": due["dedupe_key"], "status": status,
                                "release_at": release_at})
                self._audit("driver", driver.driver_id, "measure_issued", actor_id, {
                    "measure": due["measure"], "dedupe_key": due["dedupe_key"],
                    "cycle_start": due["cycle_start"], "rule_version": due["rule_version"],
                    "total": due["total"], "due_at": executed, "release_at": release_at,
                    "trigger_event_seq": due["trigger_event_seq"],
                })

        # 3) 措施对账：跨越解除时关闭或撤销已物化的措施。
        measure_rows = self.connection.execute(
            "SELECT * FROM point_measures WHERE driver_id=? AND measure != ?",
            (driver.driver_id, MEASURE_RESTORE),
        ).fetchall()
        for row in measure_rows:
            if row["status"] in {"released", "rescinded"}:
                continue
            crossing = crossing_by_key.get(row["dedupe_key"])
            if crossing is None or crossing["active"]:
                continue
            if crossing["resolve"] == "reversed":
                # 撤销/申诉变更使满分不再成立：所有措施（含暂扣）直接撤销。
                self.connection.execute(
                    "UPDATE point_measures SET status='rescinded',updated_at=? WHERE dedupe_key=?",
                    (self._now_text(), row["dedupe_key"]),
                )
                actions.append({"op": "rescinded", "measure": row["measure"],
                                "dedupe_key": row["dedupe_key"], "resolve": crossing["resolve"]})
                self._audit("driver", driver.driver_id, "measure_rescinded", actor_id, {
                    "measure": row["measure"], "dedupe_key": row["dedupe_key"],
                    "cycle_start": row["cycle_start"], "resolve": crossing["resolve"],
                })
            elif row["measure"] != MEASURE_SUSPEND:
                # 学习类措施在消分后即关闭；暂扣必须等到限制期届满，由第 4 步恢复。
                self.connection.execute(
                    "UPDATE point_measures SET status='released',updated_at=? WHERE dedupe_key=?",
                    (self._now_text(), row["dedupe_key"]),
                )
                actions.append({"op": "released", "measure": row["measure"],
                                "dedupe_key": row["dedupe_key"], "resolve": crossing["resolve"]})
                self._audit("driver", driver.driver_id, "measure_released", actor_id, {
                    "measure": row["measure"], "dedupe_key": row["dedupe_key"],
                    "cycle_start": row["cycle_start"], "resolve": crossing["resolve"],
                })

        # 4) 限制期届满：暂扣到期且满分学习已清零的，生成恢复资格事件。
        for row in self.connection.execute(
            "SELECT * FROM point_measures WHERE driver_id=? AND measure=? AND status='active'",
            (driver.driver_id, MEASURE_SUSPEND),
        ).fetchall():
            if row["release_at"] is None or parse_utc(row["release_at"], "release_at") > now:
                continue
            crossing = crossing_by_key.get(row["dedupe_key"])
            if crossing is not None and crossing["active"]:
                # 考试/满分学习尚未完成，不能恢复驾驶资格
                continue
            restore_key = f"restore:{row['dedupe_key']}"
            inserted = self.connection.execute(
                "INSERT OR IGNORE INTO point_measures(dedupe_key,driver_id,measure,cycle_start,"
                "rule_version,trigger_event_seq,status,total_points,due_at,release_at,"
                "executed_at,detail_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (restore_key, driver.driver_id, MEASURE_RESTORE, row["cycle_start"],
                 row["rule_version"], row["trigger_event_seq"], "released", 0,
                 self._now_text(), self._now_text(), self._now_text(),
                 canonical_json({"after": "suspend_days_elapsed"}),
                 self._now_text(), self._now_text()),
            )
            if inserted.rowcount:
                self.connection.execute(
                    "UPDATE point_measures SET status='released',updated_at=? WHERE dedupe_key=?",
                    (self._now_text(), row["dedupe_key"]),
                )
                actions.append({"op": "issued", "measure": MEASURE_RESTORE,
                                "dedupe_key": restore_key, "status": "released"})
                self._audit("driver", driver.driver_id, "qualification_restored", actor_id, {
                    "measure": MEASURE_RESTORE, "dedupe_key": restore_key,
                    "cycle_start": row["cycle_start"], "rule_version": row["rule_version"],
                    "released_at": self._now_text(),
                })
        return actions

    # ---------------------------------------------------------------- 查询

    def ledger(self, actor_id: str, driver_id: str, as_of: str | None = None) -> dict[str, Any]:
        """只读查询：每次都从事件流确定性重算，不产生副作用。"""
        self._require(actor_id, "ledger.read")
        driver = self._driver(driver_id)
        rulebook = self._load_rulebook()
        moment = parse_utc(as_of, "as_of") if as_of else self._now()
        result = recompute(self._events(driver_id), driver, as_of=moment, rulebook=rulebook)
        result["measures"] = [
            {
                "dedupe_key": row["dedupe_key"],
                "measure": row["measure"],
                "cycle_start": row["cycle_start"],
                "rule_version": row["rule_version"],
                "status": row["status"],
                "total_points": row["total_points"],
                "due_at": row["due_at"],
                "release_at": row["release_at"],
                "executed_at": row["executed_at"],
            }
            for row in self.connection.execute(
                "SELECT * FROM point_measures WHERE driver_id=? ORDER BY executed_at,dedupe_key",
                (driver_id,),
            ).fetchall()
        ]
        materialized_keys = {item["dedupe_key"] for item in result["measures"]}
        result["pending_actions"] = [
            item for item in result["due_actions"]
            if item["dedupe_key"] not in materialized_keys
        ]
        return result

    def events(self, actor_id: str, driver_id: str) -> list[dict[str, Any]]:
        self._require(actor_id, "ledger.read")
        self._driver(driver_id)
        return [event.as_dict() for event in self._events(driver_id)]

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute(
            "SELECT * FROM ledger_audit_events ORDER BY event_id"
        ).fetchall()
        previous_hash = "0" * 64
        valid = True
        items: list[dict[str, Any]] = []
        for row in rows:
            if row["previous_hash"] != previous_hash:
                valid = False
            payload = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            if digest(payload) != row["event_hash"]:
                valid = False
            previous_hash = row["event_hash"]
            items.append({"event_id": row["event_id"], **payload, "event_hash": row["event_hash"]})
        return {"valid": valid, "events": items}

    # ------------------------------------------------------------- 幂等表

    def _idempotency_cached(self, scope: str, key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT response_json FROM point_idempotency WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        return json.loads(row["response_json"]) if row else None

    def _idempotency_store(self, scope: str, key: str | None, request: Mapping[str, Any],
                           response: Mapping[str, Any]) -> None:
        if not key:
            return
        self.connection.execute(
            "INSERT OR IGNORE INTO point_idempotency(scope,idempotency_key,request_sha256,"
            "response_json,created_at) VALUES(?,?,?,?,?)",
            (scope, key, digest(request), canonical_json(response), self._now_text()),
        )
