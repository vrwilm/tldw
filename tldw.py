#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""TLDW — paste a YouTube URL, get a summary."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx

BUILTIN_MODEL = "google/gemini-2.5-flash-lite"

CONFIG = Path(
    os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
) / "tldw" / "config.json"


def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict) -> None:
    """Best-effort. A tool that can't remember is fine; one that crashes isn't."""
    try:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps(cfg, indent=2))
    except OSError:
        pass

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


def fetch(url: str) -> tuple[str, str]:
    """Return (title, transcript) for a YouTube URL."""
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(
            [
                "yt-dlp",
                "--skip-download",
                # --print implies --simulate, which silently suppresses subtitle
                # writing. --no-simulate turns that back off.
                "--no-simulate",
                "--write-subs",
                "--write-auto-subs",
                # Deliberately narrow: "en.*" also pulls every machine-translated
                # en-XX track, which trips YouTube's rate limiter.
                "--sub-langs", "en,en-orig,en-US,en-GB",
                "--sub-format", "vtt",
                "--print", "title",
                "--no-warnings",
                "-o", f"{tmp}/sub",
                url,
            ],
            capture_output=True,
            text=True,
        )

        title = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else "(unknown)"
        vtts = sorted(Path(tmp).glob("*.vtt"))
        # A nonzero exit on one subtitle track doesn't matter if another landed.
        if not vtts:
            err = proc.stderr.strip() or "No English subtitles available for this video."
            sys.exit(err)
        return title, parse_vtt(vtts[0].read_text(encoding="utf-8"))


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


def via_openrouter(prompt: str, model: str) -> None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        sys.exit("OPENROUTER_API_KEY not set. Get one at https://openrouter.ai/keys")

    with httpx.stream(
        "POST",
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
        },
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
                delta = json.loads(payload)["choices"][0]["delta"].get("content")
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
            if delta:
                print(delta, end="", flush=True)
    print()


def via_claude(prompt: str) -> None:
    if not shutil.which("claude"):
        sys.exit("`claude` CLI not found on PATH.")
    # Inherit stdout so output streams as the CLI produces it.
    rc = subprocess.run(["claude", "-p", prompt]).returncode
    if rc != 0:
        sys.exit(rc)


def main() -> None:
    ap = argparse.ArgumentParser(description="Summarize a YouTube video.")
    ap.add_argument("url")
    ap.add_argument(
        "--backend", choices=["openrouter", "claude"], default=None,
        help="openrouter = pay-per-token, any model. claude = Claude Code subscription.",
    )
    ap.add_argument("--model", default=None, help="OpenRouter model slug.")
    args = ap.parse_args()

    # Precedence: explicit flag > env var > last used > builtin.
    cfg = load_config()
    backend = args.backend or os.environ.get("TLDW_BACKEND") or cfg.get("backend") or "openrouter"
    model = args.model or os.environ.get("TLDW_MODEL") or cfg.get("model") or BUILTIN_MODEL

    print("Fetching transcript...", file=sys.stderr)
    title, transcript = fetch(args.url)
    if len(transcript) < 200:
        sys.exit("Transcript too short to summarize.")

    label = model if backend == "openrouter" else "claude -p"
    print(f"Summarizing ({label}): {title}\n", file=sys.stderr)
    prompt = PROMPT.format(title=title, transcript=transcript)

    if backend == "openrouter":
        via_openrouter(prompt, model)
    else:
        via_claude(prompt)

    # Persist only what was chosen by an explicit flag, and only after the run
    # succeeded. Env vars stay transient (a one-off TLDW_MODEL=... shouldn't
    # rewrite the saved default), and a typo'd slug can't poison future runs.
    if args.backend or args.model:
        save_config({
            "backend": args.backend or cfg.get("backend") or backend,
            "model": args.model or cfg.get("model") or model,
        })


if __name__ == "__main__":
    main()
