"""
╔══════════════════════════════════════════════════════════════════════════╗
║  এই ফাইলই সব AI সাইটের একমাত্র কনফিগ।                                    ║
║  নতুন সাইট যোগ / key বদল / মডেল বদল = শুধু এই ফাইল বদলান।                  ║
║  অন্য কোনো .py ফাইলে হাত দিতে হবে না।                                      ║
╚══════════════════════════════════════════════════════════════════════════╝

নতুন সাইট (যেমন "pal") যোগ করার নিয়ম — নিচের PROVIDERS-এ একটি ব্লক যোগ করুন:

    PROVIDERS["pal"] = {
        "label":   "Pal (ভিডিও)",                      # অ্যাডমিন মেনুতে যে নাম দেখাবে
        "key":     environ.get("PAL_API_KEY", ""),                 # ঐচ্ছিক ডিফল্ট key (মেনু থেকেও key দেওয়া যায়)
        "headers": {"Authorization": "Bearer {key}"},  # সব রিকোয়েস্টে যাবে
        "check":   {"url": "https://api.pal.com/v1/me"},   # ঐচ্ছিক: key যাচাইয়ের URL
        "caps": {                                      # এই সাইট কোন কোন কাজ পারে
            "video": {
                "url":  "https://api.pal.com/v1/video",
                "body": {"prompt": "{prompt}", "ratio": "{aspect}"},
                "wait": {"id": "id", "status_url": "https://api.pal.com/v1/video/{id}",
                         "status": "state", "done": ["ready"], "fail": ["error"]},
                "out": "video_url", "out_type": "url",
            },
        },
    }

ব্যস! বট নিজে থেকে "pal" কে ভিডিও-র ফলব্যাক হিসেবে ব্যবহার করবে।
আগে ব্যবহার করাতে চাইলে নিচের ROUTES-এ "video": ["pal", "gemini"] লিখুন।

কাজের ধরন (caps): text, vision, transcribe, tts, video, video_image, lipsync

রিকোয়েস্ট বডিতে ব্যবহারযোগ্য {প্লেসহোল্ডার}:
    {key} {model}                       — সব ক্ষেত্রে
    {prompt}                            — text / vision / transcribe / video
    {json_mime} {json_type}             — text-এ JSON চাইলে (না চাইলে ফিল্ডটি নিজে বাদ যায়)
    {media_b64} {media_mime} {media_data_uri}   — vision / transcribe (ছবি/অডিও)
    {text} {voice}                      — tts
    {aspect}                            — video (যেমন 16:9)
    {image_b64} {image_mime} {image_url}        — video_image (ও video-তে ছবি থাকলে)
    {video_url} {audio_url}             — lipsync
    {id}                                — wait (জব-আইডি) এর URL-এ
কোনো প্লেসহোল্ডারের মান না থাকলে ওই ফিল্ড/অবজেক্ট রিকোয়েস্ট থেকে নিজে বাদ যায়।

আউটপুট ধরন (out_type): "text" | "b64" (base64 ডেটা) | "url" (ডাউনলোড করার লিংক)
out = রেসপন্সের ভেতরে ফলাফলের পথ, যেমন "choices.0.message.content"
tts-এ সাইট mp3/wav দিলে "audio": "file" লিখুন (বট নিজে PCM-এ কনভার্ট করবে)।
"""
from os import environ


# ───────────────────────── বট / ডাটাবেস (সব environment থেকে) ─────────────────────────
# ⚠️ ডিফল্ট ("") এর জায়গায় আসল পাসওয়ার্ড/key লিখে GitHub-এ দেবেন না। Render/Docker-এর environment-এ দিন।
SESSION = environ.get("SESSION", "DreamZBotz")
API_ID = int(environ.get("API_ID", "24776633"))
API_HASH = environ.get("API_HASH", "57b1f632044b4e718f5dce004a988d69")
BOT_TOKEN = environ.get("BOT_TOKEN", "")
ADMINS = [int(x) for x in environ.get("ADMINS", "8248792819").replace(",", " ").split() if x.isdigit()]
DATABASE_URI = environ.get("DATABASE_URI", "mongodb+srv://rendamd1_db_user:M7vb8ZD9rx0AfHnP@cluster0.uzqvib6.mongodb.net/?appName=Cluster0")
DATABASE_NAME = environ.get("DATABASE_NAME", "Cluster")
PORT = int(environ.get("PORT", "8080"))


