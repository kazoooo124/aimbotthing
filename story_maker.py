#!/usr/bin/env python3
"""story_maker.py - turn a plain-text script into a finished "documentary style" video.

You write the scenes in a text file. It makes the video:
  * moving dark background (or YOUR image with a slow zoom), film grain, vignette
  * text that pops in, with *highlighted* words
  * counting-number animations (0 -> 10,900 m) for that motion-graphics look
  * sound effects (hit, whoosh, riser, tick, glitch) - synthesized by the script itself,
    so there is nothing to download and nothing to get copyright-struck for
  * a dark ambient music bed (or your own music file)
  * optional voiceover: your recordings per scene, or text-to-speech (--tts)

Needs: Python 3.8+ and ffmpeg. Voiceover via TTS also needs:  pip install edge-tts

Script format (blank line between scenes, see examples/deep_ocean.txt):

    # palette: abyss          <- header lines: palette (abyss/blood/toxic/mono),
    # music: drone               music (drone / none / path to a file)

    THE OCEAN IS *DEEPER*     <- text; *stars* make a word glow in the accent colour
    THAN YOU THINK
    @sfx hit                  <- sound effect(s) at scene start: hit whoosh riser tick glitch none
    @time 3                   <- seconds on screen (default: based on how much text)
    @image titanic.jpg        <- optional background image of YOUR choice
    @counter 0 3800 m         <- optional counting number: from to unit
    @audio line3.mp3          <- optional voiceover file for this scene

Usage:
    python story_maker.py examples/deep_ocean.txt
    python story_maker.py my_story.txt --size wide -o my_video.mp4
    python story_maker.py my_story.txt --tts en-US-GuyNeural
"""
import argparse
import array
import math
import os
import random
import re
import struct
import subprocess
import sys
import tempfile
import wave

SR = 44100
FPS = 30
SIZES = {"vertical": (1080, 1920), "wide": (1920, 1080)}
# palette -> (gradient colour 1, gradient colour 2, accent colour in ASS BGR)
PALETTES = {
    "abyss": ("0x03101f", "0x0b4a5c", "&H00FFE600&"),
    "blood": ("0x140000", "0x520808", "&H003C3CFF&"),
    "toxic": ("0x051005", "0x0f4a16", "&H0000FF78&"),
    "mono": ("0x080808", "0x303030", "&H0000D6FF&"),
}
WHITE = "&H00FFFFFF&"


def run(cmd, cwd=None):
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
               "-of", "default=nw=1:nk=1", path]).stdout
    return float(out.strip())


# ------------------------------------------------------------------ script parsing

def parse_script(text):
    meta, scenes = {}, []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        if all(l.startswith("#") for l in lines):
            for l in lines:
                k, _, v = l.lstrip("# ").partition(":")
                meta[k.strip().lower()] = v.strip()
            continue
        sc = {"text": []}
        for l in lines:
            if l.startswith("@"):
                k, _, v = l[1:].partition(" ")
                sc[k.strip().lower()] = v.strip()
            elif not l.startswith("#"):
                sc["text"].append(l)
        scenes.append(sc)
    return meta, scenes


def plain(sc):
    return " ".join(sc["text"]).replace("*", "")


# ------------------------------------------------------------------ sound design

def write_wav(path, samples):
    peak = max(1e-9, max(abs(s) for s in samples))
    gain = min(1.0, 0.9 / peak) if peak > 0.9 else 1.0
    data = array.array("h", (int(max(-1, min(1, s * gain)) * 32767) for s in samples))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data.tobytes())


