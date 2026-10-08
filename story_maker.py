#!/usr/bin/env python3
"""story_maker.py - turn a plain-text script into a finished "documentary style" video.

You write the scenes in a text file. It makes the video:
  * pictures on screen: your own images in a framed card with a slow zoom, a caption,
    and a pulsing circle that points at the thing you are explaining
  * ...or let it FIND free-to-use pictures for you (@search), with credits written to a file
  * animated charts (bars that grow, numbers that count up) to explain sizes and comparisons
  * text that pops in, with *highlighted* words
  * lots of effects: flash, shake, punch-in zoom, glitch, colour fringing, light leaks, flicker
  * sound effects (hit, whoosh, riser, tick, glitch) made by the script itself - nothing to
    download, nothing to get copyright-struck for
  * a dark ambient music bed (or your own music), optional voiceover (yours, or --tts)

Needs: Python 3.8+ and ffmpeg.   Text-to-speech also needs:  pip install edge-tts

Script format: blank line between scenes. See examples/deep_ocean.txt.

    # palette: abyss            <- header: palette (abyss / blood / toxic / mono)
    # music: drone                         music (drone / none / path to a file)
    # particles: yes                       floating dust/snow in text-only scenes (yes/no)

    THE OCEAN IS *DEEPER*       <- the words on screen; *stars* make a word glow
    THAN YOU THINK
    @time 3                     <- seconds on screen (default depends on the amount of text)
    @sfx hit                    <- sound(s) at scene start: hit whoosh riser tick glitch none
    @fx flash, shake            <- effects: flash shake punch glitch rgb leak flicker none
                                   (no @fx = automatic variety, none = clean)

    The Titanic wreck
    @image titanic.jpg          <- YOUR picture, shown in a framed card with a slow zoom
    @search titanic wreck       <- OR: fetch a free-to-use picture from Wikimedia Commons
    @caption RMS Titanic, 1986  <- small label on the picture
    @circle 0.62 0.40           <- pulsing circle at that spot of the picture (0-1 across, 0-1 down)
    @layout full                <- picture as full-screen background instead of a card

    Everest vs the deepest point
    @bars Everest:8849 | Challenger Deep:10935* | Titanic wreck:3800
                                <- animated bars (label:value, * highlights one)  @unit m
    @counter 0 3800 m           <- a big number that counts up
    @audio line3.mp3            <- voiceover recording for this scene

Usage:
    python story_maker.py examples/deep_ocean.txt
    python story_maker.py my_story.txt --size wide -o my_video.mp4
    python story_maker.py my_story.txt --tts en-US-GuyNeural
"""
import argparse
import array
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
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
GREY = "&H00C8C8C8&"
FX_NAMES = ("flash", "shake", "punch", "glitch", "rgb", "leak", "flicker")
AUTO_FX = [["flash"], ["punch"], ["glitch"], ["shake", "punch"], ["punch", "rgb"], ["flash", "rgb"]]
COMMONS_API = os.environ.get("STORY_MAKER_COMMONS_API", "https://commons.wikimedia.org/w/api.php")
USER_AGENT = "story_maker/2.0 (hobby video script; https://github.com/)"


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


def parse_bars(spec):
    """'Everest:8849 | Challenger Deep:10935* | Titanic:3800' -> [(label, value, highlight)]"""
    bars = []
    for part in spec.split("|"):
        label, _, val = part.rpartition(":")
        label, val = label.strip(), val.strip()
        hl = val.endswith("*") or label.startswith("*")
        try:
            v = float(val.rstrip("*").replace(",", ""))
        except ValueError:
            sys.exit(f"Bad @bars entry '{part.strip()}'. Use  Label:number  separated by |")
        bars.append((label.strip("*").strip(), v, hl))
    if not bars:
        sys.exit("@bars needs at least one  Label:number")
    return bars


# ------------------------------------------------------------------ free pictures (Wikimedia Commons)

