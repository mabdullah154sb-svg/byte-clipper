"""
Byte Clipper V3 (free, runs on GitHub Actions)

Input  : Telegram video / YouTube URL / Google Drive URL
AI     : faster-whisper -> candidate detection -> hook/story/duration/density
         scores -> Gemini re-scoring -> final ranking
Video  : 9:16, smart crop (face) or blur background, silence trim, loudnorm
Captions: word timestamps, karaoke highlight, hook title
Output : short_01.mp4 ..., metadata.json, titles.txt -> Telegram
"""
import glob
import json
import os
import re
import statistics
import subprocess
import sys
import time
import traceback
from pathlib import Path

import requests


def clean_secret(v):
    # phone copy-paste me kabhi invisible characters aa jate hain, unhe hata do
    return "".join(ch for ch in (v or "") if 32 < ord(ch) < 127)


BOT_TOKEN = clean_secret(os.environ["BOT_TOKEN"])
# CHAT_ID normally comes dynamically from the Telegram dispatch payload (per-user chat).
# If that's empty (e.g. a manual workflow_dispatch run with no chat_id input), fall back
# to the repo-level default secret so the job still has somewhere to deliver videos.
CHAT_ID = clean_secret(os.environ.get("CHAT_ID", "")) or clean_secret(os.environ.get("TELEGRAM_CHAT_ID", ""))
if not CHAT_ID:
    raise RuntimeError(
        "CHAT_ID nahi mila (na dispatch payload se na TELEGRAM_CHAT_ID secret se). "
        "Telegram se link bhejo ya TELEGRAM_CHAT_ID secret set karo."
    )
