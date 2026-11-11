import asyncio
import base64
import ipaddress
import logging
import os
import shutil
import secrets
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from youtube_handler import is_youtube_url, youtube_extractor_options, youtube_format, youtube_error_message
from smart_download_engine import youtube_attempt_profiles, merge_extractor_options
import sentry_sdk
from redis.asyncio import Redis
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, Message, FSInputFile,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "100"))
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
# Telegram cloud Bot API upload ceiling; keep a safety margin below 50 MB.
TELEGRAM_UPLOAD_LIMIT_MB = int(os.getenv("TELEGRAM_UPLOAD_LIMIT_MB", "49"))
TELEGRAM_UPLOAD_LIMIT_BYTES = TELEGRAM_UPLOAD_LIMIT_MB * 1024 * 1024
MAX_DURATION_SECONDS = int(os.getenv("MAX_DURATION_SECONDS", "1800"))
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "600"))
MAX_CONCURRENT_DOWNLOADS = max(1, int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2")))
MAX_PLAYLIST_ITEMS = max(1, min(20, int(os.getenv("MAX_PLAYLIST_ITEMS", "5"))))
ENABLE_ARIA2 = os.getenv("ENABLE_ARIA2", "false").lower() in {"1", "true", "yes"}
BGUTIL_POT_BASE_URL = os.getenv("BGUTIL_POT_BASE_URL", "").strip()
YOUTUBE_COOKIES_BASE64 = os.getenv("YOUTUBE_COOKIES_BASE64", "").strip()
SOCIAL_COOKIES_BASE64 = os.getenv("SOCIAL_COOKIES_BASE64", "").strip()
REDIS_URL = os.getenv("REDIS_URL", "").strip()
SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()
redis_client: Redis | None = None
if SENTRY_DSN:
    sentry_sdk.init(dsn=SENTRY_DSN, traces_sample_rate=0.0, send_default_pii=False)
router = Router()
download_slots = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
pending_links: dict[tuple[int, str], str] = {}
user_busy: set[int] = set()
PENDING_LINK_TTL = max(60, int(os.getenv("PENDING_LINK_TTL_SECONDS", "900")))


async def save_pending_link(user_id: int, token: str, url: str) -> None:
    """Persist a choice across restarts/replicas when Redis is available."""
    if redis_client is not None:
        try:
            await redis_client.set(f"downloader:pending:{user_id}:{token}", url, ex=PENDING_LINK_TTL)
            return
        except Exception as exc:
            log.warning("Could not persist pending link (%s)", type(exc).__name__)
    pending_links[(user_id, token)] = url
    keys = [k for k in pending_links if k[0] == user_id]
    for old in keys[:-5]:
        pending_links.pop(old, None)


async def get_pending_link(user_id: int, token: str) -> str | None:
    if redis_client is not None:
        try:
            value = await redis_client.get(f"downloader:pending:{user_id}:{token}")
            if value:
                return value
        except Exception as exc:
            log.warning("Could not read pending link from Redis (%s)", type(exc).__name__)
    return pending_links.get((user_id, token))


async def delete_pending_link(user_id: int, token: str) -> None:
    pending_links.pop((user_id, token), None)
    if redis_client is not None:
        try:
            await redis_client.delete(f"downloader:pending:{user_id}:{token}")
        except Exception as exc:
            log.warning("Could not delete pending link from Redis (%s)", type(exc).__name__)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("media-downloader")


def valid_public_url(raw: str) -> bool:
    try:
        parsed = urlparse(raw.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.username or parsed.password:
            return False
        host = parsed.hostname.rstrip(".").lower()
        if host in {"localhost", "localhost.localdomain"}:
            return False
        # Reject literal private/reserved IPs and hostnames resolving to them.
        try:
            ip = ipaddress.ip_address(host)
            return ip.is_global
        except ValueError:
            records = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
            return bool(records) and all(ipaddress.ip_address(r[4][0]).is_global for r in records)
    except (ValueError, OSError, socket.gaierror):
        return False



def is_playlist_url(url: str) -> bool:
    """Detect YouTube playlists and common list URLs."""
    from urllib.parse import parse_qs, urlparse
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    qs = parse_qs(parsed.query or "")
    if "list" in qs and ("youtube.com" in host or "youtu.be" in host):
        return True
    if "/playlist" in (parsed.path or "").lower():
        return True
    return False


def _extractor_options(url: str, folder: str | None = None) -> dict:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    options = {}

    # Prefer platform-specific cookies; keep SOCIAL_COOKIES_BASE64 as a fallback
    # for users who export one shared Netscape cookies file.
    platform_cookies = (
        ("youtube.com", "YOUTUBE_COOKIES_BASE64", "youtube-cookies.txt"),
        ("youtu.be", "YOUTUBE_COOKIES_BASE64", "youtube-cookies.txt"),
        ("instagram.com", "INSTAGRAM_COOKIES_BASE64", "instagram-cookies.txt"),
        ("tiktok.com", "TIKTOK_COOKIES_BASE64", "tiktok-cookies.txt"),
        ("vm.tiktok.com", "TIKTOK_COOKIES_BASE64", "tiktok-cookies.txt"),
        ("twitter.com", "TWITTER_COOKIES_BASE64", "twitter-cookies.txt"),
        ("x.com", "X_COOKIES_BASE64", "twitter-cookies.txt"),
        ("facebook.com", "FACEBOOK_COOKIES_BASE64", "facebook-cookies.txt"),
        ("fb.watch", "FACEBOOK_COOKIES_BASE64", "facebook-cookies.txt"),
        ("fb.com", "FACEBOOK_COOKIES_BASE64", "facebook-cookies.txt"),
    )
    selected_env = None
    selected_filename = None
    for domain, env_name, filename in platform_cookies:
        if host == domain or host.endswith("." + domain):
            selected_env, selected_filename = env_name, filename
            break

    if selected_env and folder:
        encoded = os.getenv(selected_env, "").strip()
        if not encoded and selected_env != "YOUTUBE_COOKIES_BASE64":
            encoded = SOCIAL_COOKIES_BASE64
            selected_filename = "social-cookies.txt"
        if encoded:
            try:
                cookie_path = Path(folder) / selected_filename
                if not cookie_path.exists():
                    raw = base64.b64decode(encoded, validate=True)
                    if not raw.startswith((b"# Netscape HTTP Cookie File", b"# HTTP Cookie File")):
                        raise ValueError("Cookie file is not in Netscape format")
                    cookie_path.write_bytes(raw)
                    os.chmod(cookie_path, 0o600)
                options["cookiefile"] = str(cookie_path)
            except Exception as exc:
                log.warning("%s could not be loaded (%s)", selected_env, type(exc).__name__)

    if is_youtube_url(url):
        # youtube_handler handles YOUTUBE_COOKIES_BASE64 and PO Token options.
        options.update(youtube_extractor_options(folder))
    return options

def _preflight(url: str, folder: str, attempt_options: dict | None = None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": not is_playlist_url(url),
        "playlistend": MAX_PLAYLIST_ITEMS if is_playlist_url(url) else 1,
        "skip_download": True,
        "socket_timeout": 20,
        "retries": 2,
        **_extractor_options(url, folder),
    }
    if attempt_options and is_youtube_url(url):
        opts.update(merge_extractor_options(opts, attempt_options))
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _download_once(url: str, folder: str, mode: str, quality: str, attempt_options: dict | None = None) -> tuple[str, dict]:
    outtmpl = str(Path(folder) / "%(title).80s-%(id)s.%(ext)s")
    fmt = youtube_format(mode, quality) if is_youtube_url(url) else (
        "bestaudio/best" if mode == "audio" else (
            f"best[height<={quality}][ext=mp4]/best[height<={quality}]/best[ext=mp4]/best"
            if quality != "best" else "best[ext=mp4]/best"
        )
    )

    opts = {
        "format": fmt,
        "outtmpl": outtmpl,
        "noplaylist": not is_playlist_url(url),
        "playlistend": MAX_PLAYLIST_ITEMS if is_playlist_url(url) else 1,
        "quiet": True,
        "no_warnings": False,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 5,
        "file_access_retries": 3,
        "extractor_retries": 3,
        "continuedl": True,
        "overwrites": True,
        "max_filesize": MAX_FILE_BYTES,
        "merge_output_format": "mp4",
        "windowsfilenames": True,
        "restrictfilenames": True,
        **_extractor_options(url, folder),
    }
    if attempt_options and is_youtube_url(url):
        opts.update(merge_extractor_options(opts, attempt_options))
    if ENABLE_ARIA2 and shutil.which("aria2c"):
        opts["external_downloader"] = {"http": "aria2c", "https": "aria2c"}
        opts["external_downloader_args"] = {
            "aria2c": ["-x", "8", "-s", "8", "-k", "1M", "--file-allocation=none"]
        }
    if mode == "audio":
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    allowed = {".mp3"} if mode == "audio" else {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
    candidates = []
    for item in info.get("requested_downloads") or []:
        for key in ("filepath", "_filename", "filename"):
            if item.get(key):
                candidates.append(Path(item[key]))
    for key in ("filepath", "_filename"):
        if info.get(key):
            candidates.append(Path(info[key]))
    candidates.extend(p for p in Path(folder).rglob("*") if p.is_file() and p.suffix.lower() in allowed)
    files, seen = [], set()
    for path in candidates:
        try:
            resolved = path.resolve()
            if resolved not in seen and resolved.is_file() and resolved.suffix.lower() in allowed:
                seen.add(resolved)
                files.append(resolved)
        except OSError:
            pass
    if not files:
        raise FileNotFoundError("لم يتم العثور على الملف بعد اكتمال التحميل.")
    final = max(files, key=lambda p: p.stat().st_mtime)
    if final.stat().st_size <= 0:
        raise ValueError("الملف الناتج فارغ.")
    if final.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"حجم الملف أكبر من الحد المسموح ({MAX_FILE_MB}MB).")
    return str(final), info


def resolve_linkedin_short_url(url: str) -> str:
    """Expand lnkd.in short links before passing the real LinkedIn URL to yt-dlp."""
    host = (urlparse(url).hostname or "").lower()
    if host != "lnkd.in" and not host.endswith(".lnkd.in"):
        return url
    import requests
    response = requests.get(
        url,
        allow_redirects=True,
        stream=True,
        timeout=(10, 20),
        headers={"User-Agent": "Mozilla/5.0"},
    )
    final_url = response.url
    response.close()
    final_host = (urlparse(final_url).hostname or "").lower()
    if not (final_host == "linkedin.com" or final_host.endswith(".linkedin.com")):
        raise ValueError("رابط lnkd.in لم يحوّل إلى منشور LinkedIn صالح. افتح الرابط وتأكد أنه متاح للعامة.")
    if not valid_public_url(final_url):
        raise ValueError("رابط LinkedIn النهائي غير صالح أو غير متاح من السيرفر.")
    # yt-dlp's LinkedIn extractor matches www.linkedin.com or linkedin.com,
    # not regional hosts such as ae.linkedin.com. Normalize the host while
    # preserving the exact post path and query string.
    from urllib.parse import urlunparse
    parsed = urlparse(final_url)
    return urlunparse(("https", "www.linkedin.com", parsed.path, parsed.params, parsed.query, parsed.fragment))


def download_media(url: str, folder: str, mode: str, quality: str) -> tuple[str, dict]:
    url = resolve_linkedin_short_url(url)
    is_yt = is_youtube_url(url)
    profiles = youtube_attempt_profiles(bool(YOUTUBE_COOKIES_BASE64)) if is_yt else [("standard", {})]
    max_attempts = len(profiles) if is_yt else 3
    last_error = None
    for attempt in range(max_attempts):
        profile_name, attempt_options = profiles[attempt] if is_yt else ("standard", {})
        try:
            attempt_folder = str(Path(folder) / ("attempt-" + str(attempt + 1)))
            Path(attempt_folder).mkdir(parents=True, exist_ok=True)
            log.info("Smart download attempt %s/%s profile=%s", attempt + 1, max_attempts, profile_name)
            info = _preflight(url, attempt_folder, attempt_options)
            duration = info.get("duration")
            if duration is not None and duration > MAX_DURATION_SECONDS:
                raise ValueError(f"مدة الفيديو أطول من {MAX_DURATION_SECONDS // 60} دقيقة.")
            approx = info.get("filesize") or info.get("filesize_approx")
            if approx and approx > MAX_FILE_BYTES:
                raise ValueError(f"الحجم المتوقع أكبر من {MAX_FILE_MB}MB.")
            try:
                return _download_once(url, attempt_folder, mode, quality, attempt_options)
            except yt_dlp.utils.DownloadError:
                host = (urlparse(url).hostname or "").lower()
                is_instagram = host == "instagram.com" or host.endswith(".instagram.com")
                if is_instagram and shutil.which("gallery-dl"):
                    command = [
                        "gallery-dl", "--quiet", "--no-mtime",
                        "--directory", attempt_folder,
                        "--filter", "extension in ('mp4', 'mkv', 'webm', 'mov', 'm4v')",
                        url,
                    ]
                    # Optional cookies for private/stories content when SOCIAL_COOKIES_BASE64 is set
                    cookie_file = Path(attempt_folder) / "social-cookies.txt"
                    if cookie_file.exists():
                        command[1:1] = ["--cookies", str(cookie_file)]
                    subprocess.run(command, check=True, capture_output=True, text=True, timeout=240)
                    found = [p for p in Path(attempt_folder).rglob("*") if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".m4v"}]
                    if not found:
                        raise FileNotFoundError("لم يتم العثور على ملف وسائط من Instagram.")
                    source = max(found, key=lambda p: p.stat().st_mtime)
                    if mode == "audio":
                        final = Path(attempt_folder) / (source.stem + ".mp3")
                        subprocess.run(["ffmpeg", "-y", "-i", str(source), "-vn", "-codec:a", "libmp3lame", "-b:a", "192k", str(final)], check=True, capture_output=True, text=True, timeout=180)
                    else:
                        final = source
                    if final.stat().st_size > MAX_FILE_BYTES:
                        raise ValueError(f"حجم الملف أكبر من الحد المسموح ({MAX_FILE_MB}MB).")
                    return str(final), {"title": source.stem, "extractor": "gallery-dl"}
                raise
        except ValueError:
            raise
        except Exception as exc:
            last_error = exc
            log.warning("Smart download attempt %s/%s profile=%s failed (%s)", attempt + 1, max_attempts, profile_name, type(exc).__name__)
            if attempt + 1 < max_attempts:
                time.sleep(min(2 + attempt * 2, 8))
    raise last_error



async def download_via_unified_api(url: str, folder: str, mode: str, quality: str) -> tuple[str, dict]:
    """Use the separate unified engine when configured; otherwise preserve local behavior."""
    api_url = os.getenv("DOWNLOAD_ENGINE_API_URL", "").strip().rstrip("/")
    api_key = os.getenv("DOWNLOAD_ENGINE_API_KEY", "").strip()
    if not api_url or not api_key:
        return await asyncio.to_thread(download_media, url, folder, mode, quality)

    import aiohttp
    timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT + 60, connect=15, sock_read=DOWNLOAD_TIMEOUT)
    payload = {"url": url, "kind": mode, "quality": quality}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            f"{api_url}/download",
            json=payload,
            headers={"X-API-Key": api_key},
        ) as response:
            if response.status != 200:
                detail = (await response.text())[:700]
                raise RuntimeError(f"محرك التنزيل الموحد أعاد HTTP {response.status}: {detail}")
            filename = response.headers.get("X-Filename", "download.bin")
            from urllib.parse import unquote
            filename = Path(unquote(filename)).name or "download.bin"
            target = Path(folder) / filename
            with target.open("wb") as output:
                async for chunk in response.content.iter_chunked(1024 * 1024):
                    output.write(chunk)
            if not target.exists() or target.stat().st_size <= 0:
                raise RuntimeError("محرك التنزيل الموحد أعاد ملفًا فارغًا.")
            title = unquote(response.headers.get("X-Media-Title", "ملف"))
            return str(target), {"title": title, "extractor": "unified-media-api"}


