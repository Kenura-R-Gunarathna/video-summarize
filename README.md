# video-summarize

Summarize a YouTube video into Markdown with snapshot frames.

Pipeline: `yt-dlp` (audio + chapters) → `faster-whisper` (local transcript) → `claude -p` (summary as JSON) → `yt-dlp` 2s clips + `ffmpeg` (one frame per section) → `summary.md`.

## Requirements

- [uv](https://docs.astral.sh/uv/) (Python >= 3.14)
- [ffmpeg](https://ffmpeg.org/)
- [Claude Code](https://claude.com/claude-code) CLI (`claude`) on your PATH
- `deno` or `node` (YouTube signature challenges)

## Usage

```sh
uv run vsum.py <youtube-url> [--out output] [--model sonnet] [--lang en] [--whisper small] [--use-subs]
```

By default the audio is downloaded and transcribed locally with Whisper (CPU, int8). `--whisper` picks the model size (`tiny`/`base`/`small`/`medium`/`large-v3`; models download on first use). `--use-subs` skips transcription and uses YouTube's subtitles instead.

Output is written to `output/` (git-ignored).