GEMINI_KEY = clean_secret(os.environ.get("GEMINI_KEY", ""))
GEMINI_MODEL = (os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash").strip()
URL = os.environ.get("VIDEO_URL", "").strip()
TG_FILE_ID = os.environ.get("TG_FILE_ID", "").strip()
NUM = int(os.environ.get("NUM_CLIPS", "5") or 5)
LAYOUT = (os.environ.get("LAYOUT") or "auto").strip()  # auto | blur | crop
WHISPER_MODEL = (os.environ.get("WHISPER_MODEL") or "small").strip()
LANG = (os.environ.get("LANGUAGE") or "auto").strip()

MIN_LEN = int(os.environ.get("MIN_LEN") or 30)  # seconds per short
MAX_LEN = int(os.environ.get("MAX_LEN") or 60)
WORK = Path("work")
OUT = Path("output")

FINAL_P = (".", "?", "!", "\u06d4", "\u061f", "\u2026")

HOOK_WORDS = {
    "how", "why", "what", "secret", "secrets", "never", "stop", "mistake", "mistakes", "truth",
    "biggest", "nobody", "imagine", "listen", "warning", "free", "money", "worst", "best",
    "kyun", "kyu", "kaise", "kese", "sach", "raaz", "galti", "kabhi", "sabse", "suno", "dekho",
    "\u06a9\u06cc\u0648\u06ba", "\u06a9\u06cc\u0633\u06d2", "\u0631\u0627\u0632", "\u063a\u0644\u0637\u06cc",
    "\u0633\u0686", "\u06a9\u0628\u06be\u06cc", "\u0633\u0646\u06cc\u06ba", "\u062f\u06cc\u06a9\u06be\u06cc\u06ba",
    "\u067e\u06cc\u0633\u06d2", "\u0915\u094d\u092f\u094b\u0902", "\u0915\u0948\u0938\u0947", "\u0938\u091a",
    "\u0930\u093e\u091c", "\u0917\u0932\u0924\u0940", "\u0915\u092d\u0940", "\u092a\u0948\u0938\u0947",
}
FILLERS = {
    "um", "uh", "so", "and", "but", "well", "okay", "ok", "like", "aur", "toh", "to", "matlab",
    "\u0627\u0648\u0631", "\u062a\u0648", "\u0644\u06cc\u06a9\u0646", "\u0645\u0637\u0644\u0628",
    "\u0914\u0930", "\u0924\u094b",
}
CLOSURE_WORDS = {
    "because", "therefore", "finally", "lesson", "result", "means", "however", "remember",
    "isliye", "islie", "lekin", "nateeja", "yaani", "yani",
    "\u0644\u06cc\u06d2", "\u0644\u06cc\u06a9\u0646", "\u0646\u062a\u06cc\u062c\u06c1", "\u06cc\u0639\u0646\u06cc",
    "\u0622\u062e\u0631", "\u0907\u0938\u0932\u093f\u090f", "\u0932\u0947\u0915\u093f\u0928",
    "\u0928\u0924\u0940\u091c\u093e", "\u092f\u093e\u0928\u0940",
}
STORY_WORDS = {
    "story", "once", "when", "jab", "kahani",
    "\u062c\u0628", "\u06a9\u06c1\u0627\u0646\u06cc", "\u0915\u0939\u093e\u0928\u0940", "\u091c\u092c",
}


# ---------------------------------------------------------------- helpers
def log(*a):
    print(*a, flush=True)


def sh(cmd):
    log("$", " ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], check=True)


def clamp(x, lo=0.0, hi=10.0):
    return max(lo, min(hi, x))


def num(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


def tg_text(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text[:4000]},
            timeout=60,
        )
    except Exception as e:
        log("telegram text failed:", e)


def tg_video(path, caption):
    try:
        with open(path, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo",
                data={"chat_id": CHAT_ID, "caption": caption[:1000], "supports_streaming": "true"},
                files={"video": f},
                timeout=900,
            )
        if not r.ok:
            tg_text(f"Send fail {Path(path).name}: {r.text[:300]}")
    except Exception as e:
        tg_text(f"Send fail {Path(path).name}: {e}")


def tg_doc(path, caption=""):
    try:
        with open(path, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                data={"chat_id": CHAT_ID, "caption": caption[:900]},
                files={"document": f},
                timeout=300,
            )
    except Exception as e:
        log("telegram doc failed:", e)


# ---------------------------------------------------------------- INPUT
def download_telegram(file_id):
    r = requests.get(
        f"https://api.telegram.org/bot{BOT_TOKEN}/getFile", params={"file_id": file_id}, timeout=60
    ).json()
    if not r.get("ok"):
        raise RuntimeError(
            "Telegram se file nahi mili (bot 20MB se badi file download nahi kar sakta). "
            "Badi video ka Drive/YouTube link /clip ke sath bhejo."
        )
    tpath = r["result"]["file_path"]
    dest = WORK / ("source" + (Path(tpath).suffix or ".mp4"))
    with requests.get(
        f"https://api.telegram.org/file/bot{BOT_TOKEN}/{tpath}", stream=True, timeout=300
    ) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(1024 * 1024):
                f.write(chunk)
    return dest


def download_url():
    if "drive.google.com" in URL:
        import gdown

        out = gdown.download(URL, str(WORK / "source.mp4"), quiet=False, fuzzy=True)
        if not out:
            raise RuntimeError("Google Drive download fail (link public hai?)")
        return Path(out)

    cookies = os.environ.get("YT_COOKIES", "").strip()
    if cookies:
        Path("cookies.txt").write_text(cookies)

    base = [
        "yt-dlp",
        "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "-o", str(WORK / "source.%(ext)s"),
    ]
    if Path("cookies.txt").exists():
        base += ["--cookies", "cookies.txt"]

    ok = False
    for extra in (["--js-runtimes", "node", "--remote-components", "ejs:github"], []):
        if subprocess.run(base + extra + [URL]).returncode == 0:
            ok = True
            break
    if not ok:
        raise RuntimeError(
            "Download fail. YouTube ne GitHub IP block kiya ho sakta hai: "
            "YT_COOKIES secret daalo ya Drive link use karo."
        )
    files = [f for f in glob.glob(str(WORK / "source.*")) if not f.endswith((".part", ".ytdl"))]
    if not files:
        raise RuntimeError("Downloaded file nahi mili")
    return Path(max(files, key=os.path.getsize))


def get_input():
    WORK.mkdir(exist_ok=True)
    if TG_FILE_ID:
        return download_telegram(TG_FILE_ID)
    if URL:
        return download_url()
    raise RuntimeError("Na URL mila na Telegram video.")


def probe(video):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
         "-of", "json", str(video)],
        capture_output=True, text=True, check=True,
    )
    w = h = 0
    audio = False
    for s in json.loads(r.stdout).get("streams", []):
        if s.get("codec_type") == "video" and not w:
            w, h = int(s.get("width", 0)), int(s.get("height", 0))
        if s.get("codec_type") == "audio":
            audio = True
    return {"w": w, "h": h, "audio": audio}


