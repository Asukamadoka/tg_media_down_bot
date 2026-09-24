"""Translators: a sentence → a :class:`Query` (or a question back), nothing more.

Three backends share one interface, one schema check and one test set:

* ``rules``: :mod:`pikpak_wms.nl.rules_parser`, always tried first;
* ``claude``: the Anthropic API, structured output bound to the Query schema;
* ``ollama``: a local model, constrained decoding with the same schema.

``NL_BACKEND`` (``rules`` | ``claude`` | ``ollama``, default ``rules``) names
the model to hand a sentence to when the rules parser declines it, and
``NL_FALLBACK`` (``none`` | ``claude`` | ``ollama``, default ``none``) a second
one after that.

**Privacy boundary.** A model is sent the sentence, the Query schema, and
the current date, time and time zone — never file names, paths from the
index, or anything else from the drive (README, "PikPak warehouse").
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Sequence
from datetime import datetime, tzinfo
from typing import Any, Protocol

from pydantic import ValidationError

from ..core.errors import WmsError
from .query import Clarification, Query, as_result, from_wire, wire_schema
from .rules_parser import RulesTranslator

log = logging.getLogger(__name__)

BACKENDS = ("rules", "claude", "ollama")
DEFAULT_CLAUDE_MODEL = "claude-opus-5"
DEFAULT_OLLAMA_MODEL = "qwen2.5:3b"
DEFAULT_OLLAMA_URL = "http://ollama:11434"

# Models that accept the server-side refusal fallback ("fallbacks": "default").
_FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5-1")


class TranslationError(WmsError):
    """A model backend failed or answered outside the schema."""


class Translator(Protocol):
    name: str

    async def translate(
        self, text: str, now: datetime, tz: tzinfo
    ) -> Query | Clarification | None: ...


SYSTEM_PROMPT = """\
You translate one instruction about a PikPak cloud drive, usually in Chinese, into a JSON \
query. You only translate: you never carry anything out, and your answer must match the \
given JSON schema exactly.

