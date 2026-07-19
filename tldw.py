#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""TLDW — paste a YouTube URL, get a summary. Then ask it questions."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

BUILTIN_MODEL = "google/gemini-2.5-flash-lite"

# `or` rather than a get() default: the XDG spec says an empty value means unset,
# and Path("") would silently resolve relative to the current directory.
CONFIG = Path(
    os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
) / "tldw" / "config.json"

CACHE = Path(
    os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
) / "tldw"

VIDEO_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})")

# Order we actually want, which is NOT alphabetical: sorted() puts "sub.en-GB.vtt"
# before "sub.en.vtt" ('-' < '.').
# "en" is the human/community track where one exists; "en-orig" is raw ASR and
# goes last. On the README's example video: en = 8KB of clean prose,
# en-orig = 44KB of rolling auto-caption with duplicate timings.
SUB_PREF = ["en", "en-US", "en-GB", "en-orig"]

# `summary` deliberately excluded: the summarize path only needs the transcript,
# so a missing summary shouldn't force a full refetch.
CACHE_FIELDS = ("id", "title", "transcript")


class BackendError(Exception):
    """A model/API failure. Recoverable inside a chat session, fatal outside one."""

PROMPT = """Below is the transcript of a YouTube video (auto-generated captions, \
so expect missing punctuation and occasional mis-transcriptions).

Write a TLDW summary:
- One-sentence gist up top.
- Then the key points as bullets, in the order the video makes them.
- Then anything actionable or concrete (numbers, names, steps, recommendations).
- Skip sponsor reads, intros, and "like and subscribe".

Be concise. No preamble — start with the gist.

<title>{title}</title>
<transcript>
{transcript}
</transcript>"""


# ---------------------------------------------------------------- config

def load_config() -> dict:
    try:
        cfg = json.loads(CONFIG.read_text())
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict) -> None:
    """Best-effort. A tool that can't remember is fine; one that crashes isn't."""
    try:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps(cfg, indent=2))
    except OSError:
        pass


# ---------------------------------------------------------------- cache