# ---------------------------------------------------------------- AI ENGINE: transcribe
def transcribe(video):
    wav = WORK / "audio.wav"
    sh(["ffmpeg", "-y", "-i", video, "-vn", "-ac", "1", "-ar", "16000", wav])

    from faster_whisper import WhisperModel

    log(f"Whisper model: {WHISPER_MODEL}")
    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8", cpu_threads=4)
    segs, info = model.transcribe(
        str(wav),
        beam_size=1,
        vad_filter=True,
        word_timestamps=True,
        condition_on_previous_text=False,
        language=None if LANG in ("", "auto") else LANG,
    )
    segments = []
    last_log = time.time()
    for s in segs:
        words = [
            {"w": w.word.strip(), "s": w.start, "e": w.end}
            for w in (s.words or [])
            if w.word.strip()
        ]
        segments.append({"start": s.start, "end": s.end, "text": s.text.strip(), "words": words})
        if time.time() - last_log > 60:
            log(f"  transcribed up to {s.end:.0f}s / {info.duration:.0f}s")
            last_log = time.time()
    log(f"Language: {info.language}, segments: {len(segments)}")
    return segments, info.duration


# ---------------------------------------------------------------- AI ENGINE: candidates
def seg_is_final(seg):
    return seg["text"].rstrip().endswith(FINAL_P)


def build_candidate(segments, i, j):
    segs = segments[i:j + 1]
    prev_gap = segments[i]["start"] - segments[i - 1]["end"] if i > 0 else 9.0
    return {
        "i": i,
        "j": j,
        "start": segs[0]["start"],
        "end": segs[-1]["end"],
        "segs": segs,
        "words": [w for s in segs for w in s["words"]],
        "text": " ".join(s["text"] for s in segs),
        "clean_start": i == 0 or seg_is_final(segments[i - 1]) or prev_gap > 0.8,
    }


def detect_candidates(segments, min_len, max_len):
    cands = []
    for i in range(len(segments)):
        s0 = segments[i]["start"]
        finals, anys = [], []
        for j in range(i, len(segments)):
            d = segments[j]["end"] - s0
            if d > max_len:
                break
            if d >= min_len:
                anys.append(j)
                if seg_is_final(segments[j]):
                    finals.append(j)
        pool = finals or anys
        if pool:
            for j in {pool[0], pool[-1]}:
                cands.append(build_candidate(segments, i, j))
    return cands


# ---------------------------------------------------------------- AI ENGINE: scores
def hook_score(c):
    first = [w for w in c["words"] if w["s"] <= c["start"] + 6]
    toks = re.findall(r"\w+", " ".join(w["w"] for w in first).lower())
    seg0 = c["segs"][0]["text"]
    score = 3.0
    if "?" in seg0 or "\u061f" in seg0:
        score += 2.5
    if re.search(r"\d", seg0):
        score += 1.0
    score += min(2.0, 0.8 * sum(1 for t in toks if t in HOOK_WORDS))
    if toks and toks[0] in FILLERS:
        score -= 1.5
    n0 = len(seg0.split())
    if 3 <= n0 <= 14:
        score += 1.0
    if n0 > 25:
        score -= 1.0
    if len(first) / 6.0 >= 2.2:
        score += 1.0
    if not c["clean_start"]:
        score -= 2.0
    return clamp(score)


