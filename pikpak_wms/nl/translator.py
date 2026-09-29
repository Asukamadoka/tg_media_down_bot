"""Translators: a sentence → a :class:`Query` (or a question back), nothing more.

Four backends share one interface, one schema check and one test set:

* ``rules``: :mod:`pikpak_wms.nl.rules_parser`, always tried first;
* ``claude``: the Anthropic API, structured output bound to the Query schema;
* ``ollama``: a local model, constrained decoding with the same schema;
* ``openai``: any OpenAI-compatible chat endpoint (docs/wms/M7 §7.3): LM
  Studio, llama.cpp ``llama-server``, Ollama's ``/v1``, vLLM, DeepSeek,
  通义千问 (DashScope compatible mode). ``response_format`` carries the
  schema; a server that does not take a schema gets ``json_object`` instead,
  and the answer is checked against the schema here either way.

``NL_BACKEND`` (``rules`` | ``claude`` | ``ollama`` | ``openai``, default
``rules``) names the model to hand a sentence to when the rules parser
declines it, and ``NL_FALLBACK`` (``none`` or one of those) a second one.

**Privacy boundary.** A model is sent the sentence, the Query schema, and
the current date, time and time zone — never file names, paths from the
index, or anything else from the drive (README, "PikPak warehouse").
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Sequence
from datetime import datetime, tzinfo
from typing import Any, Protocol

from pydantic import ValidationError

from ..core.errors import WmsError
from .hosts import BOARD, PROBE_TIMEOUT, Host, HostBoard, HostsConfigError, parse_hosts
from .query import Clarification, Query, as_result, from_wire, normalize_wire, wire_schema
from .rules_parser import RulesTranslator

log = logging.getLogger(__name__)

BACKENDS = ("rules", "claude", "ollama", "openai")
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
the recycle bin; permanent deletion is never possible), list (show matching files), \
schedule (only when the sentence sets up a recurring job and names no other action), \
organize_tree (tidy the drive's top-level folders: group loose files, set big files and \
folders apart, clear junk; scope.path may name one top-level folder), organize_inbox \
(shelve what landed in the entry folders /Telegram and /Pack From Shared; scope.path may \
name one of them), dedupe (remove duplicate copies; scope.path may limit it), or \
big_report (show the biggest files and folders). The last four take no filters and no \
schedule.
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
for rename, with fields like {name}, {stem}, {ext}. action_args.part: organize_tree only, \
"big" when the sentence is only about putting big files together, "slim" for only junk and \
empty folders, "loose" for only the loose files; null otherwise.
- schedule.cron: five-field cron in the given time zone when the sentence asks for a \
recurring run (每天 = daily); null otherwise.
- needs_clarification: null, unless the sentence cannot be turned into a query without \
guessing (no destination for a move, "big files" without a size, "old files" without an \
age, the whole drive for trash/download/move/rename, anything asking for permanent \
deletion). Then put one short question, in the user's language, and fill the rest as best \
you can.

Leave every field you have no evidence for at its empty value. Never invent folders, names \
or sizes that the sentence does not state. When you are unsure, fill needs_clarification \
rather than guess.

Correct answers (only the fields that are set are shown; every other field keeps its empty \
value):
1. "每天凌晨3点把 /Telegram 里的视频移到 /Media" -> {"intent": "move", "scope": {"path": \
"/Telegram", "recursive": true}, "filters": {"kinds": ["video"]}, "action_args": {"dest": \
"/Media"}, "schedule": {"cron": "0 3 * * *"}, "needs_clarification": null}
2. "看看最近7天转存的大于2GB的文件" -> {"intent": "list", "filters": {"created_after": "7d", \
"min_size": 2147483648}, "schedule": null, "needs_clarification": null}
3. "把大文件移走" -> {"intent": "move", "schedule": null, "needs_clarification": \
"多大算大文件? 要移到哪个文件夹?"}

Wrong answers, never write these:
1. {"schedule": {"cron": "0 0 3 * * ?"}}: that is Quartz. cron has exactly five fields: \
"0 3 * * *".
2. {"schedule": "null", "needs_clarification": "null"}: the string "null" is not null. Write \
JSON null."""


