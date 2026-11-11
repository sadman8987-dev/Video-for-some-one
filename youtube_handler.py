"""Isolated YouTube and YouTube Music extraction configuration.

Only this module owns YouTube-specific yt-dlp options. Cookies are optional
operator-provided secrets; this module never scrapes browser sessions.
"""
import base64
import logging
import os
from pathlib import Path

log = logging.getLogger("media-downloader.youtube")
BGUTIL_POT_BASE_URL = os.getenv("BGUTIL_POT_BASE_URL", "").strip()
YOUTUBE_COOKIES_BASE64 = os.getenv("YOUTUBE_COOKIES_BASE64", "").strip()


def is_youtube_url(url: str) -> bool:
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")


def youtube_extractor_options(folder: str | None = None) -> dict:
    options = {}
    if BGUTIL_POT_BASE_URL:
        options["extractor_args"] = {
            "youtubepot-bgutilhttp": {"base_url": [BGUTIL_POT_BASE_URL]}
        }
    if YOUTUBE_COOKIES_BASE64 and folder:
        try:
            cookie_path = Path(folder) / "youtube-cookies.txt"
            if not cookie_path.exists():
                raw = base64.b64decode(YOUTUBE_COOKIES_BASE64, validate=True)
                if not raw.startswith(b"# Netscape HTTP Cookie File") and not raw.startswith(b"# HTTP Cookie File"):
                    raise ValueError("Cookie file is not in Netscape format")
                cookie_path.write_bytes(raw)
                os.chmod(cookie_path, 0o600)
            options["cookiefile"] = str(cookie_path)
        except Exception as exc:
            log.warning("YouTube cookies could not be loaded (%s)", type(exc).__name__)
    return options


def youtube_format(mode: str, quality: str) -> str:
    if mode == "audio":
        return "bestaudio/best"
    if quality == "best":
        return "best[ext=mp4]/best"
    return f"best[height<={quality}][ext=mp4]/best[height<={quality}]/best[ext=mp4]/best"


def youtube_error_message(error: Exception) -> str:
    detail = str(error).lower()
    if "sign in to confirm" in detail or "not a bot" in detail or "confirm you're not a bot" in detail:
        return ("يوتيوب طلب تحقق من السيرفر. مزود PO Token مفعّل، لكن بعض الفيديوهات قد تتطلب "
                "ملف Cookies صالحًا لحسابك تضيفه بنفسك كمتغير Railway باسم YOUTUBE_COOKIES_BASE64.")
    if "age-restricted" in detail or "confirm your age" in detail:
        return "الفيديو مقيّد بالعمر ويتطلب جلسة حساب مخوّلة عبر YOUTUBE_COOKIES_BASE64."
    if "private video" in detail or "members-only" in detail:
        return "الفيديو خاص أو للأعضاء فقط. يحتاج Cookies لحساب يملك صلاحية المشاهدة."
    return "تعذّر تحميل فيديو YouTube حاليًا. جرّب رابطًا عامًا آخر أو جودة أقل."
