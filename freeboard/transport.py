"""Talking to the models -- and the one rule that decides whether a board is honest.

THE FAILURE MODE THIS EXISTS TO PREVENT: a silent 429 read as an abstention.

When a member is rate limited, it does not answer. If the board treats "no answer" as "no
objection", the vote still completes, still prints a tidy consensus, and is now a decision
made by whoever happened not to be throttled. The board looks MORE confident exactly when it
knows less. So every call here returns either an Answer or a Failure, never an empty Answer,
and the board counts failures separately from votes and says so out loud.

The other three things this handles, all of them documented behaviour rather than guesswork:

  * Retry-After. When every provider for a model returns a retry hint, the aggregated 429
    carries a Retry-After header. Honour it; fall back to exponential backoff when absent.

  * The mid-stream 429. If the limit is hit AFTER streaming has begun, the HTTP status was
    already 200, so the error arrives inside the stream as an SSE event with
    finish_reason: "error". A client that only checks the status code reads a truncated
    answer as a complete one.

  * Parameters the model does not have. OpenRouter drops unsupported parameters instead of
    erroring. Ask a model without `response_format` for JSON and you get prose, cheerfully,
    with a 200. So we only send what the catalogue says the model supports.
"""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

API = "https://openrouter.ai/api/v1/chat/completions"


@dataclass
class Answer:
    """A member actually spoke."""
    model: str
    text: str
    raw: dict = field(default_factory=dict, repr=False)
    ok: bool = True


@dataclass
class Failure:
    """A member did not speak. THIS IS NOT A VOTE AND NOT AN ABSTENTION."""
    model: str
    reason: str
    status: int | None = None
    retry_after: float | None = None
    ok: bool = False


class Transport:
    def ask(self, model: dict, messages: list[dict], **kw) -> Answer | Failure:
        raise NotImplementedError


class OfflineTransport(Transport):
    """A deterministic stand-in so the whole board runs with no key and no network.

    It does not pretend to reason. It gives each model a stable, distinct opinion derived
    from its id, which is enough to exercise seating, blind ranking, aggregation, quorum,
    and -- importantly -- the failure paths, which are the ones you actually want tested.
    """

    def __init__(self, fail: set[str] | None = None, script: dict[str, str] | None = None):
        self.fail = fail or set()
        self.script = script or {}
        self.calls: list[tuple[str, str]] = []

    def ask(self, model: dict, messages: list[dict], **kw) -> Answer | Failure:
        mid = model["id"]
        prompt = messages[-1]["content"] if messages else ""
        self.calls.append((mid, prompt))
        if mid in self.fail:
            return Failure(mid, "rate limited (simulated)", status=429, retry_after=1.0)
        if mid in self.script:
            return Answer(mid, self.script[mid])
        seed = sum(ord(c) for c in mid)
        lean = ["yes", "no", "yes, with conditions"][seed % 3]
        return Answer(mid, f"[{model.get('family', '?')}] {lean} - reasoning stub #{seed % 97}")


class OpenRouterTransport(Transport):
    """The real client. Sends only parameters the model is documented to support."""

    def __init__(self, api_key: str, *, app_url: str | None = None, app_title: str = "freeboard",
                 timeout: float = 120.0, max_retries: int = 4, sleep=time.sleep):
        if not api_key:
            raise ValueError("no API key -- use OfflineTransport to run without one")
        self.api_key = api_key
        self.app_url = app_url
        self.app_title = app_title
        self.timeout = timeout
        self.max_retries = max_retries
        self._sleep = sleep

    def _headers(self) -> dict:
        h = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
             "X-Title": self.app_title}
        if self.app_url:
            h["HTTP-Referer"] = self.app_url
        return h

    @staticmethod
    def _payload(model: dict, messages: list[dict], want_json: bool, max_tokens: int | None,
                 temperature: float | None) -> dict:
        """Only what this model supports. Anything else is dropped in silence upstream."""
        supported = set(model.get("supported_parameters") or [])
        body: dict = {"model": model["id"], "messages": messages}
        if max_tokens is not None and "max_tokens" in supported:
            body["max_tokens"] = max_tokens
        if temperature is not None and "temperature" in supported:
            body["temperature"] = temperature
        if want_json and {"response_format", "structured_outputs"} & supported:
            body["response_format"] = {"type": "json_object"}
        return body

    @staticmethod
    def _backoff(attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, 60.0)
        return min(2.0 ** attempt + random.uniform(0, 0.5), 30.0)

    def ask(self, model: dict, messages: list[dict], *, want_json: bool = False,
            max_tokens: int | None = 1024, temperature: float | None = 0.7) -> Answer | Failure:
        body = self._payload(model, messages, want_json, max_tokens, temperature)
        data = json.dumps(body).encode()
        last = "unknown error"
        for attempt in range(self.max_retries):
            req = urllib.request.Request(API, data=data, headers=self._headers())
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    payload = json.load(r)
            except urllib.error.HTTPError as e:
                raw = e.read().decode(errors="replace")
                retry_after = None
                try:
                    retry_after = float(e.headers.get("Retry-After")) if e.headers.get("Retry-After") else None
                except (TypeError, ValueError):
                    retry_after = None
                if e.code == 429 and attempt < self.max_retries - 1:
                    self._sleep(self._backoff(attempt, retry_after))
                    last = "rate limited"
                    continue
                if e.code == 402 or "negative" in raw.lower():
                    # A negative balance blocks FREE models too -- an easy one to misread as
                    # the free model having gone away.
                    return Failure(model["id"], "account balance blocks free models "
                                                "(top up above zero)", status=e.code)
                return Failure(model["id"], f"http {e.code}: {raw[:200]}", status=e.code,
                               retry_after=retry_after)
            except Exception as e:
                if attempt < self.max_retries - 1:
                    self._sleep(self._backoff(attempt, None))
                    last = f"{type(e).__name__}: {e}"
                    continue
                return Failure(model["id"], f"{type(e).__name__}: {e}")

            err = payload.get("error")
            if err:
                return Failure(model["id"], str(err)[:200], status=(err or {}).get("code")
                               if isinstance(err, dict) else None)
            choices = payload.get("choices") or []
            if not choices:
                return Failure(model["id"], "no choices in response")
            choice = choices[0]
            # The mid-stream 429: HTTP said 200 because the status went out before the limit
            # was hit. The truth is in finish_reason.
            if choice.get("finish_reason") == "error":
                return Failure(model["id"], "stream ended in error (mid-stream rate limit)",
                               status=429)
            text = ((choice.get("message") or {}).get("content") or "").strip()
            if not text:
                return Failure(model["id"], "empty content")
            return Answer(model["id"], text, raw=payload)
        return Failure(model["id"], last, status=429)