def story_score(c):
    score = 3.0
    segs = c["segs"]
    if seg_is_final(segs[-1]):
        score += 2.0
    tail = re.findall(r"\w+", " ".join(s["text"] for s in segs[-2:]).lower())
    score += min(2.0, 1.0 * sum(1 for t in tail if t in CLOSURE_WORDS))
    body = re.findall(r"\w+", c["text"].lower())
    if any(t in STORY_WORDS for t in body):
        score += 1.5
    half = max(len(segs) // 2, 1)
    if any(("?" in s["text"] or "\u061f" in s["text"]) for s in segs[:half]) and len(segs) > 2:
        score += 1.5
    if len(segs) >= 3:
        score += 1.0
    if len(segs[-1]["text"].split()) < 3:
        score -= 1.0
    return clamp(score)


def duration_score(d):
    low, high = max(MIN_LEN, 35), min(MAX_LEN, 55)
    if low <= d <= high:
        return 10.0
    edge = low if d < low else high
    return clamp(10.0 - abs(d - edge) * 0.4)


def density_score(c):
    d = max(c["end"] - c["start"], 1.0)
    wps = len(c["words"]) / d
    if 1.8 <= wps <= 3.8:
        s = 10.0
    else:
        edge = 1.8 if wps < 1.8 else 3.8
        s = 10.0 - abs(wps - edge) * 4
    gaps = sum(
        max(b["s"] - a["e"], 0)
        for a, b in zip(c["words"], c["words"][1:])
        if b["s"] - a["e"] > 0.8
    )
    return clamp(s - 15.0 * gaps / d)


def score_local(c):
    d = c["end"] - c["start"]
    c["hook"] = hook_score(c)
    c["story"] = story_score(c)
    c["dur"] = duration_score(d)
    c["dens"] = density_score(c)
    c["local"] = 0.35 * c["hook"] + 0.30 * c["story"] + 0.15 * c["dur"] + 0.20 * c["dens"]


def overlaps(a, b, tol=2.0):
    return not (a["end"] - tol <= b["start"] or a["start"] + tol >= b["end"])


def pick_non_overlapping(items, key, limit):
    out = []
    for it in sorted(items, key=key, reverse=True):
        if all(not overlaps(it, o) for o in out):
            out.append(it)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- AI ENGINE: Gemini
def gemini_json(prompt):
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3},
    }
    models = []
    for m in (GEMINI_MODEL, "gemini-2.5-flash", "gemini-flash-latest"):
        if m and m not in models:
            models.append(m)
    last = "no response"
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(3):
            r = requests.post(
                url,
                headers={"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"},
                json=body,
                timeout=240,
            )
            if r.status_code in (429, 500, 503):
                last = f"{model}: {r.status_code} {r.text[:150]}"
                time.sleep(20 * (attempt + 1))
                continue
            if r.status_code in (400, 404):
                last = f"{model}: {r.status_code} {r.text[:200]}"
                break
            r.raise_for_status()
            resp = r.json()
            cand = (resp.get("candidates") or [{}])[0]
            parts = (cand.get("content") or {}).get("parts") or [{}]
            text = (parts[0].get("text") or "").strip()
            if not text:
                last = f"{model}: empty/blocked response {json.dumps(resp)[:200]}"
                break
            if text.startswith("```"):
                text = text.strip("`")
                if text.lower().startswith("json"):
                    text = text[4:]
            data = json.loads(text)
            if isinstance(data, dict):
                data = data.get("clips") or next((v for v in data.values() if isinstance(v, list)), [])
            return data
    raise RuntimeError(f"Gemini fail: {last}")


