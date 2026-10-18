#!/usr/bin/env -S uv run
"""Summarize a YouTube video into Markdown with snapshot frames.

Usage:
    uv run vsum.py <youtube-url> [--out output] [--model sonnet] [--lang en]

Pipeline: yt-dlp (video + subtitles + chapters) -> `claude -p` (summary as JSON)
-> yt-dlp 2s clips + ffmpeg (one frame per section) -> summary.md
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yt_dlp


def fmt_ts(seconds: float) -> str:
    s = int(seconds)
    h, m, s = s // 3600, (s % 3600) // 60, s % 60
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "video"


YDL_BASE = {
    # Only video is needed for snapshots; 720p keeps it small.
    "format": "bv*[height<=720]/b[height<=720]/b",
    # YouTube needs a JS runtime to solve its signature challenges (yt-dlp-ejs).
    "js_runtimes": {"deno": {}, "node": {}},
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
}


def fetch(url: str, workdir: Path, lang: str, download_video: bool) -> tuple[dict, Path | None, Path | None]:
    """Fetch metadata + subtitles (and the full video if asked). Returns (info, video, vtt)."""
    opts = {
        **YDL_BASE,
        "outtmpl": str(workdir / "video.%(ext)s"),
        "skip_download": not download_video,
        "writesubtitles": True,
        "writeautomaticsub": True,
        # Exact tracks only: a wildcard like "en.*" also pulls dozens of translated tracks (and 429s).
        "subtitleslangs": [f"{lang}-orig", lang, f"{lang}-US", f"{lang}-GB"],
        "subtitlesformat": "vtt",
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    video = None
    if download_video:
        video = next(p for p in workdir.glob("video.*") if p.suffix not in (".vtt", ".part"))
    # Prefer the original-language auto track, then manual/regional tracks.
    subs = sorted(workdir.glob("video*.vtt"), key=lambda p: ("-orig" not in p.name, p.name))
    return info, video, subs[0] if subs else None


def parse_vtt(path: Path) -> list[tuple[float, str]]:
    """Return [(start_seconds, text)], de-duplicating YouTube's rolling auto-captions."""
    cues, seen_last = [], ""
    ts_re = re.compile(r"(\d+):(\d+):(\d+)\.(\d+)\s+-->")
    start = None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = ts_re.match(line)
        if m:
            h, mi, s, ms = map(int, m.groups())
            start = h * 3600 + mi * 60 + s + ms / 1000
            continue
        if start is None or not line.strip() or line.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        text = re.sub(r"<[^>]+>", "", line).strip()
        if text and text != seen_last:
            cues.append((start, text))
            seen_last = text
    return cues


def chunk_transcript(cues: list[tuple[float, str]], every: int = 30) -> str:
    """Group cues into ~30s paragraphs prefixed with [mm:ss]."""
    out, bucket, bucket_start = [], [], None
    for start, text in cues:
        if bucket_start is None:
            bucket_start = start
        if start - bucket_start >= every and bucket:
            out.append(f"[{fmt_ts(bucket_start)}] {' '.join(bucket)}")
            bucket, bucket_start = [], start
        bucket.append(text)
    if bucket:
        out.append(f"[{fmt_ts(bucket_start)}] {' '.join(bucket)}")
    return "\n".join(out)


PROMPT = """You are summarizing a video from its transcript.

Title: {title}
Channel: {channel}
Duration: {duration}
{chapters_block}
Return ONLY a JSON object (no prose, no code fences) with this shape:
{{
  "executive_summary": "3-5 sentence overview",
  "key_takeaways": ["...", "..."],
  "sections": [
    {{"title": "Section title", "start_seconds": 0, "bullets": ["...", "..."]}}
  ]
}}

Rules:
- {section_rule}
- start_seconds must be an integer within the video duration, taken from the transcript timestamps.
- For sponsor/ad chapters (e.g. "Commercial break"), use a single bullet noting it is a sponsor segment.
- 2-5 concise, specific bullets per section. Use the speaker's concrete details, numbers and names.

Transcript:
{transcript}
"""


def summarize(info: dict, transcript: str, model: str) -> dict:
    chapters = info.get("chapters") or []
    if chapters:
        chapters_block = "Chapters (from the video):\n" + "\n".join(
            f"- [{fmt_ts(c['start_time'])}] {c['title']} (start_seconds={int(c['start_time'])})" for c in chapters
        ) + "\n"
        section_rule = "Create exactly one section per chapter listed above, using its title and start_seconds."
    else:
        chapters_block = ""
        section_rule = "Split the video into 4-10 logical sections at natural topic changes."

    prompt = PROMPT.format(
        title=info.get("title", ""),
        channel=info.get("channel") or info.get("uploader", ""),
        duration=fmt_ts(info.get("duration") or 0),
        chapters_block=chapters_block,
        section_rule=section_rule,
        transcript=transcript,
    )
    cmd = ["claude", "-p", "--output-format", "json", "--model", model]
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"claude -p failed:\n{proc.stderr or proc.stdout}")
    result = json.loads(proc.stdout).get("result", "")
    match = re.search(r"\{.*\}", result, re.DOTALL)
    if not match:
        sys.exit(f"Could not find JSON in Claude's response:\n{result}")
    return json.loads(match.group(0))