def user_message(text: str, now: datetime, tz: tzinfo) -> str:
    local = now.astimezone(tz)
    return (
        f"Now: {local.isoformat(timespec='seconds')} (time zone {tz}).\n"
        f"Instruction: {text}"
    )


class SchemaStats:
    """How often a model's answers break the schema, before and after
    :func:`normalize_wire` (docs/wms/M8 §B; the eval reports both)."""

    def __init__(self) -> None:
        self.answers = 0
        self.invalid_before = 0
        """Answers the schema would have refused as they came."""
        self.invalid_after = 0
        """Answers still refused after normalizing: these fail the sentence."""


def _valid(data: Any) -> bool:
    try:
        from_wire(data)
    except (ValidationError, ValueError, TypeError, AttributeError):
        return False
    return True


def _parse_answer(raw: str, backend: str,
                  stats: SchemaStats | None = None) -> Query | Clarification:
    raw = raw.strip()
    if raw.startswith("```"):
        # Some local models fence their JSON even in JSON mode.
        raw = raw.strip("`").removeprefix("json").strip()
    if stats is not None:
        stats.answers += 1
    try:
        data = json.loads(raw)
        if stats is not None and not _valid(data):
            stats.invalid_before += 1
        return as_result(from_wire(normalize_wire(data)))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError, AttributeError) as exc:
        if stats is not None:
            if isinstance(exc, json.JSONDecodeError):
                stats.invalid_before += 1
            stats.invalid_after += 1
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
        self.stats = SchemaStats()

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
        return _parse_answer(raw, self.name, self.stats)


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
        self.stats = SchemaStats()

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
        return _parse_answer(raw, self.name, self.stats)