def license_ok(name):
    """Only licenses that are simple to follow: public domain, CC0, or plain CC BY (credit the author).
    Rejects share-alike, non-commercial, no-derivatives and anything unknown."""
    n = (name or "").strip().lower()
    return bool(re.match(r"^(public domain|cc0|pd[- ]|pd$|cc[- ]by[- ]\d)", n)) and not re.search(r"-(sa|nc|nd)\b", n)


def _strip_tags(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s or "")).strip()


def fetch_image(query, cache_dir, credits):
    """Find a free-to-use picture on Wikimedia Commons, download it, record who made it."""
    os.makedirs(cache_dir, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "_", query.lower()).strip("_")[:60] or "image"
    for ext in (".jpg", ".png"):
        if os.path.isfile(os.path.join(cache_dir, slug + ext)):
            return os.path.join(cache_dir, slug + ext)          # already downloaded earlier
    params = urllib.parse.urlencode({
        "action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
        "gsrsearch": f"{query} filetype:bitmap", "gsrlimit": 10, "prop": "imageinfo",
        "iiprop": "url|mime|extmetadata|size", "iiurlwidth": 1800})
    req = urllib.request.Request(f"{COMMONS_API}?{params}", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"  (could not search pictures for '{query}': {e})")
        return None
    pages = sorted((data.get("query", {}).get("pages") or {}).values(), key=lambda p: p.get("index", 99))
    for p in pages:
        info = (p.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}
        lic = (meta.get("LicenseShortName", {}).get("value") or "").strip()
        mime = info.get("mime", "")
        if not license_ok(lic):
            continue                                              # unknown or restrictive license
        if mime not in ("image/jpeg", "image/png") or info.get("width", 0) < 800:
            continue
        url = info.get("thumburl") or info.get("url")
        ext = ".png" if mime == "image/png" else ".jpg"
        path = os.path.join(cache_dir, slug + ext)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=60) as r, \
                    open(path, "wb") as f:
                f.write(r.read())
        except (urllib.error.URLError, OSError) as e:
            print(f"  (download failed for '{query}': {e})")
            continue
        who = _strip_tags(meta.get("Artist", {}).get("value")) or "unknown author"
        line = f"{p.get('title', slug)} | {who} | {lic} | {info.get('descriptionurl', '')}"
        if line not in credits:
            credits.append(line)
        print(f"  picture for '{query}': {p.get('title', '')} ({lic})")
        return path
    print(f"  (no free-to-use picture found for '{query}' - add one yourself with @image)")
    return None


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


# ------------------------------------------------------------------ layout + subtitles / animation

