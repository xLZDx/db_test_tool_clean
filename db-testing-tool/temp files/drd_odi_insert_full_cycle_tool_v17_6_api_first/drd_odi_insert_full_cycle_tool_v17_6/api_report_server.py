#!/usr/bin/env python3
"""
api_report_server.py

Optional read-only HTTP server for generated final_reports/api JSON files.

Usage:
  python api_report_server.py --api-dir out_full_cycle/final_reports/api --host 127.0.0.1 --port 8000

Endpoints:
  /health
  /manifest
  /step1
  /step1/tabs/<safe_tab_name>
  /step4
  /step4/tabs/<safe_tab_name>
  /openapi_contract
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote


def make_handler(api_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, payload, code=200):
            data = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_json(self, path: Path):
            if not path.exists() or not path.is_file():
                return None
            return json.loads(path.read_text(encoding="utf-8"))

        def do_GET(self):
            path = unquote(self.path.split("?", 1)[0]).strip("/")
            if path == "health":
                return self._send_json({"status": "ok", "api_dir": str(api_dir)})
            if path in {"manifest", ""}:
                payload = self._read_json(api_dir / "manifest.json")
                return self._send_json(payload if payload is not None else {"error": "manifest not found"}, 200 if payload else 404)
            if path == "openapi_contract":
                payload = self._read_json(api_dir / "openapi_contract.json")
                return self._send_json(payload if payload is not None else {"error": "openapi contract not found"}, 200 if payload else 404)
            if path == "step1":
                payload = self._read_json(api_dir / "step1_compare_report.json")
                return self._send_json(payload if payload is not None else {"error": "step1 not found"}, 200 if payload else 404)
            if path == "step4":
                payload = self._read_json(api_dir / "step4_full_cycle_report.json")
                return self._send_json(payload if payload is not None else {"error": "step4 not found"}, 200 if payload else 404)
            if path.startswith("step1/tabs/"):
                tab = path.split("/", 2)[2]
                payload = self._read_json(api_dir / "step1" / "tabs" / f"{tab}.json")
                return self._send_json(payload if payload is not None else {"error": "step1 tab not found", "tab": tab}, 200 if payload else 404)
            if path.startswith("step4/tabs/"):
                tab = path.split("/", 2)[2]
                payload = self._read_json(api_dir / "step4" / "tabs" / f"{tab}.json")
                return self._send_json(payload if payload is not None else {"error": "step4 tab not found", "tab": tab}, 200 if payload else 404)
            return self._send_json({"error": "not found", "path": path}, 404)
    return Handler


def main(argv=None):
    p = argparse.ArgumentParser(description="Serve generated DRD/ODI/INSERT API JSON reports")
    p.add_argument("--api-dir", required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args(argv)

    api_dir = Path(args.api_dir).expanduser().resolve()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(api_dir))
    print(f"Serving {api_dir} at http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