Fields:
- intent: download (fetch files to the user's NAS), move, rename, classify (sort into \
folders by file type), archive (move old files into dated archive folders), trash (move to \
the recycle bin; permanent deletion is never possible), list (show matching files), or \
schedule (only when the sentence sets up a recurring job and names no other action).
- scope.path: the drive folder the instruction is limited to, as an absolute path such as \
/Inbox; "/" when none is named. scope.recursive: true unless the sentence says otherwise.
- filters.created_after / created_before: when files arrived in the drive (转存, 入库, \
下载到网盘, 新增 all mean arrival). Either an ISO 8601 datetime with the given UTC offset \
(a bare date means the start of that day in the given time zone; "today" starts at local \
midnight) or a duration counted back from now: "7d", "12h", "2w" (a month is 30d, a year \
365d). "最近7天" is created_after "7d"; "30天前" / "超过30天" is created_before "30d".
- filters.min_size / max_size: bytes, binary units (1GB = 1073741824).
- filters.kinds: video, image, audio, document, archive (compressed files), subtitle.
- filters.extensions: lower-case extensions without the dot, e.g. ["mkv"].
- filters.name_contains: literal text the file name must contain; name_regex: a Python \
regular expression (use it for "starts with" / "ends with").
- action_args.dest: target folder for move (absolute drive path); for download a \
sub-folder under the NAS media folder, or null. action_args.template: the naming template \
for rename, with fields like {name}, {stem}, {ext}.
- schedule.cron: five-field cron in the given time zone when the sentence asks for a \
recurring run (每天 = daily); null otherwise.
- needs_clarification: null, unless the sentence cannot be turned into a query without \
guessing (no destination for a move, "big files" without a size, "old files" without an \
age, the whole drive for trash/download/move/rename, anything asking for permanent \
deletion). Then put one short question, in the user's language, and fill the rest as best \
you can.

Leave every field you have no evidence for at its empty value. Never invent folders, names \
or sizes that the sentence does not state."""


def user_message(text: str, now: datetime, tz: tzinfo) -> str:
    local = now.astimezone(tz)
    return (
        f"Now: {local.isoformat(timespec='seconds')} (time zone {tz}).\n"
        f"Instruction: {text}"
    )


def _parse_answer(raw: str, backend: str) -> Query | Clarification:
    try:
        return as_result(from_wire(json.loads(raw)))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as exc:
        raise TranslationError(f"{backend} answered outside the schema: {exc}",
                               key="nl.error.schema", backend=backend) from exc


class ClaudeTranslator:
    """The Anthropic API; the answer is constrained to the Query schema."""

    name = "claude"

    def __init__(self, *, model: str | None = None, client: Any = None,
                 effort: str | None = None) -> None:
        self.model = model or os.environ.get("NL_CLAUDE_MODEL", "").strip() or DEFAULT_CLAUDE_MODEL
        self.effort = (effort if effort is not None
                       else os.environ.get("NL_CLAUDE_EFFORT", "low")).strip()
        self._client = client

    def _api(self) -> Any:
        if self._client is None:
            from anthropic import AsyncAnthropic  # only needed when this backend is used

            # Credentials come from ANTHROPIC_API_KEY (or the SDK's other sources).
            self._client = AsyncAnthropic()
        return self._client

    async def translate(self, text: str, now: datetime, tz: tzinfo) -> Query | Clarification:
        import anthropic

        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": wire_schema()}}
        if self.effort:
            # A one-sentence extraction needs little thinking; "low" keeps it quick.
            output_config["effort"] = self.effort
        extra: dict[str, Any] = {}
        if self.model in _FALLBACK_MODELS:
            # A safety decline is re-run server-side on a fallback model.
            extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        try:
            response = await self._api().beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                output_config=output_config,
                messages=[{"role": "user", "content": user_message(text, now, tz)}],
                **extra,
            )
        except anthropic.APIConnectionError as exc:
            raise TranslationError(f"claude: cannot connect: {exc}", key="nl.error.backend",
                                   backend=self.name, error="connection") from exc
        except anthropic.RateLimitError as exc:
            raise TranslationError("claude: rate limited", key="nl.error.backend",
                                   backend=self.name, error="rate limited") from exc
        except anthropic.APIStatusError as exc:
            raise TranslationError(f"claude: HTTP {exc.status_code}: {exc.message}",
                                   key="nl.error.backend", backend=self.name,
                                   error=f"HTTP {exc.status_code}") from exc
        except (TypeError, anthropic.AnthropicError) as exc:
            # The SDK raises TypeError when it finds no credentials at all.
            raise TranslationError(f"claude: {exc}", key="nl.error.backend", backend=self.name,
                                   error="no ANTHROPIC_API_KEY" if isinstance(exc, TypeError)
                                   else type(exc).__name__) from exc
        if response.stop_reason == "refusal":
            return Clarification(question="nl.ask.declined")
        if response.stop_reason == "max_tokens":
            raise TranslationError("claude: answer cut off", key="nl.error.backend",
                                   backend=self.name, error="max_tokens")
        raw = next((block.text for block in response.content if block.type == "text"), "")
        return _parse_answer(raw, self.name)


class OllamaTranslator:
    """A local Ollama model, with the Query schema as its output format."""

    name = "ollama"

    def __init__(self, *, url: str | None = None, model: str | None = None,
                 post: Any = None, timeout: float = 180.0) -> None:
        raw_url = url or os.environ.get("OLLAMA_URL", "").strip() or DEFAULT_OLLAMA_URL
        self.url = raw_url.rstrip("/")
        self.model = model or os.environ.get("NL_OLLAMA_MODEL", "").strip() or DEFAULT_OLLAMA_MODEL
        self.timeout = timeout
        self._post = post or self._http_post

    async def _http_post(  # pragma: no cover - real network
        self, url: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session, session.post(
            url, json=body
        ) as response:
            response.raise_for_status()
            return await response.json(content_type=None)

    async def translate(self, text: str, now: datetime, tz: tzinfo) -> Query | Clarification:
        body = {
            "model": self.model,
            "stream": False,
            "format": wire_schema(),
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message(text, now, tz)},
            ],
        }
        try:
            answer = await self._post(f"{self.url}/api/chat", body)
        except Exception as exc:
            raise TranslationError(f"ollama: {exc}", key="nl.error.backend",
                                   backend=self.name, error=type(exc).__name__) from exc
        raw = ((answer or {}).get("message") or {}).get("content") or ""
        return _parse_answer(raw, self.name)


class Chain:
    """Try each translator in turn; the first that handles the sentence wins.

    A model that fails is logged and skipped, so a dead Ollama does not stop
    the rules parser from answering what it can.
    """

    def __init__(self, translators: Sequence[Translator]) -> None:
        self.translators = list(translators)
        self.name = "+".join(t.name for t in self.translators)
        self.last_used: str | None = None

    async def translate(self, text: str, now: datetime, tz: tzinfo) -> Query | Clarification | None:
        failure: TranslationError | None = None
        for translator in self.translators:
            try:
                result = await translator.translate(text, now, tz)
            except TranslationError as exc:
                log.warning("translator %s failed: %s", translator.name, exc)
                failure = exc
                continue
            if result is not None:
                self.last_used = translator.name
                return result
        if failure is not None:
            raise failure
        return None


def _make(name: str) -> Translator:
    if name == "rules":
        return RulesTranslator()
    if name == "claude":
        return ClaudeTranslator()
    if name == "ollama":
        return OllamaTranslator()
    raise ValueError(f"unknown translator {name!r}; use one of {', '.join(BACKENDS)}")


def build(backend: str = "rules", fallback: str = "none", *, rules_first: bool = True) -> Chain:
    order: list[str] = ["rules"] if rules_first or backend == "rules" else []
    for name in (backend, fallback):
        if name not in ("none", "") and name not in order:
            order.append(name)
    return Chain([_make(name) for name in order])


def from_environment() -> Chain:
    backend = os.environ.get("NL_BACKEND", "rules").strip().lower() or "rules"
    fallback = os.environ.get("NL_FALLBACK", "none").strip().lower() or "none"
    for value, variable in ((backend, "NL_BACKEND"), (fallback, "NL_FALLBACK")):
        if value not in (*BACKENDS, "none"):
            raise WmsError(f"{variable}={value} is not one of rules, claude, ollama, none",
                           key="nl.error.setting", variable=variable, value=value)
    return build(backend, fallback)