async def _fetch_thumbnail(info: dict) -> BufferedInputFile | None:
    """Download a small thumbnail when the extractor provides a URL."""
    thumb_url = info.get("thumbnail") or (info.get("thumbnails") or [{}])[-1].get("url")
    if not thumb_url:
        return None
    try:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(thumb_url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
                if not data or len(data) > 200_000:
                    return None
                return BufferedInputFile(data, filename="thumb.jpg")
    except Exception:
        return None


def build_quality_keyboard(token: str, heights: list[int]) -> InlineKeyboardMarkup:
    rows = []
    buttons = [
        InlineKeyboardButton(
            text=f"{height}p" + (" HD" if height >= 720 else ""),
            callback_data=f"dl:q{height}:{token}",
        )
        for height in sorted(set(heights), reverse=True)[:8]
    ]
    for index in range(0, len(buttons), 2):
        rows.append(buttons[index:index + 2])
    rows.append([InlineKeyboardButton(text="⭐ أعلى جودة متاحة", callback_data=f"dl:qbest:{token}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def available_video_heights(url: str) -> list[int]:
    """Inspect the source and return only heights actually offered by the platform."""
    resolved_url = resolve_linkedin_short_url(url)
    with tempfile.TemporaryDirectory(prefix="media-probe-") as folder:
        info = _preflight(resolved_url, folder)
    heights = {
        int(item["height"])
        for item in (info.get("formats") or [])
        if item.get("height") and str(item.get("height")).isdigit()
    }
    if info.get("height"):
        heights.add(int(info["height"]))
    return sorted(heights, reverse=True)


@router.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "أهلاً بيك في Video Downloader Bot 🎬\n\n"
        "ابعت أي رابط فيديو عام (يوتيوب، تيك توك، إنستجرام، تويتر، لينكدإن...)\n"
        "وبعدين اختار: فيديو أو صوت MP3 + الجودة.\n\n"
        f"📦 حد التحميل: {MAX_FILE_MB}MB\n"
        f"⏱ أقصى مدة: {MAX_DURATION_SECONDS // 60} دقيقة\n"
        f"📤 حد الإرسال عبر Telegram: {TELEGRAM_UPLOAD_LIMIT_MB}MB\n\n"
        f"الأوامر: /start  /help  /status  /cookies\n"
        f"بلاي ليست يوتيوب: حتى {MAX_PLAYLIST_ITEMS} فيديوهات\n"
        "ملاحظة: المحتوى الخاص أو المحمي بـ DRM غير مدعوم."
    )


@router.message(Command("help"))
async def help_command(message: Message):
    await message.answer(
        "📖 طريقة الاستخدام:\n"
        "1) ابعت رابط HTTPS عام\n"
        "2) اختار فيديو أو صوت MP3\n"
        "3) لو اخترت فيديو → حدد الجودة\n\n"
        f"الحدود الحالية:\n"
        f"• حجم التحميل: {MAX_FILE_MB}MB\n"
        f"• المدة: {MAX_DURATION_SECONDS // 60} دقيقة\n"
        f"• الإرسال عبر Telegram: {TELEGRAM_UPLOAD_LIMIT_MB}MB\n\n"
        "• بلاي ليست يوتيوب: يحمّل أول عناصر محدودة (MAX_PLAYLIST_ITEMS)\n• ستوريز/محتوى خاص: يحتاج SOCIAL_COOKIES_BASE64 أو YOUTUBE_COOKIES_BASE64\n• DRM غير مدعوم\n\nنزّل فقط المحتوى الذي تملك حق تنزيله."
    )



@router.message(Command("status"))
async def status_command(message: Message):
    busy = len(user_busy)
    redis_ok = redis_client is not None
    await message.answer(
        "📊 حالة البوت\n\n"
        f"• عمليات جارية: {busy}\n"
        f"• الحد الأقصى المتزامن: {MAX_CONCURRENT_DOWNLOADS}\n"
        f"• حد الملف: {MAX_FILE_MB}MB\n"
        f"• حد الإرسال عبر Telegram: {TELEGRAM_UPLOAD_LIMIT_MB}MB\n"
        f"• Redis: {'متصل' if redis_ok else 'غير متصل'}\n"
        f"• yt-dlp: {yt_dlp.version.__version__}"
    )



@router.message(Command("cookies"))
async def cookies_help(message: Message):
    has_yt = bool(YOUTUBE_COOKIES_BASE64)
    has_social = bool(SOCIAL_COOKIES_BASE64)
    await message.answer(
        "🍪 نظام الـ Cookies\n\n"
        "بعض الفيديوهات (مقيّدة بالعمر / خاصة / ستوريز إنستجرام) تحتاج جلسة متصفح.\n\n"
        "طريقة الإعداد على السيرفر:\n"
        "1) صدّر cookies.txt من المتصفح بصيغة Netscape\n"
        "2) حوّله إلى Base64\n"
        "3) ضعه في المتغيرات:\n"
        "   • YOUTUBE_COOKIES_BASE64 — ليوتيوب\n"
        "   • SOCIAL_COOKIES_BASE64 — لإنستجرام/تيك توك/تويتر/فيسبوك\n\n"
        f"الحالة الحالية:\n"
        f"• YouTube cookies: {'✅ مفعّل' if has_yt else '❌ غير مفعّل'}\n"
        f"• Social cookies: {'✅ مفعّل' if has_social else '❌ غير مفعّل'}\n\n"
        "⚠️ البوت لا يتجاوز DRM ولا يخترق حسابات. استخدم Cookies لحسابك فقط."
    )


@router.message(F.text)
async def receive_link(message: Message):
    url = (message.text or "").strip()
    if not valid_public_url(url):
        await message.answer("ابعت رابط HTTP/HTTPS عام وصحيح من منصة مدعومة.")
        return
    token = secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:11]
    await save_pending_link(message.from_user.id, token, url)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎥 تحويل إلى فيديو", callback_data=f"dl:video:{token}"),
        InlineKeyboardButton(text="🎧 تحويل إلى صوت MP3", callback_data=f"dl:audio:{token}"),
    ]])
    await message.answer("اختار صيغة التحويل:", reply_markup=keyboard)


