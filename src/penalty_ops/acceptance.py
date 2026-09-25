"""离线命令行验收入口：驾驶记分资格账本。"""
from __future__ import annotations
import argparse,json
from .clock import FrozenClock
from .service import PenaltyService
def run(clock_text: str = "2026-09-25T00:00:00Z"):
    clock=FrozenClock(clock_text)
    s=PenaltyService(clock=clock); s.bootstrap()
    t=s.auth.login("admin","enforcement-admin")
    s.points_ledger.register_driver(t,"D-DEMO","驾照1100002026000001","2026-01-01T00:00:00Z")

    # 2026-06 两次违法（适用旧规则 v1：闯红灯 6 + 打电话 3 = 9 分）。
    s.points_ledger.register_penalty(t,"D-DEMO","PN-2026-001","RUN_RED_LIGHT","2026-06-10T08:00:00Z")
    s.points_ledger.register_penalty(t,"D-DEMO","PN-2026-002","PHONE","2026-06-20T09:00:00Z")
    before=s.points_ledger.ledger(t,"D-DEMO")

    # 2026-08 超速 50%（适用新规则 v2：9 分）→ 滚动周期累计 18 → 触发学习。
    r=s.points_ledger.register_penalty(t,"D-DEMO","PN-2026-003","SPEED_50","2026-08-15T10:00:00Z")

    # 重复决定再次提交：幂等，不重复记分、不重复触发。
    dup=s.points_ledger.register_penalty(t,"D-DEMO","PN-2026-003","SPEED_50","2026-08-15T10:00:00Z")

    # 申诉：PHONE 认定变更，记 1 分（原 3 分）→ 回落至 16 分，学习依据仍成立但已不重复执行。
    adj=s.points_ledger.appeal_adjustment(t,"D-DEMO","PN-2026-002",1,"证据复核后变更")

    # 业务时钟走到限制/学习相关日期之后：无额外清零事件（学习须人工确认完成）。
    clock.advance(days=10)
    advanced=s.points_ledger.advance(t,"D-DEMO")

    # 满分学习完成 → 写入 study_completed 事件，措施完结，余额清零。
    s.points_ledger.complete_study(t,"D-DEMO")
    after=s.points_ledger.ledger(t,"D-DEMO")

    # 再犯：周期内重新累计；随后撤销使该处罚有效贡献归零（旧事件行原样保留）。
    clock.advance(days=2)
    s.points_ledger.register_penalty(t,"D-DEMO","PN-2026-004","SEATBELT","2026-10-07T08:00:00Z")
    s.points_ledger.reverse_penalty(t,"D-DEMO","PN-2026-004","处罚被撤销")
    final=s.points_ledger.ledger(t,"D-DEMO")

    return {"status":"ok","driver":"D-DEMO","before_threshold":{"effective_points":before["effective_points"],"upcoming_kind":[u["kind"] for u in before["upcoming"]]},"triggered_by_penalty":[m["kind"] for m in r["triggered"]],"duplicate":dup["duplicate"],"appeal_delta":adj["delta"],"advance_triggered":advanced["triggered"],"after_study_points":after["effective_points"],"after_reversal_points":final["effective_points"],"formation_length":len(final["formation"])}
def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--workspace",default="."); parser.parse_args(); print(json.dumps(run(),ensure_ascii=False))
if __name__=="__main__":main()
