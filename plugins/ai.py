"""
জেনেরিক AI ইঞ্জিন।
কোনো সাইটের নাম/URL/মডেল এই ফাইলে নেই — সব info.py (PROVIDERS, ROUTES) থেকে আসে।
নতুন সাইট যোগ করতে এই ফাইলে হাত দিতে হয় না।
"""
import asyncio
import base64
import json
import logging
import os
import re
import tempfile
import time

import aiohttp

import info
from plugins import media, store

LOGGER = logging.getLogger(__name__)
CAP_LABEL = {"text": "টেক্সট", "vision": "ছবি-বিশ্লেষণ", "transcribe": "অডিও→টেক্সট", "tts": "ভয়েস",
             "video": "ভিডিও", "lipsync": "লিপ-সিঙ্ক"}
_MISSING = object()
_PH = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_AUTH_WORDS = ("api key", "api_key", "apikey", "unauthorized", "unauthenticated", "invalid key",
               "quota", "rate limit", "billing", "credit", "balance", "permission", "forbidden", "expired")


class AIError(Exception):
    def __init__(self, msg, retry=False, status=0):
        super().__init__(msg)
        self.retry = retry    # true = অন্য key / অন্য সাইটে চেষ্টা করা যায়
        self.status = status  # HTTP স্ট্যাটাস (জানা থাকলে)


# ───────────── টেমপ্লেট ইঞ্জিন ─────────────
def render(obj, v: dict):
    """{name} বসায়। মান না থাকলে ওই ফিল্ড/অবজেক্ট বাদ যায় (_MISSING)।"""
    if isinstance(obj, str):
        ms = list(_PH.finditer(obj))
        if not ms:
            return obj
        if len(ms) == 1 and ms[0].span() == (0, len(obj)):
            x = v.get(ms[0].group(1))
            return _MISSING if x is None else x
        for m in ms:
            if v.get(m.group(1)) is None:
                return _MISSING
        return _PH.sub(lambda m: str(v[m.group(1)]), obj)
    if isinstance(obj, dict):
        out = {k: r for k, r in ((k, render(x, v)) for k, x in obj.items()) if r is not _MISSING}
        return out if out or not obj else _MISSING
    if isinstance(obj, list):
        out = [r for r in (render(x, v) for x in obj) if r is not _MISSING]
        return out if out or not obj else _MISSING
    return obj


def _r(obj, v):
    x = render(obj, v)
    return {} if x is _MISSING else x


def get_path(obj, path):
    """'a.b.0.c' ধরনের পথ ধরে মান তোলে।"""
    for p in str(path).split("."):
        if isinstance(obj, list):
            try:
                obj = obj[int(p)]
            except (ValueError, IndexError):
                return None
        elif isinstance(obj, dict):
            obj = obj.get(p)
        else:
            return None
        if obj is None:
            return None
    return obj


_BUSY_WORDS = ("high demand", "overloaded", "unavailable", "try again later", "temporarily")


def _transient(e) -> bool:
    """সার্ভারে সাময়িক চাপ/বিভ্রাট — একই অনুরোধ একটু পরে আবার করলে ঠিক হতে পারে।"""
    return e.status in (500, 502, 503, 504) or any(w in str(e).lower() for w in _BUSY_WORDS)


def _norm(x):
    return str(x).strip().lower()


# ───────────── HTTP ─────────────
def _err_text(js) -> str:
    if not isinstance(js, dict):
        return str(js)[:200]
    e = js.get("error")
    if isinstance(e, dict):
        e = e.get("message") or e
    if not e:
        e = js.get("detail") or js.get("message") or js.get("title") or ""
    if isinstance(e, list):
        e = "; ".join(str(x.get("msg", x)) if isinstance(x, dict) else str(x) for x in e)
    return str(e)[:200]


