"""Operator evidence uses synthetic fixtures, never a real model or host engine."""

import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tools import capacity
from tools import local_capacity_smoke as smoke


def engine(slots=2):
    return {
        "context_tokens": 1024,
        "sequences": slots,
        "total_context_tokens": 1024 * slots,
        "model_alias": "fixture",
        "model_path": "/fixture/model.gguf",
        "build_commit": "abcdef123",
        "build_info": "fixture abcdef123",
        "fingerprint": "fixture-fingerprint",
        "snapshot_id": "fixture-snapshot",
        "capacity_strategy": "native_context",
        "memory_management": "os_managed",
        "native_context_tokens": 1024,
        "allocation": {"cache_type_k": "f16", "cache_type_v": "f16"},
        "serving_profile_id": "profile-fixture",
        "projection": {
            "core_host_bytes": 14 * capacity.MIB,
            "core_device_bytes": {"Metal": 11 * capacity.MIB},
        },
    }


class FakeEngine:
    def __init__(self, plan, bad_count=False, bad_usage=False):
        self.plan, self.bad_count, self.bad_usage = plan, bad_count, bad_usage
        self.lock = threading.Lock()
        self.active = 0
        self.peak_active = 0
        self.preparation_seen = False
        self.release = threading.Event()
        self.paths = []

    def count(self, payload):
        return 20 + payload["messages"][0]["content"].count("λ =") * 10

    def request(self, path, payload=None):
        with self.lock:
            self.paths.append(path)
        if path == "/props":
            return {
                "model_alias": self.plan["model_alias"],
                "model_path": self.plan["model_path"],
                "build_info": self.plan["build_info"],
                "is_sleeping": False,
                "total_slots": self.plan["sequences"],
                "default_generation_settings": {"n_ctx": self.plan["context_tokens"]},
            }
        if path == "/slots":
            with self.lock:
                value = [
                    {"is_processing": index < self.active}
                    for index in range(self.plan["sequences"])
                ]
                if self.preparation_seen:
                    threading.Timer(0.02, self.release.set).start()
                return value
        if path.endswith("/input_tokens"):
            with self.lock:
                if self.active == self.plan["sequences"]:
                    self.preparation_seen = True
                    if self.bad_count:
                        self.release.set()
                        return {"input_tokens": True}
            return {"input_tokens": self.count(payload)}
        if path == "/v1/chat/completions":
            with self.lock:
                self.active += 1
                self.peak_active = max(self.peak_active, self.active)
            if not self.release.wait(2):
                raise smoke.SmokeError("fixture execution timed out")
            with self.lock:
                self.active -= 1
            return {
                "usage": {
                    "prompt_tokens": self.count(payload),
                    "completion_tokens": 0 if self.bad_usage else payload["max_tokens"],
                }
            }
        raise AssertionError(path)


class SyntheticWorkTests(unittest.TestCase):
    def test_counted_near_window_prompt_includes_tools_code_unicode_and_thinking(self):
        plan = engine()
        client = FakeEngine(plan)
        payload, count = smoke.near_window_prompt(client, plan, 16)
        self.assertLessEqual(count + 16, plan["context_tokens"])
        self.assertGreaterEqual(count, (plan["context_tokens"] - 16) * 0.95)
        self.assertIn("日本語", payload["messages"][0]["content"])
        self.assertTrue(payload["chat_template_kwargs"]["enable_thinking"])
        self.assertTrue(payload["tools"])
        self.assertTrue(payload["ignore_eos"])

    def test_full_slots_and_preparation_overlap_is_observed_and_rss_is_owned_pid_only(self):
        plan = engine()
        client = FakeEngine(plan)
        rss = mock.Mock(return_value=50 * capacity.MIB)
        result = smoke.exercise_engine(client, plan, 4321, 16, 2, rss)
        self.assertEqual(client.peak_active, 2)
        self.assertTrue(client.preparation_seen)
        self.assertTrue(result["preparation_while_all_slots_occupied"])
        self.assertEqual(result["peak_owned_engine_rss_bytes"], 50 * capacity.MIB)
        self.assertNotIn("within_planned_host_bytes", result)
        self.assertEqual({call.args for call in rss.call_args_list}, {(4321,)})
        self.assertEqual(len(result["execution_usages"]), 2)

    def test_malformed_preparation_and_short_generation_fail_evidence(self):
        for option in ("bad_count", "bad_usage"):
            with self.subTest(option=option):
                plan = engine()
                client = FakeEngine(plan, **{option: True})
                with self.assertRaises(smoke.SmokeError):
                    smoke.exercise_engine(client, plan, 4321, 16, 2, lambda _: 50 * capacity.MIB)

    def test_unmeasurable_rss_and_exceeded_memory_are_distinct(self):
        plan = engine()
        with self.assertRaisesRegex(smoke.SmokeError, "RSS"):
            smoke.exercise_engine(FakeEngine(plan), plan, 1, 16, 2, lambda _: None)
        result = smoke.exercise_engine(
            FakeEngine(plan), plan, 1, 16, 2, lambda _: 101 * capacity.MIB
        )
        self.assertEqual(result["peak_owned_engine_rss_bytes"], 101 * capacity.MIB)

    def test_busy_engine_is_rejected_without_starting_requests(self):
        plan = engine()
        client = FakeEngine(plan)
        client.active = 1
        with self.assertRaisesRegex(smoke.SmokeError, "busy"):
            smoke.exercise_engine(client, plan, 1, 16, 2)
        self.assertNotIn("/v1/chat/completions", client.paths)

    def test_constant_or_invalid_counts_cannot_claim_near_window_work(self):
        for result in ({"input_tokens": 12}, {"input_tokens": True}, {}, []):
            client = mock.Mock()
            client.request.return_value = result
            with self.subTest(result=result), self.assertRaises(smoke.SmokeError):
                smoke.near_window_prompt(client, engine(), 16)