def gemini_rank(pool):
    """Gemini filter + rank for short-form virality.

    Hard criteria baked into the prompt:
      * HOOK    -> a <=3 second curiosity hook in the very first sentence,
      * TENSION -> high emotional tension / conflict / stakes in the middle,
      * PAYOFF  -> a clear, self-contained payoff at the end (zero prior context).
    Failed candidates get hook/story/virality = 0, so the weighted final score
    in rank_clips naturally filters them out.
    """
    blocks = []
    for k, c in enumerate(pool):
        blocks.append(
            f"[id {k}] {c['start']:.0f}s-{c['end']:.0f}s ({c['end'] - c['start']:.0f}s)\n{c['text'][:900]}"
        )
    prompt = f"""You are an elite short-form video editor for TikTok, YouTube Shorts, and Reels (ClipCut / Opus Clip style).
You will see {len(pool)} candidate chunks transcribed from a long video, each labeled [id N].

FILTER every candidate against these three hard criteria:
1. HOOK: the first sentence must work as a <=3 second curiosity hook (a question, a
   shocking/contrarian claim, a number, or an in-media-res story start). Openers that
   are generic, slow, filler, or need prior context FAIL this criterion (score 0).
2. TENSION: the middle must carry visible emotional tension, conflict, debate, or stakes.
3. PAYOFF: the ending must be a clear, self-contained payoff/conclusion the viewer can
   fully understand with ZERO context from the rest of the video. No dangling thoughts.

Then RANK the survivors for viral potential on Shorts/Reels.

For EACH candidate id return one JSON object with:
- id: the integer N from its [id N] label (REQUIRED, must match exactly).
- hook: 0-10 (0 = fails criterion 1, 10 = perfect 3-second hook)
- story: 0-10 (0 = no self-contained payoff, 10 = airtight ending)
- virality: 0-10 (overall rewatch/share potential AFTER filtering)
- title: Max 7 words, clicky/curious title with 1 emoji, same language script.
- description: 1-2 lines viral summary.
- hashtags: 4 viral hashtags.

Return ONLY a JSON array of these objects, one per candidate id, nothing else.

Candidates:
""" + "\n\n".join(blocks)

    data = gemini_json(prompt)
    out = {}
    for it in data if isinstance(data, list) else []:
        try:
            out[int(it["id"])] = it
        except Exception:
            pass
    return out


def fallback_title(c):
    words = c["segs"][0]["text"].split()
    return " ".join(words[:8]) or "Short"


def rank_clips(cands):
    for c in cands:
        score_local(c)
    pool = pick_non_overlapping(cands, lambda c: c["local"], max(NUM * 3, 12))
    g = {}
    if GEMINI_KEY:
        tg_text(f"{len(pool)} candidate moments mile. AI unhe score kar raha hai...")
        try:
            g = gemini_rank(pool)
        except Exception as e:
            log("Gemini failed:", e)
            tg_text(f"Gemini fail hua, sirf local scoring use ho rahi hai: {str(e)[:300]}")
    else:
        tg_text("GEMINI_KEY nahi mili, sirf local scoring use ho rahi hai.")

    for k, c in enumerate(pool):
        it = g.get(k)
        c["gemini"] = None
        c["title"] = fallback_title(c)
        c["description"] = ""
        c["hashtags"] = ""
        if it:
            gs = 0.4 * clamp(num(it.get("hook"), 5)) + 0.3 * clamp(num(it.get("story"), 5)) \
                + 0.3 * clamp(num(it.get("virality"), 5))
            c["gemini"] = round(gs, 2)
            c["final"] = 0.4 * c["local"] + 0.6 * gs
            if str(it.get("title", "")).strip():
                c["title"] = str(it["title"]).strip()
            c["description"] = str(it.get("description", "")).strip()
            tags = it.get("hashtags") or []
            if isinstance(tags, str):
                tags = tags.split()
            c["hashtags"] = " ".join(str(t) for t in tags[:6])
        else:
            c["final"] = c["local"]
    return pick_non_overlapping(pool, lambda c: c["final"], NUM)