async def _req(method, url, *, headers=None, body=None, data=None, timeout=300, raw=False, who=""):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.request(method, url, headers=headers, json=body, data=data) as r:
                if raw:
                    if r.status >= 400:
                        raise AIError(f"{who} ডাউনলোড ব্যর্থ (HTTP {r.status})", retry=r.status >= 500)
                    return await r.read()
                try:
                    js = await r.json(content_type=None)
                except Exception:
                    js = {"error": {"message": (await r.text())[:200]}}
                if r.status >= 400:
                    msg = _err_text(js)
                    retry = r.status in (401, 402, 403, 404, 408, 429) or r.status >= 500 \
                        or any(w in msg.lower() for w in _AUTH_WORDS)
                    raise AIError(f"{who} HTTP {r.status}: {msg}", retry=retry, status=r.status)
                return js
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise AIError(f"{who} নেটওয়ার্ক ত্রুটি: {type(e).__name__}", retry=True)


def _json(text: str):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"[\[{]", text)
    if not m:
        raise AIError("AI থেকে সঠিক JSON আসেনি")
    try:
        return json.JSONDecoder().raw_decode(text[m.start():])[0]
    except json.JSONDecodeError as e:
        raise AIError(f"JSON পার্স হয়নি: {e}")


# ───────────── প্রোভাইডার ও key ─────────────
def providers() -> dict:
    return info.PROVIDERS


async def keys_for(name: str) -> list:
    """মেনুতে ON key আগে, তারপর info.py/env-এর key (ডুপ্লিকেট বাদ)।"""
    out = []
    try:
        k = await store.get_key(0, name)
    except Exception as e:  # ডাটাবেস সমস্যা হলেও env key দিয়ে চলবে
        LOGGER.warning("db key read failed: %s", e)
        k = None
    for x in (k, info.PROVIDERS.get(name, {}).get("key")):
        if x and x not in out:
            out.append(x)
    return out


def _spec(prov: dict, cap: str, has_image=False):
    caps = prov.get("caps", {})
    if has_image and cap + "_image" in caps:
        return caps[cap + "_image"]
    return caps.get(cap)


def _order(cap: str) -> list:
    names = [n for n in info.ROUTES.get(cap, []) if n in info.PROVIDERS]
    names += [n for n, p in info.PROVIDERS.items()
              if n not in names and (cap in p.get("caps", {}) or cap + "_image" in p.get("caps", {}))]
    return names


async def available(cap: str) -> bool:
    for n in _order(cap):
        p = info.PROVIDERS[n]
        if (cap in p.get("caps", {}) or cap + "_image" in p.get("caps", {})) and await keys_for(n):
            return True
    return False


async def check(provider: str, key: str) -> bool:
    """অ্যাডমিন key যোগ করার সময় যাচাই। check না থাকলে গ্রহণ করে নেয়।"""
    p = info.PROVIDERS.get(provider)
    if not p:
        return False
    c = p.get("check")
    if not c:
        return True
    v = {"key": key}
    try:
        await _req(c.get("method", "GET"), render(c["url"], v), headers=_r(p.get("headers", {}), v),
                   timeout=20, who=provider)
        return True
    except Exception:
        return False


# ───────────── মডেল নিজে খুঁজে নেওয়া (discover) ─────────────
_DISC: dict = {}


def _ver(name: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)", name)
    return float(m.group(1)) if m else 0.0


