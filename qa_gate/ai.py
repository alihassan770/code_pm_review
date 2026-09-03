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

# ---- providers --------------------------------------------------------------
#
# Three providers, two wire formats. DeepSeek and OpenAI both speak the
# OpenAI-compatible `/chat/completions` shape, so they share a client and differ
# only in a table of constants. Anthropic's `/v1/messages` is a genuinely
# different request and response, so it gets its own client rather than a pile
# of branches inside the first one.
#
# The registry is data, not classes, because everything that varies between
# DeepSeek and OpenAI IS data. A provider that needed different behaviour would
# get a client, which is exactly what Anthropic has.


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    api_root: str
    #: "openai" or "anthropic". Which client class speaks to it.
    dialect: str
    reasoning: str
    fast: str
    vision: str
    #: What a key from this provider looks like, shown next to the input so a
    #: key pasted from the wrong console is obvious before it is submitted.
    key_prefix: str
    console: str

    @property
    def models(self) -> list[str]:
        return sorted({self.reasoning, self.fast, self.vision})


PROVIDERS: dict[str, Provider] = {
    "deepseek": Provider(
        key="deepseek", label="DeepSeek", api_root="https://api.deepseek.com",
        dialect="openai",
        reasoning="deepseek-v4-pro", fast="deepseek-v4-flash",
        vision="deepseek-v4-flash-vision-exp",
        key_prefix="sk-", console="https://platform.deepseek.com/api_keys"),
    "anthropic": Provider(
        key="anthropic", label="Anthropic (Claude)",
        api_root="https://api.anthropic.com", dialect="anthropic",
        reasoning="claude-opus-5", fast="claude-sonnet-5",
        # Claude reads images on the same models, so vision is not a separate
        # one. Pointed at the cheaper model because describing a screenshot is
        # not work that needs the expensive one.
        vision="claude-sonnet-5",
        key_prefix="sk-ant-", console="https://console.anthropic.com/settings/keys"),
    "openai": Provider(
        key="openai", label="OpenAI (ChatGPT)", api_root="https://api.openai.com/v1",
        dialect="openai",
        reasoning="gpt-5", fast="gpt-5-mini", vision="gpt-5-mini",
        key_prefix="sk-", console="https://platform.openai.com/api-keys"),
}

DEFAULT_PROVIDER = "deepseek"


def secret_key_name(provider: str) -> str:
    """Where this provider's API key is stored.

    One row per provider rather than one shared row, so switching provider to
    compare them does not destroy the key you were using. Switching back needs
    no retyping.
    """
    return f"ai_key_{provider}"


def selected() -> Provider:
    """The provider an administrator chose, falling back to the default."""
    from . import app_settings
    name = app_settings.get(app_settings.AI_PROVIDER, DEFAULT_PROVIDER)
    return PROVIDERS.get(name) or PROVIDERS[DEFAULT_PROVIDER]

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


