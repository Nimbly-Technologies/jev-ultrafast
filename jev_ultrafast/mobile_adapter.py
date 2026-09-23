"""Local HTTP adapter used by the opt-in Maestro Android healing hook."""

from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .mobile import MAX_MOBILE_CANDIDATES, MobileHealingRefused, choose_mobile_candidate

MAX_BODY_BYTES = 64 * 1024


def heal_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Handle one bounded request without exposing the model request or raw hierarchy."""

    if not isinstance(payload, dict):
        raise ValueError("Request must be a JSON object")
    selector = payload.get("selector")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("candidates must be a list")
    if len(candidates) > MAX_MOBILE_CANDIDATES:
        raise ValueError("candidate list exceeds the mobile bound")
    if any(not isinstance(candidate, dict) for candidate in candidates):
        raise ValueError("candidates must contain JSON objects")
    decision = choose_mobile_candidate(selector, candidates)
    return {"ok": True, **decision}


class MobileAdapterHandler(BaseHTTPRequestHandler):
    server_version = "jev-mobile-adapter/0.1"

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/v1/mobile/heal":
            self._write(404, {"ok": False, "reason": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            self._write(400, {"ok": False, "reason": "invalid_content_length"})
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self._write(413, {"ok": False, "reason": "request_too_large"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
            result = heal_payload(payload)
        except MobileHealingRefused:
            self._write(422, {"ok": False, "reason": "refused"})
        except RuntimeError:
            self._write(503, {"ok": False, "reason": "provider_unavailable"})
        except (json.JSONDecodeError, ValueError, TypeError):
            self._write(400, {"ok": False, "reason": "invalid_request"})
        except Exception:
            self._write(500, {"ok": False, "reason": "adapter_error"})
        else:
            self._write(200, result)

    def log_message(self, format: str, *args: Any) -> None:
        # Do not log request bodies, labels, selectors, or model responses.
        return

    def _write(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Serve Jev's local Android healing adapter")
    parser.add_argument("--host", default=os.environ.get("MAESTRO_JEV_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MAESTRO_JEV_PORT", "8767")))
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("the mobile adapter must bind to loopback")
    server = ThreadingHTTPServer((args.host, args.port), MobileAdapterHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