async def _models(name, prov, spec, key, v) -> list:
    """চেষ্টার ক্রমে মডেলের তালিকা। নির্দিষ্ট মডেল দিলে শুধু সেটি; auto হলে সেরা ৩টি + fallback।"""
    m = spec.get("model")
    if m and m != "auto":
        return [m]
    out = []
    d = spec.get("discover")
    if d:
        ck = (name, d["url"], key)
        c = _DISC.get(ck)
        try:
            if not c or time.monotonic() - c[0] > 3600:
                js = await _req("GET", render(d["url"], v), headers=_r(prov.get("headers", {}), v),
                                timeout=30, who=name)
                _DISC[ck] = c = (time.monotonic(), get_path(js, d["list"]) or [])
            names = []
            for it in c[1]:
                n = str(get_path(it, d.get("name", "name")) or "")
                if d.get("strip") and n.startswith(d["strip"]):
                    n = n[len(d["strip"]):]
                ms = get_path(it, d["methods"]) if d.get("methods") else None
                if d.get("method") and d["method"] not in (ms or []):
                    continue
                if d.get("prefix") and not n.startswith(d["prefix"]):
                    continue
                if any(x not in n for x in d.get("include", [])) or any(x in n for x in d.get("exclude", [])):
                    continue
                names.append(n)
            pref = d.get("prefer", [])
            names.sort(key=lambda n: (_ver(n), any(x in n for x in pref), "preview" not in n), reverse=True)
            out = names[:spec.get("try_models", 3)]
        except Exception as e:
            LOGGER.warning("model discover failed (%s): %s", name, e)
    fb = spec.get("fallback")
    if fb and fb not in out:
        out.append(fb)
    return out or [m or ""]


# ───────────── ফাইল ইনপুট (ছবি/ভিডিও/অডিও) ─────────────
async def _upload(name, prov, key, data: bytes, mime, fname) -> str:
    u = prov["upload"]
    v = {"key": key}
    fd = aiohttp.FormData()
    fd.add_field(u.get("field", "file"), data, filename=fname, content_type="application/octet-stream")
    js = await _req("POST", u["url"], headers=_r(prov.get("headers", {}), v), data=fd, timeout=300, who=name)
    url = get_path(js, u["out"])
    if not url:
        raise AIError(f"{name}: ফাইল আপলোডের URL পাওয়া যায়নি", retry=True)
    return url


async def _bind_files(name, prov, spec, key, files: dict, v: dict):
    """files = {"image": (bytes, mime, filename)} → {image_b64, image_mime, image_url} ভ্যারিয়েবল।"""
    for k, (data, mime, fname) in (files or {}).items():
        b64 = base64.b64encode(data).decode()
        v[f"{k}_b64"], v[f"{k}_mime"] = b64, mime
        if spec.get("upload") and prov.get("upload"):
            v[f"{k}_url"] = await _upload(name, prov, key, data, mime, fname)
        else:
            v[f"{k}_url"] = f"data:{mime};base64,{b64}"


# ───────────── একটি সাইটে একটি কাজ চালানো ─────────────
async def _to_pcm(data: bytes) -> bytes:
    """mp3/wav ইত্যাদি → PCM s16le 24kHz mono।"""
    d = tempfile.mkdtemp(prefix="pcm_")
    a, b = os.path.join(d, "in.bin"), os.path.join(d, "out.pcm")
    try:
        open(a, "wb").write(data)
        await media.run("ffmpeg", "-y", "-i", a, "-f", "s16le", "-ar", str(media.RATE), "-ac", "1", b)
        return open(b, "rb").read()
    except media.MediaError as e:
        raise AIError(f"অডিও কনভার্ট ব্যর্থ: {e}")
    finally:
        for f in (a, b):
            if os.path.exists(f):
                os.remove(f)
        os.rmdir(d)


