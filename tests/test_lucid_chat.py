"""Lucid chat receives AirDrop only from the bridge on this computer."""

import importlib.util
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("lucid_chat", ROOT / "bin" / "lucid_chat.py")
lucid_chat = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(lucid_chat)


class DescribeTests(unittest.TestCase):
    def test_text_excerpt_has_no_directory(self):
        # The name is the basename only, even when the file lives in a home path.
        path = Path("/tmp") / f"omdrop-lucid-{os.getpid()}.txt"
        path.write_text("apples\n", encoding="utf-8")
        self.addCleanup(path.unlink, missing_ok=True)
        drop = lucid_chat.describe_file(path)
        self.assertEqual(drop["kind"], "text")
        self.assertEqual(drop["excerpt"], "apples\n")
        self.assertNotIn("/", drop["name"])
        self.assertNotIn(str(path.parent), json.dumps(drop))

    def test_binary_has_no_excerpt(self):
        path = Path("/tmp") / f"omdrop-lucid-{os.getpid()}.bin"
        path.write_bytes(b"\x00\x01\x02")
        self.addCleanup(path.unlink, missing_ok=True)
        drop = lucid_chat.describe_file(path)
        self.assertEqual(drop["kind"], "file")
        self.assertNotIn("excerpt", drop)

    def test_web_link_is_kept_and_other_schemes_are_not(self):
        web = Path("/tmp") / f"omdrop-lucid-{os.getpid()}.url"
        web.write_text("[InternetShortcut]\nURL=https://omarchy.org/docs\n", encoding="utf-8")
        self.addCleanup(web.unlink, missing_ok=True)
        drop = lucid_chat.describe_file(web)
        self.assertEqual(drop["kind"], "link")
        self.assertEqual(drop["url"], "https://omarchy.org/docs")

        other = Path("/tmp") / f"omdrop-lucid-file-{os.getpid()}.url"
        other.write_text("[InternetShortcut]\nURL=file:///etc/passwd\n", encoding="utf-8")
        self.addCleanup(other.unlink, missing_ok=True)
        refused = lucid_chat.describe_file(other)
        self.assertEqual(refused["kind"], "file")
        self.assertNotIn("url", refused)
        self.assertNotIn("passwd", json.dumps(refused))


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Path("/tmp") / f"omdrop-lucid-{os.getpid()}" / "omdrop" / "lucid.json"
        self.server = lucid_chat.open_server(0, self.runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.addCleanup(self._close)

    def _close(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.runtime.unlink(missing_ok=True)

    def _request(self, path, method="GET", data=None, token=None, origin=None):
        headers = {}
        if token is not None:
            headers["X-Omdrop-Token"] = token
        if origin is not None:
            headers["Origin"] = origin
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def test_the_bridge_listens_on_this_device_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        state = json.loads(self.runtime.read_text(encoding="utf-8"))
        self.assertEqual(state["port"], self.port)
        self.assertEqual(oct(self.runtime.stat().st_mode & 0o777), "0o600")

    def test_inbox_requires_the_code_and_returns_the_arrival(self):
        status, _ = self._request("/v1/inbox")
        self.assertEqual(status, 403)
        body = json.dumps({"name": "notes.txt", "bytes": 6, "kind": "text", "excerpt": "apples", "path": "/secret"}).encode()
        status, raw = self._request("/v1/arrived", "POST", body, token=self.server.token)
        self.assertEqual(status, 200)
        stored = json.loads(raw)
        self.assertNotIn("path", stored)
        status, raw = self._request("/v1/inbox?after=0", token=self.server.token, origin="https://lucid.dreamsequence.pro")
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        self.assertTrue(payload["epoch"])
        drops = payload["drops"]
        self.assertEqual(drops[0]["excerpt"], "apples")
        self.assertNotIn("path", drops[0])

    def test_a_foreign_origin_is_refused(self):
        status, _ = self._request(
            "/v1/health",
            origin="https://example.test",
        )
        self.assertEqual(status, 403)

    def test_note_posts_the_file_and_a_missing_bridge_is_quiet(self):
        path = Path("/tmp") / f"omdrop-lucid-note-{os.getpid()}.txt"
        path.write_text("cider", encoding="utf-8")
        self.addCleanup(path.unlink, missing_ok=True)
        self.assertEqual(lucid_chat.note(path, self.runtime), 0)
        status, raw = self._request("/v1/inbox?after=0", token=self.server.token)
        self.assertEqual(json.loads(raw)["drops"][0]["excerpt"], "cider")
        self.assertEqual(status, 200)
        missing = self.runtime.with_suffix(".absent")
        self.assertEqual(lucid_chat.note(path, missing), 0)


class HelpTests(unittest.TestCase):
    def test_help_offers_the_lucid_receiver(self):
        import subprocess
        result = subprocess.run([str(ROOT / "bin" / "omdrop"), "help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("lucid [on|off|status]", result.stdout)
        self.assertIn("this device", result.stdout)


if __name__ == "__main__":
    unittest.main()