@router.callback_query(F.data.startswith("dl:"))
async def choose_download(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or not callback.from_user or not callback.message:
        return
    _, choice, token = parts
    key = (callback.from_user.id, token)
    url = await get_pending_link(callback.from_user.id, token)
    if not url:
        await callback.message.edit_text("انتهى الاختيار. ابعت الرابط مرة تانية.")
        return
    if choice == "video":
        await callback.message.edit_text("⏳ بفحص الجودات المتاحة على الرابط...")
        try:
            heights = await asyncio.wait_for(
                asyncio.to_thread(available_video_heights, url),
                timeout=45,
            )
        except Exception as exc:
            log.info("Could not inspect available formats (%s); showing best-quality fallback", type(exc).__name__)
            heights = []
        await callback.message.edit_text(
            "اختار الجودة المتاحة للفيديو:" if heights else "لم أتمكن من قراءة قائمة الجودات؛ جرّب أعلى جودة متاحة:",
            reply_markup=build_quality_keyboard(token, heights),
        )
        return
    if choice == "audio":
        mode, quality = "audio", "best"
    elif choice.startswith("q"):
        mode = "video"
        quality = "best" if choice == "qbest" else (
            choice[1:] if choice.startswith("q") and choice[1:].isdigit() else None
        )
        if not quality:
            await callback.message.edit_text("اختيار الجودة غير صالح.")
            return
    else:
        return

    user_id = callback.from_user.id
    if user_id in user_busy:
        await callback.message.answer("عندك عملية تحميل شغالة بالفعل. استنى لما تخلص.")
        return
    lock_value = f"{user_id}:{token}:{time.time_ns()}"
    redis_locked = False
    redis_check_succeeded = False
    if redis_client is not None:
        try:
            redis_locked = bool(await redis_client.set(f"downloader:user:{user_id}:busy", lock_value, nx=True, ex=DOWNLOAD_TIMEOUT + 120))
            redis_check_succeeded = True
        except Exception as exc:
            log.warning("Redis lock unavailable (%s); using process-local lock", type(exc).__name__)
        if redis_check_succeeded and not redis_locked:
            await callback.message.answer("عندك عملية تحميل شغالة بالفعل. استنى لما تخلص.")
            return
    user_busy.add(user_id)
    await delete_pending_link(user_id, token)
    status = await callback.message.edit_text("⏳ جاري التحميل... قد يستغرق دقيقة أو أكثر حسب الحجم.")
    try:
        async with download_slots:
            with tempfile.TemporaryDirectory(prefix="telegram-media-") as folder:
                filename, info = await asyncio.wait_for(
                    download_via_unified_api(url, folder, mode, quality),
                    timeout=DOWNLOAD_TIMEOUT + 90,
                )
                allowed_ext = {".mp3"} if mode == "audio" else {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
                media_files = []
                if is_playlist_url(url):
                    seen = set()
                    for p in sorted(Path(folder).rglob("*"), key=lambda x: x.stat().st_mtime if x.is_file() else 0):
                        if p.is_file() and p.suffix.lower() in allowed_ext:
                            rp = p.resolve()
                            if rp not in seen and rp.stat().st_size > 0:
                                seen.add(rp)
                                media_files.append(rp)
                    media_files = media_files[:MAX_PLAYLIST_ITEMS]
                if not media_files:
                    media_files = [Path(filename)]

                await status.edit_text(f"✅ التحميل اكتمل ({len(media_files)} ملف)، جاري الإرسال...")
                thumb = await _fetch_thumbnail(info)
                uploader = str(info.get("uploader") or info.get("channel") or info.get("artist") or "").strip()[:64]
                source = str(info.get("extractor") or info.get("extractor_key") or "").strip()
                quality_label = "أفضل جودة" if quality == "best" else f"{quality}p"
                sent = 0
                skipped_big = 0
                for idx, fpath in enumerate(media_files, start=1):
                    size = fpath.stat().st_size
                    if size > TELEGRAM_UPLOAD_LIMIT_BYTES:
                        skipped_big += 1
                        continue
                    title = fpath.stem[:64] if len(media_files) > 1 else str(info.get("title") or fpath.stem)[:64]
                    media = FSInputFile(str(fpath), filename=fpath.name)
                    prefix = f"({idx}/{len(media_files)}) " if len(media_files) > 1 else ""
                    if mode == "audio":
                        caption_parts = [f"{prefix}🎵 MP3", title]
                        if uploader:
                            caption_parts.append(f"👤 {uploader}")
                        await bot.send_audio(
                            chat_id=callback.message.chat.id,
                            audio=media,
                            title=title,
                            performer=uploader or None,
                            thumbnail=thumb if idx == 1 else None,
                            caption="\n".join(caption_parts),
                        )
                    else:
                        caption_parts = [f"{prefix}🎬 {title}", f"📐 {quality_label}"]
                        if uploader:
                            caption_parts.append(f"👤 {uploader}")
                        await bot.send_video(
                            chat_id=callback.message.chat.id,
                            video=media,
                            caption="\n".join(caption_parts),
                            supports_streaming=True,
                            thumbnail=thumb if idx == 1 else None,
                        )
                    sent += 1
                if sent == 0 and skipped_big:
                    await status.edit_text(
                        f"⚠️ تم التنزيل لكن كل الملفات أكبر من حد الإرسال ({TELEGRAM_UPLOAD_LIMIT_MB}MB). جرّب جودة أقل."
                    )
                elif skipped_big:
                    await status.edit_text(f"✅ تم إرسال {sent} ملف، وتخطي {skipped_big} لتجاوز حد الحجم.")
                else:
                    await status.edit_text(f"✅ تم الإرسال بنجاح ({sent} ملف).")
    except asyncio.TimeoutError:
        await status.edit_text("انتهت مهلة التحميل. جرّب رابطاً آخر أو جودة أقل.")
    except yt_dlp.utils.DownloadError as exc:
        sentry_sdk.capture_exception(exc)
        detail = str(exc).lower()
        log.warning("Extractor/download failure: %s", str(exc)[:700])
        if is_youtube_url(url):
            msg = youtube_error_message(exc)
        elif "sign in to confirm" in detail or "not a bot" in detail:
            msg = "المنصة طلبت تحقق/تسجيل دخول من السيرفر؛ جرّب رابطاً عاماً آخر."
        else:
            msg = "المنصة رفضت التحميل أو الرابط/الجودة غير متاحين حالياً. جرّب جودة أقل."
        await status.edit_text("❌ " + msg)
    except ValueError as exc:
        await status.edit_text("❌ " + str(exc))
    except Exception as exc:
        sentry_sdk.capture_exception(exc)
        log.exception("Unexpected download/send failure")
        await status.edit_text("❌ حصل خطأ أثناء التحميل أو الإرسال. جرّب مرة أخرى.")
    finally:
        user_busy.discard(user_id)
        if redis_locked and redis_client is not None:
            try:
                await redis_client.eval("if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end", 1, f"downloader:user:{user_id}:busy", lock_value)
            except Exception as exc:
                log.warning("Could not release Redis lock (%s)", type(exc).__name__)


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing from Railway Variables.")
    global redis_client
    if REDIS_URL:
        try:
            redis_client = Redis.from_url(REDIS_URL, socket_connect_timeout=3, socket_timeout=3, decode_responses=True)
            await redis_client.ping()
            log.info("Redis connected; distributed per-user locks enabled")
        except Exception as exc:
            log.warning("Redis unavailable (%s); falling back to local locks", type(exc).__name__)
            if redis_client is not None:
                await redis_client.aclose()
            redis_client = None
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    log.info(
        "Bot starting; yt-dlp=%s aria2=%s redis=%s max_file=%sMB upload_limit=%sMB",
        yt_dlp.version.__version__, bool(shutil.which("aria2c")),
        redis_client is not None, MAX_FILE_MB, TELEGRAM_UPLOAD_LIMIT_MB,
    )
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if redis_client is not None:
            await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())