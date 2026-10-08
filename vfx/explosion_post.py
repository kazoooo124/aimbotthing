#!/usr/bin/env python3
"""explosion_post.py - turn the raw Blender renders into a finished cinematic clip.

Adds what makes raw renders look like a movie shot:
  * bloom + warm anamorphic light streaks on the bright fire
  * teal-and-orange colour grade, contrast, vignette, film grain
  * camera shake: a hard hit when the shockwave arrives, then handheld wobble
  * impact flash, slight lens colour fringing
  * 2.39:1 widescreen bars  (or --vertical for a 1080x1920 Short with blurred sides)
  * sound, built from scratch: sub-bass boom, crack, rumble, debris rain, ear ringing.
    The sound arrives AFTER the flash, delayed by the camera distance (light is instant,
    sound travels ~343 m/s) - that delay is what makes it feel real.

Usage:
    python explosion_post.py render_dir -o explosion.mp4
    python explosion_post.py render_dir --vertical -o explosion_short.mp4
Needs: Python 3.8+ and ffmpeg.
"""
import argparse
import array
import math
import os
import random
import subprocess
import sys
import tempfile
import wave

SR = 44100
FPS = 24


def run(cmd):
    try:
        return subprocess.run(cmd, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        sys.exit(f"'{cmd[0]}' not found. Install ffmpeg and make sure it is on your PATH.")
    except subprocess.CalledProcessError as e:
        sys.exit(f"{cmd[0]} failed:\n" + "\n".join((e.stderr or "").strip().splitlines()[-15:]))


# ------------------------------------------------------------------ sound

def synth_blast(total, impact_t, delay, seed=11):
    """Stereo explosion sound. `impact_t` = when the missile hits; `delay` = seconds between the flash
    and the sound reaching the camera."""
    rnd = random.Random(seed)
    n = int(SR * total)
    L, R = [0.0] * n, [0.0] * n
    t0 = int((impact_t + delay) * SR)

    # incoming missile: a hissing whistle that rises in pitch and level until it hits
    ph, y = 0.0, 0.0
    for i in range(int(max(0.0, impact_t - 0.05) * SR)):
        t = i / SR
        k = t / max(impact_t, 0.1)
        ph += 2 * math.pi * (700 + 2600 * k * k) / SR
        y += (1 - math.exp(-2 * math.pi * (1500 + 3000 * k) / SR)) * ((rnd.random() * 2 - 1) - y)
        v = (0.5 * math.sin(ph) + 1.2 * y) * 0.12 * k ** 1.6
        L[i + int(0.05 * SR)] += v
        R[i + int(0.05 * SR)] += v * 0.9

    # quiet battlefield wind before and after: low brown noise
    b = 0.0
    for i in range(n):
        b = b * 0.998 + (rnd.random() * 2 - 1) * 0.02
        L[i] += b * 0.5
        b2 = b * 0.97
        R[i] += b2 * 0.5

    # sub-bass boom: sine that drops from 75 Hz to 26 Hz
    ph = 0.0
    for i in range(int(SR * 3.2)):
        t = i / SR
        f = 26 + 50 * math.exp(-t * 5.0)
        ph += 2 * math.pi * f / SR
        env = min(1.0, t / 0.012) * math.exp(-t * 1.5)
        v = math.tanh(2.2 * math.sin(ph) * env)
        if t0 + i < n:
            L[t0 + i] += v * 0.95
            R[t0 + i] += v * 0.95

    # crack: broadband noise with a cutoff that falls fast
    y1 = y2 = 0.0
    for i in range(int(SR * 1.4)):
        t = i / SR
        fc = 300 + 5200 * math.exp(-t * 7)
        a = 1 - math.exp(-2 * math.pi * fc / SR)
        wl, wr = rnd.random() * 2 - 1, rnd.random() * 2 - 1
        y1 += a * (wl - y1)
        y2 += a * (wr - y2)
        env = min(1.0, t / 0.004) * math.exp(-t * 3.8)
        if t0 + i < n:
            L[t0 + i] += math.tanh(2.6 * y1) * env * 0.85
            R[t0 + i] += math.tanh(2.6 * y2) * env * 0.85

    # rumble: brown noise that swells then fades over ~5 s
    r1 = r2 = 0.0
    for i in range(int(SR * 5.5)):
        t = i / SR
        r1 = r1 * 0.9985 + (rnd.random() * 2 - 1) * 0.03
        r2 = r2 * 0.9985 + (rnd.random() * 2 - 1) * 0.03
        env = min(1.0, t / 0.25) * math.exp(-t * 0.75)
        if t0 + i < n:
            L[t0 + i] += math.tanh(5.0 * r1) * env * 0.55
            R[t0 + i] += math.tanh(5.0 * r2) * env * 0.55

    # debris falling: random clicks/thuds, thinning out over time
    for _ in range(420):
        t = 0.35 + rnd.random() ** 1.7 * 4.5
        pos = t0 + int(t * SR)
        amp = rnd.uniform(0.05, 0.22) * math.exp(-t * 0.45)
        f = rnd.uniform(90, 900)
        ln = int(SR * rnd.uniform(0.02, 0.09))
        pan = rnd.random()
        for k in range(ln):
            if pos + k >= n:
                break
            e = math.exp(-k / (ln / 4.0))
            v = math.sin(2 * math.pi * f * k / SR) * e * amp + (rnd.random() * 2 - 1) * e * amp * 0.4
            L[pos + k] += v * (1 - pan)
            R[pos + k] += v * pan

    # ears ringing after the blast
    for i in range(int(SR * 3.5)):
        t = i / SR
        v = math.sin(2 * math.pi * 6200 * t) * 0.035 * min(1.0, t / 0.3) * math.exp(-t * 0.9)
        if t0 + int(0.15 * SR) + i < n:
            L[t0 + int(0.15 * SR) + i] += v
            R[t0 + int(0.15 * SR) + i] += v

    peak = max(max(map(abs, L)), max(map(abs, R)), 1e-9)
    g = 0.93 / peak
    data = array.array("h")
    for i in range(n):
        data.append(int(max(-1, min(1, L[i] * g)) * 32767))
        data.append(int(max(-1, min(1, R[i] * g)) * 32767))
    return data


def write_wav(path, data):
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data.tobytes())