class ArtifactTests(unittest.TestCase):
    def test_stale_local_plan_is_not_reinterpreted_as_native_context(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            (runtime / "model-policy.json").write_text(json.dumps({"schema_version": 2}))
            with self.assertRaisesRegex(smoke.SmokeError, "relaunch"):
                smoke.run_smoke(runtime, 16, 2)

    def test_run_records_identity_and_results_without_credentials_or_text(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            (runtime / "credentials").mkdir()
            (runtime / "credentials" / "local__default").write_text("fixture-private-token")
            plan = {
                "schema_version": 3,
                "engines": {"local/a": engine()},
                "resources": {},
                "lanes": {
                    "primary": {
                        "engine_id": "local/a",
                        "provider": "local",
                        "credential": "default",
                        "base_url": "http://host.docker.internal:8181",
                    }
                },
            }
            (runtime / "model-policy.json").write_text(json.dumps(plan))
            client = FakeEngine(plan["engines"]["local/a"])
            with (
                mock.patch.object(smoke, "owned_receipt", return_value={"engine_pid": 123}),
                mock.patch.object(smoke, "EngineClient", return_value=client),
                mock.patch.object(
                    smoke,
                    "exercise_engine",
                    return_value={
                        "status": "synthetic_smoke_recorded",
                        "memory_management": "os_managed",
                    },
                ),
            ):
                path = smoke.run_smoke(runtime, 16, 2)
            record = json.loads(path.read_text())
            self.assertFalse(record["native_qualification_complete"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("fixture-private-token", path.read_text())
            self.assertNotIn("messages", path.read_text())
            self.assertEqual(
                record["engines"]["local/a"]["identity"]["serving_profile_id"], "profile-fixture"
            )

    def test_stale_receipt_refuses_before_sampling(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            path = smoke.local_llm.readiness_path(runtime, "local/a")
            path.parent.mkdir()
            path.write_text(
                json.dumps(
                    {
                        "engine_pid": 1,
                        "supervisor_pid": 2,
                        "fingerprint": "stale",
                        "snapshot_id": "stale",
                    }
                )
            )
            with (
                self.assertRaisesRegex(smoke.SmokeError, "differs"),
                mock.patch.object(smoke.subprocess, "run") as process,
            ):
                smoke.owned_receipt(runtime, "local/a", engine())
            process.assert_not_called()

    def test_recorded_pid_must_still_belong_to_its_supervisor(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            path = smoke.local_llm.readiness_path(runtime, "local/a")
            path.parent.mkdir()
            path.write_text(
                json.dumps(
                    {
                        "engine_pid": 123,
                        "supervisor_pid": 456,
                        "fingerprint": "fixture-fingerprint",
                        "snapshot_id": "fixture-snapshot",
                    }
                )
            )
            with (
                mock.patch.object(smoke.os, "kill"),
                mock.patch.object(smoke.subprocess, "run", return_value=mock.Mock(stdout="789")),
                self.assertRaisesRegex(smoke.SmokeError, "no longer a child"),
            ):
                smoke.owned_receipt(runtime, "local/a", engine())

    def test_failed_execution_keeps_private_partial_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            (runtime / "credentials").mkdir()
            (runtime / "credentials" / "local__default").write_text("fixture-private-token")
            plan = {
                "schema_version": 3,
                "engines": {"local/a": engine()},
                "resources": {},
                "lanes": {
                    "primary": {
                        "engine_id": "local/a",
                        "provider": "local",
                        "credential": "default",
                        "base_url": "http://host.docker.internal:8181",
                    }
                },
            }
            (runtime / "model-policy.json").write_text(json.dumps(plan))
            with (
                mock.patch.object(smoke, "owned_receipt", return_value={"engine_pid": 123}),
                mock.patch.object(
                    smoke, "exercise_engine", side_effect=smoke.SmokeError("fixture failure")
                ),
                self.assertRaises(smoke.SmokeError),
            ):
                smoke.run_smoke(runtime, 16, 2)
            path = next((runtime / smoke.RECORD_DIRECTORY).glob("smoke-*.json"))
            record = json.loads(path.read_text())
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["failure"], "fixture failure")
            self.assertFalse(record["native_qualification_complete"])
            self.assertNotIn("fixture-private-token", path.read_text())

    def test_shutdown_records_observations_without_memory_admission_or_qualification(self):
        record = {
            "kind": "local_capacity_native_smoke",
            "engines": {"local/a": {"identity": smoke.engine_evidence(engine())}},
        }
        event = {
            "type": "fit_memory_breakdown",
            "data": {
                "unit": "MiB",
                "rows": [
                    {"kind": "device", "name": "Metal", "self": 10, "total": 128},
                    {"kind": "host", "name": "Host", "self": 2},
                ],
            },
        }
        smoke.compare_shutdown(record, "local/a", json.dumps(event))
        comparison = record["engines"]["local/a"]["shutdown_comparison"]
        self.assertEqual(comparison["shutdown_host_bytes"], 14 * capacity.MIB)
        self.assertNotIn("within_core_projection", comparison)
        self.assertEqual(comparison["attribution"], "operator_provided_isolated_event")
        self.assertFalse(record["native_qualification_complete"])
        event["data"]["rows"][0]["self"] = 20
        smoke.compare_shutdown(record, "local/a", json.dumps(event))
        self.assertEqual(
            record["engines"]["local/a"]["shutdown_comparison"]["shutdown_host_bytes"],
            24 * capacity.MIB,
        )
        self.assertEqual(record["status"], "shutdown_compared_review_required")

    def test_duplicate_shutdown_events_and_unknown_engines_fail(self):
        record = {
            "kind": "local_capacity_native_smoke",
            "engines": {"local/a": {"identity": smoke.engine_evidence(engine())}},
        }
        for model, text in (("local/b", "{}"), ("local/a", "{}\n{}")):
            with self.subTest(model=model), self.assertRaises(smoke.SmokeError):
                smoke.compare_shutdown(copy.deepcopy(record), model, text)


class HTTPContractTests(unittest.TestCase):
    def test_authentication_is_sent_but_redirects_are_disabled(self):
        client = smoke.EngineClient("http://127.0.0.1:1234", "fixture-secret", 2)
        response = mock.MagicMock()
        response.status = 200
        response.headers = {}
        response.read.return_value = b'{"input_tokens": 4}'
        response.__enter__.return_value = response
        client.opener = mock.Mock()
        client.opener.open.return_value = response
        self.assertEqual(
            client.request("/v1/chat/completions/input_tokens", {}), {"input_tokens": 4}
        )
        request = client.opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture-secret")
        self.assertIsNone(
            smoke.NoRedirect().redirect_request(None, None, 302, None, None, "https://elsewhere")
        )

    def test_duplicate_json_and_oversized_metadata_are_refused(self):
        for body in (b'{"input_tokens":1,"input_tokens":2}', b" " * (smoke.METADATA_BYTES + 1)):
            client = smoke.EngineClient("http://127.0.0.1:1234", "secret", 2)
            response = mock.MagicMock(status=200, headers={})
            response.read.return_value = body
            response.__enter__.return_value = response
            client.opener = mock.Mock()
            client.opener.open.return_value = response
            with self.subTest(length=len(body)), self.assertRaises(smoke.SmokeError):
                client.request("/props")


if __name__ == "__main__":
    unittest.main()