def download_clips(url: str, times: list[int], workdir: Path) -> dict[int, Path]:
    """Download a 2s clip at each time, so long videos never need a full download.

    (ffmpeg can't seek YouTube's stream URLs directly - they 403 - but yt-dlp's
    section downloader can.)
    """
    from yt_dlp.utils import download_range_func

    opts = {
        **YDL_BASE,
        "outtmpl": str(workdir / "clip_%(section_start)d.%(ext)s"),
        "download_ranges": download_range_func(None, [(t, t + 2) for t in sorted(set(times))]),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    return {int(p.stem.split("_")[1]): p for p in workdir.glob("clip_*") if p.suffix != ".part"}


def snapshot(video: Path, seconds: float, dest: Path) -> bool:
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", str(seconds), "-i", str(video),
        "-frames:v", "1", "-q:v", "3", "-vf", "scale='min(1280,iw)':-2",
        str(dest),
    ]
    return subprocess.run(cmd).returncode == 0 and dest.exists()


def build_markdown(info: dict, summary: dict, url: str, frames: dict[int, str]) -> str:
    title = info.get("title", "Video")
    lines = [
        f"# Video Summary: {title}",
        "",
        f"**Source:** [{title}]({url})  ",
        f"**Channel:** {info.get('channel') or info.get('uploader', '')}  ",
        f"**Duration:** {fmt_ts(info.get('duration') or 0)}",
        "",
        "## 📌 Executive Summary",
        "",
        summary.get("executive_summary", "").strip(),
        "",
    ]
    if summary.get("key_takeaways"):
        lines += ["## 🔑 Key Takeaways", ""]
        lines += [f"- {t}" for t in summary["key_takeaways"]]
        lines.append("")
    lines += ["## ⏱️ Visual Timeline & Chapters", ""]
    sep = "&" if "?" in url else "?"
    for i, sec in enumerate(summary.get("sections", []), 1):
        t = int(sec.get("start_seconds", 0))
        lines.append(f"### {i}. {sec['title']} [[{fmt_ts(t)}]({url}{sep}t={t}s)]")
        lines.append("")
        lines += [f"- {b}" for b in sec.get("bullets", [])]
        lines.append("")
        if i - 1 in frames:
            lines.append(f'![Snapshot at {fmt_ts(t)}]({frames[i - 1]} "{sec["title"]}")')
            lines.append("")
        lines += ["---", ""]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("--out", default="output", help="output root directory (default: output)")
    ap.add_argument("--model", default="sonnet", help="model passed to `claude -p` (default: sonnet)")
    ap.add_argument("--lang", default="en", help="subtitle language (default: en)")
    ap.add_argument("--download", action="store_true",
                    help="download the full video (kept in the output folder) instead of short clips")
    args = ap.parse_args()

    for tool in ("ffmpeg", "claude"):
        if not shutil.which(tool):
            sys.exit(f"`{tool}` not found on PATH")

    tmp = Path(args.out) / ".work"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)

    print("⬇️  Fetching metadata and subtitles…" if not args.download else "⬇️  Downloading video and subtitles…")
    info, video, vtt = fetch(args.url, tmp, args.lang, args.download)
    if not vtt:
        sys.exit(f"No '{args.lang}' subtitles (manual or auto) available for this video.")

    outdir = Path(args.out) / slugify(info.get("title", "video"))
    snapdir = outdir / "snapshots"
    snapdir.mkdir(parents=True, exist_ok=True)

    transcript = chunk_transcript(parse_vtt(vtt))
    (outdir / "transcript.md").write_text(f"# Transcript: {info.get('title', '')}\n\n" + transcript.replace("\n", "\n\n"))

    print(f"🧠 Summarizing with claude -p ({args.model})…")
    summary = summarize(info, transcript, args.model)

    print("📸 Capturing snapshots…")
    duration = info.get("duration") or 0
    sections = summary.get("sections", [])
    starts = [int(sec.get("start_seconds", 0)) for sec in sections]
    # Skip a few seconds past the boundary to avoid black/transition frames.
    at = {t: min(t + 3, max(duration - 2, 0)) if duration else t + 3 for t in starts}
    clips = None if video else download_clips(args.url, list(at.values()), tmp)

    def grab(i: int, t: int) -> tuple[int, str | None]:
        name = f"frame_{fmt_ts(t).replace(':', '_')}.jpg"
        src, seek = (video, at[t]) if video else (clips.get(at[t]), 0)
        ok = src is not None and snapshot(src, seek, snapdir / name)
        return i, f"./snapshots/{name}" if ok else None

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = pool.map(lambda a: grab(*a), enumerate(starts))
    frames = {i: path for i, path in results if path}

    md_path = outdir / "summary.md"
    md_path.write_text(build_markdown(info, summary, args.url, frames))

    if args.download:
        video.rename(outdir / video.name)
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"✅ Done: {md_path}")


if __name__ == "__main__":
    main()