# ───────────────────────── Gemini ─────────────────────────
GEMINI = "https://generativelanguage.googleapis.com/v1beta"

_GEMINI_TEXT = {
    # model = "auto" হলে নিজে সর্বশেষ মডেল খুঁজে নেয়; fallback = খুঁজে না পেলে এটি
    "model": environ.get("GEMINI_TEXT_MODEL", "auto"),
    "fallback": "gemini-3.8-flash",
    "discover": {
        "url": GEMINI + "/models?pageSize=200", "list": "models", "name": "name", "strip": "models/",
        "methods": "supportedGenerationMethods", "method": "generateContent",
        "prefix": "gemini-", "include": ["flash"],
        "exclude": ["tts", "image", "live", "audio", "embedding", "robotics", "computer",
                    "exp", "imagen", "veo", "aqa", "lite"],
    },
    "url": GEMINI + "/models/{model}:generateContent",
    "body": {
        "contents": [{"parts": [
            {"text": "{prompt}"},
            {"inline_data": {"mime_type": "{media_mime}", "data": "{media_b64}"}},
        ]}],
        "generationConfig": {"responseMimeType": "{json_mime}"},
    },
    "out": "candidates.0.content.parts.0.text", "out_type": "text",
    "out_fail": "promptFeedback.blockReason",
    "timeout": 180,
}

PROVIDERS = {
    "gemini": {
        "label": "Gemini (টেক্সট • ভয়েস • ভিডিও)",
        "key": environ.get("GEMINI_API_KEY", ""),
        "headers": {"x-goog-api-key": "{key}"},
        "check": {"url": GEMINI + "/models?pageSize=1"},
        "caps": {
            "text": _GEMINI_TEXT,
            "vision": _GEMINI_TEXT,
            "transcribe": _GEMINI_TEXT,
            "tts": {
                "model": environ.get("GEMINI_TTS_MODEL", "auto"),
                "fallback": "gemini-2.5-flash-preview-tts",
                "discover": {
                    "url": GEMINI + "/models?pageSize=200", "list": "models", "name": "name", "strip": "models/",
                    "methods": "supportedGenerationMethods", "method": "generateContent",
                    "include": ["tts"], "prefer": ["flash"],
                },
                "url": GEMINI + "/models/{model}:generateContent",
                "body": {
                    "contents": [{"parts": [{"text": "{text}"}]}],
                    "generationConfig": {
                        "responseModalities": ["AUDIO"],
                        "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "{voice}"}}},
                    },
                },
                "out": "candidates.0.content.parts.0.inlineData.data", "out_type": "b64",
                "audio": "pcm",          # আউটপুট আগে থেকেই PCM 24kHz mono
                "timeout": 120,
            },
            "video": {
                "model": environ.get("VEO_MODEL", "auto"),
                "fallback": "veo-3.0-generate-001",
                "discover": {
                    "url": GEMINI + "/models?pageSize=200", "list": "models", "name": "name", "strip": "models/",
                    "methods": "supportedGenerationMethods", "method": "predictLongRunning",
                    "prefix": "veo", "prefer": ["fast", "lite"],
                },
                "url": GEMINI + "/models/{model}:predictLongRunning",
                "body": {
                    "instances": [{
                        "prompt": "{prompt}",
                        "image": {"bytesBase64Encoded": "{image_b64}", "mimeType": "{image_mime}"},
                    }],
                    "parameters": {"aspectRatio": "{aspect}"},
                },
                "wait": {
                    "id": "name", "status_url": GEMINI + "/{id}",
                    "status": "done", "done": [True], "error": "error.message",
                    "interval": 8, "timeout": 720,
                },
                "out": "response.generateVideoResponse.generatedSamples.0.video.uri", "out_type": "url",
                "out_auth": True,        # ডাউনলোডেও key লাগে
                "out_fail": "response.generateVideoResponse.raiMediaFilteredReasons.0",
            },
        },
    },

    # ───────────────────────── Replicate ─────────────────────────
    "replicate": {
        "label": "Replicate (লিপ-সিঙ্ক)",
        "key": environ.get("REPLICATE_API_TOKEN", ""),
        "headers": {"Authorization": "Bearer {key}"},
        "check": {"url": "https://api.replicate.com/v1/account"},
        # ফাইল আপলোড করে URL নেওয়ার নিয়ম (cap-এ "upload": True দিলে ব্যবহার হয়)
        "upload": {"url": "https://api.replicate.com/v1/files", "field": "content", "out": "urls.get"},
        "caps": {
            "lipsync": {
                "model": environ.get("LIPSYNC_MODEL", "sync/lipsync-2"),
                "url": "https://api.replicate.com/v1/models/{model}/predictions",
                "headers": {"Prefer": "wait=30"},
                "upload": True,
                "body": {"input": {
                    environ.get("LIPSYNC_VIDEO_FIELD", "video"): "{video_url}",
                    environ.get("LIPSYNC_AUDIO_FIELD", "audio"): "{audio_url}",
                }},
                "wait": {
                    "id": "id", "status_url_path": "urls.get",
                    "status": "status", "done": ["succeeded"], "fail": ["failed", "canceled"],
                    "error": "error", "interval": 5, "timeout": 900,
                },
                "out": "output", "out_type": "url",
            },
        },
    },
}


