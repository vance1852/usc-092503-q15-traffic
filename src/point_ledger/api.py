"""记分资格账本的无第三方依赖 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import LedgerError, ValidationFailed
from .service import PointLedgerService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any] | list[Any]


class LedgerApplication:
    def __init__(self, service: PointLedgerService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(self, method: str, target: str, headers: Mapping[str, str] | None = None,
               body: bytes = b"") -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok", "service": "point-ledger"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized)
            if method == "POST" and path == "/users":
                return Response(201, self.service.create_user(
                    actor, payload["user_id"], payload["display_name"], payload["role"]))
            if method == "POST" and path == "/drivers":
                return Response(201, self.service.register_driver(
                    actor, payload["driver_id"], payload["license_issued_on"]))
            if method == "POST" and path == "/rules":
                return Response(201, self.service.register_rule(
                    actor, payload["version"], payload["effective_from"],
                    payload["thresholds"], payload.get("suspend_days"),
                    payload.get("effective_to")))
            if (method == "POST" and len(parts) == 3
                    and parts[0] == "drivers" and parts[2] == "penalties"):
                return Response(201, self.service.record_penalty(
                    actor, parts[1], penalty_id=payload["penalty_id"],
                    violation_id=payload["violation_id"], points=int(payload["points"]),
                    occurred_at=payload["occurred_at"],
                    idempotency_key=payload.get("idempotency_key")))
            if (method == "POST" and len(parts) == 3
                    and parts[0] == "drivers" and parts[2] == "revocations"):
                return Response(201, self.service.revoke_penalty(
                    actor, parts[1], penalty_id=payload["penalty_id"],
                    reason=payload["reason"], occurred_at=payload.get("occurred_at"),
                    note=payload.get("note", "")))
            if (method == "POST" and len(parts) == 3
                    and parts[0] == "drivers" and parts[2] == "studies"):
                return Response(201, self.service.complete_study(
                    actor, parts[1], study_record_id=payload["study_record_id"],
                    kind=payload.get("kind", "period_study"),
                    occurred_at=payload.get("occurred_at")))
            if method == "POST" and path == "/advance":
                return Response(200, self.service.advance(actor))
            if method == "GET" and len(parts) == 3 and parts[0] == "drivers" and parts[2] == "ledger":
                return Response(200, self.service.ledger(
                    actor, parts[1], query.get("as_of", [None])[0]))
            if method == "GET" and len(parts) == 3 and parts[0] == "drivers" and parts[2] == "events":
                return Response(200, {"events": self.service.events(actor, parts[1])})
            if method == "GET" and path == "/audit/chain":
                return Response(200, self.service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except LedgerError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: LedgerApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PointLedger/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动驾驶证记分资格账本服务")
    parser.add_argument("--database", type=Path, default=Path("point_ledger.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    service = PointLedgerService(connection)
    service.bootstrap()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(LedgerApplication(service)))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