# ------------------------------------------------------------------ picture

def video_graph(w, h, vertical, shake, impact_t, delay, denoise, letterbox, hold):
    """ffmpeg filter graph. Returns (graph_string, output_label)."""
    big_w, big_h = int(w * 1.09) // 2 * 2, int(h * 1.09) // 2 * 2
    ti = impact_t                                 # the flash
    tb = impact_t + delay                         # the shockwave reaches the camera with the sound
    amp = 26.0 * shake
    # decaying hit after the shockwave arrives + a little constant handheld sway
    sx = (f"({amp}*if(gte(t,{tb}),exp(-(t-{tb})*2.2),0)*(sin(t*61)+0.6*sin(t*97+1.3))"
          f"+{1.6 * shake}*sin(t*2.3)+{0.9 * shake}*sin(t*5.1+2))")
    sy = (f"({amp}*0.8*if(gte(t,{tb}),exp(-(t-{tb})*2.0),0)*(sin(t*73+0.7)+0.6*sin(t*113))"
          f"+{1.4 * shake}*sin(t*1.9+1)+{0.8 * shake}*sin(t*4.4))")
    pre = f"[0:v]tpad=stop_mode=clone:stop_duration={hold},format=rgb24{',hqdn3d=3:3:6:6' if denoise else ''}"
    g = (
        f"{pre},scale={big_w}:{big_h}:flags=lanczos,"
        f"crop={w}:{h}:x='(iw-{w})/2+{sx}':y='(ih-{h})/2+{sy}'[base];"
        # bloom on everything bright, plus a long warm horizontal streak (anamorphic lens look)
        f"[base]split=3[o][h1][h2];"
        f"[h1]curves=all='0/0 0.74/0 1/1',scale={w // 4}:{h // 4},gblur=sigma=11,scale={w}:{h}[bloom];"
        f"[h2]curves=all='0/0 0.86/0 1/1',scale={w // 4}:{h // 4},gblur=sigma=70:sigmaV=1.2,scale={w}:{h},"
        f"colorchannelmixer=rr=0.95:gg=0.62:bb=0.32[streak];"
        f"[o][bloom]blend=all_mode=screen:all_opacity=0.7[t1];"
        f"[t1][streak]blend=all_mode=screen:all_opacity=0.55[t2];"
        # grade: teal shadows, orange highlights, punchier contrast
        f"[t2]eq=contrast=1.14:saturation=1.12:gamma=0.97,"
        f"colorbalance=rs=-0.05:gs=0.0:bs=0.07:rm=0.02:gm=0.0:bm=-0.02:rh=0.07:gh=0.01:bh=-0.07,"
        # impact flash that decays over ~0.4 s, tiny colour fringing, vignette, grain
        f"eq=brightness='0.30*gte(t,{ti})*pow(max(0,1-(t-{ti})/0.4),2)':eval=frame,"
        f"rgbashift=rh=-2:bh=2,vignette=angle=PI/4.6,noise=alls=7:allf=t+u,format=yuv420p[graded]"
    )
    out = "graded"
    if vertical:
        g += (
            f";[graded]split=2[a][b];"
            f"[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=8:2,"
            f"scale=1080:1920,eq=brightness=-0.15[bg];"
            f"[b]scale=1080:-2:flags=lanczos[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[vout]"
        )
        out = "vout"
    elif letterbox:
        lh = int(w / 2.39) // 2 * 2
        g += f";[graded]crop={w}:{lh},scale=1920:-2:flags=lanczos,format=yuv420p[vout]"
        out = "vout"
    return g, out