# ───────────────────────── fal.ai ─────────────────────────
def fal_job(model: str, body: dict, out: str = "video.url", **extra) -> dict:
    """fal.ai-র যেকোনো মডেলের জন্য একই ছাঁচ — শুধু model ও body বদলায়।"""
    return {
        "model": model,
        "url": "https://queue.fal.run/{model}",
        "body": body,
        "wait": {"id": "request_id", "status_url_path": "status_url", "result_url_path": "response_url",
                 "status": "status", "done": ["COMPLETED"], "interval": 5, "timeout": 900},
        "out": out, "out_type": "url", **extra,
    }


PROVIDERS["fal"] = {
    "label": "fal.ai (ভিডিও • লিপ-সিঙ্ক)",
    "key": environ.get("FAL_KEY", ""),
    "headers": {"Authorization": "Key {key}"},
    # fal-এ ফাইল সরাসরি data-URI হিসেবে যায়, আলাদা আপলোড লাগে না
    "caps": {
        # ⚠️ মডেলের নাম ও ইনপুট ফিল্ড fal.ai/models পেজ থেকে মিলিয়ে নিন; বদলাতে চাইলে এখানে বা env-এ দিন।
        "video": fal_job(environ.get("FAL_VIDEO_MODEL", "fal-ai/veo3"),
                         {"prompt": "{prompt}", "aspect_ratio": "{aspect}", "duration": "8s",
                          "generate_audio": False}),
        "video_image": fal_job(environ.get("FAL_VIDEO_IMAGE_MODEL", "fal-ai/veo3/image-to-video"),
                               {"prompt": "{prompt}", "image_url": "{image_url}", "duration": "8s",
                                "generate_audio": False}),
        "lipsync": fal_job(environ.get("FAL_LIPSYNC_MODEL", "fal-ai/sync-lipsync"),
                           {"video_url": "{video_url}", "audio_url": "{audio_url}"}),
    },
}


# ───────────────────────── কোন কাজে কোন সাইট (ক্রম = অগ্রাধিকার / ফলব্যাক) ─────────────────────────
# তালিকায় না থাকা সাইট, যার ওই কাজের ক্ষমতা আছে, সেটি শেষে নিজে থেকে ফলব্যাক হিসেবে যোগ হয়।
# কোনো সাইটের key না থাকলে (মেনুতে ON নেই, info.py/env-তেও নেই) সেটি বাদ পড়ে।
ROUTES = {
    "text": ["gemini"],
    "vision": ["gemini"],
    "transcribe": ["gemini"],
    "tts": ["gemini"],
    "video": ["gemini", "fal"],
    "lipsync": ["replicate", "fal"],
}
