"""Reading a message out loud.

macOS already has a narrator and this app already knows how to use it: `assemble.speak` records a
line of narration with `say` (or with a cloned voice) when a film is assembled. Reading a chat
message aloud is the same call with a different caller, so it goes through the same function rather
than a second implementation of "how do we talk to `say` here".

Two things a chat needs that a film does not:

  * a **cache**. Chat lines are short and get replayed — collapsing and expanding a message, or two
    people reading the same one — and re-synthesising identical text would be a wasted second every
    time. The key is the text plus the voice, so changing either is a new file;
  * a **cap**. A member's reply can be pages long, and reading it all takes minutes. The first part
    is read and the caller is told it was cut, rather than either refusing or reading for five
    minutes in silence (see `cut`).
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

from . import assemble, i18n

_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff]")

# Beyond this a message is read up to the cap and the reader is told. Measured: `say` runs about
# 4.5 Chinese characters per second, so 1200 characters is a bit over four minutes of speech — past
# that the user has stopped listening, whatever the text says.
MAX_CHARS = 1200


# ------------------------------------------------------------------ not sounding like a machine
#
# macOS `say` is a formant synthesiser: asked to read a sentence it does so at a constant rate, on a
# constant pitch, with no pause at the full stops — which is what "生硬" means. It does respond to
# Apple's embedded speech commands (`[[...]]`), documented in *Speech Synthesis Programming Guide*,
# and those are the whole of what can be done about it **locally**: the ones used here are
#   `[[slnc ms]]` silence, `[[rate wpm]]`, `[[pbas ±n]]` pitch baseline, `[[volm 0.x]]` volume.
# Measured: a two-sentence line goes from 3.74 s to 4.06 s with a 320 ms pause and a slower final
# question, i.e. the commands are really taking effect and not being read out as words.
#
# Ranges are deliberately narrow. A synthesiser pushed hard sounds *worse*, not more emotional: this
# is the difference between a reader who pauses and lifts their voice at a question, and a machine
# doing an impression of one. What is not attempted here is acting.
SENTENCE_PAUSE = 260          # between sentences
CLAUSE_PAUSE = 110            # after a comma, so a long sentence is not one breath
PARAGRAPH_PAUSE = 430         # between paragraphs
BASE_RATE = 172               # a little under `say`'s own pace: hurried is the other half of 生硬

_ENDERS = "。!?！？…"
_CLAUSES = ",,;;::"
_HEADING_HINT = ("：", ":", "——", "—")
# A message may itself contain `[[`, which would start a command; the brackets are broken up first.
_LITERAL = re.compile(r"\[\[")


def _kind(sentence: str) -> str:
    """How this sentence should be read: a question lifts and slows, a heading is read plainly."""
    body = sentence.strip()
    if not body:
        return "plain"
    if body[-1] in "?？":
        return "question"
    if body[-1] in "!！":
        return "exclaim"
    if len(body) <= 14 or body.endswith(_HEADING_HINT):
        return "heading"
    return "plain"


def _speakable(sentence: str, index: int) -> str:
    """One sentence, with the commands that give it a voice rather than a readout.

    The alternation of `pbas` on plain sentences is the part that does the most work for the least
    risk: a monotone is the thing people notice, and a two-point drift between neighbouring sentences
    removes it without any of them sounding odd on their own.
    """
    kind = _kind(sentence)
    if kind == "question":
        return f"[[pbas +6]][[rate 156]]{sentence}"
    if kind == "exclaim":
        return f"[[pbas +5]][[rate 182]][[volm 0.95]]{sentence}"
    if kind == "heading":
        return f"[[pbas +3]][[rate 164]]{sentence}"
    drift = (0, 2, -1, 1)[index % 4]
    return f"[[pbas {drift:+d}]][[rate {BASE_RATE}]]{sentence}"


def emotive(text: str) -> str:
    """The message as `say` should be given it: sentences, pauses and a little movement in the voice.

    Paragraph breaks are kept (a pause, not a comma), Chinese and Latin punctuation both count, and
    a clause that runs long gets a breath in the middle — the three things whose absence makes a
    synthesiser sound like a formant synthesiser.
    """
    text = _LITERAL.sub("[ [", str(text or ""))
    out: list[str] = []
    # Paragraphs first: a blank line is a real pause, and collapsing it into the sentence stream is
    # how a read-out ends up sounding like a wall.
    for para in re.split(r"\n\s*\n", text):
        para = " ".join(para.split())
        if not para:
            continue
        pieces = re.split(r"(?<=[" + _ENDERS + r"])", para)
        sentences = [p.strip() for p in pieces if p.strip()]
        for i, sentence in enumerate(sentences):
            body = _speakable(sentence, i)
            # A sentence long enough to need a breath gets one at its own commas (~20 characters is
            # about where a Chinese sentence stops fitting in one breath when read aloud).
            if len(sentence) >= 20:
                body = re.sub(r"([" + _CLAUSES + r"])\s*",
                              r"\1[[slnc " + str(CLAUSE_PAUSE) + r"]]", body)
            out.append(body)
            if i < len(sentences) - 1:
                out.append(f"[[slnc {SENTENCE_PAUSE}]]")
        out.append(f"[[slnc {PARAGRAPH_PAUSE}]]")
    # No trailing pause: it would only make the file longer than what is read.
    return re.sub(r"\[\[slnc \d+\]\]$", "", "".join(out)).strip()


def cut(text: str) -> str:
    """The part of a message that will be read, on a sentence boundary where possible."""
    body = " ".join(str(text or "").split())
    if len(body) <= MAX_CHARS:
        return body
    head = body[:MAX_CHARS]
    for stop in ("。", "!", "?", "!", "?", ".", ";", ";", "\n"):
        at = head.rfind(stop)
        if at > MAX_CHARS * 0.6:
            return head[: at + 1]
    return head


def was_cut(text: str) -> bool:
    return len(" ".join(str(text or "").split())) > MAX_CHARS


def file_for(text: str, voice: str, folder: Path) -> Path | None:
    """The cached reading of this text in this voice, if it has been made already."""
    found = folder / f"{key(text, voice)}.wav"
    return found if found.is_file() else None


def key(text: str, voice: str) -> str:
    raw = f"{voice}\x00{text}"
    return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:20]      # noqa: S324 — a cache key


# The system voices this app reads aloud with, by language. Deliberately *system* voices and not the
# cloned ones: a clone is a local model that takes minutes to load (measured 398 s on this machine),
# which is not a "read this to me" button — it is a production step, and it lives in `assemble`.
CJK_VOICE = "Tingting"
LATIN_VOICE = "Samantha"


def voice_for(text: str, installed: set[str] | None = None) -> str:
    """Which system voice reads this text, with a fallback that cannot fail.

    A voice name that is not installed makes `say` fail outright, so the choice is checked against
    what this machine actually has; when neither preference is there, `say`'s own default is used
    (an empty name means "do not pass -v"). Getting this wrong is silent — the message would simply
    never be read out — so it is a lookup rather than an assumption.
    """
    have = installed if installed is not None else voices()
    cjk = bool(_CJK.search(text or ""))
    wanted = CJK_VOICE if cjk else LATIN_VOICE
    if wanted in have:
        return wanted
    fallback = ("Eddy (Chinese (China mainland))", "Meijia") if cjk else ("Daniel", "Alex")
    return next((v for v in fallback if v in have), "")


def voices() -> set[str]:
    """The names `say` is willing to accept on this machine (the first field of `say -v ?`)."""
    try:
        done = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return set()
    return {line.split("  ")[0].strip() for line in (done.stdout or "").splitlines() if line.strip()}


async def record(text: str, voice: str, folder: Path, timeout: float = 120.0) -> tuple[Path, bool]:
    """`(file, reused)` — the reading of this text, recorded now or taken from the cache.

    The file is written under the app's own data directory, never into a group's workspace: this is
    the app talking, not material a member produced, and it must not turn up in a file listing or be
    picked up as a shot by a later `assemble_video`. WAV rather than `say`'s own AIFF, because the
    browser is the thing that plays it and Chromium cannot play AIFF.
    """
    body = cut(text)
    if not body:
        raise assemble.AssembleError(i18n.pick_now("There is nothing to read out.",
                                                   "这条消息里没有可念的内容。"))
    # The key is the text *as it will be spoken*, markup and all: it is what was actually synthesised,
    # so changing how the voice moves produces a new file instead of serving yesterday's monotone.
    spoken = emotive(body)
    folder.mkdir(parents=True, exist_ok=True)
    got = file_for(spoken, voice, folder)
    if got is not None:
        return got, True
    out = folder / f"{key(spoken, voice)}.wav"
    # The temporary name keeps the `.wav` extension on purpose: `say` picks the container from the
    # extension, so writing through `x.wav.part` would produce an AIFF named like a WAV — which plays
    # in Safari and not in Electron's Chromium. Dot-prefixed so a half-written file is never mistaken
    # for a cached one, and `os.replace` so a reader sees either nothing or the whole thing.
    tmp = folder / f".{key(spoken, voice)}-{os.getpid()}.wav"
    try:
        await assemble.speak(spoken, tmp, voice, timeout)
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)
        # `speak` hands the text to `say` through a `.txt` beside the audio and leaves it there (in the
        # film pipeline that folder is temporary, here it is the cache). Removed so the cache holds
        # exactly one file per reading and nothing else.
        tmp.with_suffix(".txt").unlink(missing_ok=True)
    return out, False