def geometry(W, H):
    """Where the picture card and the text go."""
    if H > W:   # vertical
        return {"card": (60, 230, 960, 720), "text": (W // 2, 1290), "side": False, "fs": 96, "big": 210}
    return {"card": (90, 200, 1040, 640), "text": (1530, 540), "side": True, "fs": 80, "big": 170}


def ass_time(t):
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def ass_text(line, accent):
    line = line.replace("\\", "").replace("{", "(").replace("}", ")")
    return re.sub(r"\*(.+?)\*", lambda m: "{\\c%s}%s{\\c%s}" % (accent, m.group(1), WHITE), line)


def circle_path(r):
    """ASS vector circle of radius r (bounding box 0..2r)."""
    c = 0.5523 * r
    return (f"m {2 * r} {r} b {2 * r} {r + c:.1f} {r + c:.1f} {2 * r} {r} {2 * r} "
            f"b {r - c:.1f} {2 * r} 0 {r + c:.1f} 0 {r} "
            f"b 0 {r - c:.1f} {r - c:.1f} 0 {r} 0 "
            f"b {r + c:.1f} 0 {2 * r} {r - c:.1f} {2 * r} {r}")


def build_ass(sc, d, W, H, accent, layout):
    g = geometry(W, H)
    fs, big = g["fs"], g["big"]
    cx = W // 2
    card = layout == "card"
    side = card and g["side"]
    ml, mr = (1150, 60) if side else (90, 90)
    head = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Main,Arial,{fs},{WHITE},&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,2,0,1,6,3,5,{ml},{mr},80,1",
        f"Style: Big,Arial,{big},{accent},&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,2,0,1,7,3,5,40,40,40,1",
        f"Style: Bar,Arial,{int(fs * 0.56)},{WHITE},&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,4,2,4,40,40,40,1",
        f"Style: Label,Arial,{int(fs * 0.4)},{WHITE},&H000000FF,&H90000000,&H90000000,0,0,0,0,100,100,1,0,3,12,0,1,20,20,20,1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    ev = []
    counter, bars = sc.get("counter"), sc.get("bars")
    body = "\\N".join(ass_text(l, accent) for l in sc["text"])

    # ---- main text
    if bars:
        ty = int(H * 0.14)
        tx = cx
    elif card:
        tx, ty = g["text"]
    else:
        tx, ty = cx, int(H * (0.66 if counter else 0.5))
    if body:
        pop = (f"{{\\an5\\pos({tx},{ty})\\fad(220,150)\\fscx88\\fscy88"
               f"\\t(0,380,\\fscx100\\fscy100)}}")
        ev.append(f"Dialogue: 1,{ass_time(0)},{ass_time(d)},Main,,0,0,0,,{pop}{body}")

    # ---- counting number
    if counter and not card and not bars:
        parts = counter.split()
        try:
            a, b = float(parts[0]), float(parts[1])
        except (IndexError, ValueError):
            sys.exit(f"Bad @counter '{counter}'. Use:  @counter 0 3800 m")
        unit = " ".join(parts[2:])
        T, t, cy = max(0.8, d * 0.75), 0.0, int(H * 0.34)
        while t < d:
            p = min(1.0, t / T)
            v = a + (b - a) * (1 - (1 - p) ** 3)       # ease-out: fast start, slow finish
            small = f"{{\\fs{big // 2}}} {unit}" if unit else ""
            ev.append(f"Dialogue: 2,{ass_time(t)},{ass_time(min(d, t + 0.05))},Big,,0,0,0,,"
                      f"{{\\an5\\pos({cx},{cy})}}{int(round(v)):,}{small}")
            t += 0.05

    # ---- caption label + callout circle on the picture card
    if card:
        x0, y0, cw, ch = g["card"]
        if sc.get("caption"):
            cap = ass_text(sc["caption"], accent)
            ev.append(f"Dialogue: 3,{ass_time(0.5)},{ass_time(d)},Label,,0,0,0,,"
                      f"{{\\an7\\pos({x0},{y0 + ch + 24})\\fad(250,100)}}{cap}")
        if sc.get("circle"):
            try:
                vals = [float(v) for v in sc["circle"].split()]
                fx_, fy_ = vals[0], vals[1]
                rad = (vals[2] if len(vals) > 2 else 0.12) * cw
            except (ValueError, IndexError):
                sys.exit(f"Bad @circle '{sc['circle']}'. Use:  @circle 0.6 0.4   (x y, both 0-1)")
            px, py = x0 + fx_ * cw, y0 + fy_ * ch
            t0 = min(1.2, d * 0.35)
            ring = (f"{{\\an5\\pos({px:.0f},{py:.0f})\\p1\\1a&HFF&\\3c{accent}\\bord9\\shad0"
                    f"\\fscx40\\fscy40\\fad(150,100)\\t(0,350,\\fscx100\\fscy100)"
                    f"\\t(600,900,\\fscx112\\fscy112)\\t(900,1200,\\fscx100\\fscy100)}}{circle_path(rad)}")
            ev.append(f"Dialogue: 4,{ass_time(t0)},{ass_time(d)},Main,,0,0,0,,{ring}")

    # ---- animated bars
    if bars:
        rows = parse_bars(bars)
        unit = sc.get("unit", "")
        vmax = max(v for _, v, _ in rows)
        x0 = 110 if H > W else 160
        full = (W - 2 * x0 - 230)
        top, step = (int(H * 0.30), int(H * 0.165)) if H > W else (int(H * 0.32), int(H * 0.20))
        bh = 72 if H > W else 56
        for i, (label, v, hl) in enumerate(rows):
            y = top + i * step
            start = 0.35 + i * 0.3
            grow = min(1.4, d * 0.4)
            col = accent if hl else GREY
            blen = max(8, int(full * v / vmax))
            ev.append(f"Dialogue: 2,{ass_time(start)},{ass_time(d)},Bar,,0,0,0,,"
                      f"{{\\an1\\pos({x0},{y - bh // 2 - 14})\\fad(200,100)}}{ass_text(label, accent)}")
            ev.append(f"Dialogue: 2,{ass_time(start)},{ass_time(d)},Bar,,0,0,0,,"
                      f"{{\\an4\\pos({x0},{y + bh // 2})\\p1\\bord0\\shad0\\1c{col}\\fscx1"
                      f"\\t(0,{int(grow * 1000)},\\fscx100)}}m 0 0 l {blen} 0 {blen} {bh} 0 {bh}")
            t = start
            while t < d:                               # value counts up in step with the bar
                p = min(1.0, (t - start) / grow)
                e = p                              # same linear growth as the bar itself
                shown = f"{int(round(v * e)):,}{(' ' + unit) if unit else ''}"
                ev.append(f"Dialogue: 3,{ass_time(t)},{ass_time(min(d, t + 0.05))},Bar,,0,0,0,,"
                          f"{{\\an4\\pos({x0 + int(blen * e) + 22},{y + bh // 2})\\c{col}}}{shown}")
                t += 0.05
    return "\n".join(head + ev) + "\n"


# ------------------------------------------------------------------ rendering

def fx_filters(fx, d, W, H):
    """Effect chain applied to the finished frame (text included). Returns (chain, flash?)."""
    f = []
    if "shake" in fx:
        f.append(f"scale={int(W * 1.07) // 2 * 2}:{int(H * 1.07) // 2 * 2}")
        f.append(f"crop={W}:{H}:x='(iw-{W})/2+24*exp(-t*4)*sin(t*67)+3*sin(t*9)':"
                 f"y='(ih-{H})/2+18*exp(-t*4)*sin(t*83+1)+2*sin(t*7)'")
    if "punch" in fx:
        k = "(1+0.15*pow(max(0,1-t/0.3),2))"
        f.append(f"scale=w='2*trunc({W}*{k}/2)':h='2*trunc({H}*{k}/2)':eval=frame")
        f.append(f"crop={W}:{H}")
    if "glitch" in fx:
        f.append("rgbashift=rh=-18:bh=18:gv=5:enable='lt(t,0.4)'")
        f.append("eq=contrast=1.5:brightness=0.08:enable='between(t,0.06,0.12)+between(t,0.22,0.27)'")
    if "rgb" in fx:
        f.append("rgbashift=rh=-3:bh=3")
    if "flicker" in fx:
        f.append("eq=brightness='0.05*sin(t*53)+0.03*sin(t*91)':eval=frame")
    return f


def render_scene(i, sc, d, W, H, pal, tmp, sfx_paths, voice, particles):
    c0, c1, accent = pal
    g = geometry(W, H)
    img = sc.get("image")
    layout = sc.get("layout", "card" if img else "full").lower()
    if not img or sc.get("counter") or sc.get("bars"):
        layout = "full"                    # cards only for plain picture scenes
    fx = sc["_fx"]
    with open(os.path.join(tmp, f"s{i}.ass"), "w", encoding="utf-8") as f:
        f.write(build_ass(sc, d, W, H, accent, layout))

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    chains = []
    if img:
        if not os.path.isfile(img):
            sys.exit(f"Scene {i + 1}: image not found: {img}")
        cmd += ["-loop", "1", "-framerate", str(FPS), "-t", f"{d:.2f}", "-i", os.path.abspath(img)]
        coef = (0.05 if sc.get("circle") else 0.16) / max(1, d * FPS)
        z = f"min(1.3,1+{coef:.6f}*on)" if i % 2 == 0 else f"max(1.0,1.16-{coef:.6f}*on)"
        if layout == "card":
            x0, y0, cw, ch = g["card"]
            chains.append("[0:v]split=2[ia][ib]")
            chains.append(f"[ia]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=10:2,"
                          f"scale={W}:{H},eq=brightness=-0.22:saturation=0.8,fps={FPS}[bg]")
            chains.append(f"[ib]scale={int(cw * 1.4)}:{int(ch * 1.4)}:force_original_aspect_ratio=increase,"
                          f"crop={int(cw * 1.4)}:{int(ch * 1.4)},"
                          f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={cw}x{ch}:fps={FPS},"
                          f"eq=saturation=1.05,pad={cw + 12}:{ch + 12}:6:6:color=0xF2F2F2[pic]")
            chains.append(f"[bg]drawbox={x0 - 6 + 14}:{y0 - 6 + 18}:{cw + 12}:{ch + 12}:color=black@0.45:t=fill[sh]")
            chains.append(f"[sh][pic]overlay={x0 - 6}:{y0 - 6}[v0]")
        else:
            big_w, big_h = int(W * 1.5), int(H * 1.5)
            chains.append(f"[0:v]scale={big_w}:{big_h}:force_original_aspect_ratio=increase,crop={big_w}:{big_h},"
                          f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={FPS},"
                          f"eq=brightness=-0.10:saturation=0.85,drawbox=0:0:iw:ih:color=black@0.32:t=fill[v0]")
    else:
        cmd += ["-f", "lavfi", "-t", f"{d:.2f}", "-i",
                f"gradients=s={W}x{H}:c0={c0}:c1={c1}:x0=0:y0=0:x1={W}:y1={H}:speed=0.02:rate={FPS}"]
        if particles:
            # drifting specks (marine snow / dust): sparse noise, blurred, scrolling upward
            chains.append(f"color=c=black:s=270x960:r={FPS}:d={d:.2f},"
                          f"noise=alls=100:allf=u:all_seed={7 + i},lutyuv=y='if(gt(val,63),255,0)',"
                          f"gblur=sigma=1.1,crop=270:480:0:'mod(t*38,480)',scale={W}:{H}:flags=bilinear,"
                          f"format=gbrp[snow]")
            chains.append("[0:v]format=gbrp[bg0]")
            chains.append("[bg0][snow]blend=all_mode=screen:all_opacity=0.55[v0]")
        else:
            chains.append("[0:v]null[v0]")

    if "leak" in fx:                                    # warm light leak drifting across the frame
        chains.append(f"gradients=s={W}x{H}:c0=0xff7a1a:c1=0x200030:x0=0:y0=0:x1={W}:y1={H}:speed=0.06:"
                      f"rate={FPS}:d={d:.2f},format=gbrp[lk]")
        chains.append("[v0]format=gbrp[v0b]")
        chains.append("[v0b][lk]blend=all_mode=screen:all_opacity=0.22[v1]")
        cur = "[v1]"
    else:
        cur = "[v0]"

    flash = "flash" in fx
    tail = ["vignette=angle=PI/4", f"ass=s{i}.ass"] + fx_filters(fx, d, W, H) + [   # vignette first: keeps text bright
        "noise=alls=4:allf=t",
        f"fade=t=in:st=0:d={0.28 if flash else 0.12}" + (":color=white" if flash else ""),
        f"fade=t=out:st={max(0, d - 0.12):.2f}:d=0.12", "setsar=1", "format=yuv420p"]
    chains.append(f"{cur}{','.join(tail)}[v]")

    cmd += ["-f", "lavfi", "-t", f"{d:.2f}", "-i", "anullsrc=r=44100:cl=stereo"]
    fmt = "aformat=sample_rates=44100:channel_layouts=stereo"
    a_idx = 1
    achain, labels = [f"[{a_idx}:a]{fmt}[sil]"], ["[sil]"]
    n = a_idx + 1
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

    out = os.path.join(tmp, f"scene{i}.mp4")
    cmd += ["-filter_complex", ";".join(chains + achain), "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-r", str(FPS),
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-t", f"{d:.2f}", out]
    run(cmd, cwd=tmp)       # cwd=tmp so the .ass file is found by plain name (no path escaping)
    return out


def choose_fx(i, sc):
    """Effects for a scene: what the script says, or an automatic, varied default."""
    spec = sc.get("fx")
    if spec is None:
        fx = list(AUTO_FX[i % len(AUTO_FX)])
        if sc.get("image"):
            fx.append("leak")
        if sc.get("bars"):
            fx = ["flash"]
        return fx
    names = [n for n in re.split(r"[,\s]+", spec.lower()) if n and n != "none"]
    bad = [n for n in names if n not in FX_NAMES]
    if bad:
        sys.exit(f"Unknown effect '{bad[0]}'. Use: {' '.join(FX_NAMES)} none")
    return names


DEFAULT_SFX = {"flash": "hit", "punch": "hit", "shake": "hit", "glitch": "glitch"}


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
    particles = meta.get("particles", "yes").lower() != "no"
    music = args.music or meta.get("music", "drone")
    out = args.output or os.path.splitext(args.script)[0] + ".mp4"
    script_dir = os.path.dirname(os.path.abspath(args.script))
    stem = os.path.splitext(os.path.abspath(args.script))[0]
    rnd = random.Random(7)          # same script -> same sounds every time
    credits = []

    with tempfile.TemporaryDirectory() as tmp:
        sfx_paths, durations, voices = {}, [], []
        for i, sc in enumerate(scenes):
            if sc.get("search") and not sc.get("image"):
                print(f"Looking for a picture: {sc['search']}")
                found = fetch_image(sc["search"], stem + "_pictures", credits)
                if found:
                    sc["image"] = found
            for key in ("image", "audio"):           # media paths are relative to the script file
                if sc.get(key) and not os.path.isabs(sc[key]):
                    sc[key] = os.path.join(script_dir, sc[key])
            sc["_fx"] = choose_fx(i, sc)
            if "sfx" in sc:
                names = [n for n in re.split(r"[,\s]+", sc["sfx"]) if n and n != "none"]
            else:
                names = [next((DEFAULT_SFX[f] for f in sc["_fx"] if f in DEFAULT_SFX), "whoosh")]
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
            elif sc.get("bars"):
                d = 1.5 + 0.35 * len(sc["bars"].split("|")) + 2.5
            else:
                d = max(2.2, 1.2 + 0.38 * len(plain(sc).split()))
            durations.append(d)

        parts = []
        for i, sc in enumerate(scenes):
            print(f"Scene {i + 1}/{len(scenes)} ({durations[i]:.1f}s)  fx: {', '.join(sc['_fx']) or 'none'}")
            parts.append(render_scene(i, sc, durations[i], W, H, pal, tmp, sfx_paths, voices[i], particles))

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
                "-c:v", "libx264", "-preset", "medium", "-crf", "24", "-maxrate", "4M",
                "-bufsize", "8M", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-t", f"{total:.2f}",
                os.path.abspath(out)]
        run(cmd)
    if credits:
        cpath = stem + "_credits.txt"
        with open(cpath, "w", encoding="utf-8") as f:
            f.write("Pictures used (Wikimedia Commons). Put this in your video description:\n\n")
            f.write("\n".join(credits) + "\n")
        print(f"Picture credits written to {cpath}  <- paste them in your video description")
    print(f"Done: {out}  ({total:.0f}s)")
    print("Double-check every fact in your script before you post it, and only use images/music you're allowed to.")


if __name__ == "__main__":
    main()
