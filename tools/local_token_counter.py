#!/usr/bin/env python3
"""Tokenise chat requests in a process that never touches the device.

Exact input-token counting used to travel down the same pipe as generation, behind the same lock,
into a worker that reads one command at a time. A queued request's count therefore waited for an
unrelated prefill to finish — and the proxy bounds that wait with LOCAL_PREPARATION_TIMEOUT, so on a
single-slot engine the normal case was a 503 that re-queued behind the very generation that starved
it. Splitting the listener from the executor moved the socket; it could not move the fact that one
command loop serves one thing at a time.

This process removes that dependency. It loads only the tokenizer — ``mlx_lm.load_tokenizer``
pulls the ``*.json``/``tokenizer.model`` family and no weights, so it neither allocates a model nor
issues a Metal kernel, and for a local artifact directory it performs no network access at all. It
imports :func:`mlx_worker.prepare_prompt` rather than reimplementing it, which is the only thing
that makes "the count the proxy admitted on" and "the prompt the engine will spend" the same number.

Line-delimited JSON on stdin, one document per request, replies carrying the request's ``sequence``.
Standard library plus mlx_lm; no HTTP, no auth: the frontend owns the socket and the credential, and
this is a private child of it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent
for _entry in (str(_ROOT), str(_ROOT / "local_engines"), str(_ROOT.parent / "proxy")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import mlx_worker  # noqa: E402 - reused so counting and generation cannot disagree


class CounterFatal(RuntimeError):
    """The tokenizer is unusable. The parent treats this as 'degrade', never as engine damage."""


def build_tokenizer(model_dir: str) -> Any:
    """Load just the tokenizer for a pinned artifact directory."""
    from mlx_lm import load_tokenizer

    if not Path(model_dir).is_dir():
        raise CounterFatal(f"artifact directory is not present: {model_dir}")
    try:
        return load_tokenizer(model_dir)
    except CounterFatal:
        raise
    except Exception as exc:  # noqa: BLE001 - a tokenizer that will not load has many causes
        raise CounterFatal(f"tokenizer could not be loaded: {exc}") from exc


def count_with(tokenizer: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """The same preparation the engine will perform, reduced to a token count.

    ``WorkerRefused`` propagates unchanged: a request the engine would refuse must not be admitted
    on the strength of a count, or the proxy reserves for work that never happens.
    """
    ids, _prompt, template_identity = mlx_worker.prepare_prompt(tokenizer, payload)
    return {
        "input_tokens": len(ids),
        "tokenizer_identity": mlx_worker.digest_of_tokenizer(tokenizer),
        "template_identity": template_identity,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", required=True)
    return parser.parse_args(argv)


def serve(stdin, stdout, tokenizer: Any) -> int:
    """Answer count requests until stdin closes. Any single bad request is answered, not fatal."""
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            _write(
                stdout,
                {"kind": "refused", "code": str(mlx_worker.WorkerError.MALFORMED_REQUEST)},
            )
            continue
        sequence = request.get("sequence")
        action = request.get("action")
        if action != "input_tokens":
            # Anything else belongs to the engine. Answering it here would let a routing bug in the
            # frontend turn into a second, quieter execution path.
            _write(
                stdout,
                {
                    "kind": "refused",
                    "code": str(mlx_worker.WorkerError.UNSUPPORTED_SETTING),
                    "sequence": sequence,
                },
            )
            continue
        try:
            count = count_with(tokenizer, request.get("payload") or {})
        except mlx_worker.WorkerRefused as refusal:
            _write(
                stdout,
                {"kind": "refused", "code": str(refusal.code), "sequence": sequence},
            )
            continue
        except mlx_worker.WorkerFatal as failure:
            # A template that rejects the request is a plan/engine skew and affects generation too,
            # so the counter exits and lets the frontend report the degradation once.
            _write(stdout, {"kind": "fatal", "reason": str(failure)})
            return 1
        _write(stdout, {"kind": "input_tokens", "sequence": sequence, "count": count})
    return 0


def _write(stream, payload: dict[str, Any]) -> None:
    stream.write(json.dumps(payload, separators=(",", ":"), allow_nan=False) + "\n")
    stream.flush()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        tokenizer = build_tokenizer(args.model_dir)
    except CounterFatal as failure:
        print(f"token counter refused: {failure}", file=sys.stderr)
        return 2
    os.environ.pop("LOCAL_MPS_TOKEN", None)
    return serve(sys.stdin, sys.stdout, tokenizer)


if __name__ == "__main__":
    raise SystemExit(main())
