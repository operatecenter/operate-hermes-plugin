"""Security and behavior regression tests for operate-hermes-plugin."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PLUGIN_DIR = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("operate_hermes_plugin", PLUGIN_DIR / "__init__.py")
assert SPEC and SPEC.loader
MOD = importlib.util.module_from_spec(SPEC)
sys.modules["operate_hermes_plugin"] = MOD
SPEC.loader.exec_module(MOD)


class _DummyState:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self._store = {}

    def get(self, key: str, default=None):
        return self._store.get(key, default)

    def set(self, key: str, value) -> None:
        self._store[key] = value


class _DummyCtx:
    def __init__(self, data_dir: Path, config=None) -> None:
        self.state = _DummyState(data_dir)
        self._config = dict(config or {})
        self.tools = {}
        self.hooks = {}
        self.commands = {}
        self.blueprints = {}
        self.skills = {}

    def get_config(self, key: str, default=None):
        return self._config.get(key, default)

    def register_tool(self, *, name, toolset, schema, handler, description="", emoji=""):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler}

    def register_hook(self, hook_name: str, callback, **_kwargs):
        self.hooks.setdefault(hook_name, []).append(callback)

    def register_command(self, name: str, handler, **_kwargs):
        self.commands[name] = handler

    def register_skill(self, name: str, skill_md: Path, **_kwargs):
        self.skills[name] = skill_md

    def register_automation_blueprint(self, bp_id: str, **kwargs):
        self.blueprints[bp_id] = kwargs


class PluginSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.outbox = self.base / "outbox"
        self.deliveries = self.base / "deliveries"
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.deliveries.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_upload_confined_to_outbox_and_blocks_traversal_and_symlinks(self) -> None:
        outside_file = self.base / "secret.txt"
        outside_file.write_text("TOP_SECRET", encoding="utf-8")

        inside_file = self.outbox / "deliverable.txt"
        inside_file.write_text("OK_CONTENT", encoding="utf-8")

        symlink_file = self.outbox / "leak_link.txt"
        symlink_file.symlink_to(outside_file)

        subdir_outside = self.base / "other_dir"
        subdir_outside.mkdir()
        (subdir_outside / "nested.txt").write_text("NESTED_SECRET", encoding="utf-8")
        dir_symlink = self.outbox / "linked_dir"
        dir_symlink.symlink_to(subdir_outside)

        client = MOD.OperateCenterClient(
            api_key="agt_secret",
            outbox_dir=str(self.outbox),
            deliveries_dir=str(self.deliveries),
        )

        # Inside outbox works (both relative and resolved path)
        self.assertEqual(client.resolve_safe_upload_path("deliverable.txt"), inside_file.resolve())
        self.assertEqual(client.resolve_safe_upload_path(str(inside_file)), inside_file.resolve())

        # Outside paths, traversal, and symlinks are blocked
        for bad in (
            str(outside_file),
            "../secret.txt",
            str(symlink_file),
            "leak_link.txt",
            "linked_dir/nested.txt",
            "~/.ssh/id_rsa",
        ):
            with self.assertRaises(PermissionError, msg=f"Should have refused {bad}"):
                client.resolve_safe_upload_path(bad)

        # Non-list file_paths rejected
        with self.assertRaises(ValueError):
            client.submit_fulfillment("job-1", {"ok": True}, file_paths=str(outside_file))  # type: ignore[arg-type]

    def test_same_api_origin_and_no_bearer_leak_to_prefix_lookalike_or_redirect(self) -> None:
        self.assertTrue(MOD._is_same_api_origin("https://operate.center/api/download/123", "https://operate.center"))
        self.assertFalse(MOD._is_same_api_origin("https://operate.center.evil.com/steal", "https://operate.center"))
        self.assertFalse(MOD._is_same_api_origin("http://operate.center/api/download/123", "https://operate.center"))

        client = MOD.OperateCenterClient(
            api_key="agt_topsecret_token",
            api_url="https://operate.center",
            deliveries_dir=str(self.deliveries),
            outbox_dir=str(self.outbox),
        )

        seen_requests = []

        class _FakeResp:
            def __init__(self, headers, body: bytes) -> None:
                self.headers = headers
                self._body = body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return self._body

        class _FakeOpener:
            def open(self, req, timeout=60):
                seen_requests.append((req.full_url, dict(req.header_items())))
                if req.full_url == "https://operate.center/api/download/abc":
                    raise urllib.error.HTTPError(
                        req.full_url,
                        302,
                        "Found",
                        {"Location": "https://storage.public.example/file.bin"},
                        None,
                    )
                return _FakeResp({"Content-Type": "application/octet-stream"}, b"FILE_BYTES")

        with (
            patch.object(MOD, "_build_https_no_redirect_opener", return_value=_FakeOpener()),
            patch.object(MOD.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]),
        ):
            data = client._download_artifact_bytes(
                "ord-1",
                {"url": "/api/download/abc"},
            )

        self.assertEqual(data, b"FILE_BYTES")
        self.assertEqual(len(seen_requests), 2)
        self.assertIn("Authorization", seen_requests[0][1])
        self.assertNotIn("Authorization", seen_requests[1][1])

        # Lookalike host must NEVER receive Authorization even on initial request
        seen_requests.clear()
        with (
            patch.object(MOD, "_build_https_no_redirect_opener", return_value=_FakeOpener()),
            patch.object(MOD.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]),
        ):
            client._download_artifact_bytes(
                "ord-1",
                {"url": "https://operate.center.attacker.com/exfil"},
            )
        self.assertNotIn("Authorization", seen_requests[0][1])

    def test_artifact_fetch_blocks_file_http_and_private_loopback_linklocal_on_all_hops(self) -> None:
        client = MOD.OperateCenterClient(
            api_key="agt_secret",
            deliveries_dir=str(self.deliveries),
            outbox_dir=str(self.outbox),
        )

        # file:// and http:// rejected
        for bad_url in (
            "file:///etc/passwd",
            "http://example.com/file.bin",
            "https://127.0.0.1/secret",
            "https://169.254.169.254/latest/meta-data/",
            "https://10.0.0.1/internal",
            "https://[::1]/secret",
        ):
            with self.assertRaises(ValueError, msg=f"Should reject {bad_url}"):
                client._safe_fetch_url(bad_url)

        # Public DNS resolving to 169.254.169.254 on a redirect hop must be rejected
        class _RedirectOpener:
            def open(self, req, timeout=60):
                raise urllib.error.HTTPError(
                    req.full_url,
                    302,
                    "Found",
                    {"Location": "https://metadata-redirect.example/latest"},
                    None,
                )

        def _fake_dns(host, _port, **_kw):
            if host == "public.example":
                return [(2, 1, 6, "", ("93.184.216.34", 443))]
            return [(2, 1, 6, "", ("169.254.169.254", 443))]

        with (
            patch.object(MOD, "_build_https_no_redirect_opener", return_value=_RedirectOpener()),
            patch.object(MOD.socket, "getaddrinfo", side_effect=_fake_dns),
        ):
            with self.assertRaises(ValueError):
                client._safe_fetch_url("https://public.example/file")

    def test_url_encoding_of_all_id_path_segments(self) -> None:
        client = MOD.OperateCenterClient(
            api_key="agt_secret",
            deliveries_dir=str(self.deliveries),
            outbox_dir=str(self.outbox),
        )
        captured_paths = []

        def _fake_req(method: str, path: str, payload=None):
            captured_paths.append((method, path))
            return {"ok": True}

        with patch.object(client, "_request", side_effect=_fake_req):
            evil_id = "../../admin?x=1#frag"
            expected = MOD.urllib.parse.quote(evil_id, safe="")
            client.accept_job(evil_id)
            client.submit_fulfillment(evil_id, {"done": True})
            client.get_order(evil_id)
            client.submit_requirements(evil_id, {"brief": "hi"})
            client.approve_delivery(evil_id)
            client.request_revision(evil_id, "fix it")
            client.get_order_messages(evil_id)
            client.send_order_message(evil_id, "hello")

        self.assertEqual(len(captured_paths), 8)
        for _method, path in captured_paths:
            self.assertIn(expected, path)
            self.assertNotIn("../", path)
            self.assertNotIn("?x=1", path)
        with self.assertRaises(ValueError):
            client.get_order("..")

    def test_save_order_delivery_slugifies_id_and_parses_date_no_traversal(self) -> None:
        client = MOD.OperateCenterClient(
            api_key="agt_secret",
            deliveries_dir=str(self.deliveries),
            outbox_dir=str(self.outbox),
        )
        evil_payload = {
            "order": {
                "id": "../../etc/cron.d/pwn",
                "created_at": "../../../evil-date",
                "operator_id": "op-1",
                "status": "delivered",
                "operators": {"name": "../../Bad Operator Name"},
                "result_data": {"summary": "done"},
            }
        }
        res = client.save_order_delivery("../../etc/cron.d/pwn", order_payload=evil_payload, download_files=False)
        order_dir = Path(res["order_dir"]).resolve()
        self.assertTrue(order_dir.is_relative_to(self.deliveries.resolve()))
        self.assertNotIn("..", order_dir.name)
        self.assertTrue((order_dir / "v1" / "summary.md").is_file())

    def test_plugin_registration_hooks_commands_blueprint_and_secrets_isolation(self) -> None:
        plugin_data = self.base / "profile-a" / "plugin-data" / "operate-hermes-plugin"
        ctx = _DummyCtx(
            data_dir=plugin_data,
            config={"api_key": "agt_live_supersecret_9999"},
        )
        MOD.register(ctx)

        # 1. pre_tool_call hook gates all 6 money/state/upload tools and blocks outbox escapes
        self.assertIn("pre_tool_call", ctx.hooks)
        gate = ctx.hooks["pre_tool_call"][0]

        for tool_name in (
            "operate_create_gig",
            "operate_update_gig",
            "operate_accept_job",
            "operate_submit_fulfillment",
            "operate_hire_gig",
            "operate_manage_order",
        ):
            verdict = gate(tool_name=tool_name, args={"action": "approve", "orderId": "ord-1", "jobId": "job-1"})
            self.assertIsInstance(verdict, dict)
            self.assertEqual(verdict.get("action"), "approve", f"{tool_name} must require approval")

        # Outbox violation triggers hard block in pre_tool_call
        block_verdict = gate(
            tool_name="operate_submit_fulfillment",
            args={"jobId": "job-1", "result_data": {}, "file_paths": ["/etc/passwd"]},
        )
        self.assertEqual(block_verdict.get("action"), "block")

        # Read-only tools pass without approval gate
        self.assertIsNone(gate(tool_name="operate_check_queue", args={}))
        self.assertIsNone(gate(tool_name="operate_list_gigs", args={}))

        # 2. SYNC_DELIVERIES_SCHEMA has no model-supplied deliveries_dir parameter
        sync_props = MOD.SYNC_DELIVERIES_SCHEMA["parameters"]["properties"]
        self.assertNotIn("deliveries_dir", sync_props)

        # 3. Blueprint only heartbeats, checks queue, checks earnings/deliveries & reports to user
        bp = ctx.blueprints["autonomous-worker"]
        prompt_text = bp["prompt_template"]
        self.assertNotIn("operate_accept_job", prompt_text)
        self.assertNotIn("operate_submit_fulfillment", prompt_text)
        self.assertIn("operate_check_queue", prompt_text)
        self.assertIn("untrusted data", prompt_text.lower())

        # 4. /operate status does not leak API key characters
        with patch.object(MOD.OperateCenterClient, "get_earnings", side_effect=RuntimeError("offline")):
            status_out = ctx.commands["operate"]("status")
        self.assertIn("Configured", status_out)
        self.assertNotIn("agt_live", status_out)
        self.assertNotIn("9999", status_out)

        # 5. /operate demo does not mutate persisted telemetry ledger
        demo_out = ctx.commands["operate"]("demo")
        self.assertIn("DEMO", demo_out)
        self.assertEqual(ctx.state.get("telemetry"), None)

        # 6. Inside Hermes (_hermes_get_secret present), os.environ is NEVER used even if get_secret returns None or raises
        with (
            patch.object(MOD, "_hermes_get_secret", side_effect=RuntimeError("no scope")),
            patch.dict(os.environ, {"OPERATE_API_KEY": "agt_leaked_from_other_profile"}, clear=False),
        ):
            self.assertEqual(MOD._resolve_secret("OPERATE_API_KEY", ""), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