class OpenAICompatible:
    """DeepSeek and OpenAI, which speak the same `/chat/completions` shape.

    One class for two providers because everything that differs between them is
    a constant: the host, the model ids, the key. Anthropic is not here, because
    what differs there is the request and the response.
    """

    def __init__(self, api_key: str, provider: Provider) -> None:
        if not api_key:
            raise NotConfigured(
                f"No {provider.label} API key is stored. An administrator can "
                "add one under Settings, AI provider.")
        self._key = api_key
        self.provider = provider
        self._root = provider.api_root.rstrip("/")

    @property
    def label(self) -> str:
        return self.provider.label

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json"}

    #: Transient on the provider's side, not our request's fault. A review that
    #: takes six model calls would otherwise fail whole runs on a blip that
    #: clears in seconds, observed live: a 503 "Server Overloaded" mid-run.
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
                last = AIError(f"{self.label} returned {resp.status_code}")
                if attempt == self.MAX_ATTEMPTS - 1:
                    return resp
            # Exponential with jitter: several runs retrying in lockstep would
            # re-create the overload they are backing off from.
            delay = min(2 ** attempt, 8) + random.random()
            log.info("%s attempt %s/%s failed (%s); retrying in %.1fs",
                     self.label, attempt + 1, self.MAX_ATTEMPTS, last, delay)
            time.sleep(delay)
        raise AIError(
            f"Could not reach {self.label} after {self.MAX_ATTEMPTS} attempts: {last}")

    def complete(self, system: str, user: str, *, model: str = "",
                 reasoning: bool = True, json_object: bool = False,
                 max_tokens: int = DEFAULT_MAX_TOKENS,
                 reasoning_effort: str = "high") -> Answer:
        model = model or self.provider.reasoning
        body: dict = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "stream": False,
        }
        # DeepSeek reasons when `thinking` is omitted, so turning it off takes
        # an explicit disable rather than silence. Verified against the live
        # API: omitting the field still spent reasoning tokens. OpenAI has no
        # such field and rejects unknown keys, so it is sent only to DeepSeek.
        if self.provider.key == "deepseek":
            if reasoning:
                body["thinking"] = {"type": "enabled"}
                body["reasoning_effort"] = reasoning_effort
            else:
                body["thinking"] = {"type": "disabled"}
        elif reasoning:
            body["reasoning_effort"] = reasoning_effort
        if json_object:
            body["response_format"] = {"type": "json_object"}

        resp = self._post_with_retry(body)

        # A whole model can be down while its siblings are fine, observed live:
        # `deepseek-v4-pro` answering 503 on every attempt while
        # `deepseek-v4-flash` answered 200 immediately. A six-call review should
        # not be lost to that, so fall back once and say so rather than either
        # failing or pretending the weaker answer came from the better model.
        degraded = ""
        fast = self.provider.fast
        if resp.status_code in self.RETRY_STATUS and model != fast:
            log.warning("%s unavailable (%s); falling back to %s",
                        model, resp.status_code, fast)
            body["model"] = fast
            fallback = self._post_with_retry(body)
            if fallback.status_code == 200:
                degraded = (f"{model} was unavailable, so this ran on {fast}, "
                            "which does not reason as deeply.")
                resp = fallback

        self._raise_for_status(resp)

        try:
            payload = resp.json()
            choice = payload["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError) as exc:
            raise AIError(f"Unreadable response from {self.label}: {exc}") from exc

        # Running out of output budget returns HTTP 200 with an empty `content`
        # and all the tokens spent on reasoning, so the failure looks exactly
        # like a model that answered with nothing. Caught here because further
        # up it becomes "the model returned JSON with no summary in it", which
        # sends whoever reads it looking at the prompt instead of at max_tokens.
        if choice.get("finish_reason") == "length":
            spent = Usage.from_payload(payload.get("usage"))
            raise AIError(
                f"{self.label} hit the {max_tokens}-token output limit before "
                f"finishing ({spent.reasoning_tokens} of them spent on "
                "reasoning). Raise max_tokens, or lower reasoning_effort.")

        return Answer(
            text=(message.get("content") or "").strip(),
            reasoning=(message.get("reasoning_content") or "").strip(),
            model=payload.get("model") or model,
            usage=Usage.from_payload(payload.get("usage")),
            degraded=degraded,
        )

    def _raise_for_status(self, resp) -> None:
        if resp.status_code == 401:
            raise AIError(f"{self.label} rejected the stored API key (401). It may "
                          "have been revoked or rotated, replace it under Settings.")
        if resp.status_code == 402:
            raise AIError(f"The {self.label} account has insufficient balance (402). "
                          "Top it up; nothing is wrong with the key itself.")
        if resp.status_code == 429:
            raise AIError(f"{self.label} is rate limiting this key (429). "
                          "Try again shortly.")
        if resp.status_code != 200:
            raise AIError(f"{self.label} returned {resp.status_code}: {resp.text[:300]}")

    def vision(self, system: str, image_data_url: str, question: str, *,
               max_tokens: int = 800) -> Answer:
        """Read one image.

        Used for the mockups colleagues attach to tasks: a screenshot with an
        arrow saying "put the field here" states a requirement that appears
        nowhere in the prose, and a reviewer working from the text alone would
        never check it.

        Verified against a real mockup on DeepSeek: it reported the field label,
        its position relative to the other fields, and the red box marking it.
        """
        body = {
            "model": self.provider.vision,
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
            raise AIError(f"Could not reach {self.label}: {exc}") from exc
        self._raise_for_status(resp)
        try:
            payload = resp.json()
            message = payload["choices"][0]["message"]
        except (ValueError, KeyError, IndexError) as exc:
            raise AIError(f"Unreadable response from {self.label}: {exc}") from exc
        return Answer(
            text=(message.get("content") or "").strip(),
            reasoning=(message.get("reasoning_content") or "").strip(),
            model=payload.get("model") or self.provider.vision,
            usage=Usage.from_payload(payload.get("usage")),
        )


#: The old name. Kept so `addon_digest` and `review` type hints, and anything
#: else written against one provider, still resolve.
DeepSeek = OpenAICompatible


class Anthropic:
    """Claude, over `/v1/messages`.

    Its own class rather than branches inside `OpenAICompatible`, because
    nothing about the call is the same: the system prompt is a top-level field
    instead of a message, the reply is a list of typed content blocks instead of
    one string, thinking is a request field rather than a separate model, and
    authentication is `x-api-key` rather than a bearer token. Four differences
    in one method is a second class.
    """

    #: Required on every request. Not optional and not defaulted by the server.
    VERSION = "2023-06-01"

    RETRY_STATUS = (429, 500, 502, 503, 504, 529)
    MAX_ATTEMPTS = 4

    def __init__(self, api_key: str, provider: Provider) -> None:
        if not api_key:
            raise NotConfigured(
                "No Anthropic API key is stored. An administrator can add one "
                "under Settings, AI provider.")
        self._key = api_key
        self.provider = provider
        self._root = provider.api_root.rstrip("/")

    @property
    def label(self) -> str:
        return self.provider.label

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._key, "anthropic-version": self.VERSION,
                "Content-Type": "application/json"}

    def _post_with_retry(self, body: dict) -> "httpx.Response":
        import random
        import time

        last: Exception | None = None
        for attempt in range(self.MAX_ATTEMPTS):
            try:
                resp = httpx.post(f"{self._root}/v1/messages",
                                  headers=self._headers(), json=body, timeout=TIMEOUT)
            except httpx.HTTPError as exc:
                last = exc
            else:
                if resp.status_code not in self.RETRY_STATUS:
                    return resp
                last = AIError(f"Anthropic returned {resp.status_code}")
                if attempt == self.MAX_ATTEMPTS - 1:
                    return resp
            delay = min(2 ** attempt, 8) + random.random()
            log.info("Anthropic attempt %s/%s failed (%s); retrying in %.1fs",
                     attempt + 1, self.MAX_ATTEMPTS, last, delay)
            time.sleep(delay)
        raise AIError(
            f"Could not reach Anthropic after {self.MAX_ATTEMPTS} attempts: {last}")

    def _raise_for_status(self, resp) -> None:
        if resp.status_code == 401:
            raise AIError("Anthropic rejected the stored API key (401). It may have "
                          "been revoked or rotated, replace it under Settings.")
        if resp.status_code == 400:
            raise AIError(f"Anthropic refused the request (400): {resp.text[:300]}")
        if resp.status_code == 429:
            raise AIError("Anthropic is rate limiting this key (429). Try again shortly.")
        if resp.status_code != 200:
            raise AIError(f"Anthropic returned {resp.status_code}: {resp.text[:300]}")

    @staticmethod
    def _read(payload: dict, fallback_model: str) -> Answer:
        """Flatten Claude's content blocks into the one Answer shape.

        The reply is a list of blocks, and a thinking block sits alongside the
        text one rather than in a separate field. Joining them all into `text`
        would put the reasoning into the summary, so they are split by type the
        way the rest of the app expects.
        """
        text, thinking = [], []
        for block in payload.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text.append(block.get("text") or "")
            elif block.get("type") == "thinking":
                thinking.append(block.get("thinking") or "")
        usage = payload.get("usage") or {}
        return Answer(
            text="".join(text).strip(),
            reasoning="".join(thinking).strip(),
            model=payload.get("model") or fallback_model,
            usage=Usage(
                prompt_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                cache_hit_tokens=int(usage.get("cache_read_input_tokens") or 0),
            ),
        )

    def complete(self, system: str, user: str, *, model: str = "",
                 reasoning: bool = True, json_object: bool = False,
                 max_tokens: int = DEFAULT_MAX_TOKENS,
                 reasoning_effort: str = "high") -> Answer:
        model = model or self.provider.reasoning
        body: dict = {
            "model": model,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "max_tokens": max_tokens,
        }
        if reasoning:
            # `adaptive` rather than a token budget: the budget form is
            # rejected outright by the current models, and adaptive lets the
            # model spend what the question needs instead of what we guessed.
            body["thinking"] = {"type": "adaptive"}
        if json_object:
            # There is no `response_format` here. The prompt already demands a
            # JSON object, and `as_json` fails loudly if one does not arrive,
            # which is better than inventing a parameter the API would reject.
            body["system"] = (system + "\n\nReply with a single JSON object and "
                              "nothing else. No prose, no code fence.")

        resp = self._post_with_retry(body)

        degraded = ""
        fast = self.provider.fast
        if resp.status_code in self.RETRY_STATUS and model != fast:
            log.warning("%s unavailable (%s); falling back to %s",
                        model, resp.status_code, fast)
            body["model"] = fast
            fallback = self._post_with_retry(body)
            if fallback.status_code == 200:
                degraded = (f"{model} was unavailable, so this ran on {fast}, "
                            "which does not reason as deeply.")
                resp = fallback

        self._raise_for_status(resp)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise AIError(f"Unreadable response from Anthropic: {exc}") from exc

        # The same trap as the OpenAI shape, spelled differently: hitting the
        # cap returns 200 with a truncated body, and the only signal is
        # stop_reason.
        if payload.get("stop_reason") == "max_tokens":
            raise AIError(
                f"Anthropic hit the {max_tokens}-token output limit before "
                "finishing. Raise max_tokens.")

        answer = self._read(payload, model)
        answer.degraded = degraded
        return answer

    def vision(self, system: str, image_data_url: str, question: str, *,
               max_tokens: int = 800) -> Answer:
        """Read one image.

        Claude takes images as a base64 block with an explicit media type, not
        as a data URL, so the URL the rest of the app passes around is split
        back into its two halves here rather than at every call site.
        """
        try:
            header, b64 = image_data_url.split(",", 1)
            media_type = header.split(";")[0].removeprefix("data:") or "image/png"
        except ValueError as exc:
            raise AIError("Anthropic needs a base64 data URL for an image.") from exc

        body = {
            "model": self.provider.vision,
            "system": system,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64",
                                             "media_type": media_type, "data": b64}},
                {"type": "text", "text": question},
            ]}],
        }
        try:
            resp = httpx.post(f"{self._root}/v1/messages", headers=self._headers(),
                              json=body, timeout=TIMEOUT)
        except httpx.HTTPError as exc:
            raise AIError(f"Could not reach Anthropic: {exc}") from exc
        self._raise_for_status(resp)
        try:
            return self._read(resp.json(), self.provider.vision)
        except ValueError as exc:
            raise AIError(f"Unreadable response from Anthropic: {exc}") from exc


