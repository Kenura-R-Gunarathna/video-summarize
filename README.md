# video-summarize

Summarize a YouTube video into Markdown with snapshot frames.

Pipeline: `yt-dlp` (video + subtitles + chapters) → `claude -p` (summary as JSON) → `yt-dlp` 2s clips + `ffmpeg` (one frame per section) → `summary.md`.

## Requirements

- [uv](https://docs.astral.sh/uv/) (Python >= 3.14)
- [ffmpeg](https://ffmpeg.org/)
- [Claude Code](https://claude.com/claude-code) CLI (`claude`) on your PATH
- `deno` or `node` (YouTube signature challenges)

## Usage

```sh
uv run vsum.py <youtube-url> [--out output] [--model sonnet] [--lang en]
```

Output is written to `output/` (git-ignored).