async def _call(name, prov, spec, key, vars_, files, tick):
    v = {**vars_, "key": key}
    hdr = lambda extra=None: _r({**prov.get("headers", {}), **(extra or {})}, v)
    models = await _models(name, prov, spec, key, v)
    await _bind_files(name, prov, spec, key, files, v)
    if "voice_map" in spec and v.get("voice"):
        v["voice"] = spec["voice_map"].get(v["voice"], v["voice"])

    delays = spec.get("retry_delays", (2, 5))  # সার্ভার ব্যস্ত (503) হলে এতক্ষণ পরপর আবার চেষ্টা
    js, last = None, None
    for mdl in models:
        v["model"] = mdl
        url = render(spec["url"], v)
        if url is _MISSING:
            raise AIError(f"{name}: URL-এর প্লেসহোল্ডার মেলেনি")
        for attempt in range(len(delays) + 1):
            try:
                js = await _req(spec.get("method", "POST"), url, headers=hdr(spec.get("headers")),
                                body=_r(spec.get("body"), v) if spec.get("body") is not None else None,
                                timeout=spec.get("timeout", 120), who=name)
                break
            except AIError as e:
                last = e
                if not _transient(e):
                    raise
                if attempt < len(delays):
                    LOGGER.warning("%s (%s) ব্যস্ত, %ss পরে আবার: %s", name, mdl, delays[attempt], e)
                    await asyncio.sleep(delays[attempt])
        if js is not None:
            break
        LOGGER.warning("%s: মডেল %s ব্যস্ত, পরের মডেলে যাচ্ছি", name, mdl)
    if js is None:
        last.retry = True  # অন্য key/সাইটে চেষ্টা করার সুযোগ থাকুক
        raise last

    w = spec.get("wait")
    if w:
        v["id"] = get_path(js, w["id"]) if w.get("id") else None
        if w.get("id") and not v["id"]:
            raise AIError(f"{name}: জব শুরু হয়নি ({_err_text(js) or 'id নেই'})", retry=True)
        st_url = (get_path(js, w["status_url_path"]) if w.get("status_url_path") else None) \
            or (render(w["status_url"], v) if w.get("status_url") else None)
        if st_url is None or st_url is _MISSING:
            raise AIError(f"{name}: স্ট্যাটাস URL পাওয়া যায়নি")
        done, fail = [_norm(x) for x in w.get("done", [])], [_norm(x) for x in w.get("fail", [])]
        sub = js  # সাবমিটের রেসপন্স (এখানেই result URL থাকে)
        iv, limit, t0, cur = w.get("interval", 5), w.get("timeout", 600), time.monotonic(), js
        while True:
            err = get_path(cur, w["error"]) if w.get("error") else None
            if err:
                msg = err.get("message") if isinstance(err, dict) else err
                raise AIError(f"{name}: {str(msg)[:200]}", retry=True)
            s = _norm(get_path(cur, w["status"])) if w.get("status") else ""
            if s in fail:
                raise AIError(f"{name}: কাজ ব্যর্থ ({s})", retry=True)
            if s in done:
                break
            await asyncio.sleep(iv)
            el = time.monotonic() - t0
            if tick:
                await tick(el)
            if el > limit:
                raise AIError(f"{name}: অতিরিক্ত সময় লাগছে")
            cur = await _req("GET", st_url, headers=hdr(), timeout=60, who=name)
        js = cur
        rurl = ((get_path(sub, w["result_url_path"]) or get_path(cur, w["result_url_path"]))
                if w.get("result_url_path") else None) \
            or (render(w["result_url"], v) if w.get("result_url") else None)
        if rurl and rurl is not _MISSING:
            js = await _req("GET", rurl, headers=hdr(), timeout=120, who=name)

    val = get_path(js, spec["out"])
    if isinstance(val, list):
        val = val[0] if val else None
    if isinstance(val, dict):
        val = val.get("url") or val.get("uri")
    if not val:
        why = get_path(js, spec["out_fail"]) if spec.get("out_fail") else None
        raise AIError(f"{name}: কোনো ফলাফল আসেনি" + (f" ({str(why)[:150]})" if why else " (নিরাপত্তা ফিল্টার হতে পারে)"))

    t = spec.get("out_type", "text")
    if t == "text":
        return str(val)
    if t == "b64":
        data = base64.b64decode(str(val).split(",", 1)[-1])
    else:
        data = await _req("GET", str(val), headers=hdr() if spec.get("out_auth") else None,
                          timeout=300, raw=True, who=name)
    if spec.get("audio") == "file":
        data = await _to_pcm(data)
    return data


