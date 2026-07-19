# tldw

**Too Long; Didn't Watch.** Paste a YouTube URL, get a summary in your terminal.

```console
$ tldw 'https://www.youtube.com/watch?v=8S0FDjFBj8o'
Fetching transcript...
Summarizing (google/gemini-2.5-flash-lite): How to sound smart in your TEDx Talk | Will Stephen | TEDxNewYork

A comedic TEDx talk where the speaker admits he has nothing to say, then
demonstrates every rhetorical trick used to *sound* smart while saying nothing.

- Opens by declaring he has nothing researched or inspirational to offer.
- Demonstrates each trick live, naming it as he does it: deliberate hand
  gestures, a rhetorical audience question, a relatable personal anecdote.
- Recites meaningless statistics and shows charts with admittedly irrelevant
  data — credible-looking with the sound off.
- Descends into literal gibberish while gesticulating and building intensity.
- Removes his (fake, plain-frame) glasses to fake a climactic moment.
```

It pulls the video's captions with `yt-dlp`, strips the timestamps, and sends the
text to an LLM. No API-heavy scraping, no Whisper transcription, no browser.

One file, no install step. Summarizing a two-hour video costs about **$0.002** on
the default model — or nothing at all on the `claude` backend. Ask it follow-up
questions with `-c`, or come back to any video later with `tldw ask`.

## Install

Requires [`uv`](https://docs.astral.sh/uv/) and [`yt-dlp`](https://github.com/yt-dlp/yt-dlp):

```bash
# Arch
sudo pacman -S yt-dlp
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then clone and symlink it onto your `PATH`:

```bash
git clone https://github.com/vrwilm/tldw.git ~/code/tldw
ln -s ~/code/tldw/tldw.py ~/.local/bin/tldw
```

There's no install step for Python packages — the script declares its own
dependencies inline ([PEP 723](https://peps.python.org/pep-0723/)) and `uv`
resolves them on first run.

Finally, set a key for whichever backend you use (see below).

## Usage

```bash
tldw 'https://www.youtube.com/watch?v=...'                      # default model
tldw 'https://...' --model deepseek/deepseek-v4-flash           # pick a model
tldw 'https://...' --backend claude                             # no API key
TLDW_MODEL=openai/gpt-5-nano tldw 'https://...'                 # one-off override
```

## Follow-up questions

A summary always leaves something out. Pass `-c` to keep the conversation open
once it's printed:

```console
$ tldw -c 'https://www.youtube.com/watch?v=...'
... summary ...

Ask a follow-up (Ctrl-D or empty line to quit).

> Name the single prop he uses.
Glasses (frames).

> Is it real?
No, they are just frames.
```

Or come back to it later — every transcript is cached, so `ask` reopens the
most recent video from any directory, in any terminal, days afterwards:

```bash
tldw ask 'What were the exact numbers he cited?'   # one-shot answer
tldw ask                                           # interactive session
```

`-c` deliberately does nothing when output is piped or redirected, so the
default stays scriptable:

```bash
tldw 'https://...' > summary.md     # never blocks waiting for input
```

## Caching

Transcripts don't change, so each one is stored under `~/.cache/tldw/<video-id>.json`
alongside its summary. Re-running a video you've already seen skips `yt-dlp`
entirely — roughly two seconds instead of the usual fetch — and `tldw ask` reads
straight from there.

The transcript is written *before* the model is called, so if the summary fails
(dead wifi, bad model name, rate limit) the fetch isn't wasted — the retry reads
from cache. Fetching is the slow, rate-limited half; the summary is the cheap half.

```bash
tldw 'https://...' --fresh    # ignore the cache and re-fetch
rm -rf ~/.cache/tldw          # clear everything
```

Nothing is ever evicted. Entries are small (tens of KB), but the directory grows
forever; delete it whenever you like.

## Backends

| Backend | Setup | Cost | Notes |
|---|---|---|---|
| `openrouter` *(default)* | `export OPENROUTER_API_KEY=...` | ~$0.002 / 2h video | Any model, streamed output |
| `claude` | [Claude Code](https://claude.com/product/claude-code) installed and logged in | Included in subscription | Slower; consumes your rate limits |

> **Note on the `claude` backend:** it shells out to `claude -p`, which inherits
> your global `~/.claude/CLAUDE.md`. Personal instructions in that file will bleed
> into the summary's tone. The `openrouter` backend has no such contamination.

### Picking a model

Any [OpenRouter model](https://openrouter.ai/models) works. Transcripts are long,
so favour cheap models with large context windows:

| Model | $/M input | Context |
|---|---|---|
| `google/gemini-2.5-flash-lite` *(default)* | 0.10 | 1M |
| `openai/gpt-5-nano` | 0.05 | 400k |
| `deepseek/deepseek-v4-flash` | 0.098 | 1M |
| `qwen/qwen3.5-flash-02-23` | 0.065 | 1M |

## Remembering your choice

Pass `--model` or `--backend` once and it sticks, so later runs need only the URL.
Settings resolve highest-priority first:

| Source | Example | Persisted? |
|---|---|---|
| Command-line flag | `--model X` | ✅ saved |
| Environment variable | `TLDW_MODEL=X` | ❌ this run only |
| Saved config | `~/.config/tldw/config.json` | — |
| Built-in default | `google/gemini-2.5-flash-lite` | — |

Two deliberate rules keep the implicit state from biting you:

- **Only successful runs are saved.** A typo'd model slug fails and leaves your
  config untouched, instead of wedging every future invocation.
- **Environment variables never persist.** A one-off `TLDW_MODEL=... tldw ...`
  shouldn't silently rewrite your default.

Every run prints the model it used to stderr, so the active setting is never a
mystery. The config is plain JSON — edit or `rm` it freely.

## Limitations

- **English only.** It requests `en` caption tracks and gives up if none exist.
- **Captions required.** No audio download, no Whisper fallback. Videos with
  captions disabled won't work.
- **Caption quality varies.** Human-written captions are preferred when a video
  has them; otherwise it falls back to YouTube's auto-generated track, which
  means missing punctuation, mangled proper nouns and no speaker labels.
  Summaries inherit those flaws.
- **Very long videos** are sent as a single request. Fine within a 1M-token
  context window, but a small-context model will reject a multi-hour transcript.

## License

MIT — see [LICENSE](LICENSE).
