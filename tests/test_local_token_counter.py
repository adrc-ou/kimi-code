"""The standalone counting process, driven as the frontend drives it.

This process exists for one reason: the number the proxy admits a request on must be the number the
engine later spends, because with this engine that number decides both admission and the output
budget. Every case calls the real :func:`local_token_counter.serve` and compares the reply with
what :func:`mlx_worker.prepare_prompt` computes for the same payload, so a drift between counting
and generating fails here rather than showing up as an over-full context.

The refusals matter as much as the counts. This is a private child of the frontend on a line
protocol: a reply the frontend cannot route, or a loop that dies on one bad line costs every
request after it.
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for _directory in (
    ROOT,
    ROOT / "tests",
    ROOT / "tools",
    ROOT / "tools" / "local_engines",
    ROOT / "proxy",
):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

import fakemlx  # noqa: E402
import local_token_counter as counter  # noqa: E402
import mlx_worker  # noqa: E402

#: The proxy bounds every local request before dispatch, and the worker refuses one that arrives
#: without a limit -- so a payload here has to carry one or the happy path is a refusal.
PAYLOAD = {"messages": [{"role": "user", "content": "count me"}], "max_tokens": 8}


class ServeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tokenizer = fakemlx.FakeTokenizer()

    def request(
        self,
        sequence: int = 1,
        action: str = "input_tokens",
        payload: Any = PAYLOAD,
    ) -> str:
        return json.dumps({"sequence": sequence, "action": action, "payload": payload})

    def exchange(self, *lines: str) -> tuple[int, list[dict[str, Any]]]:
        out = io.StringIO()
        script = "".join(f"{line}\n" for line in lines)
        code = counter.serve(io.StringIO(script), out, self.tokenizer)
        return code, [json.loads(row) for row in out.getvalue().splitlines() if row.strip()]

    def test_the_count_is_the_number_the_engine_will_spend(self) -> None:
        code, documents = self.exchange(self.request())
        self.assertEqual(code, 0, documents)
        self.assertEqual([document["kind"] for document in documents], ["input_tokens"])
        document = documents[0]
        ids, _prompt, template_identity = mlx_worker.prepare_prompt(self.tokenizer, PAYLOAD)
        self.assertEqual(
            document["count"],
            {
                "input_tokens": len(ids),
                "tokenizer_identity": mlx_worker.digest_of_tokenizer(self.tokenizer),
                "template_identity": template_identity,
            },
        )

    def test_the_reply_carries_the_sequence_the_frontend_correlates_on(self) -> None:
        # local_model_server.count() skips every line whose sequence is not its own, so a reply
        # without one is a reply that will never be read.
        _code, documents = self.exchange(self.request(sequence=7))
        self.assertEqual(documents[0]["sequence"], 7)

    def test_an_action_that_belongs_to_the_engine_is_refused_with_its_sequence(self) -> None:
        # Answering it here would open a second, quieter execution path; dropping it silently would
        # cost the frontend a timeout. It has to be refused, and refused routably.
        code, documents = self.exchange(self.request(sequence=3, action="generate"))
        self.assertEqual(code, 0, documents)
        self.assertEqual(
            documents[0],
            {
                "kind": "refused",
                "code": str(mlx_worker.WorkerError.UNSUPPORTED_SETTING),
                "sequence": 3,
            },
        )

    def test_a_bad_line_costs_a_refusal_and_not_the_counter(self) -> None:
        code, documents = self.exchange(
            "[1, 2]", "null", "not json at all", self.request(sequence=9)
        )
        self.assertEqual(code, 0, documents)
        self.assertEqual([document["kind"] for document in documents][-1], "input_tokens")
        self.assertEqual(documents[-1]["sequence"], 9, "the good request after the bad ones")
        self.assertEqual(len(documents), 4)

    def test_a_payload_that_is_not_an_object_is_refused_not_fatal(self) -> None:
        # A payload of the wrong shape used to reach .get() inside the shared validator, and an
        # AttributeError here had no handler at all: the process died over one bad request.
        for payload in ("not-an-object", ["messages"], 7):
            with self.subTest(payload=payload):
                code, documents = self.exchange(self.request(sequence=5, payload=payload))
                self.assertEqual(code, 0, documents)
                self.assertEqual(
                    documents,
                    [
                        {
                            "kind": "refused",
                            "code": str(mlx_worker.WorkerError.MALFORMED_REQUEST),
                            "sequence": 5,
                        }
                    ],
                )

    def test_a_request_the_engine_would_refuse_is_never_counted(self) -> None:
        # count_with()'s contract: admitting on a count the engine would not honour reserves
        # capacity for work that never happens, so the refusal has to travel instead.
        code, documents = self.exchange(self.request(sequence=4, payload={"messages": []}))
        self.assertEqual(code, 0, documents)
        self.assertEqual(
            documents[0]["code"], str(mlx_worker.WorkerError.MALFORMED_REQUEST), documents
        )
        self.assertNotIn("count", documents[0])

    def test_a_template_that_rejects_the_request_is_fatal_by_design(self) -> None:
        class Skewed:
            chat_template = "x"

            def apply_chat_template(self, messages: Any, **kwargs: Any) -> str:
                raise TypeError("missing required kwarg: add_generation_prompt")

            def encode(self, text: str, **kwargs: Any) -> list[int]:
                return [1]

        out = io.StringIO()
        code = counter.serve(io.StringIO(f"{self.request()}\n"), out, Skewed())
        documents = [json.loads(row) for row in out.getvalue().splitlines() if row.strip()]
        self.assertEqual(code, 1, documents)
        self.assertEqual([document["kind"] for document in documents], ["fatal"])

    def test_blank_lines_cost_nothing(self) -> None:
        code, documents = self.exchange("", self.request(sequence=2))
        self.assertEqual(code, 0, documents)
        self.assertEqual(len(documents), 1)


class ProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tokenizer = fakemlx.FakeTokenizer()

    def test_the_session_credential_never_reaches_the_serving_loop(self) -> None:
        # The counter has no reason to hold the token: the frontend owns the socket. It is popped
        # rather than never-set because this process inherits the frontend's whole environment.
        with (
            mock.patch.dict(os.environ, {"LOCAL_MPS_TOKEN": "s" * 48}),
            mock.patch.object(counter, "build_tokenizer", return_value=self.tokenizer),
            mock.patch.object(sys, "stdin", io.StringIO("")),
            mock.patch.object(sys, "stdout", io.StringIO()),
        ):
            code = counter.main(["--model-dir", "/fixture/artifact"])
            self.assertEqual(code, 0)
            # assertFalse, not assertNotIn: on failure the latter reprs the whole mapping, and an
            # environment this size holds credentials that have no business reaching a log.
            self.assertFalse(
                "LOCAL_MPS_TOKEN" in os.environ,
                "the counter served while still holding the session credential",
            )

    def test_an_absent_artifact_directory_is_named_without_loading_mlx(self) -> None:
        # The check runs ahead of the import. On a host with no mlx_lm -- every CI machine -- a
        # guard that came second would raise ModuleNotFoundError here, blaming a missing dependency
        # for what is really a missing model directory.
        with self.assertRaises(counter.CounterFatal) as caught:
            counter.build_tokenizer(str(ROOT / "no-such-artifact-directory"))
        self.assertIn("not present", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