def verify(api_key: str, provider: Provider | None = None) -> list[str]:
    """Prove a key works, returning the model ids it can see.

    Called before storing, for the same reason the Odoo and GitHub credentials
    are: a key that only fails the first time somebody generates a digest is the
    worst moment to discover it was pasted with a trailing space.
    """
    provider = provider or selected()
    root = provider.api_root.rstrip("/")
    if provider.dialect == "anthropic":
        url, headers = f"{root}/v1/models", {"x-api-key": api_key,
                                             "anthropic-version": Anthropic.VERSION}
    else:
        url, headers = f"{root}/models", {"Authorization": f"Bearer {api_key}"}
    try:
        resp = httpx.get(url, timeout=30.0, headers=headers)
    except httpx.HTTPError as exc:
        raise AIError(f"Could not reach {provider.label}: {exc}") from exc
    if resp.status_code in (401, 403):
        raise AIError(f"{provider.label} rejected that key. Check it was copied "
                      "whole and has not been revoked.")
    if resp.status_code != 200:
        raise AIError(f"{provider.label} returned {resp.status_code}: {resp.text[:200]}")
    try:
        return [m["id"] for m in (resp.json().get("data") or []) if m.get("id")]
    except (ValueError, TypeError) as exc:
        raise AIError(f"Unreadable model list from {provider.label}: {exc}") from exc


def client(secret_key: str, *, provider: Provider | None = None):
    """The stored key for the selected provider, decrypted.

    Raises NotConfigured when there is none, which is a different problem from a
    key that does not work and has a different fix: a settings page, not a retry.
    """
    provider = provider or selected()
    stored = app_secrets.get(secret_key_name(provider.key), secret_key)
    key = stored.secret
    if not key and provider.key == DEFAULT_PROVIDER:
        # Installs that predate per-provider storage kept the key under the old
        # single-provider name. The migration copies it across, but reading the
        # old row too means an install that skipped the migration still works.
        key = app_secrets.get(app_secrets.DEEPSEEK_KEY, secret_key).secret
    if provider.dialect == "anthropic":
        return Anthropic(key, provider)
    return OpenAICompatible(key, provider)


def is_configured(provider: Provider | None = None) -> bool:
    provider = provider or selected()
    if app_secrets.is_configured(secret_key_name(provider.key)):
        return True
    return (provider.key == DEFAULT_PROVIDER
            and app_secrets.is_configured(app_secrets.DEEPSEEK_KEY))
