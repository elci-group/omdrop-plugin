#!/usr/bin/env python3
"""Hand AirDrop arrivals to Lucid chat on this computer.

The receiver is this device. Lucid's server never joins the AirDrop link.
``omdrop lucid on`` listens on 127.0.0.1. The Lucid page on this same
computer polls it with the pairing code printed at start. A file stays on
this device. The page receives the file name, its size, and a short text
excerpt when the bytes are text.
"""

from __future__ import annotations

import json
import os
import plistlib
import secrets
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

HOST = "127.0.0.1"
DEFAULT_PORT = 8772
EXCERPT_BYTES = 32_000
LINK_BYTES = 65_536
MAX_DROPS = 50
ALLOWED_ORIGIN = "https://lucid.dreamsequence.pro"


def runtime_path() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        return Path(base) / "omdrop" / "lucid.json"
    return Path.home() / ".omdrop" / "lucid.json"


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # The runtime directory is ours. Leave /tmp itself alone.
    if path.parent.name == "omdrop":
        path.parent.chmod(0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def _read_runtime(path: Path | None = None) -> dict | None:
    target = path or runtime_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _web_url(value: str) -> str | None:
    text = value.strip()
    lower = text.lower()
    if not (lower.startswith("http://") or lower.startswith("https://")):
        return None
    if any(ord(ch) < 32 or ch.isspace() for ch in text):
        return None
    rest = lower.split("://", 1)[1]
    if not rest or rest.startswith("/"):
        return None
    return text


def _link_target(path: Path) -> str | None:
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > LINK_BYTES:
        return None
    name = path.name.lower()
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if name.endswith(".url"):
        section = False
        for line in raw.decode("utf-8", "replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("["):
                section = stripped.lower().replace(" ", "") == "[internetshortcut]"
                continue
            if section and stripped.lower().startswith("url="):
                return _web_url(stripped.split("=", 1)[1])
        return None
    if name.endswith(".webloc"):
        try:
            url = plistlib.loads(raw).get("URL")
        except Exception:
            return None
        if isinstance(url, str):
            return _web_url(url)
    return None


def describe_file(path: Path) -> dict:
    """Name, size, and either a web link or a text excerpt. No directory path."""
    name = path.name.replace("\n", " ").replace("\r", " ")[:240] or "file"
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    url = _link_target(path) if name.lower().endswith((".url", ".webloc")) else None
    if url:
        return {"name": name, "bytes": size, "kind": "link", "url": url}
    if name.lower().endswith((".url", ".webloc")):
        return {"name": name, "bytes": size, "kind": "file"}
    try:
        raw = path.read_bytes()[:EXCERPT_BYTES]
    except OSError:
        raw = b""
    if raw and b"\x00" not in raw:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = ""
        if text:
            return {"name": name, "bytes": size, "kind": "text", "excerpt": text}
    return {"name": name, "bytes": size, "kind": "file"}


class Inbox:
    def __init__(self) -> None:
        self._drops: list[dict] = []
        self._next = 1
        self._lock = threading.Lock()

    def add(self, drop: dict) -> dict:
        with self._lock:
            stored = {
                "id": str(self._next),
                "name": str(drop.get("name") or "file")[:240],
                "bytes": int(drop.get("bytes") or 0),
                "kind": drop.get("kind") if drop.get("kind") in ("text", "file", "link") else "file",
            }
            self._next += 1
            if stored["kind"] == "text" and isinstance(drop.get("excerpt"), str):
                stored["excerpt"] = drop["excerpt"][:EXCERPT_BYTES]
            if stored["kind"] == "link":
                url = _web_url(str(drop.get("url") or ""))
                if url:
                    stored["url"] = url
                else:
                    stored["kind"] = "file"
            self._drops.append(stored)
            self._drops = self._drops[-MAX_DROPS:]
            return stored

    def since(self, after: str) -> list[dict]:
        with self._lock:
            try:
                floor = int(after)
            except (TypeError, ValueError):
                floor = 0
            return [drop for drop in self._drops if int(drop["id"]) > floor]


def origin_allowed(origin: str) -> bool:
    if not origin:
        return True
    if origin == ALLOWED_ORIGIN:
        return True
    parts = urlsplit(origin)
    return (
        parts.scheme in ("http", "https")
        and parts.hostname in ("127.0.0.1", "localhost")
    )


def _token_ok(presented: str, expected: str) -> bool:
    if len(presented) != len(expected):
        return False
    return secrets.compare_digest(presented, expected)


def make_handler(inbox: Inbox, token: str, epoch: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            return

        def _cors(self) -> bool:
            origin = self.headers.get("Origin", "")
            if not origin_allowed(origin):
                self.send_error(403, "origin")
                return False
            return True

        def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
            origin = self.headers.get("Origin", "")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if origin and origin_allowed(origin):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            if not self._cors():
                return
            origin = self.headers.get("Origin", "")
            body = b""
            self.send_response(204)
            self.send_header("Content-Length", "0")
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Allow-Headers", "X-Omdrop-Token, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            if not self._cors():
                return False
            presented = self.headers.get("X-Omdrop-Token", "")
            if not _token_ok(presented, token):
                self._send(403, b'{"error":"code"}')
                return False
            return True

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/v1/health":
                if not self._cors():
                    return
                self._send(200, b'{"ok":true,"receiver":"client"}')
                return
            if path != "/v1/inbox":
                self._send(404, b'{"error":"not-found"}')
                return
            if not self._authorized():
                return
            query = parse_qs(urlsplit(self.path).query)
            after = (query.get("after") or ["0"])[0]
            # epoch changes every time this process starts, because arrival
            # ids start again at 1. The page keeps its high-water only while
            # the epoch matches.
            body = json.dumps({"epoch": epoch, "drops": inbox.since(after)}).encode("utf-8")
            self._send(200, body)

        def do_POST(self):
            if urlsplit(self.path).path != "/v1/arrived":
                self._send(404, b'{"error":"not-found"}')
                return
            if not self._authorized():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length < 0 or length > 80_000:
                self._send(413, b'{"error":"size"}')
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send(400, b'{"error":"json"}')
                return
            if not isinstance(payload, dict):
                self._send(400, b'{"error":"json"}')
                return
            # The arrival record carries no path. This process does not open
            # a file named by the request.
            payload.pop("path", None)
            stored = inbox.add(payload)
            self._send(200, json.dumps(stored).encode("utf-8"))

    return Handler


def open_server(port: int, runtime: Path) -> ThreadingHTTPServer:
    token = secrets.token_hex(8)
    epoch = secrets.token_hex(8)
    inbox = Inbox()
    httpd = ThreadingHTTPServer((HOST, port), make_handler(inbox, token, epoch))
    bound = httpd.server_address
    if bound[0] != HOST:
        httpd.server_close()
        raise SystemExit("omdrop lucid refuses to listen outside this device")
    _write_private(runtime, json.dumps({
        "pid": os.getpid(),
        "port": bound[1],
        "token": token,
    }))
    httpd.runtime = runtime  # type: ignore[attr-defined]
    httpd.token = token  # type: ignore[attr-defined]
    return httpd


def serve(port: int, runtime: Path) -> None:
    httpd = open_server(port, runtime)
    bound_port = httpd.server_address[1]
    print(f"Lucid chat can receive AirDrop on this device at http://{HOST}:{bound_port}", flush=True)
    print(f"AirDrop code: {httpd.token}", flush=True)
    print("The file stays on this computer. Paste the code into Lucid.", flush=True)

    def stop(_signum, _frame):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        try:
            runtime.unlink()
        except OSError:
            pass


def note(path: Path, runtime: Path | None = None) -> int:
    """Tell a running bridge about one saved file. Missing bridge is success."""
    state = _read_runtime(runtime)
    if not state:
        return 0
    try:
        port = int(state["port"])
        token = str(state["token"])
    except (KeyError, TypeError, ValueError):
        return 0
    if not path.is_file():
        return 0
    payload = json.dumps(describe_file(path)).encode("utf-8")
    request = Request(
        f"http://{HOST}:{port}/v1/arrived",
        data=payload,
        headers={"Content-Type": "application/json", "X-Omdrop-Token": token},
        method="POST",
    )
    try:
        with urlopen(request, timeout=2) as response:
            response.read()
    except (URLError, TimeoutError, OSError):
        return 0
    return 0


def stop_bridge(runtime: Path | None = None) -> int:
    state = _read_runtime(runtime)
    if not state or not state.get("pid"):
        print("omdrop lucid is not running")
        return 0
    pid = int(state["pid"])
    cmdline = ""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        cmdline = ""
    if "lucid_chat.py" not in cmdline:
        print("omdrop lucid is not running")
        return 0
    os.kill(pid, signal.SIGTERM)
    print("omdrop lucid is off")
    return 0


def status(runtime: Path | None = None) -> int:
    state = _read_runtime(runtime)
    if not state:
        print("omdrop lucid is off. Run: omdrop lucid on")
        return 0
    print(f"omdrop lucid is on at http://{HOST}:{state.get('port', DEFAULT_PORT)}")
    print(f"AirDrop code: {state.get('token', '')}")
    print("This device is the receiver.")
    return 0


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "status"
    runtime = runtime_path()
    if command == "serve":
        port = int(os.environ.get("OMDROP_LUCID_PORT", str(DEFAULT_PORT)))
        serve(port, runtime)
        return 0
    if command == "note":
        if len(argv) < 3:
            print("usage: lucid_chat.py note FILE", file=sys.stderr)
            return 2
        return note(Path(argv[-1]), runtime)
    if command == "off":
        return stop_bridge(runtime)
    if command == "status":
        return status(runtime)
    print("usage: lucid_chat.py serve|note FILE|off|status", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