# ---------------------------------------------------------------- VIDEO ENGINE
def keep_intervals(words, dur, gap_thr=0.9, pad=0.25):
    """Clip ke andar lambi khamoshi (pauses) kaat do. Times clip ke start se relative."""
    if not words:
        return [(0.0, dur)]
    iv = []
    cur_s = 0.0
    if words[0]["s"] > gap_thr:
        cur_s = max(words[0]["s"] - pad, 0.0)
    prev_e = words[0]["e"]
    for w in words[1:]:
        if w["s"] - prev_e > gap_thr:
            iv.append((cur_s, min(prev_e + pad, dur)))
            cur_s = max(w["s"] - pad, iv[-1][1])
        prev_e = max(prev_e, w["e"])
    end = dur if dur - prev_e <= gap_thr else min(prev_e + 0.5, dur)
    iv.append((cur_s, end))
    return [(a, b) for a, b in iv if b - a > 0.1] or [(0.0, dur)]


def make_mapper(iv):
    offs, acc = [], 0.0
    for a, b in iv:
        offs.append((a, b, acc))
        acc += b - a

    def f(t):
        for a, b, o in offs:
            if t <= b:
                return o + max(t - a, 0.0)
        return acc

    return f, acc


def face_centers(video, ps, pe):
    try:
        import cv2
    except Exception:
        return []
    xs = []
    cap = None
    try:
        cap = cv2.VideoCapture(str(video))
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        n = 8
        for k in range(n):
            t = ps + (pe - ps) * (k + 0.5) / n
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if not ok:
                continue
            if frame.shape[1] > 640:
                sc = 640.0 / frame.shape[1]
                frame = cv2.resize(frame, None, fx=sc, fy=sc)
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            wmin = int(0.08 * frame.shape[1])
            faces = cascade.detectMultiScale(gray, 1.2, 5, minSize=(wmin, wmin))
            if len(faces) == 0:
                continue
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
            xs.append((x + w / 2.0) / frame.shape[1])
    except Exception as e:
        log("face detect failed:", e)
    finally:
        if cap is not None:
            cap.release()
    return xs


def decide_layout(video, info, ps, pe):
    iw, ih = info["w"], info["h"]
    if iw <= ih:
        return {"mode": "fill"}
    if LAYOUT == "blur":
        return {"mode": "blur"}
    xs = face_centers(video, ps, pe)
    cw = int(ih * 9 / 16) // 2 * 2
    if LAYOUT == "crop":
        cx = statistics.median(xs) if xs else 0.5
    else:
        if len(xs) >= 4 and max(xs) - min(xs) <= 0.25:
            cx = statistics.median(xs)
        else:
            return {"mode": "blur"}
    x = int(min(max(cx * iw - cw / 2, 0), iw - cw)) // 2 * 2
    return {"mode": "crop", "cw": cw, "x": x, "ih": ih}


def build_filter(iv, layout, ass, audio):
    parts = []
    n = len(iv)
    for k, (a, b) in enumerate(iv):
        parts.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{k}]")
        if audio:
            parts.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[a{k}]")
    if audio:
        for k in range(n):
            parts.append(f"[a{k}]aresample=async=1:first_pts=0[a{k}r]")
        ins = "".join(f"[v{k}][a{k}r]" for k in range(n))
        parts.append(f"{ins}concat=n={n}:v=1:a=1[vc][ac]")
        parts.append("[ac]loudnorm=I=-16:TP=-1.5:LRA=11[aout]")
    else:
        ins = "".join(f"[v{k}]" for k in range(n))
        parts.append(f"{ins}concat=n={n}:v=1:a=0[vc]")

    mode = layout["mode"]
    if mode == "crop":
        parts.append(
            f"[vc]crop={layout['cw']}:{layout['ih']}:{layout['x']}:0,scale=1080:1920,ass={ass}[vout]"
        )
    elif mode == "fill":
        parts.append(
            f"[vc]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,ass={ass}[vout]"
        )
    else:
        parts.append(
            "[vc]split[s1][s2];"
            "[s1]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:5[bg];"
            "[s2]scale=1080:-2[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,ass={ass}[vout]"
        )
    return ";".join(parts)