class OpenAITranslator:
    """Any OpenAI-compatible ``/chat/completions`` endpoint (docs/wms/M7 §7.3).

    ``NL_OPENAI_BASE_URL`` (e.g. ``http://192.168.1.10:1234/v1``),
    ``NL_OPENAI_MODEL``, and ``NL_OPENAI_API_KEY`` (may be empty for a local
    server). The schema goes in ``response_format``; when the server refuses
    that (HTTP 400/404/415/422, as DeepSeek does), the request is repeated
    once with ``{"type": "json_object"}`` and the schema in the prompt, and
    the answer is validated here all the same.

    Several hosts may be named, comma-separated and best first, with one
    model for all or one per host (docs/wms/M8 §A). The first host that
    answers ``GET /models`` gets the sentence (:mod:`pikpak_wms.nl.hosts`);
    one that drops out mid-way counts as offline and the next is tried.
    When none is up, the sentence fails at once with ``nl.error.offline``.
    Connecting is bounded by 1.5 s, the answer by ``NL_OPENAI_TIMEOUT``
    (default 60 s).
    """

    name = "openai"

    def __init__(self, *, base_url: str | None = None, model: str | None = None,
                 api_key: str | None = None, post: Any = None, get: Any = None,
                 timeout: float | None = None, board: HostBoard | None = None) -> None:
        urls = base_url if base_url is not None else os.environ.get("NL_OPENAI_BASE_URL", "")
        models = model if model is not None else os.environ.get("NL_OPENAI_MODEL", "")
        self.config_error = ""
        try:
            self.hosts = parse_hosts(urls, models)
        except HostsConfigError as exc:
            self.hosts, self.config_error = [], str(exc)
        self.api_key = (api_key if api_key is not None
                        else os.environ.get("NL_OPENAI_API_KEY", "")).strip()
        self.timeout = timeout if timeout is not None else _env_seconds("NL_OPENAI_TIMEOUT", 60.0)
        self._post = post or self._http_post
        self._get = get
        self.board = board or BOARD
        self._json_hosts: set[str] = set()
        self.stats = SchemaStats()
        """Hosts that turned the schema down; later calls skip straight to json_object."""

    @property
    def base_url(self) -> str:
        return self.hosts[0].base_url if self.hosts else ""

    @property
    def model(self) -> str:
        return self.hosts[0].model if self.hosts else ""

    async def _http_post(  # pragma: no cover - real network
        self, url: str, body: dict[str, Any], headers: dict[str, str]
    ) -> tuple[int, dict[str, Any]]:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=self.timeout, connect=PROBE_TIMEOUT,
                                        sock_connect=PROBE_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session, session.post(
            url, json=body, headers=headers
        ) as response:
            try:
                data = await response.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                data = {"error": {"message": (await response.text())[:200]}}
            return response.status, data if isinstance(data, dict) else {}

    def _body(self, host: Host, text: str, now: datetime, tz: tzinfo, *,
              json_mode: bool) -> dict[str, Any]:
        system = SYSTEM_PROMPT
        if json_mode:
            system += ("\n\nAnswer with one JSON object and nothing else, matching this JSON "
                       "Schema:\n" + json.dumps(wire_schema(), ensure_ascii=False))
            response_format: dict[str, Any] = {"type": "json_object"}
        else:
            response_format = {"type": "json_schema", "json_schema": {
                "name": "query", "strict": True, "schema": wire_schema()}}
        return {
            "model": host.model,
            "temperature": 0,
            "response_format": response_format,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_message(text, now, tz)},
            ],
        }

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _ask(self, host: Host, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        started = time.perf_counter()
        try:
            answer = await self._post(f"{host.base_url}/chat/completions", body, self._headers())
        except Exception as exc:
            # Refused, reset, or no answer in time: the host went away.
            raise _HostDown(f"{type(exc).__name__}: {exc}"[:120]) from exc
        self.board.answered(host, time.perf_counter() - started)
        return answer

    async def translate(self, text: str, now: datetime, tz: tzinfo) -> Query | Clarification:
        if self.config_error:
            raise TranslationError(f"openai: {self.config_error}", key="nl.error.backend",
                                   backend=self.name, error=self.config_error)
        if not self.hosts or not all(host.model for host in self.hosts):
            raise TranslationError("openai: NL_OPENAI_BASE_URL and NL_OPENAI_MODEL are needed",
                                   key="nl.error.backend", backend=self.name,
                                   error="NL_OPENAI_BASE_URL / NL_OPENAI_MODEL")
        for host in self.hosts:
            if not await self.board.online(host, self._headers(), self._get):
                continue
            try:
                return await self._translate_on(host, text, now, tz)
            except _HostDown as exc:
                self.board.offline(host, str(exc))
        raise TranslationError(
            "openai: every model host is offline", key="nl.error.offline",
            hosts=", ".join(host.label for host in self.hosts))

    async def _translate_on(self, host: Host, text: str, now: datetime,
                            tz: tzinfo) -> Query | Clarification:
        json_mode = host.base_url in self._json_hosts
        status, answer = await self._ask(host, self._body(host, text, now, tz,
                                                          json_mode=json_mode))
        if status in (400, 404, 415, 422) and not json_mode:
            log.info("openai backend: %s refused json_schema (HTTP %s); using json_object",
                     host.label, status)
            self._json_hosts.add(host.base_url)
            status, answer = await self._ask(host, self._body(host, text, now, tz,
                                                              json_mode=True))
        if status >= 400:
            message = str(((answer or {}).get("error") or {}).get("message") or "")[:120]
            raise TranslationError(f"openai: HTTP {status}: {message}", key="nl.error.backend",
                                   backend=self.name, error=f"HTTP {status}")
        choice = ((answer or {}).get("choices") or [{}])[0] or {}
        message = choice.get("message") or {}
        if message.get("refusal"):
            return Clarification(question="nl.ask.declined")
        if choice.get("finish_reason") == "length":
            raise TranslationError("openai: answer cut off", key="nl.error.backend",
                                   backend=self.name, error="length")
        return _parse_answer(str(message.get("content") or ""), self.name, self.stats)


class _HostDown(Exception):
    """A model host stopped answering in the middle of a translation."""


def _env_seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        log.warning("%s is not a number of seconds; using %s", name, default)
        return default
    return value if value > 0 else default


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
    if name == "openai":
        return OpenAITranslator()
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
            raise WmsError(f"{variable}={value} is not one of rules, claude, ollama, openai, none",
                           key="nl.error.setting", variable=variable, value=value)
    return build(backend, fallback)