def synth_sfx(name, rnd):
    out = []
    if name == "hit":                       # deep cinematic impact
        ph = 0.0
        for i in range(int(SR * 1.5)):
            t = i / SR
            ph += 2 * math.pi * (38 + 130 * math.exp(-t * 14)) / SR
            body = math.sin(ph) * math.exp(-t * 3.0)
            click = (rnd.random() * 2 - 1) * math.exp(-t * 45) * 0.7
            out.append(math.tanh(1.4 * (body + click)))
    elif name == "whoosh":                  # filtered noise sweeping up and down
        dur, y = 0.9, 0.0
        for i in range(int(SR * dur)):
            p = i / SR / dur
            fc = 250 + 3800 * math.sin(math.pi * p) ** 2
            y += (1 - math.exp(-2 * math.pi * fc / SR)) * ((rnd.random() * 2 - 1) - y)
            out.append(math.tanh(3.0 * y * math.sin(math.pi * p) ** 2))
    elif name == "riser":                   # tension builder
        dur, y, ph = 2.2, 0.0, 0.0
        for i in range(int(SR * dur)):
            p = i / SR / dur
            fc = 150 + 6000 * p ** 2
            y += (1 - math.exp(-2 * math.pi * fc / SR)) * ((rnd.random() * 2 - 1) - y)
            ph += 2 * math.pi * (110 + 700 * p ** 2) / SR
            out.append(math.tanh(2.5 * y * p ** 2 + 0.35 * math.sin(ph) * p))
    elif name == "tick":                    # small UI click
        for i in range(int(SR * 0.15)):
            t = i / SR
            out.append(math.sin(2 * math.pi * 1900 * t) * math.exp(-t * 55) * 0.8
                       + (rnd.random() * 2 - 1) * math.exp(-t * 120) * 0.3)
    elif name == "glitch":                  # digital stutter
        while len(out) < int(SR * 0.4):
            seg = int(SR * rnd.uniform(0.02, 0.06))
            f = rnd.choice([220, 440, 880, 1320, 90])
            gate = rnd.random() > 0.3
            for i in range(seg):
                v = 1.0 if math.sin(2 * math.pi * f * i / SR) > 0 else -1.0
                out.append(v * 0.35 if gate else (rnd.random() * 2 - 1) * 0.1)
    else:
        sys.exit(f"Unknown sfx '{name}'. Use: hit whoosh riser tick glitch none")
    return out


def synth_drone(dur=8.0):
    """Dark ambient pad. Every frequency completes whole cycles in 8 s, so it loops seamlessly."""
    out = []
    for i in range(int(SR * dur)):
        t = i / SR
        lfo = 0.5 + 0.5 * math.sin(2 * math.pi * 0.25 * t)
        s = (0.55 * math.sin(2 * math.pi * 55 * t)
             + 0.35 * math.sin(2 * math.pi * 82.5 * t + lfo)
             + 0.18 * math.sin(2 * math.pi * 110.25 * t)
             + 0.07 * lfo * math.sin(2 * math.pi * 440.5 * t))
        out.append(s * (0.7 + 0.3 * lfo))
    return out


def tts(text, voice, out):
    try:
        import asyncio
        import edge_tts
    except ImportError:
        sys.exit("--tts needs a small install first:  pip install edge-tts")
    try:
        asyncio.run(edge_tts.Communicate(text, voice).save(out))
    except Exception as e:  # network / bad voice name
        sys.exit(f"Text-to-speech failed ({e}). Check your internet and the voice name.")


# ------------------------------------------------------------------ subtitles / text animation

def ass_time(t):
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def ass_text(line, accent):
    line = line.replace("\\", "").replace("{", "(").replace("}", ")")
    return re.sub(r"\*(.+?)\*", lambda m: "{\\c%s}%s{\\c%s}" % (accent, m.group(1), WHITE), line)


def build_ass(sc, d, W, H, accent):
    vertical = H > W
    fs = 96 if vertical else 80
    big = 210 if vertical else 170
    cx = W // 2
    counter = sc.get("counter")
    ty = int(H * (0.66 if counter else 0.5))
    cy = int(H * 0.34)
    head = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Main,Arial,{fs},{WHITE},&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,2,0,1,6,3,5,90,90,80,1",
        f"Style: Big,Arial,{big},{accent},&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,2,0,1,7,3,5,40,40,40,1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    ev = []
    body = "\\N".join(ass_text(l, accent) for l in sc["text"])
    if body:
        pop = (f"{{\\an5\\pos({cx},{ty})\\fad(220,150)\\fscx88\\fscy88"
               f"\\t(0,380,\\fscx100\\fscy100)}}")
        ev.append(f"Dialogue: 1,{ass_time(0)},{ass_time(d)},Main,,0,0,0,,{pop}{body}")
    if counter:
        parts = counter.split()
        try:
            a, b = float(parts[0]), float(parts[1])
        except (IndexError, ValueError):
            sys.exit(f"Bad @counter '{counter}'. Use:  @counter 0 3800 m")
        unit = " ".join(parts[2:])
        T, t = max(0.8, d * 0.75), 0.0
        while t < d:
            p = min(1.0, t / T)
            v = a + (b - a) * (1 - (1 - p) ** 3)       # ease-out: fast start, slow finish
            shown = f"{int(round(v)):,}"
            small = f"{{\\fs{big // 2}}} {unit}" if unit else ""
            ev.append(f"Dialogue: 2,{ass_time(t)},{ass_time(min(d, t + 0.05))},Big,,0,0,0,,"
                      f"{{\\an5\\pos({cx},{cy})}}{shown}{small}")
            t += 0.05
    return "\n".join(head + ev) + "\n"