def render(video, ps, pe, iv, layout, ass, audio, out, script_path):
    Path(script_path).write_text(build_filter(iv, layout, ass, audio))
    cmd = [
        "ffmpeg", "-y", "-ss", f"{ps:.2f}", "-t", f"{pe - ps:.2f}", "-i", video,
        "-filter_complex_script", script_path,
        "-map", "[vout]",
    ]
    cmd += ["-map", "[aout]"] if audio else ["-an"]
    cmd += [
        "-r", "30",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-maxrate", "4M", "-bufsize", "8M", "-pix_fmt", "yuv420p",
    ]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    cmd += ["-movflags", "+faststart", out]
    sh(cmd)


# ---------------------------------------------------------------- CAPTION ENGINE
def ass_time(t):
    t = max(t, 0)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def ass_clean(t):
    return t.replace("{", "").replace("}", "").replace("\\", "").replace("\n", " ").strip()


def chunk_words(words):
    chunks, cur = [], []
    for w in words:
        too_long = sum(len(x["w"]) for x in cur) + len(w["w"]) > 22
        if cur and (len(cur) >= 3 or w["s"] - cur[-1]["e"] > 0.8 or too_long):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def make_ass(words, path, title=""):
    """Burned-in ASS captions for 1080x1920 shorts:
    - bold, high-contrast WHITE text on an OPAQUE BLACK bounding box
      (BorderStyle 3 = box, Outline = box padding; OutlineColour AND BackColour
      are both pure black so the box stays dark on every libass/VSFilter build),
    - horizontally centered (Alignment 2 = bottom-center, MarginV 480),
    - word-by-word karaoke: the active word flips to bright yellow (&H00FFFF = BGR yellow).
    """
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,"
        "Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
        "Alignment,MarginL,MarginR,MarginV,Encoding\n"
        "Style: Default,DejaVu Sans,78,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
        "-1,0,0,0,100,100,0,0,3,10,0,2,60,60,480,1\n"
        "Style: Title,DejaVu Sans,65,&H0000FFFF,&H00FFFFFF,&H00000000,&H00000000,"
        "-1,0,0,0,100,100,0,0,3,8,0,8,80,80,230,1\n\n"
        "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    )
    lines = []
    title = ass_clean(title)[:70]
    if title:
        lines.append(
            f"Dialogue: 1,{ass_time(0)},{ass_time(3.5)},Title,,0,0,0,,{{\\fad(250,350)}}{title}"
        )
    chunks = chunk_words(words)
    for ci, ch in enumerate(chunks):
        next_start = chunks[ci + 1][0]["s"] if ci + 1 < len(chunks) else None
        for j, w in enumerate(ch):
            t0 = max(w["s"], 0)
            if j + 1 < len(ch):
                t1 = ch[j + 1]["s"]
            else:
                t1 = w["e"] + 0.15
                if next_start is not None:
                    t1 = min(t1, next_start)
            t1 = max(t1, t0 + 0.05)
            parts = []
            for k, x in enumerate(ch):
                txt = ass_clean(x["w"]).upper()
                if k == j:
                    parts.append("{\\c&H00FFFF&}" + txt + "{\\c&HFFFFFF&}")
                else:
                    parts.append(txt)
            lines.append(
                f"Dialogue: 0,{ass_time(t0)},{ass_time(t1)},Default,,0,0,0,,{' '.join(parts)}"
            )
    Path(path).write_text(header + "\n".join(lines), encoding="utf-8")

    
# ---------------------------------------------------------------- OUTPUT
def process_clip(idx, c, video, info, duration):
    ps = max(c["start"] - 0.2, 0.0)
    pe = min(c["end"] + 0.35, duration)
    rel = [
        {"w": w["w"], "s": w["s"] - ps, "e": w["e"] - ps}
        for w in c["words"]
        if w["s"] >= ps - 0.05 and w["e"] <= pe + 0.05
    ]
    dur = pe - ps
    thr = 0.9
    iv = keep_intervals(rel, dur, thr)
    while len(iv) > 45:
        thr += 0.4
        iv = keep_intervals(rel, dur, thr)
    mapper, new_dur = make_mapper(iv)
    mapped = [{"w": w["w"], "s": mapper(w["s"]), "e": mapper(w["e"])} for w in rel]

    tag = f"{idx:02d}"
    ass = str(WORK / f"clip{tag}.ass")
    out = str(OUT / f"short_{tag}.mp4")
    make_ass(mapped, ass, c["title"])
    layout = decide_layout(video, info, ps, pe)
    render(str(video), ps, pe, iv, layout, ass, info["audio"], out,
           str(WORK / f"filter{tag}.txt"))
    c["file"] = f"short_{tag}.mp4"
    c["layout"] = layout["mode"]
    c["out_duration"] = round(new_dur, 1)
    c["trimmed_seconds"] = round(dur - new_dur, 1)
    return out


