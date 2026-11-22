#!/usr/bin/env python3
"""Turn a section of a (live)stream into a vertical short, in the same style as
the reference clip:

  * 1080x1920 (9:16), ~58 s long
  * background = the same footage, zoomed to fill and heavily blurred
  * foreground = the original 16:9 footage, full width, centred
  * top band   = bold white title with black outline (+ colour emoji)
  * bottom band= one-word-at-a-time captions synced to speech

Usage
-----
  # you already know the moment:
  python3 make_clip.py stream.mp4 --start 1:23:45 --duration 58 \
      --title "Streamer tried X and it went wrong 😳" -o short.mp4

  # let the script pick the loudest / most hype 58 s window:
  python3 make_clip.py stream.mp4 --auto --title "..." -o short.mp4

  # input can also be a YouTube URL (needs yt-dlp + network access):
  python3 make_clip.py "https://www.youtube.com/live/ID" --start 2:10:00 ...

Captions are generated with faster-whisper when it is installed and its model
can be downloaded; otherwise pass --srt file.srt, or --no-captions.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920
FG_H = (W * 9 // 16) // 2 * 2        # 606: even height of the centred 16:9 video
FG_Y = (H - FG_H) // 2                 # 656
TITLE_CENTER_Y = FG_Y // 2 - 40        # middle of the top band
CAPTION_Y = FG_Y + FG_H + 200          # inside the bottom band

FONT_BOLD = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
FONT_EMOJI = "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"
EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF️‍]+")


def run(cmd, **kw):
    print("+", " ".join(cmd) if isinstance(cmd, list) else cmd, file=sys.stderr)
    return subprocess.run(cmd, check=True, **kw)


def parse_time(t):
    if t is None:
        return None
    parts = [float(p) for p in str(t).split(":")]
    s = 0.0
    for p in parts:
        s = s * 60 + p
    return s


# --------------------------------------------------------------------------- input
def fetch(src, start, duration, workdir):
    """Return a local file containing [start, start+duration] of src."""
    if os.path.exists(src):
        return src, start
    if not shutil.which("yt-dlp"):
        sys.exit("Input is not a file and yt-dlp is not installed.")
    if start is None:
        sys.exit("--start is required when downloading from a URL "
                 "(downloading a whole livestream to auto-scan it is too large).")
    out = os.path.join(workdir, "source.%(ext)s")
    section = f"*{start}-{start + duration + 1}"
    run(["yt-dlp", "-f", "bv*[height<=1080]+ba/b", "--download-sections", section,
         "--force-keyframes-at-cuts", "--merge-output-format", "mp4",
         "-o", out, src])
    return os.path.join(workdir, "source.mp4"), 0.0


# ------------------------------------------------------------- highlight finder
def find_hype_window(path, duration):
    """Pick the window whose audio is loudest relative to the stream's norm.

    Streams spike in loudness when something happens (streamer yells, laughs,
    alerts fire). We take per-second momentary loudness and maximise the sum of
    the above-median part over a sliding window.
    """
    print("Scanning audio loudness to find the highlight...", file=sys.stderr)
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-vn", "-af",
         "aresample=8000,ebur128=metadata=1,ametadata=print:key=lavfi.r128.M",
         "-f", "null", "-"], capture_output=True, text=True)
    times, vals = [], []
    t = None
    for line in p.stderr.splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            t = float(m.group(1))
        m = re.search(r"lavfi\.r128\.M=(-?[\d.]+|-inf)", line)
        if m and t is not None:
            v = float(m.group(1)) if m.group(1) != "-inf" else -70.0
            times.append(t)
            vals.append(max(v, -70.0))
    if not vals:
        return 0.0
    per_sec = {}
    for t, v in zip(times, vals):
        per_sec.setdefault(int(t), []).append(v)
    secs = sorted(per_sec)
    loud = [max(per_sec[s]) for s in secs]
    med = sorted(loud)[len(loud) // 2]
    excess = [max(0.0, v - med) ** 2 for v in loud]
    win = int(duration)
    if len(excess) <= win:
        return 0.0
    best, best_i, cur = -1, 0, sum(excess[:win])
    for i in range(len(excess) - win):
        if cur > best:
            best, best_i = cur, i
        cur += excess[i + win] - excess[i]
    # start a few seconds before the action ramps up
    return max(0.0, float(secs[best_i]))


# ------------------------------------------------------------------ title image
def _font(size):
    return ImageFont.truetype(FONT_BOLD, size)


def _runs(text):
    """Split text into (is_emoji, chunk) runs."""
    out, pos = [], 0
    for m in EMOJI_RE.finditer(text):
        if m.start() > pos:
            out.append((False, text[pos:m.start()]))
        out.append((True, m.group()))
        pos = m.end()
    if pos < len(text):
        out.append((False, text[pos:]))
    return out


def _measure(text, font, emoji_px):
    w = 0
    for is_e, chunk in _runs(text):
        if is_e:
            w += emoji_px * len([c for c in chunk if c not in "️‍"])
        else:
            w += font.getlength(chunk)
    return w


def render_title(text, path, size=68, max_w=W - 120):
    font = _font(size)
    emoji_px = int(size * 1.05)
    words, lines, cur = [], [], ""
    for w_ in text.split():
        # glue emoji-only tokens to the word before so they never wrap alone
        if words and EMOJI_RE.fullmatch(w_):
            words[-1] += " " + w_
        else:
            words.append(w_)
    for w_ in words:
        trial = (cur + " " + w_).strip()
        if _measure(trial, font, emoji_px) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w_
    if cur:
        lines.append(cur)
    # avoid an orphan like "6 😳" on the last line: pull the previous word down
    if len(lines) > 1:
        last_words = [w_ for w_ in lines[-1].split() if not EMOJI_RE.fullmatch(w_)]
        prev = lines[-2].split()
        if prev and EMOJI_RE.fullmatch(prev[-1]):
            prev = prev[:-1]  # don't move a lone emoji; keep things simple
        if len(last_words) <= 1 and len(prev) > 1:
            lines[-2] = " ".join(prev[:-1])
            lines[-1] = prev[-1] + " " + lines[-1]

    line_h = int(size * 1.25)
    img = Image.new("RGBA", (W, line_h * len(lines) + 40), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    efont = ImageFont.truetype(FONT_EMOJI, 109)  # only size NotoColorEmoji supports
    stroke = max(4, size // 12)
    for li, line in enumerate(lines):
        x = (W - _measure(line, font, emoji_px)) / 2
        y = 20 + li * line_h
        for is_e, chunk in _runs(line):
            if is_e:
                for ch in chunk:
                    if ch in "️‍":
                        continue
                    em = Image.new("RGBA", (140, 140), (0, 0, 0, 0))
                    ImageDraw.Draw(em).text((0, 0), ch, font=efont, embedded_color=True)
                    em = em.crop(em.getbbox() or (0, 0, 1, 1)).resize((emoji_px, emoji_px))
                    img.alpha_composite(em, (int(x), int(y + size * 0.05)))
                    x += emoji_px
            else:
                d.text((x, y), chunk, font=font, fill="white",
                       stroke_width=stroke, stroke_fill="black")
                x += font.getlength(chunk)
    img.save(path)
    return img.size[1]


# --------------------------------------------------------------------- captions
def transcribe_words(path, start, duration, model_size):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("faster-whisper not installed; skipping captions.", file=sys.stderr)
        return []
    import numpy as np
    pcm = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(start), "-t", str(duration), "-i", path,
         "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
        check=True, capture_output=True).stdout
    audio = np.frombuffer(pcm, dtype=np.float32)
    try:
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
    except Exception as e:  # model download blocked, etc.
        print(f"Could not load whisper model ({e}); skipping captions.", file=sys.stderr)
        return []
    segs, _ = model.transcribe(audio, word_timestamps=True, vad_filter=True)
    words = []
    for s in segs:
        for w in s.words or []:
            txt = w.word.strip()
            if txt:
                words.append((w.start, w.end, txt))
    return words


def words_from_srt(path, start):
    """Split each SRT cue evenly into words (cue times are absolute stream times
    if they exceed the clip, otherwise clip-relative)."""
    words = []
    blocks = open(path, encoding="utf-8").read().strip().split("\n\n")
    def ts(s):
        h, m, rest = s.strip().replace(",", ".").split(":")
        return int(h) * 3600 + int(m) * 60 + float(rest)
    for b in blocks:
        lines = b.strip().splitlines()
        if len(lines) < 2 or "-->" not in lines[1]:
            continue
        a, z = [ts(x) for x in lines[1].split("-->")]
        toks = " ".join(lines[2:]).split()
        if not toks:
            continue
        step = (z - a) / len(toks)
        for i, tok in enumerate(toks):
            words.append((a + i * step, a + (i + 1) * step, tok))
    if words and words[0][0] >= start:
        words = [(a - start, z - start, t) for a, z, t in words]
    return words


def ass_time(t):
    t = max(0.0, t)
    h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def write_ass(words, path, size=64):
    hdr = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Word,Liberation Sans,{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,5,2,5,40,40,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    for i, (a, z, txt) in enumerate(words):
        # keep each word on screen until the next one starts (max +0.6 s)
        nxt = words[i + 1][0] if i + 1 < len(words) else z + 0.6
        z = min(max(z, a + 0.15), nxt, z + 0.6) if nxt > a else z
        txt = txt.replace("{", "").replace("}", "")
        ev.append(f"Dialogue: 0,{ass_time(a)},{ass_time(z)},Word,,0,0,0,,"
                  f"{{\\pos({W // 2},{CAPTION_Y})\\fscx80\\fscy80\\t(0,80,\\fscx100\\fscy100)}}{txt}")
    open(path, "w", encoding="utf-8").write(hdr + "\n".join(ev) + "\n")


# ------------------------------------------------------------------------ render
def render(src, start, duration, title_png, title_h, ass, out, crop=None, rotate=None):
    title_y = max(40, TITLE_CENTER_Y - title_h // 2)
    pre = {"ccw": "transpose=2,", "cw": "transpose=1,", None: ""}[rotate]
    pre += f"crop={crop}," if crop else ""
    fc = (
        f"[0:v]{pre}split=2[a][b];"
        f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
        f"scale={W // 8}:{H // 8},gblur=sigma=6,scale={W}:{H},eq=brightness=-0.06[bg];"
        f"[b]scale={W}:{FG_H}:force_original_aspect_ratio=decrease,"
        f"pad={W}:{FG_H}:(ow-iw)/2:(oh-ih)/2:black[fg];"
        f"[bg][fg]overlay=0:{FG_Y}[v1];"
        f"[v1][1:v]overlay=0:{title_y}[v2]"
    )
    last = "v2"
    if ass:
        esc = ass.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        fc += f";[v2]ass='{esc}'[v3]"
        last = "v3"
    run(["ffmpeg", "-y", "-v", "error", "-stats",
         "-ss", str(start), "-t", str(duration), "-i", src,
         "-i", title_png,
         "-filter_complex", fc, "-map", f"[{last}]", "-map", "0:a?",
         "-r", "30", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
         "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
         "-movflags", "+faststart", out])


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="video file or YouTube URL")
    ap.add_argument("--start", help="start time (s or H:MM:SS)")
    ap.add_argument("--duration", type=float, default=58.0)
    ap.add_argument("--auto", action="store_true",
                    help="pick the most hype window by audio loudness (local files)")
    ap.add_argument("--title", required=True)
    ap.add_argument("--srt", help="use this SRT for captions instead of whisper")
    ap.add_argument("--no-captions", action="store_true")
    ap.add_argument("--whisper-model", default="small")
    ap.add_argument("--crop", help="ffmpeg crop W:H:X:Y to cut the 16:9 stream out of "
                    "a phone screen recording (removes black bars / UI)")
    ap.add_argument("--rotate", choices=["cw", "ccw"],
                    help="rotate 90° first, for landscape recordings saved sideways "
                    "(applied before --crop)")
    ap.add_argument("-o", "--output", default="short.mp4")
    a = ap.parse_args()

    work = tempfile.mkdtemp(prefix="clip_")
    start = parse_time(a.start)
    src, start = fetch(a.input, start, a.duration, work)
    if start is None:
        start = find_hype_window(src, a.duration) if a.auto else 0.0
    print(f"Clip window: {start:.1f}s -> {start + a.duration:.1f}s", file=sys.stderr)

    title_png = os.path.join(work, "title.png")
    title_h = render_title(a.title, title_png)

    ass = None
    if not a.no_captions:
        words = (words_from_srt(a.srt, start) if a.srt
                 else transcribe_words(src, start, a.duration, a.whisper_model))
        if words:
            ass = os.path.join(work, "captions.ass")
            write_ass(words, ass)

    render(src, start, a.duration, title_png, title_h, ass, a.output, a.crop, a.rotate)
    print(json.dumps({"output": a.output, "start": start, "duration": a.duration,
                      "captions": bool(ass)}))


if __name__ == "__main__":
    main()