async def run(cap: str, vars_: dict, files=None, tick=None, has_image=False):
    """ক্রমানুযায়ী সাইট ও key চেষ্টা করে। অথেন্টিকেশন/কোটা/সার্ভার সমস্যায় পরেরটিতে যায়।"""
    last = None
    for name in _order(cap):
        prov = info.PROVIDERS[name]
        spec = _spec(prov, cap, has_image)
        if not spec:
            continue
        for key in await keys_for(name):
            try:
                return await _call(name, prov, spec, key, vars_, files, tick)
            except AIError as e:
                last = e
                LOGGER.warning("%s/%s ব্যর্থ: %s", name, cap, e)
                if not e.retry:
                    raise
    if last:
        raise last
    raise AIError(f"{CAP_LABEL.get(cap, cap)}-এর জন্য কোনো AI API চালু নেই")


# ───────────── টেক্সট ও ইউটিলিটি ─────────────
async def text(prompt, media_bytes=None, mime=None, as_json=False, cap="text") -> str:
    v = {"prompt": prompt, "json_mime": "application/json" if as_json else None,
         "json_type": "json_object" if as_json else None}
    if media_bytes:
        b64 = base64.b64encode(media_bytes).decode()
        v.update(media_b64=b64, media_mime=mime, media_data_uri=f"data:{mime};base64,{b64}")
    return await run(cap, v)


async def understand(text_: str, voices) -> dict:
    prompt = (
        "You are the intent router of a Telegram bot for AI video creation, AI voice and video dubbing. "
        "Read the user's message (Bengali/Hindi/English) and reply ONLY with JSON "
        '{"action": <one of create_video, speak, add_character, set_voice, set_language, subtitles, lipsync, dub, api, chat>, "args": {...}}.\n'
        "- create_video: args.prompt (full scene text)\n- speak: args.text\n"
        "- add_character: args.name, args.description\n"
        f"- set_voice: args.character, args.voice (one of {list(voices)})\n"
        '- set_language: args.language ("bn"|"hi")\n- subtitles / lipsync: args.on (true|false)\n'
        '- dub: args.language ("bn"|"hi")\n- api: args.provider, args.op ("add"|"delete")\n'
        "- chat: args.reply (short helpful reply in the user's language)\n\n"
        f"User message:\n{text_}"
    )
    d = _json(await text(prompt, as_json=True))
    return d if isinstance(d, dict) else {"action": "chat", "args": {"reply": "বুঝতে পারিনি।"}}


async def plan_story(text_: str, lang_name: str, saved: list, max_scenes: int) -> dict:
    """গল্প থেকে চরিত্র + দৃশ্য + সংলাপের পরিকল্পনা বানায়।"""
    known = [{"name": c["name"], "description": c.get("desc", "")} for c in saved]
    prompt = (
        "You are a film director's assistant. Turn the user's story/idea into a plan for short AI video clips "
        f"(each clip is about 8 seconds). Return ONLY JSON of this shape:\n"
        '{"characters":[{"name":str,"gender":"male"|"female","description":str}],'
        '"scenes":[{"visual":str,"continuous":bool,"dialogue":[{"speaker":str,"text":str}]}]}\n'
        f"Rules:\n- At most {max_scenes} scenes, in story order. If the story is long, compress it into the key moments.\n"
        "- 'visual': one cinematic English paragraph (setting, action, camera, lighting). No text/subtitles on screen.\n"
        "- characters: every person/animal who appears. 'description' in English: age, face, hair, skin tone, clothes - "
        "detailed and fixed so the character looks the same in all scenes. Use the character's name exactly as in the story.\n"
        f"- dialogue: spoken lines in natural spoken {lang_name}, at most ~20 words per scene in total. "
        "Use speaker \"\" for a narrator. If the story has no dialogue, write short fitting dialogue or narration. "
        "Keep any dialogue the user wrote.\n"
        "- continuous: true only if the scene continues in the same place right after the previous scene.\n"
        f"- Already saved characters (reuse these exact names, do not redefine them): {json.dumps(known, ensure_ascii=False)}\n\n"
        f"Story:\n{text_}"
    )
    d = _json(await text(prompt, as_json=True))
    if not isinstance(d, dict) or not d.get("scenes"):
        raise AIError("গল্প থেকে দৃশ্য বের করা যায়নি, আরেকটু বিস্তারিত লিখুন")
    d["scenes"] = d["scenes"][:max_scenes]
    d["characters"] = [c for c in d.get("characters", []) if isinstance(c, dict) and c.get("name")]
    return d