def cache_read(video_id: str | None) -> dict | None:
    if not video_id:
        return None
    try:
        entry = json.loads((CACHE / f"{video_id}.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    # A truncated or hand-edited entry is a cache miss, not a traceback.
    if not isinstance(entry, dict) or not all(entry.get(k) for k in CACHE_FIELDS):
        return None
    return entry


def cache_write(entry: dict) -> None:
    """Best-effort, same reasoning as save_config."""
    video_id = entry.get("id")
    if not video_id:
        return  # never create CACHE/".json" or point `last` at nothing
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        (CACHE / f"{video_id}.json").write_text(json.dumps(entry))
        (CACHE / "last").write_text(video_id)
    except OSError:
        pass


def cache_last() -> dict | None:
    try:
        return cache_read((CACHE / "last").read_text().strip())
    except OSError:
        return None


# ---------------------------------------------------------------- transcript

def pick_sub(vtts: list[Path]) -> Path:
    def rank(p: Path) -> tuple[int, str]:
        lang = p.stem.split(".")[-1]
        # Name breaks ties: glob order follows the filesystem and isn't stable.
        return (SUB_PREF.index(lang) if lang in SUB_PREF else len(SUB_PREF), p.name)
    return min(vtts, key=rank)


def fetch(url: str) -> tuple[str, str, str]:
    """Return (video_id, title, transcript) for a YouTube URL."""
    if not shutil.which("yt-dlp"):
        sys.exit("`yt-dlp` not found on PATH. Install it: https://github.com/yt-dlp/yt-dlp")

    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(
            [
                "yt-dlp",
                "--skip-download",
                # --print implies --simulate, which silently suppresses subtitle
                # writing. --no-simulate turns that back off.
                "--no-simulate",
                # Two different cases, so both flags are needed:
                #   watch?v=X&list=Y  -- --no-playlist pins it to video X.
                #   /playlist?list=Y  -- not a video URL, so --no-playlist does
                #     nothing and it still expands to every entry; each one
                #     overwrites the same output file, leaving the title from
                #     entry 1 and the transcript from whichever entry wrote last.
                "--no-playlist",
                "--playlist-items", "1",
                "--write-subs",
                "--write-auto-subs",
                # Deliberately narrow: "en.*" also pulls every machine-translated
                # en-XX track, which trips YouTube's rate limiter.
                "--sub-langs", "en,en-orig,en-US,en-GB",
                "--sub-format", "vtt",
                "--print", "id",
                "--print", "title",
                "--no-warnings",
                "-o", f"{tmp}/sub",
                url,
            ],
            capture_output=True,
            text=True,
        )

        out = proc.stdout.strip().splitlines()
        video_id = out[0] if out else ""
        title = out[1] if len(out) > 1 else "(unknown)"

        vtts = list(Path(tmp).glob("*.vtt"))
        # A nonzero exit on one subtitle track doesn't matter if another landed.
        if not vtts:
            err = proc.stderr.strip() or "No English subtitles available for this video."
            sys.exit(err)
        return video_id, title, parse_vtt(pick_sub(vtts).read_text(encoding="utf-8"))


def parse_vtt(raw: str) -> str:
    """VTT -> plain text. Auto-captions repeat lines heavily; dedupe them."""
    lines: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or "-->" in line or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        line = re.sub(r"<[^>]+>", "", line)  # inline timing tags
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    return " ".join(lines)


# ---------------------------------------------------------------- backends

def respond(messages: list[dict], backend: str, model: str) -> str:
    """Stream one assistant turn to stdout and return the full text."""
    if backend == "openrouter":
        return via_openrouter(messages, model)
    return via_claude(messages)


def via_openrouter(messages: list[dict], model: str) -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        sys.exit("OPENROUTER_API_KEY not set. Get one at https://openrouter.ai/keys")

    chunks: list[str] = []
    try:
        _stream_openrouter(chunks, key, model, messages)
    except httpx.HTTPError as e:
        # httpx errors derive from Exception, not OSError, so without this a
        # dropped connection or read timeout escapes as a traceback and takes
        # the whole chat session with it.
        raise BackendError(f"OpenRouter request failed: {e}") from e

    text = "".join(chunks).strip()
    if not text:
        raise BackendError("Model returned an empty response.")
    return text


def _stream_openrouter(chunks: list[str], key: str, model: str, messages: list[dict]) -> None:
    """Appends streamed text to `chunks`. Split out only so the caller can wrap
    the whole request in one httpx.HTTPError handler."""
    with httpx.stream(
        "POST",
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "messages": messages, "stream": True},
        timeout=180,
    ) as r:
        if r.status_code != 200:
            r.read()
            raise BackendError(f"OpenRouter {r.status_code}: {r.text.strip()}")
        for line in r.iter_lines():
            # OpenRouter sends ": OPENROUTER PROCESSING" keepalive comments.
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            # Mid-stream errors arrive after a 200. Without this they'd be
            # swallowed and cached as a successful but empty summary.
            if isinstance(obj, dict) and obj.get("error"):
                err = obj["error"]
                msg = err.get("message", err) if isinstance(err, dict) else err
                raise BackendError(f"OpenRouter stream error: {str(msg).strip()}")
            try:
                # AttributeError covers {"choices":[{"delta":null}]}, which is
                # legal SSE and would otherwise abort a chat session.
                delta = obj["choices"][0]["delta"].get("content")
            except (KeyError, IndexError, TypeError, AttributeError):
                continue
            if delta:
                chunks.append(delta)
                print(delta, end="", flush=True)
    print()


def via_claude(messages: list[dict]) -> str:
    """`claude -p` is one-shot, so flatten the history into a single prompt.

    Deliberately not using --session-id/--resume: that would mean a second
    conversation mechanism plus a fallback for pruned sessions. Resending costs
    a little latency and no money on a subscription.
    """
    if not shutil.which("claude"):
        sys.exit("`claude` CLI not found on PATH.")

    parts = []
    for m in messages:
        if m["role"] == "user":
            parts.append(m["content"])
        else:
            parts.append(f"<your_previous_answer>\n{m['content']}\n</your_previous_answer>")
    prompt = "\n\n".join(parts)

    # Prompt goes over stdin, not argv, so a long transcript can't hit ARG_MAX.
    # Fed from a thread so a full pipe buffer can't deadlock against our reads.
    proc = subprocess.Popen(
        ["claude", "-p"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
    )

    def feed() -> None:
        # close() flushes, so if the child died early the EPIPE lands here, not
        # on write(). Both must be inside the guard or the thread dumps a
        # traceback over the real error message.
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except OSError:  # BrokenPipeError is a subclass
            pass

    threading.Thread(target=feed, daemon=True).start()

    chunks: list[str] = []
    for line in proc.stdout:
        chunks.append(line)
        print(line, end="", flush=True)
    if proc.wait() != 0:
        raise BackendError(f"claude exited {proc.returncode}")

    text = "".join(chunks).strip()
    if not text:
        raise BackendError("claude returned an empty response.")
    return text


# ---------------------------------------------------------------- chat

def chat_loop(messages: list[dict], backend: str, model: str) -> bool:
    """Returns False if the session couldn't start (not a terminal)."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False  # piped or redirected — stay one-shot
    print("\nAsk a follow-up (Ctrl-D or empty line to quit).", file=sys.stderr)
    while True:
        try:
            q = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return True
        if not q:
            return True
        print()
        messages.append({"role": "user", "content": q})
        try:
            answer = respond(messages, backend, model)
        except (BackendError, KeyboardInterrupt) as e:
            # Only recoverable failures land here. Missing keys and missing
            # binaries still sys.exit, rather than looping the same error
            # forever with no way to fix it from inside the prompt.
            print(f"\nerror: {e or type(e).__name__}", file=sys.stderr)
            messages.pop()
            continue
        messages.append({"role": "assistant", "content": answer})


def seed(title: str, transcript: str, summary: str | None = None) -> list[dict]:
    messages = [{"role": "user", "content": PROMPT.format(title=title, transcript=transcript)}]
    if summary:
        messages.append({"role": "assistant", "content": summary})
    return messages


def settings(args, cfg: dict) -> tuple[str, str]:
    """Precedence: explicit flag > env var > last used > builtin."""
    backend = args.backend or os.environ.get("TLDW_BACKEND") or cfg.get("backend") or "openrouter"
    model = args.model or os.environ.get("TLDW_MODEL") or cfg.get("model") or BUILTIN_MODEL
    return backend, model


def add_common(ap: argparse.ArgumentParser) -> None:
    ap.add_argument(
        "--backend", choices=["openrouter", "claude"], default=None,
        help="openrouter = pay-per-token, any model. claude = Claude Code subscription.",
    )
    ap.add_argument("--model", default=None, help="OpenRouter model slug.")


# ---------------------------------------------------------------- commands

def cmd_summarize(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(prog="tldw", description="Summarize a YouTube video.")
    ap.add_argument("url")
    ap.add_argument(
        "-c", "--chat", action="store_true",
        help="Ask follow-up questions after the summary.",
    )
    ap.add_argument("--fresh", action="store_true", help="Ignore the cached transcript.")
    add_common(ap)
    args = ap.parse_args(argv)

    cfg = load_config()
    backend, model = settings(args, cfg)

    # Transcripts never change, so a cache hit skips the network entirely.
    m = VIDEO_ID.search(args.url)
    url_id = m.group(1) if m else None
    hit = None if args.fresh else cache_read(url_id)

    if hit:
        video_id, title, transcript = hit["id"], hit["title"], hit["transcript"]
        print(f"Using cached transcript: {title}", file=sys.stderr)
    else:
        print("Fetching transcript...", file=sys.stderr)
        video_id, title, transcript = fetch(args.url)

    # Validated on every path, not just on a fresh fetch.
    if len(transcript) < 200:
        sys.exit("Transcript too short to summarize.")

    entry = {
        "id": video_id, "url": args.url, "title": title,
        "transcript": transcript, "ts": int(time.time()),
    }
    # Bank the transcript before calling the model. Fetching it is the slow,
    # rate-limited half, so if the summary then fails a rerun shouldn't re-pay
    # for it. This is the case CACHE_FIELDS omits `summary` for.
    if not hit:
        cache_write(entry)

    label = model if backend == "openrouter" else "claude -p"
    print(f"Summarizing ({label}): {title}\n", file=sys.stderr)

    messages = seed(title, transcript)
    summary = respond(messages, backend, model)
    messages.append({"role": "assistant", "content": summary})

    cache_write(entry | {"summary": summary})

    # Persist only explicit flags, and only after a successful run: resolved
    # values fold in env vars, which must stay transient, and a typo'd slug must
    # not poison future invocations. Merge rather than replace, so hand-added
    # keys in config.json survive.
    explicit = {k: v for k, v in (("backend", args.backend), ("model", args.model)) if v}
    if explicit:
        save_config(cfg | explicit)

    if args.chat:
        chat_loop(messages, backend, model)


def cmd_ask(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(prog="tldw ask", description="Question the last video.")
    ap.add_argument("question", nargs="*", help="Omit to start an interactive session.")
    add_common(ap)
    args = ap.parse_args(argv)

    entry = cache_last()
    if not entry:
        sys.exit("No cached video yet. Run `tldw <url>` first.")

    backend, model = settings(args, load_config())
    print(f"Re: {entry['title']}", file=sys.stderr)

    # .get: cache_read deliberately accepts entries without a summary.
    messages = seed(entry["title"], entry["transcript"], entry.get("summary"))

    if args.question:
        print(file=sys.stderr)
        messages.append({"role": "user", "content": " ".join(args.question)})
        messages.append({"role": "assistant", "content": respond(messages, backend, model)})
    elif not chat_loop(messages, backend, model):
        # Reached only when no question was given AND no session could start,
        # which is a usage error however the streams are wired -- otherwise the
        # command produces no output and still reports success.
        sys.exit("No question given, and no interactive session is possible. "
                 "Pass one: tldw ask 'your question'")


def main() -> None:
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "ask":
            cmd_ask(sys.argv[2:])
        else:
            cmd_summarize(sys.argv[1:])
    except BackendError as e:
        sys.exit(str(e))
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        # `tldw <url> | head` closes the pipe early. Standard tools exit quietly;
        # without redirecting the fd, Python also prints "Exception ignored" when
        # it flushes stdout at shutdown.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(141)  # 128 + SIGPIPE


if __name__ == "__main__":
    main()