def main():
    ap = argparse.ArgumentParser(description="Raw explosion renders -> finished clip.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("frames", help="folder with frame_0001.png, frame_0002.png ...")
    ap.add_argument("-o", "--output", default="explosion.mp4")
    ap.add_argument("--vertical", action="store_true", help="1080x1920 for Shorts/TikTok instead of widescreen")
    ap.add_argument("--no-letterbox", action="store_true", help="keep 16:9 instead of 2.39:1 bars")
    ap.add_argument("--shake", type=float, default=1.0, help="camera shake strength (0 = none)")
    ap.add_argument("--distance", type=float, default=250.0, help="camera distance in metres (sets how late the boom arrives)")
    ap.add_argument("--impact", type=int, default=24, help="frame where the missile hits (flash, shake and sound key off this)")
    ap.add_argument("--no-sound", action="store_true")
    ap.add_argument("--denoise", action="store_true", help="extra noise cleanup (use if the render was grainy)")
    ap.add_argument("--hold", type=float, default=1.2, help="seconds of silence/freeze after the last frame for the sound tail")
    args = ap.parse_args()

    folder = os.path.abspath(args.frames)
    names = sorted(f for f in os.listdir(folder) if f.startswith("frame_") and f.endswith(".png"))
    if not names:
        sys.exit(f"No frame_XXXX.png files in {folder}")
    first = int(names[0][6:10])
    nframes = len(names)
    probe = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                 "-of", "csv=p=0", os.path.join(folder, names[0])]).stdout.strip().split(",")
    w, h = int(probe[0]), int(probe[1])
    dur = nframes / FPS
    delay = args.distance / 343.0
    impact_t = (args.impact - first) / FPS
    total = dur + args.hold
    print(f"{nframes} frames, {w}x{h}, {dur:.1f}s picture, impact at {impact_t:.2f}s, boom arrives at {impact_t + delay:.2f}s")

    with tempfile.TemporaryDirectory() as tmp:
        graph, label = video_graph(w, h, args.vertical, args.shake, impact_t, delay, args.denoise, not args.no_letterbox, args.hold)
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-framerate", str(FPS), "-start_number", str(first), "-i", os.path.join(folder, "frame_%04d.png")]
        if not args.no_sound:
            wav = os.path.join(tmp, "blast.wav")
            print("Building the sound...")
            write_wav(wav, synth_blast(total, impact_t, delay))
            cmd += ["-i", wav]
        cmd += ["-filter_complex", graph, "-map", f"[{label}]"]
        if not args.no_sound:
            cmd += ["-map", "1:a", "-af", "acompressor=threshold=0.35:ratio=3:attack=5:release=120,"
                    "alimiter=limit=0.95", "-c:a", "aac", "-b:a", "192k"]
        cmd += ["-c:v", "libx264", "-preset", "slow", "-crf", "17", "-pix_fmt", "yuv420p",
                "-r", str(FPS), "-movflags", "+faststart", "-t", f"{total:.2f}",
                os.path.abspath(args.output)]
        print("Rendering the final video...")
        run(cmd)
    print("Done:", args.output)


if __name__ == "__main__":
    main()