async def describe_image(img: bytes, mime="image/jpeg") -> str:
    return (await text(
        "Describe this person/character's appearance in one dense English sentence for a video generator "
        "(face, age, hair, skin tone, clothing, distinctive features). No preamble.",
        img, mime, cap="vision")).strip()


async def transcribe(audio: bytes, mime="audio/mp3") -> list:
    prompt = (
        "Transcribe all speech in this audio. Return ONLY a JSON array of objects "
        '{"start": seconds(float), "end": seconds(float), "speaker": "S1"|"S2"|..., "text": "..."}. '
        "Split into natural sentences/phrases (max ~12 seconds each), keep the original language, "
        "ignore music and noise. Return [] if no speech."
    )
    d = _json(await text(prompt, audio, mime, as_json=True, cap="transcribe"))
    segs = []
    for x in d if isinstance(d, list) else []:
        try:
            s, e = float(x["start"]), float(x["end"])
            t = str(x["text"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if t and e > s:
            segs.append({"start": s, "end": e, "speaker": str(x.get("speaker", "S1")), "text": t})
    segs.sort(key=lambda z: z["start"])
    return segs


async def translate_lines(lines: list, lang_name: str) -> list:
    out = []
    for i in range(0, len(lines), 40):
        chunk = lines[i:i + 40]
        prompt = (
            f"Translate every item of this JSON array into natural spoken {lang_name}. "
            f"Keep the meaning, keep length similar (it will be dubbed). If an item is already in {lang_name}, "
            f"keep it unchanged. Return ONLY a JSON array of strings with exactly {len(chunk)} items.\n"
            + json.dumps(chunk, ensure_ascii=False)
        )
        res = _json(await text(prompt, as_json=True))
        if not isinstance(res, list) or len(res) != len(chunk):
            raise AIError("অনুবাদের লাইন সংখ্যা মেলেনি, আবার চেষ্টা করুন")
        out += [str(x) for x in res]
    return out


# ───────────── ভয়েস / ভিডিও / লিপ-সিঙ্ক ─────────────
async def tts(text_: str, voice: str) -> bytes:
    """PCM s16le 24kHz mono ফেরত দেয়।"""
    last = None
    for attempt in range(3):
        try:
            return await run("tts", {"text": text_, "voice": voice})
        except AIError as e:
            last = e
            await asyncio.sleep(2 * (attempt + 1))
    raise AIError(f"ভয়েস তৈরি ব্যর্থ: {last}")


async def generate_video(prompt, image=None, mime="image/jpeg", aspect="16:9", tick=None) -> bytes:
    files = {"image": (image, mime, "ref.jpg")} if image else None
    return await run("video", {"prompt": prompt, "aspect": aspect}, files=files, tick=tick,
                     has_image=bool(image))


async def lipsync(video_path, wav_path, tick=None) -> bytes:
    files = {"video": (open(video_path, "rb").read(), "video/mp4", os.path.basename(video_path)),
             "audio": (open(wav_path, "rb").read(), "audio/wav", os.path.basename(wav_path))}
    return await run("lipsync", {}, files=files, tick=tick)
