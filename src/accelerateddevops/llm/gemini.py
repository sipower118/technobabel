"""Gemini generation with a cascading model + key ladder.

Quota behaviour on the Gemini API is per (project, model) pair. Rather than
picking one model and hoping, this client holds an ordered ladder:

    for model in models:            # pro -> flash -> flash-lite
        for key in keys:            # every API key you own
            try: generate
            on 429: cooldown that (model) in the DB, try the next model

So a run degrades gracefully - it starts on the strongest model and lands on
whatever still has quota, instead of failing. Cooldowns are persisted so a run
at 09:15 does not waste calls rediscovering that pro is exhausted until 10:00.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from google import genai
from google.genai import types

from ..models import Slide
from ..store import Store
from .base import GeneratedPost, LLMUnavailable
from .prompts import build_prompt

log = logging.getLogger(__name__)


def _silence_afc_notice() -> None:
    """Drop the SDK's automatic-function-calling (AFC) noise.

    google-genai enables AFC by default and logs an "AFC is enabled with max
    remote calls: 10." info line on every generate_content() call, plus a
    once-per-process warning recommending Chat.send_message instead. This app
    never declares functions or tools, so AFC never actually loops - the SDK
    breaks out after a single call. Both messages are pure noise that looks
    like a failure on every generation, so it is filtered at the logger level.
    """

    class _AfcFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return "AFC" not in record.getMessage()

    for name in ("google_genai.models", "google.genai.models"):
        logger_obj = logging.getLogger(name)
        for f in list(logger_obj.filters):
            if isinstance(f, _AfcFilter):  # keep idempotent on re-import
                logger_obj.removeFilter(f)
        logger_obj.addFilter(_AfcFilter())


_silence_afc_notice()

# The key itself is bad. Only this key is skipped: another key in the list may
# be perfectly fine, and the model must not be benched for someone else's typo.
INVALID_KEY_MARKERS = (
    "api key not valid",
    "api_key_invalid",
    "invalid api key",
    "api key expired",
    "unauthenticated",
    "api keys are not supported",
)
# The key is fine but this (key, model) pair is not allowed - usually the model
# is not enabled in the key's project. Per-model, so it benches the model.
ACCESS_MARKERS = ("permission denied", "forbidden", "403")

# Statuses that mean "this specific model/key is throttled, move on".
QUOTA_MARKERS = (
    "429",
    "resource_exhausted",
    "quota",
    "rate limit",
    "ratelimit",
    "too many requests",
)
# Statuses that mean "the whole service is having a bad time, back off".
TRANSIENT_MARKERS = (
    "503", "unavailable", "overloaded", "500", "internal", "deadline",
    # httpx raises bare timeouts with no status code; classify them as
    # transient so the ladder moves on instead of benching the model for 30min.
    "timed out", "timeout",
)

QUOTA_COOLDOWN_MINUTES = 30
TRANSIENT_COOLDOWN_MINUTES = 3

# The SDK defaults to NO HTTP timeout (it passes timeout=None into httpx), so a
# stalled connection blocks forever and the ladder appears to hang. Observed
# successful calls take up to ~90s (full-article prompt, 4096-token JSON), so
# bound at 2x that: slow-but-fine calls still pass, true stalls fail over.
GENAI_TIMEOUT_MS = 180_000

# Which side of the ladder a failure belongs to: skip the key, or bench the model.
SCOPE_KEY = "key"
SCOPE_MODEL = "model"


@dataclass
class _Attempt:
    """Result of one (model, key) generation attempt."""

    text: str = ""
    should_advance: bool = False
    reason: str = ""
    scope: str = SCOPE_MODEL


class GeminiProvider:
    """Turns a source item into a GeneratedPost, rotating through the ladder."""

    def __init__(
        self,
        api_keys: list[str],
        models: list[str],
        store: Store | None = None,
        temperature: float = 0.9,
    ) -> None:
        if not api_keys:
            raise ValueError("GeminiProvider requires at least one API key")
        if not models:
            raise ValueError("GeminiProvider requires at least one model")
        self.models = models
        self.api_keys = api_keys
        self.store = store
        self.temperature = temperature
        self._clients: dict[str, genai.Client] = {}

    # ── ladder ───────────────────────────────────────────────────────────

    def _client(self, api_key: str) -> genai.Client:
        if api_key not in self._clients:
            self._clients[api_key] = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(timeout=GENAI_TIMEOUT_MS),
            )
        return self._clients[api_key]

    def available_models(self) -> list[str]:
        """Models not currently serving a quota/transient cooldown."""
        if self.store is None:
            return list(self.models)
        cooled = self.store.cooled_down_models()
        live = [m for m in self.models if m not in cooled]
        if not live:
            log.warning(
                "every model is on cooldown (%s) - trying the ladder anyway",
                ", ".join(sorted(cooled)),
            )
            return list(self.models)
        return live

    def _classify(self, exc: Exception) -> _Attempt:
        """Sort a failure into: bad key, access, quota, transient, or unknown.

        The distinction matters because the two scopes are handled differently.
        A rejected API key is a per-key problem - rotate to the next key and
        leave the model alone. Quota is per (project, model) - bench the model
        and drop down the ladder without wasting calls on its other keys.
        """
        text = f"{exc}".lower()
        status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        blob = f"{status} {text}"

        if any(marker in blob for marker in INVALID_KEY_MARKERS):
            return _Attempt(
                should_advance=True,
                scope=SCOPE_KEY,
                reason=f"invalid key: {exc}",
            )
        if any(marker in blob for marker in ACCESS_MARKERS):
            return _Attempt(
                should_advance=True, scope=SCOPE_MODEL, reason=f"access: {exc}"
            )
        if any(marker in blob for marker in QUOTA_MARKERS):
            return _Attempt(should_advance=True, scope=SCOPE_MODEL, reason=f"quota: {exc}")
        if any(marker in blob for marker in TRANSIENT_MARKERS):
            return _Attempt(
                should_advance=True, scope=SCOPE_MODEL, reason=f"transient: {exc}"
            )
        # Unknown failures: advance rather than giving up on the whole run.
        return _Attempt(should_advance=True, scope=SCOPE_MODEL, reason=f"error: {exc}")

    def _cooldown(self, model: str, reason: str) -> None:
        if self.store is None:
            return
        minutes = (
            QUOTA_COOLDOWN_MINUTES
            if reason.startswith(("quota", "error"))
            else TRANSIENT_COOLDOWN_MINUTES
        )
        self.store.cooldown_model(model, minutes, reason[:300])

    # ── generation ───────────────────────────────────────────────────────

    def generate(
        self,
        *,
        title: str,
        body: str,
        url: str,
        source_name: str,
        brand: str,
        tagline: str,
        max_slides: int,
        hook_intensity: float,
    ) -> GeneratedPost:
        system, prompt = build_prompt(
            brand=brand,
            tagline=tagline,
            source_name=source_name,
            title=title,
            url=url,
            body=body,
            max_slides=max_slides,
            hook_intensity=hook_intensity,
        )

        failures: list[str] = []
        bad_keys: set[int] = set()

        for model in self.available_models():
            for key_index, api_key in enumerate(self.api_keys):
                if key_index in bad_keys:
                    continue
                attempt = self._try(model, api_key, system, prompt)
                if attempt.text:
                    log.info("generated with %s (key #%d)", model, key_index + 1)
                    if self.store is not None:
                        self.store.clear_cooldowns_for(model)
                    return _parse(attempt.text, max_slides)

                failures.append(f"{model}/key{key_index + 1}: {attempt.reason}")
                log.warning(
                    "model %s key #%d unusable (%s)", model, key_index + 1, attempt.reason
                )

                if attempt.scope == SCOPE_KEY:
                    # A revoked/typo'd key is that key's problem only. Try the
                    # next one, and do not let it bench the model.
                    bad_keys.add(key_index)
                    continue

                # A quota/transient/access failure is per-model: stop trying this
                # model's other keys and move down the ladder.
                self._cooldown(model, attempt.reason)
                break

        if bad_keys and len(bad_keys) >= len(self.api_keys):
            # Every key we hold was rejected, so no model will help either.
            # One clear line beats a wall of per-model noise.
            indexes = ", ".join(str(i + 1) for i in sorted(bad_keys))
            raise LLMUnavailable(
                f"Gemini rejected every configured API key (index {indexes} of "
                "GEMINI_API_KEYS). Check .env - a key may be wrong, revoked, or "
                "restricted to a different project."
            )

        raise LLMUnavailable(
            "every model/key in the ladder failed:\n  " + "\n  ".join(failures[-8:])
        )

    def _try(self, model: str, api_key: str, system: str, prompt: str) -> _Attempt:
        log.info(
            "attempting %s with key #%d",
            model,
            self.api_keys.index(api_key) + 1,
        )
        try:
            client = self._client(api_key)
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    temperature=self.temperature,
                    top_p=0.95,
                    max_output_tokens=4096,
                    response_mime_type="application/json",
                    response_schema=_RESPONSE_SCHEMA,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - classified, not swallowed
            return self._classify(exc)

        text = _extract_text(response)
        if not text:
            return _Attempt(should_advance=True, reason="empty response")
        return _Attempt(text=text)

    # ── introspection ────────────────────────────────────────────────────

    def describe_ladder(self) -> str:
        cooled = self.store.cooled_down_models() if self.store else set()
        parts = []
        for model in self.models:
            state = "cooldown" if model in cooled else "ready"
            parts.append(f"{model}[{state}]")
        return " -> ".join(parts)


# ── response schema ──────────────────────────────────────────────────────
# Declaring the schema lets Gemini guarantee valid JSON, which removes an
# entire class of retry-and-regex bugs from the pipeline.
_SLIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "order": {"type": "integer"},
        "layout": {
            "type": "string",
            "enum": [
                "cover", "bullets", "stat", "contrast",
                "steps", "quote", "takeaway", "cta",
            ],
        },
        "headline": {"type": "string"},
        "body": {"type": "string"},
        "footer": {"type": "string"},
    },
    "required": ["order", "layout", "headline"],
}

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "hook": {"type": "string"},
        "subhook": {"type": "string"},
        "slides": {"type": "array", "items": _SLIDE_SCHEMA},
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "alt_text": {"type": "string"},
        "sources_note": {"type": "string"},
    },
    "required": ["hook", "slides", "caption", "hashtags"],
}


def _extract_text(response: object) -> str:
    """Pull text out of a GenerateContentResponse across SDK shapes."""
    text = getattr(response, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()

    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            part_text = getattr(part, "text", None)
            if isinstance(part_text, str) and part_text.strip():
                return part_text.strip()
    return ""


def _parse(raw: str, max_slides: int) -> GeneratedPost:
    """Parse the model's JSON into a GeneratedPost, repairing what we can."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            raise LLMUnavailable(f"model returned non-JSON: {raw[:200]}")
        data = json.loads(match.group(0))

    slides = [
        Slide(
            order=int(s.get("order") or i + 1),
            layout=str(s.get("layout") or "bullets").lower().strip(),
            headline=str(s.get("headline") or "").strip(),
            body=str(s.get("body") or "").strip(),
            footer=str(s.get("footer") or "").strip(),
        )
        for i, s in enumerate(data.get("slides") or [])
        if str(s.get("headline") or "").strip()
    ]
    if not slides:
        raise LLMUnavailable("model returned no usable slides")

    # The renderer only knows these layouts; anything else falls back safely.
    valid = {"cover", "bullets", "stat", "contrast", "steps", "quote", "takeaway", "cta"}
    for slide in slides:
        if slide.layout not in valid:
            slide.layout = "bullets"

    slides = slides[:max_slides]
    for index, slide in enumerate(slides, start=1):
        slide.order = index
    # Slide 1 is always the cover; a model that mislabels it still looks right.
    slides[0].layout = "cover"
    if not slides[0].body:
        slides[0].body = str(data.get("subhook") or "").strip()

    hashtags = []
    for tag in data.get("hashtags") or []:
        normalised = re.sub(r"[^a-z0-9]", "", str(tag).lower())
        if normalised and normalised not in hashtags:
            hashtags.append(normalised)

    return GeneratedPost(
        hook=str(data.get("hook") or slides[0].headline).strip(),
        subhook=str(data.get("subhook") or "").strip(),
        caption=str(data.get("caption") or "").strip(),
        hashtags=hashtags[:30],
        alt_text=str(data.get("alt_text") or "").strip(),
        slides=slides,
        sources_note=str(data.get("sources_note") or "").strip(),
    )
