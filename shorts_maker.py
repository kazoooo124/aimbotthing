#!/usr/bin/env python3
"""shorts_maker.py - turn a gameplay recording into ready-to-post vertical shorts.

What it does (all automatic):
  1. Finds the loudest / most hyped moments in your recording (or you pick the time).
  2. Cuts them out as 9:16 videos (1080x1920): gameplay in the middle over a
     blurred copy of itself, or a centre crop.
  3. Adds a hook title on top (optional).
  4. Adds big captions from your voice (optional, needs faster-whisper).
  5. Evens out the volume and exports an mp4 that YouTube / TikTok / Reels accept.

You bring the footage. It does not invent any - use your OWN gameplay.

Needs: Python 3.8+ and ffmpeg (ffmpeg + ffprobe must work in a terminal).
Optional captions: pip install faster-whisper

Examples:
  python shorts_maker.py gameplay.mp4
  python shorts_maker.py gameplay.mp4 --count 5 --duration 25 --title "WAIT FOR IT"
  python shorts_maker.py gameplay.mp4 --start 125 --duration 30 --captions
  python shorts_maker.py gameplay.mp4 --layout crop --crop-x 0.3
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

W, H = 1080, 1920


def run(cmd, cwd=None):
    """Run a command, die with a readable message if it fails."""
    try:
        return subprocess.run(cmd, check=True, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", cwd=cwd)
    except FileNotFoundError:
        sys.exit(f"'{cmd[0]}' not found. Install ffmpeg and make sure it is on your PATH.")
    except subprocess.CalledProcessError as e:
        tail = "\n".join((e.stderr or "").strip().splitlines()[-15:])
        sys.exit(f"{cmd[0]} failed:\n{tail}")


def probe_duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "json", path]).stdout
    return float(json.loads(out)["format"]["duration"])


def loudness_per_second(path):
    """Average power for every second of audio (linear scale, 0 = silence)."""
    af = ("aresample=8000,asetnsamples=n=8000:p=0,astats=metadata=1:reset=1,"
          "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-")
    out = run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-vn",
               "-af", af, "-f", "null", "-"]).stdout
    levels = []
    for m in re.finditer(r"RMS_level=(-?[\d.]+|-inf|inf|nan)", out):
        try:
            db = float(m.group(1))
        except ValueError:
            db = float("-inf")
        levels.append(0.0 if db == float("-inf") or db != db else 10 ** (db / 10))
    return levels


def pick_windows(levels, total, dur, count):
    """Greedy: best `count` non-overlapping windows of `dur` seconds by average loudness."""
    dur_i = max(1, int(round(dur)))
    if not levels or len(levels) <= dur_i:
        return [0.0]
    # prefix sums so every window is O(1)
    pre = [0.0]
    for v in levels:
        pre.append(pre[-1] + v)
    scores = [(pre[i + dur_i] - pre[i], i) for i in range(len(levels) - dur_i + 1)]
    scores.sort(reverse=True)
    chosen = []
    for _, i in scores:
        if all(abs(i - j) >= dur_i for j in chosen):
            chosen.append(i)
        if len(chosen) == count:
            break
    return sorted(float(i) for i in chosen)


# ---------------------------------------------------------------- text / captions

def ass_time(t):
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def ass_escape(text):
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_ass(duration, title, words, layout):
    # Where captions go: under the gameplay (blur layout) or lower-middle (crop layout).
    if layout == "blur":
        cap_align, cap_margin = 8, 1330      # top-anchored, just below the video
    else:
        cap_align, cap_margin = 2, 380       # bottom-anchored
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}",
        "WrapStyle: 0", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Hook,Arial,84,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,2,8,60,60,230,1",
        f"Style: Cap,Arial,100,&H0000FFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,8,2,{cap_align},60,60,{cap_margin},1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if title:
        lines.append(f"Dialogue: 0,{ass_time(0)},{ass_time(duration)},Hook,,0,0,0,,{ass_escape(title.upper())}")
    # group words into short punchy chunks (max 3 words, break on pauses)
    chunk, chunks = [], []
    for w in words:
        if chunk and (len(chunk) >= 3 or w[0] - chunk[-1][1] > 0.7):
            chunks.append(chunk)
            chunk = []
        chunk.append(w)
    if chunk:
        chunks.append(chunk)
    for i, c in enumerate(chunks):
        start = c[0][0]
        end = chunks[i + 1][0][0] if i + 1 < len(chunks) and chunks[i + 1][0][0] - c[-1][1] < 0.3 else c[-1][1] + 0.15
        text = ass_escape(" ".join(x[2] for x in c).upper())
        lines.append(f"Dialogue: 1,{ass_time(start)},{ass_time(min(end, duration))},Cap,,0,0,0,,{text}")
    return "\n".join(lines) + "\n"


def transcribe_words(src, start, dur, tmpdir, model_size):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("  (captions skipped: run  pip install faster-whisper  to enable them)")
        return []
    wav = os.path.join(tmpdir, "speech.wav")
    run(["ffmpeg", "-y", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", src,
         "-vn", "-ac", "1", "-ar", "16000", wav])
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(wav, word_timestamps=True, vad_filter=True)
    return [(w.start, w.end, w.word.strip()) for s in segments for w in (s.words or [])
            if w.word.strip()]


# ---------------------------------------------------------------- rendering

def render(src, start, dur, out, args):
    dur = min(dur, probe_duration(src) - start)
    with tempfile.TemporaryDirectory() as tmp:
        words = transcribe_words(src, start, dur, tmp, args.whisper_model) if args.captions else []
        use_ass = bool(args.title) or bool(words)
        if use_ass:
            with open(os.path.join(tmp, "text.ass"), "w", encoding="utf-8") as f:
                f.write(build_ass(dur, args.title, words, args.layout))

        if args.layout == "blur":
            graph = (
                "[0:v]split=2[a][b];"
                # small blurry background, scaled back up: fast and extra smooth
                "[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
                f"boxblur=10:2,scale={W}:{H},eq=brightness=-0.12[bg];"
                f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease:force_divisible_by=2[fg];"
                "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v0]"
            )
        else:
            graph = (
                f"[0:v]scale=-2:{H},crop={W}:{H}:(iw-{W})*{args.crop_x}:0,setsar=1[v0]"
            )
        graph += ";[v0]ass=text.ass[v]" if use_ass else ";[v0]null[v]"

        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", os.path.abspath(src),
               "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
               "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
               "-c:v", "libx264", "-preset", "medium", "-crf", "20",
               "-pix_fmt", "yuv420p", "-r", "30",
               "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
               "-movflags", "+faststart", os.path.abspath(out)]
        # cwd=tmp so the subtitle file can be referenced by a plain name (no path escaping)
        run(cmd, cwd=tmp)


def main():
    p = argparse.ArgumentParser(description="Turn gameplay into vertical shorts.",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="Examples:" + __doc__.split("Examples:")[1])
    p.add_argument("video", help="your gameplay recording")
    p.add_argument("-o", "--output", help="output file (default: <name>_short.mp4)")
    p.add_argument("--start", type=float, help="start time in seconds (skip auto-detect)")
    p.add_argument("--duration", type=float, default=30, help="length of each short, seconds (default 30)")
    p.add_argument("--count", type=int, default=1, help="how many shorts to cut from the video (default 1)")
    p.add_argument("--title", help="hook text shown at the top")
    p.add_argument("--captions", action="store_true", help="auto captions (needs faster-whisper)")
    p.add_argument("--whisper-model", default="base", help="captions model: tiny/base/small (default base)")
    p.add_argument("--layout", choices=["blur", "crop"], default="blur",
                   help="blur = full gameplay over blurred background, crop = fill the screen")
    p.add_argument("--crop-x", type=float, default=0.5,
                   help="crop layout only: 0 = left edge, 0.5 = centre, 1 = right edge")
    args = p.parse_args()

    if not os.path.isfile(args.video):
        sys.exit(f"File not found: {args.video}")
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"{tool} not found. Install ffmpeg and make sure it is on your PATH.")

    total = probe_duration(args.video)
    if args.start is not None:
        starts = [args.start]
    else:
        print("Listening for the loudest moments...")
        starts = pick_windows(loudness_per_second(args.video), total, args.duration, args.count)

    base = os.path.splitext(args.output or args.video)[0]
    for n, start in enumerate(starts, 1):
        out = args.output if args.output and len(starts) == 1 else \
            f"{base}_short{n if len(starts) > 1 else ''}.mp4"
        print(f"[{n}/{len(starts)}] {start:.0f}s -> {out}")
        render(args.video, start, args.duration, out, args)
    print("Done. Watch them before posting - the tool finds LOUD, you decide what's GOOD.")


if __name__ == "__main__":
    main()