def caption_for(idx, c):
    cap = f"{idx}. {c['title']}"
    if c.get("description"):
        cap += f"\n\n{c['description']}"
    if c.get("hashtags"):
        cap += f"\n{c['hashtags']}"
    cap += f"\n\nScore {c['final']:.1f}/10 | {c['start']:.0f}s-{c['end']:.0f}s"
    return cap


def write_metadata(clips):
    meta = []
    lines = []
    for i, c in enumerate(clips, 1):
        meta.append({
            "file": c.get("file"),
            "title": c["title"],
            "description": c["description"],
            "hashtags": c["hashtags"],
            "start": round(c["start"], 2),
            "end": round(c["end"], 2),
            "duration_after_trim": c.get("out_duration"),
            "layout": c.get("layout"),
            "scores": {
                "hook": round(c["hook"], 2),
                "story": round(c["story"], 2),
                "duration": round(c["dur"], 2),
                "density": round(c["dens"], 2),
                "local": round(c["local"], 2),
                "gemini": c.get("gemini"),
                "final": round(c["final"], 2),
            },
            "transcript": c["text"][:600],
        })
        lines.append(f"{i:02d}. {c['title']}\n{c['description']}\n{c['hashtags']}\n")
    (OUT / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "titles.txt").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------- main
def main():
    log("Byte Clipper V3 started")
    OUT.mkdir(exist_ok=True)
    WORK.mkdir(exist_ok=True)
    tg_text(f"Job shuru ho gayi (V3).\n{URL or 'Telegram video'}")

    video = get_input()
    info = probe(video)
    tg_text("Video mil gayi. Transcribe ho raha hai (thoda time lagega)...")
    segments, duration = transcribe(video)
    if not segments:
        raise RuntimeError("Transcript khali hai (video me awaaz nahi?)")

    cands = []
    for lo in (MIN_LEN, 15, 5):
        cands = detect_candidates(segments, lo, MAX_LEN)
        if cands:
            break
    if not cands:
        raise RuntimeError("Is video me clip banane layak hissa nahi mila (bahut chhoti/khali).")

    clips = rank_clips(cands)
    summary = "\n".join(
        f"{i}. {c['title']} ({c['start']:.0f}s-{c['end']:.0f}s, score {c['final']:.1f})"
        for i, c in enumerate(clips, 1)
    )
    tg_text(f"Top {len(clips)} clips ban rahi hain:\n{summary}")

    sent = 0
    for i, c in enumerate(clips, 1):
        try:
            out = process_clip(i, c, video, info, duration)
            size_mb = os.path.getsize(out) / 1e6
            if size_mb > 49:
                tg_text(f"short_{i:02d} {size_mb:.0f}MB hai, Telegram limit se bada. Artifact me milega.")
                continue
            tg_video(out, caption_for(i, c))
            sent += 1
        except Exception as e:
            log(traceback.format_exc())
            tg_text(f"Clip {i} fail: {str(e)[:200]}")

    write_metadata([c for c in clips if c.get("file")])
    tg_doc(OUT / "metadata.json", "metadata (scores, titles, hashtags)")
    tg_text(f"Ho gaya. {sent}/{len(clips)} shorts ready to upload.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(traceback.format_exc())
        tg_text(f"Job fail: {str(e)[:500]}")
        sys.exit(1)
