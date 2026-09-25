"""依赖标准库的 JSON HTTP API。"""
from __future__ import annotations
import argparse,json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from .models import ViolationRecord,CaseRecord
from .service import PenaltyService
class Handler(BaseHTTPRequestHandler):
    service=PenaltyService()
    def _send(self,status,payload):
        data=json.dumps(payload,ensure_ascii=False).encode(); self.send_response(status); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data)
    def _token(self):return self.headers.get("Authorization","").removeprefix("Bearer ")
    def do_GET(self):
        try:
            parsed=urlparse(self.path); path=parsed.path
            if path=="/health":return self._send(200,{"status":"ok","service":"urban-enforcement"})
            if path.startswith("/case_records/") and path.endswith("/risk"):return self._send(200,self.service.risk_report(self._token(),path.split("/")[2]))
            if path.startswith("/case_records/"):return self._send(200,self.service.case_record(self._token(),path.split("/",2)[2]))
            if path.startswith("/drivers/"):
                parts=path.split("/")
                driver_id=parts[2]; sub=parts[3] if len(parts)>3 else ""
                if sub=="ledger":
                    as_of=(parse_qs(parsed.query).get("as_of") or [None])[0]
                    return self._send(200,self.service.points_ledger.ledger(self._token(),driver_id,as_of))
                if sub=="events":return self._send(200,{"events":self.service.points_ledger.events(self._token(),driver_id)})
            if path=="/rules":
                return self._send(200,{"versions":[v.to_dict() for v in self.service.points_ledger.rulebook.all()]})
            return self._send(404,{"error":"not found"})
        except PermissionError as e:return self._send(403,{"error":str(e)})
        except KeyError as e:return self._send(404,{"error":str(e)})
        except Exception as e:return self._send(400,{"error":str(e)})
    def do_POST(self):
        try:
            path=urlparse(self.path).path
            body=json.loads(self.rfile.read(int(self.headers.get("Content-Length","0"))) or b"{}")
            if path=="/login":return self._send(200,{"token":self.service.auth.login(body["user_id"],body["password"])})
            token=self._token()
            if path=="/case_records":return self._send(201,self.service.register_case_record(token,CaseRecord(body["case_record_id"],body["district"],body["enforcement_type"],body["length_m"],body["criticality"])))
            if path.startswith("/case_records/") and path.endswith("/violation_records"):
                sid=path.split("/")[2]; r=ViolationRecord(body["violation_record_id"],sid,body["evidence_source_id"],body["speed_kmh"],body["traffic_flow_vph"],body["impact_index"],body["observed_at"]); return self._send(201,self.service.ingest_violation_record(token,r))
            if path.startswith("/case_records/") and path.endswith("/work-orders"):
                return self._send(201,self.service.create_case_ticket(token,path.split("/")[2],body["alert_id"],body["assignee"],body.get("priority",3)))
            if path=="/drivers":
                return self._send(201,self.service.points_ledger.register_driver(token,body["driver_id"],body["license_no"],body["anchor_date"]))
            if path.startswith("/drivers/"):
                parts=path.split("/"); driver_id=parts[2]; sub=parts[3] if len(parts)>3 else ""
                ledger=self.service.points_ledger
                if sub=="penalties":
                    return self._send(201,ledger.register_penalty(token,driver_id,body["decision_no"],body["violation_code"],body["occurred_at"],body.get("points"),body.get("rule_version")))
                if sub=="reversals":
                    return self._send(201,ledger.reverse_penalty(token,driver_id,body["decision_ref"],body["reason"],body.get("effective_at")))
                if sub=="appeal-adjustments":
                    return self._send(201,ledger.appeal_adjustment(token,driver_id,body["decision_ref"],body["new_points"],body["reason"],body.get("effective_at")))
                if sub=="study-completions":
                    return self._send(201,ledger.complete_study(token,driver_id,body.get("effective_at")))
                if sub=="advance":
                    return self._send(200,ledger.advance(token,driver_id))
            return self._send(404,{"error":"not found"})
        except PermissionError as e:return self._send(403,{"error":str(e)})
        except KeyError as e:return self._send(404,{"error":str(e)})
        except Exception as e:return self._send(400,{"error":str(e)})
def main():
    p=argparse.ArgumentParser(); p.add_argument("--database",default=":memory:"); p.add_argument("--host",default="127.0.0.1"); p.add_argument("--port",type=int,default=8080); a=p.parse_args(); Handler.service=PenaltyService(a.database); Handler.service.bootstrap(); ThreadingHTTPServer((a.host,a.port),Handler).serve_forever()
if __name__=="__main__":main()
