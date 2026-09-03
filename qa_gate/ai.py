"""The DeepSeek client.

One provider, reached over its OpenAI-compatible `/chat/completions` endpoint
with `httpx`, which the project already depends on. No SDK: the surface we use
is three fields of one JSON body, and a dependency that exists to save writing
those three fields is a dependency that will outlive its usefulness.

## Where this may and may not be used

§14 of the plan requires a verdict be computed from assertion results, and this
module must never be on that path. A model here writes prose *about* a decision
already made, and proposes things a human ratifies. It does not decide.

Concretely, the legitimate callers are:

  - `addon_digest`, which reads a module's source and proposes what it does.
    Every field it produces is a proposal for `qa/knowledge.yml`, which changes
    by pull request.
  - later, §11 task interpretation and the §14 summary paragraph.

If you find yourself importing this from something that returns pass or fail,
that is the bug.

## Reasoning

`deepseek-v4-pro` with `thinking` enabled is the reasoning configuration, and
what `reasoning_effort` buys is real on source-reading work: the difference
between listing a module's imports and noticing that one of them talks to a
payment gateway. The reasoning text comes back on `reasoning_content` and is
kept, because a proposal a human has to ratify is much easier to ratify when you
can see why it was made.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import httpx

from . import app_secrets

log = logging.getLogger(__name__)

API_ROOT = "https://api.deepseek.com"

#: The reasoning model. Used for anything that reads source and forms a view.
MODEL_REASONING = "deepseek-v4-pro"
#: The cheap one, for mechanical work that needs no deliberation. Roughly a
#: third of pro's price per token and correspondingly less careful.
MODEL_FAST = "deepseek-v4-flash"
#: Reads images. Task descriptions carry mockups with arrows marking where a
#: field or button goes — a requirement stated only in a picture.
MODEL_VISION = "deepseek-v4-flash-vision-exp"

#: Generous, because a digest that stops mid-JSON is worthless and the reasoning
#: tokens come out of the same budget. Both models cap far above this.
DEFAULT_MAX_TOKENS = 8000

#: Reading a large addon can take a while at high reasoning effort. Well past
#: what a page load should wait for, which is why callers run it in a thread and
#: cache the result rather than generating on render.
TIMEOUT = httpx.Timeout(300.0, connect=15.0)


class AIError(Exception):
    """Anything that stopped us getting an answer."""


class NotConfigured(AIError):
    """No API key stored. Distinct because the fix is a settings page, not a retry."""


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    #: DeepSeek caches prompt prefixes automatically — there is nothing to opt
    #: into. Surfaced because it is the number that says whether re-reading the
    #: same addon is costing full price or a fortieth of it.
    cache_hit_tokens: int = 0

    @classmethod
    def from_payload(cls, raw: dict | None) -> "Usage":
        raw = raw or {}
        return cls(
            prompt_tokens=int(raw.get("prompt_tokens") or 0),
            output_tokens=int(raw.get("completion_tokens") or 0),
            reasoning_tokens=int(
                (raw.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0),
            cache_hit_tokens=int(raw.get("prompt_cache_hit_tokens") or 0),
        )


@dataclass
class Answer:
    text: str = ""
    #: The model's own reasoning. Kept so a proposal can be judged on its
    #: argument rather than only on its conclusion.
    reasoning: str = ""
    model: str = ""
    usage: Usage = field(default_factory=Usage)
    #: Set when the request had to fall back to a weaker model because the one
    #: asked for was unavailable. Never silent: a caller that recorded this
    #: answer as if it came from the reasoning model would be overstating it.
    degraded: str = ""

    def as_json(self) -> dict:
        """Parse `text` as a JSON object, or raise AIError.

        Worth its own method because `response_format=json_object` guarantees
        syntactically valid JSON and nothing at all about its shape, so every
        caller has to handle "valid JSON, wrong thing" anyway.
        """
        try:
            parsed = json.loads(self.text)
        except json.JSONDecodeError as exc:
            raise AIError(f"{self.model} did not return JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise AIError(f"{self.model} returned {type(parsed).__name__}, not an object.")
        return parsed


class DeepSeek:
    def __init__(self, api_key: str, *, api_root: str = API_ROOT) -> None:
        if not api_key:
            raise NotConfigured(
                "No DeepSeek API key is stored. An administrator can add one "
                "under Settings → AI provider.")
        self._key = api_key
        self._root = api_root.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json"}

    #: Transient on the provider's side, not our request's fault. A review that
    #: takes six model calls would otherwise fail whole runs on a blip that
    #: clears in seconds — observed live: a 503 "Server Overloaded" mid-run.
    RETRY_STATUS = (429, 500, 502, 503, 504)
    MAX_ATTEMPTS = 4

    def _post_with_retry(self, body: dict) -> "httpx.Response":
        import random
        import time

        last: Exception | None = None
        for attempt in range(self.MAX_ATTEMPTS):
            try:
                resp = httpx.post(f"{self._root}/chat/completions",
                                  headers=self._headers(), json=body, timeout=TIMEOUT)
            except httpx.HTTPError as exc:
                last = exc
            else:
                if resp.status_code not in self.RETRY_STATUS:
                    return resp
                last = AIError(f"DeepSeek returned {resp.status_code}")
                if attempt == self.MAX_ATTEMPTS - 1:
                    return resp
            # Exponential with jitter: several runs retrying in lockstep would
            # re-create the overload they are backing off from.
            delay = min(2 ** attempt, 8) + random.random()
            log.info("DeepSeek attempt %s/%s failed (%s); retrying in %.1fs",
                     attempt + 1, self.MAX_ATTEMPTS, last, delay)
            time.sleep(delay)
        raise AIError(f"Could not reach DeepSeek after {self.MAX_ATTEMPTS} attempts: {last}")

    def complete(self, system: str, user: str, *, model: str = MODEL_REASONING,
                 reasoning: bool = True, json_object: bool = False,
                 max_tokens: int = DEFAULT_MAX_TOKENS,
                 reasoning_effort: str = "high") -> Answer:
        body: dict = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "stream": False,
        }
        # Reasoning is ON when `thinking` is omitted, so turning it off takes an
        # explicit disable rather than silence. Verified against the live API:
        # omitting the field still spent reasoning tokens.
        if reasoning:
            body["thinking"] = {"type": "enabled"}
            body["reasoning_effort"] = reasoning_effort
        else:
            body["thinking"] = {"type": "disabled"}
        if json_object:
            body["response_format"] = {"type": "json_object"}

        resp = self._post_with_retry(body)

        # A whole model can be down while its siblings are fine — observed live:
        # `deepseek-v4-pro` answering 503 on every attempt while
        # `deepseek-v4-flash` answered 200 immediately. A six-call review should
        # not be lost to that, so fall back once and say so rather than either
        # failing or pretending the weaker answer came from the better model.
        degraded = ""
        if resp.status_code in self.RETRY_STATUS and model != MODEL_FAST:
            log.warning("%s unavailable (%s); falling back to %s",
                        model, resp.status_code, MODEL_FAST)
            body["model"] = MODEL_FAST
            fallback = self._post_with_retry(body)
            if fallback.status_code == 200:
                degraded = (f"{model} was unavailable, so this ran on {MODEL_FAST}, "
                            "which does not reason as deeply.")
                resp = fallback

        if resp.status_code == 401:
            raise AIError("DeepSeek rejected the stored API key (401). It may have "
                          "been revoked or rotated — replace it under Settings.")
        if resp.status_code == 402:
            raise AIError("The DeepSeek account has insufficient balance (402). "
                          "Top it up; nothing is wrong with the key itself.")
        if resp.status_code == 429:
            raise AIError("DeepSeek is rate limiting this key (429). Try again shortly.")
        if resp.status_code != 200:
            raise AIError(f"DeepSeek returned {resp.status_code}: {resp.text[:300]}")

        try:
            payload = resp.json()
            choice = payload["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError) as exc:
            raise AIError(f"Unreadable response from DeepSeek: {exc}") from exc

        # Running out of output budget returns HTTP 200 with an empty `content`
        # and all the tokens spent on reasoning — so the failure looks exactly
        # like a model that answered with nothing. Caught here because further
        # up it becomes "the model returned JSON with no summary in it", which
        # sends whoever reads it looking at the prompt instead of at max_tokens.
        if choice.get("finish_reason") == "length":
            spent = Usage.from_payload(payload.get("usage"))
            raise AIError(
                f"DeepSeek hit the {max_tokens}-token output limit before finishing"
                f" ({spent.reasoning_tokens} of them spent on reasoning). Raise "
                "max_tokens, or lower reasoning_effort.")

        return Answer(
            text=(message.get("content") or "").strip(),
            reasoning=(message.get("reasoning_content") or "").strip(),
            model=payload.get("model") or model,
            usage=Usage.from_payload(payload.get("usage")),
            degraded=degraded,
        )


    def vision(self, system: str, image_data_url: str, question: str, *,
               max_tokens: int = 800) -> Answer:
        """Read one image.

        A separate method because it is a separate model — the reasoning model
        does not take images, and the vision model does not reason. Used for the
        mockups colleagues attach to tasks: a screenshot with an arrow saying
        "put the field here" states a requirement that appears nowhere in the
        prose, and a reviewer working from the text alone would never check it.

        Verified against a real mockup: it reported the field label, its
        position relative to the other fields, and the red box marking it.
        """
        body = {
            "model": MODEL_VISION,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": image_data_url}},
                    {"type": "text", "text": question},
                ]},
            ],
            "max_tokens": max_tokens,
            "stream": False,
        }
        try:
            resp = httpx.post(f"{self._root}/chat/completions",
                              headers=self._headers(), json=body, timeout=TIMEOUT)
        except httpx.HTTPError as exc:
            raise AIError(f"Could not reach DeepSeek: {exc}") from exc
        if resp.status_code != 200:
            raise AIError(f"DeepSeek returned {resp.status_code}: {resp.text[:300]}")
        try:
            payload = resp.json()
            message = payload["choices"][0]["message"]
        except (ValueError, KeyError, IndexError) as exc:
            raise AIError(f"Unreadable response from DeepSeek: {exc}") from exc
        return Answer(
            text=(message.get("content") or "").strip(),
            reasoning=(message.get("reasoning_content") or "").strip(),
            model=payload.get("model") or MODEL_VISION,
            usage=Usage.from_payload(payload.get("usage")),
        )


def verify(api_key: str, *, api_root: str = API_ROOT) -> list[str]:
    """Prove a key works, returning the model ids it can see.

    Called before storing, for the same reason the Odoo and GitHub credentials
    are: a key that only fails the first time somebody generates a digest is the
    worst moment to discover it was pasted with a trailing space.
    """
    try:
        resp = httpx.get(f"{api_root.rstrip('/')}/models", timeout=30.0,
                         headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError as exc:
        raise AIError(f"Could not reach DeepSeek: {exc}") from exc
    if resp.status_code == 401:
        raise AIError("DeepSeek rejected that key. Check it was copied whole and "
                      "has not been revoked.")
    if resp.status_code != 200:
        raise AIError(f"DeepSeek returned {resp.status_code}: {resp.text[:200]}")
    try:
        return [m["id"] for m in (resp.json().get("data") or []) if m.get("id")]
    except (ValueError, TypeError) as exc:
        raise AIError(f"Unreadable model list from DeepSeek: {exc}") from exc


def client(secret_key: str, *, api_root: str = API_ROOT) -> DeepSeek:
    """The stored key, decrypted. Raises NotConfigured when there is none."""
    stored = app_secrets.get(app_secrets.DEEPSEEK_KEY, secret_key)
    return DeepSeek(stored.secret, api_root=api_root)


def is_configured() -> bool:
    return app_secrets.is_configured(app_secrets.DEEPSEEK_KEY)
