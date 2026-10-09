"""Frames to answers: one request carries several images and gets a
structured answer back, on Anthropic or OpenAI, many requests at once.

- Structured output: a JSON schema on both providers (Anthropic's
  ``output_config.format``, OpenAI's strict ``json_schema``). A model that
  refuses the schema format on Anthropic falls back once to a forced tool;
  the newest ones refuse the forced tool instead.
- Prompt caching: the system prompt and the meeting context come first and
  are identical across a run's requests (Anthropic needs the cache markers;
  OpenAI caches long prefixes by itself), then the images.
- ``Gate`` adapts how many requests are in flight: it grows after clean
  answers and halves on a rate limit, honouring retry-after, so a run goes
  as fast as the account allows without hammering it.

Clients are the app's synchronous SDK clients, used from worker threads (the
codebase has no asyncio). Everything here is provider plumbing; what to ask
lives in prompts.py.
"""
from __future__ import annotations

import base64
import json
import random
import threading
import time
from dataclasses import dataclass, field

from core import log

from ai.speaker_detect import prompts


@dataclass
class Usage:
    requests: int = 0
    images: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    retries: int = 0
    seconds: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, *, images: int, inp: int, out: int, cached: int, secs: float) -> None:
        with self.lock:
            self.requests += 1
            self.images += images
            self.input_tokens += inp
            self.output_tokens += out
            self.cache_read_tokens += cached
            self.seconds += secs

    def as_dict(self) -> dict:
        return {"requests": self.requests, "images": self.images,
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cache_read_tokens": self.cache_read_tokens, "retries": self.retries}


class Gate:
    """A semaphore whose size follows the provider: +1 after ``grow_after``
    clean answers (up to ``high``), halved on a rate limit (down to ``low``)."""

    def __init__(self, start: int = 6, low: int = 2, high: int = 16, grow_after: int = 5):
        # Never below one in flight: a zero or negative setting would wait forever.
        self.low = max(1, low)
        self.high = max(self.low, high)
        self.limit = max(1, min(self.high, start))
        self.grow_after = grow_after
        self.active = 0
        self.streak = 0
        self.cond = threading.Condition()

    def __enter__(self):
        with self.cond:
            while self.active >= self.limit:
                self.cond.wait()
            self.active += 1
        return self

    def __exit__(self, *exc):
        with self.cond:
            self.active -= 1
            self.cond.notify_all()

    def ok(self) -> None:
        with self.cond:
            self.streak += 1
            if self.streak >= self.grow_after and self.limit < self.high:
                self.limit += 1
                self.streak = 0
                self.cond.notify_all()

    def throttled(self) -> None:
        with self.cond:
            # Halve, to the floor; a limit already below the floor stays put.
            self.limit = max(1, min(self.limit, max(self.low, self.limit // 2)))
            self.streak = 0


class VisionError(RuntimeError):
    pass


def _status(e: Exception) -> int | None:
    for attr in ("status_code", "status"):
        v = getattr(e, attr, None)
        if isinstance(v, int):
            return v
    resp = getattr(e, "response", None)
    return getattr(resp, "status_code", None)


def _thinking_off(model: str) -> dict | None:
    """The thinking setting that keeps a frame read fast. Haiku 5.5 takes
    "disabled" (at effort high or below); Sonnet 5.5 turns thinking off with
    "between_tools" ("disabled" is a 400 there); Opus 5.x and Fable always
    think, so they are steered by effort alone; older models think only when
    asked."""
    m = model.lower()
    if "haiku-5" in m:
        return {"type": "disabled"}
    if "sonnet-5-5" in m:
        return {"type": "between_tools"}
    return None


def _takes_effort(model: str) -> bool:
    m = model.lower()
    return any(s in m for s in ("opus-4-6", "opus-4-7", "opus-4-8", "opus-5", "sonnet-4-6",
                                "sonnet-5", "haiku-5", "fable", "mythos"))


def _retry_after(e: Exception) -> float | None:
    resp = getattr(e, "response", None)
    headers = getattr(resp, "headers", None) or {}
    try:
        v = headers.get("retry-after")
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


class VisionClient:
    """``analyze(images, labels, task, context)`` -> parsed answer dict.

    ``client`` is an Anthropic or OpenAI SDK client (``ai.assistant``'s,
    so keys, truststore and WARP handling are the app's own)."""

    def __init__(self, provider: str, client, *, gate: Gate | None = None,
                 timeout: float = 60.0, attempts: int = 4):
        self.provider = provider
        self.client = client
        self.gate = gate or Gate()
        self.timeout = timeout
        self.attempts = attempts
        self.usage = Usage()
        self._modes: dict[str, str] = {}      # model -> "json" | "tool"

    # ── one request ─────────────────────────────────────────────────────────

    def _anthropic(self, model: str, images: list[bytes], labels: list[str], task: str,
                   context: str, max_tokens: int):
        content: list[dict] = [{"type": "text", "text": context,
                                "cache_control": {"type": "ephemeral"}}]
        for label, jpeg in zip(labels, images):
            content.append({"type": "text", "text": label})
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg",
                "data": base64.b64encode(jpeg).decode()}})
        content.append({"type": "text", "text": task})
        kwargs = dict(model=model, max_tokens=max_tokens,
                      system=[{"type": "text", "text": prompts.SYSTEM,
                               "cache_control": {"type": "ephemeral"}}],
                      messages=[{"role": "user", "content": content}],
                      timeout=self.timeout)
        thinking = _thinking_off(model)
        if thinking:
            kwargs["thinking"] = thinking
        effort = {"effort": "low"} if _takes_effort(model) else {}
        mode = self._modes.get(model, "json")
        if mode == "json":
            # Structured outputs: the answer is guaranteed JSON for the schema.
            # The newest models (Sonnet 5.5, Opus 5.5, Fable 5.1) refuse the
            # forced tool choice that older ones need.
            kwargs["output_config"] = {"format": {"type": "json_schema",
                                                  "schema": prompts.SCHEMA}, **effort}
        else:
            kwargs["tools"] = [prompts.TOOL]
            kwargs["tool_choice"] = {"type": "tool", "name": prompts.TOOL_NAME}
            if effort:
                kwargs["output_config"] = effort
        try:
            resp = self.client.messages.create(**kwargs)
        except Exception as e:  # noqa: BLE001 - switch request style once per model
            msg = str(e).lower()
            if _status(e) == 400 and model not in self._modes and (
                    ("output_config" in msg or "format" in msg) if mode == "json"
                    else "tool_choice" in msg):
                self._modes[model] = "tool" if mode == "json" else "json"
                return self._anthropic(model, images, labels, task, context, max_tokens)
            raise
        self._modes.setdefault(model, mode)
        u = resp.usage
        usage = (getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0,
                 getattr(u, "cache_read_input_tokens", 0) or 0)
        if getattr(resp, "stop_reason", None) == "refusal":
            log.warn("speakers", f"{model} declined a frame batch "
                                 f"({getattr(resp, 'stop_details', None)})")
            return {}, usage
        if getattr(resp, "stop_reason", None) == "max_tokens":
            raise ValueError(f"{model} ran out of room answering {len(images)} images")
        if mode == "json":
            text = next((b.text for b in resp.content if b.type == "text"), "")
            return (json.loads(text) if text else {}), usage
        return next((b.input for b in resp.content if b.type == "tool_use"), None) or {}, usage

    def _openai(self, model: str, images: list[bytes], labels: list[str], task: str,
                context: str, max_tokens: int):
        content: list[dict] = [{"type": "input_text", "text": context}]
        for label, jpeg in zip(labels, images):
            content.append({"type": "input_text", "text": label})
            content.append({"type": "input_image", "detail": "high",
                            "image_url": "data:image/jpeg;base64,"
                                         + base64.b64encode(jpeg).decode()})
        content.append({"type": "input_text", "text": task})
        resp = self.client.responses.create(
            model=model, instructions=prompts.SYSTEM,
            input=[{"role": "user", "content": content}],
            # Room for a reasoning model's own tokens, which count here too.
            max_output_tokens=max_tokens + 2048,
            text={"format": {"type": "json_schema", "name": prompts.TOOL_NAME,
                             "strict": True, "schema": prompts.SCHEMA}},
            timeout=self.timeout,
        )
        text = (resp.output_text or "").strip()
        answer = json.loads(text) if text else {}
        u = getattr(resp, "usage", None)
        details = getattr(u, "input_tokens_details", None)
        return answer, (getattr(u, "input_tokens", 0) or 0,
                        getattr(u, "output_tokens", 0) or 0,
                        getattr(details, "cached_tokens", 0) or 0)

    def analyze(self, model: str, images: list[bytes], labels: list[str], task: str,
                context: str, *, max_tokens: int | None = None) -> dict:
        """Ask about ``images`` (JPEG bytes; ``labels`` name each one, e.g.
        "Image 0 (14:32)"). Retries rate limits and server errors with
        jittered backoff. Raises VisionError when every attempt failed."""
        if self.client is None:
            raise VisionError(f"No {self.provider} API key is configured.")
        # Generous: a scout's roster with boxes runs long, a cut-off answer is
        # unparseable, and unused allowance costs nothing.
        max_tokens = max_tokens or (600 + 700 * len(images))
        call = self._openai if self.provider == "openai" else self._anthropic
        delay = 1.0
        last: Exception | None = None
        for attempt in range(self.attempts):
            t0 = time.perf_counter()
            try:
                with self.gate:
                    answer, (inp, out, cached) = call(model, images, labels, task, context,
                                                      max_tokens)
                self.gate.ok()
                self.usage.add(images=len(images), inp=inp, out=out, cached=cached,
                               secs=time.perf_counter() - t0)
                return answer if isinstance(answer, dict) else {}
            except Exception as e:  # noqa: BLE001 - classified below
                last = e
                status = _status(e)
                if status == 429:
                    self.gate.throttled()
                # An unparseable answer will not parse better the second time.
                retryable = (status in (408, 409, 429, 500, 502, 503, 504, 529)
                             or (status is None and not isinstance(e, ValueError)))
                if not retryable or attempt == self.attempts - 1:
                    break
                with self.usage.lock:
                    self.usage.retries += 1
                wait = _retry_after(e) or delay
                time.sleep(wait + random.uniform(0, 0.25 * wait))
                delay = min(delay * 2, 20.0)
        log.warn("speakers", f"Vision request failed ({self.provider} {model}): {last}")
        raise VisionError(str(last) if last else "vision request failed")
