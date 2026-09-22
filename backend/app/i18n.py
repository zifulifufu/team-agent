"""Language handling for built-in content.

Built-in content files (``data/catalog.json``, ``data/local_models.json`` and the
files under ``templates/``) keep the **English** text in the plain field and the
Chinese translation in the ``<field>_zh`` sibling, for example::

    {"name": "Qwen 3.8", "name_zh": "千问 3.8"}

``localize()`` is applied at the API boundary: when the client asks for ``zh`` the
``*_zh`` value is swapped into the base key, and when it asks for ``en`` the
``*_zh`` keys are dropped. Clients therefore never see both languages at once and
never see the bookkeeping keys.

The requested language is resolved once per request and kept in a ``ContextVar``
so that any code (routes, helpers, formatters) can read it without threading a
parameter through every call.
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from collections.abc import Iterator
from typing import Any, Awaitable, Callable

DEFAULT_LANG = "en"
LANGS = ("en", "zh")
ZH_SUFFIX = "_zh"

_lang: ContextVar[str] = ContextVar("lang", default=DEFAULT_LANG)


# ----------------------------------------------------------------------- helpers
def normalize(value: str | None) -> str | None:
    """Turn a loose language tag (``zh-CN``, ``en-US``, ``ZH``) into a supported code."""
    if not value:
        return None
    tag = value.strip().lower().replace("_", "-")
    if tag.startswith("zh"):
        return "zh"
    if tag.startswith("en"):
        return "en"
    return None


def resolve(lang: str | None = None, accept_language: str | None = None) -> str:
    """Pick the language for a request: explicit ``?lang=`` wins, then ``Accept-Language``.

    Anything unknown falls back to English, which is the default UI language.
    """
    explicit = normalize(lang)
    if explicit:
        return explicit
    # Accept-Language is a weighted list, e.g. "zh-CN,zh;q=0.9,en;q=0.8"
    for part in (accept_language or "").split(","):
        picked = normalize(part.split(";")[0])
        if picked:
            return picked
    return DEFAULT_LANG


@contextlib.contextmanager
def pinned(lang: str) -> Iterator[None]:
    """Run a block with the language pinned, whatever the request asked for.

    For work whose *result is stored* rather than displayed: a value written to disk must not
    depend on who happened to trigger the run, or the next reader sees a language they cannot
    read. (`pick_now` would otherwise resolve against the request that started the task.)
    """
    token = _lang.set(lang)
    try:
        yield
    finally:
        _lang.reset(token)


def current() -> str:
    """The language of the request being handled (``zh`` or ``en``)."""
    return _lang.get()


def set_current(lang: str) -> None:
    _lang.set(lang if lang in LANGS else DEFAULT_LANG)


def pick(lang: str, en: str, zh: str | None = None) -> str:
    """Inline bilingual string, for text that lives in code rather than in a data file."""
    return (zh or en) if lang == "zh" else en


def pick_now(en: str, zh: str | None = None) -> str:
    """``pick()`` using the language of the current request."""
    return pick(current(), en, zh)


def localize(node: Any, lang: str | None = None) -> Any:
    """Return ``node`` with ``*_zh`` values swapped in (``zh``) or dropped (``en``)."""
    lang = lang or current()
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key.endswith(ZH_SUFFIX):
                continue                       # only ever reached through its base key
            zh = node.get(key + ZH_SUFFIX) if lang == "zh" else None
            out[key] = localize(zh if zh is not None else value, lang)
        return out
    if isinstance(node, list):
        return [localize(item, lang) for item in node]
    return node


# --------------------------------------------------------------------- middleware
class LanguageMiddleware:
    """Pure ASGI middleware: resolves the request language into the ContextVar.

    Written as a raw ASGI app (rather than ``BaseHTTPMiddleware``) so the value is
    set in the same task that runs the route, which keeps the ContextVar visible
    to everything downstream.
    """

    def __init__(self, app: Callable[..., Awaitable[None]]):
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        query = scope.get("query_string", b"").decode("latin-1")
        requested = next(
            (p.split("=", 1)[1] for p in query.split("&") if p.startswith("lang=")), None
        )
        token = _lang.set(resolve(requested, headers.get("accept-language")))
        try:
            await self.app(scope, receive, send)
        finally:
            _lang.reset(token)
