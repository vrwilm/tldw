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
# before "sub.en.vtt" ('-' < '.'), silently preferring a machine-translated track
# over the original.
SUB_PREF = ["en-orig", "en", "en-US", "en-GB"]

CACHE_FIELDS = ("id", "title", "transcript", "summary")

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


def cache_write(entry: dict, alias: str | None = None) -> None:
    """Best-effort, same reasoning as save_config."""
    video_id = entry.get("id")
    if not video_id:
        return  # never create CACHE/".json" or point `last` at nothing
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        blob = json.dumps(entry)
        (CACHE / f"{video_id}.json").write_text(blob)
        # If the URL's ID didn't match yt-dlp's canonical one, store it under both
        # so the next run of the same URL still hits.
        if alias and alias != video_id:
            (CACHE / f"{alias}.json").write_text(blob)
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
    def rank(p: Path) -> int:
        lang = p.stem.split(".")[-1]
        return SUB_PREF.index(lang) if lang in SUB_PREF else len(SUB_PREF)
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
                # A URL copied from a playlist (watch?v=X&list=Y) otherwise expands
                # to the whole playlist: every entry overwrites the same output
                # file, so the title and the transcript end up from different videos.
                "--no-playlist",
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
    with httpx.stream(
        "POST",
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "messages": messages, "stream": True},
        timeout=180,
    ) as r:
        if r.status_code != 200:
            r.read()
            sys.exit(f"OpenRouter {r.status_code}: {r.text.strip()}")
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
                sys.exit(f"\nOpenRouter stream error: {msg}")
            try:
                delta = obj["choices"][0]["delta"].get("content")
            except (KeyError, IndexError, TypeError):
                continue
            if delta:
                chunks.append(delta)
                print(delta, end="", flush=True)
    print()

    text = "".join(chunks).strip()
    if not text:
        sys.exit("Model returned an empty response.")
    return text


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
        try:
            proc.stdin.write(prompt)
        except BrokenPipeError:
            pass
        finally:
            proc.stdin.close()

    threading.Thread(target=feed, daemon=True).start()

    chunks: list[str] = []
    for line in proc.stdout:
        chunks.append(line)
        print(line, end="", flush=True)
    if proc.wait() != 0:
        sys.exit(f"claude exited {proc.returncode}")

    text = "".join(chunks).strip()
    if not text:
        sys.exit("claude returned an empty response.")
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
        except SystemExit as e:
            # A transient API error shouldn't discard the whole conversation.
            print(f"error: {e}", file=sys.stderr)
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

    label = model if backend == "openrouter" else "claude -p"
    print(f"Summarizing ({label}): {title}\n", file=sys.stderr)

    messages = seed(title, transcript)
    summary = respond(messages, backend, model)
    messages.append({"role": "assistant", "content": summary})

    cache_write({
        "id": video_id, "url": args.url, "title": title,
        "transcript": transcript, "summary": summary, "ts": int(time.time()),
    }, alias=url_id)

    # Persist only explicit flags, and only after a successful run. Read from
    # args/cfg alone -- resolved values fold in env vars, which must stay
    # transient, and a typo'd slug must not poison future invocations.
    if args.backend or args.model:
        save_config({
            "backend": args.backend or cfg.get("backend") or "openrouter",
            "model": args.model or cfg.get("model") or BUILTIN_MODEL,
        })

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

    messages = seed(entry["title"], entry["transcript"], entry["summary"])

    if args.question:
        print(file=sys.stderr)
        messages.append({"role": "user", "content": " ".join(args.question)})
        messages.append({"role": "assistant", "content": respond(messages, backend, model)})
    elif not chat_loop(messages, backend, model):
        sys.exit("No question given, and stdin is not a terminal. Pass one: tldw ask 'your question'")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "ask":
        cmd_ask(sys.argv[2:])
    else:
        cmd_summarize(sys.argv[1:])


if __name__ == "__main__":
    main()