# ------------------------------------------------------------------ rendering

def render_scene(i, sc, d, W, H, pal, tmp, sfx_paths, voice):
    c0, c1, accent = pal
    with open(os.path.join(tmp, f"s{i}.ass"), "w", encoding="utf-8") as f:
        f.write(build_ass(sc, d, W, H, accent))

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    img = sc.get("image")
    if img:
        if not os.path.isfile(img):
            sys.exit(f"Scene {i + 1}: image not found: {img}")
        cmd += ["-loop", "1", "-framerate", str(FPS), "-t", f"{d:.2f}", "-i", os.path.abspath(img)]
        big_w, big_h = int(W * 1.5), int(H * 1.5)
        coef = 0.16 / max(1, d * FPS)
        z = f"min(1.3,1+{coef:.6f}*on)" if i % 2 == 0 else f"max(1.0,1.16-{coef:.6f}*on)"
        bg = (f"[0:v]scale={big_w}:{big_h}:force_original_aspect_ratio=increase,crop={big_w}:{big_h},"
              f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={FPS},"
              f"eq=brightness=-0.10:saturation=0.85,drawbox=0:0:iw:ih:color=black@0.32:t=fill")
    else:
        cmd += ["-f", "lavfi", "-t", f"{d:.2f}", "-i",
                f"gradients=s={W}x{H}:c0={c0}:c1={c1}:x0=0:y0=0:x1={W}:y1={H}:speed=0.02:rate={FPS}"]
        bg = "[0:v]null"
    cmd += ["-f", "lavfi", "-t", f"{d:.2f}", "-i", "anullsrc=r=44100:cl=stereo"]

    fmt = "aformat=sample_rates=44100:channel_layouts=stereo"
    achain, labels, n = [f"[1:a]{fmt}[sil]"], ["[sil]"], 2
    for name in sc["_sfx"]:
        cmd += ["-i", sfx_paths[name]]
        achain.append(f"[{n}:a]{fmt},volume=0.9[x{n}]")
        labels.append(f"[x{n}]")
        n += 1
    if voice:
        cmd += ["-i", voice]
        achain.append(f"[{n}:a]{fmt},adelay=250:all=1[x{n}]")
        labels.append(f"[x{n}]")
        n += 1
    achain.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=first:normalize=0,"
                  f"afade=t=out:st={max(0, d - 0.06):.2f}:d=0.06[a]")

    vf = (f"{bg},noise=alls=4:allf=t,vignette=angle=PI/4,"
          f"fade=t=in:st=0:d=0.12,fade=t=out:st={max(0, d - 0.12):.2f}:d=0.12,"
          f"ass=s{i}.ass,format=yuv420p[v]")
    out = os.path.join(tmp, f"scene{i}.mp4")
    cmd += ["-filter_complex", ";".join([vf] + achain), "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-r", str(FPS),
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-t", f"{d:.2f}", out]
    run(cmd, cwd=tmp)       # cwd=tmp so the .ass file is found by plain name (no path escaping)
    return out


def main():
    ap = argparse.ArgumentParser(description="Text script -> documentary-style video.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("Usage:")[0].split("Needs:")[0])
    ap.add_argument("script", help="your scenes, as a text file")
    ap.add_argument("-o", "--output", help="output mp4 (default: next to the script)")
    ap.add_argument("--size", choices=list(SIZES), default="vertical",
                    help="vertical = Shorts/TikTok (default), wide = normal YouTube")
    ap.add_argument("--music", help="your own music file (overrides '# music:' in the script)")
    ap.add_argument("--tts", metavar="VOICE", help="auto voiceover, e.g. en-US-GuyNeural, pl-PL-MarekNeural")
    args = ap.parse_args()

    if not os.path.isfile(args.script):
        sys.exit(f"File not found: {args.script}")
    with open(args.script, encoding="utf-8") as f:
        meta, scenes = parse_script(f.read())
    if not scenes:
        sys.exit("No scenes found. Separate scenes with a blank line.")

    W, H = SIZES[args.size]
    pal_name = meta.get("palette", "abyss").lower()
    if pal_name not in PALETTES:
        sys.exit(f"Unknown palette '{pal_name}'. Use: {', '.join(PALETTES)}")
    pal = PALETTES[pal_name]
    music = args.music or meta.get("music", "drone")
    out = args.output or os.path.splitext(args.script)[0] + ".mp4"
    script_dir = os.path.dirname(os.path.abspath(args.script))
    rnd = random.Random(7)          # same script -> same sounds every time

    with tempfile.TemporaryDirectory() as tmp:
        sfx_paths, durations, voices = {}, [], []
        for i, sc in enumerate(scenes):
            # resolve media paths relative to the script file
            for key in ("image", "audio"):
                if sc.get(key) and not os.path.isabs(sc[key]):
                    sc[key] = os.path.join(script_dir, sc[key])
            names = [n for n in re.split(r"[,\s]+", sc.get("sfx", "whoosh")) if n and n != "none"]
            sc["_sfx"] = names
            for n in names:
                if n not in sfx_paths:
                    sfx_paths[n] = os.path.join(tmp, f"sfx_{n}.wav")
                    write_wav(sfx_paths[n], synth_sfx(n, rnd))

            voice = sc.get("audio")
            if not voice and args.tts and plain(sc):
                voice = os.path.join(tmp, f"voice{i}.mp3")
                print(f"Voiceover {i + 1}/{len(scenes)}...")
                tts(plain(sc), args.tts, voice)
            voices.append(voice)
            if sc.get("time"):
                d = float(sc["time"])
            elif voice:
                d = probe_duration(voice) + 0.6
            else:
                d = max(2.2, 1.2 + 0.38 * len(plain(sc).split()))
            durations.append(d)

        parts = []
        for i, sc in enumerate(scenes):
            print(f"Scene {i + 1}/{len(scenes)} ({durations[i]:.1f}s)")
            parts.append(render_scene(i, sc, durations[i], W, H, pal, tmp, sfx_paths, voices[i]))

        total = sum(durations)
        print("Mixing music + final render...")
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        for p in parts:
            cmd += ["-i", p]
        bed = None
        if music != "none":
            if music == "drone":
                bed = os.path.join(tmp, "drone.wav")
                write_wav(bed, synth_drone())
                vol = 0.30
            else:
                bed = music if os.path.isabs(music) else os.path.join(script_dir, music)
                if not os.path.isfile(bed):
                    sys.exit(f"Music file not found: {bed}")
                vol = 0.25
            cmd += ["-stream_loop", "-1", "-i", bed]
        n = len(parts)
        cat = "".join(f"[{k}:v][{k}:a]" for k in range(n)) + f"concat=n={n}:v=1:a=1[v][a0]"
        if bed:
            af = (f"{cat};[{n}:a]aformat=sample_rates=44100:channel_layouts=stereo,volume={vol},"
                  f"afade=t=in:d=1.5[bed];[a0][bed]amix=inputs=2:duration=first:normalize=0,"
                  f"loudnorm=I=-14:TP=-1.5:LRA=11,afade=t=out:st={max(0, total - 1.0):.2f}:d=1.0[a]")
        else:
            af = f"{cat};[a0]loudnorm=I=-14:TP=-1.5:LRA=11[a]"
        cmd += ["-filter_complex", af, "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", "medium", "-crf", "23", "-maxrate", "8M",
                "-bufsize", "16M", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-t", f"{total:.2f}",
                os.path.abspath(out)]
        run(cmd)
    print(f"Done: {out}  ({total:.0f}s)")
    print("Double-check every fact in your script before you post it, and only use images/music you're allowed to.")


if __name__ == "__main__":
    main()
