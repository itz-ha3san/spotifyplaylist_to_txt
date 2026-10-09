from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import secrets
import webbrowser
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import requests
import spotipy
from spotipy.cache_handler import MemoryCacheHandler
from spotipy.oauth2 import SpotifyOAuth
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


MESSAGE_THRESHOLD = 4000
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SPOTIFY_SCOPE = "playlist-read-private playlist-read-collaborative"

PLAYLIST_PATH = re.compile(
    r"^/(?:intl-[a-zA-Z-]+/)?playlist/([A-Za-z0-9]{22})/?$"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

# جلوگیری از ثبت URLهای حاوی توکن تلگرام در لاگ معمولی
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


class InvalidPlaylistURL(ValueError):
    """لینک ورودی معتبر نیست."""


class EmptyPlaylistError(ValueError):
    """پلی‌لیست ترک قابل‌خواندن ندارد."""


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    spotify_client_id: str
    spotify_client_secret: str
    allowed_user_id: int | None = None

    @classmethod
    def from_env(cls) -> Settings:
        required = (
            "TELEGRAM_BOT_TOKEN",
            "SPOTIFY_CLIENT_ID",
            "SPOTIFY_CLIENT_SECRET",
        )

        values = {
            name: os.getenv(name, "").strip()
            for name in required
        }

        missing = [
            name for name, value in values.items()
            if not value
        ]

        if missing:
            raise RuntimeError(
                "Missing environment variables: "
                + ", ".join(missing)
            )

        allowed_id_text = os.getenv(
            "TELEGRAM_ALLOWED_USER_ID", ""
        ).strip()

        allowed_user_id: int | None = None

        if allowed_id_text:
            try:
                allowed_user_id = int(allowed_id_text)
            except ValueError as exc:
                raise RuntimeError(
                    "TELEGRAM_ALLOWED_USER_ID must be a numeric user ID."
                ) from exc

            if allowed_user_id <= 0:
                raise RuntimeError(
                    "TELEGRAM_ALLOWED_USER_ID must be positive."
                )

        return cls(
            telegram_token=values["TELEGRAM_BOT_TOKEN"],
            spotify_client_id=values["SPOTIFY_CLIENT_ID"],
            spotify_client_secret=values["SPOTIFY_CLIENT_SECRET"],
            allowed_user_id=allowed_user_id,
        )


@dataclass(frozen=True)
class PlaylistResult:
    name: str
    tracks: list[str]
    skipped_items: int


def clean_inline(value: str) -> str:
    return " ".join(value.split())


def extract_playlist_id(text: str) -> str:
    try:
        parsed = urlparse(text.strip())

        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname != "open.spotify.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
        ):
            raise ValueError("Invalid Spotify URL.")

        match = PLAYLIST_PATH.fullmatch(parsed.path)

        if match is None:
            raise ValueError("Invalid playlist path.")

        return match.group(1)

    except ValueError as exc:
        raise InvalidPlaylistURL(
            "لینک کامل یک پلی‌لیست Spotify را بفرست؛ مثلاً:\n"
            "https://open.spotify.com/playlist/PLAYLIST_ID"
        ) from exc


def authorize_spotify(settings: Settings) -> SpotifyOAuth:
    """ورود کاربر؛ بدون ایجاد فایل کش روی دیسک."""
    state = secrets.token_urlsafe(32)

    auth_manager = SpotifyOAuth(
        client_id=settings.spotify_client_id,
        client_secret=settings.spotify_client_secret,
        redirect_uri=REDIRECT_URI,
        scope=SPOTIFY_SCOPE,
        state=state,
        cache_handler=MemoryCacheHandler(),
        open_browser=False,
        requests_timeout=20,
    )

    authorization_url = auth_manager.get_authorize_url()

    print("\n=== Spotify login ===")
    print("Log in to Spotify and approve access.")
    print("If the browser does not open, open this URL:\n")
    print(authorization_url)

    try:
        webbrowser.open(authorization_url)
    except webbrowser.Error:
        print("\nPlease open the URL manually.")

    print(
        "\nAfter approval, the browser may show a connection error.\n"
        "This is expected because no local callback server is running.\n"
        "Copy the COMPLETE address from the browser address bar.\n"
        "Paste it below, NOT in Telegram or chat."
    )

    callback_url = input("\nCallback URL: ").strip()
    parsed = urlparse(callback_url)

    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port != 8888
        or parsed.path != "/callback"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise RuntimeError(
            "Unexpected callback URL. Restart and use the full redirect URL."
        )

    parameters = parse_qs(parsed.query)

    if parameters.get("state") != [state]:
        raise RuntimeError(
            "OAuth state mismatch. Restart and complete a new login."
        )

    if "error" in parameters:
        raise RuntimeError(
            "Spotify authorization was denied."
        )

    codes = parameters.get("code", [])

    if len(codes) != 1 or not codes[0]:
        raise RuntimeError(
            "Authorization code is missing from the callback URL."
        )

    token_info = auth_manager.get_access_token(
        code=codes[0],
        as_dict=True,
        check_cache=False,
    )

    if not token_info or not token_info.get("access_token"):
        raise RuntimeError(
            "Spotify did not return an access token."
        )

    print("\nSpotify OAuth login succeeded.\n")
    return auth_manager


def fetch_playlist(
    playlist_id: str,
    auth_manager: SpotifyOAuth,
) -> PlaylistResult:
    """کد همگام Spotipy؛ handler آن را در thread اجرا می‌کند."""
    tracks: list[str] = []
    skipped_items = 0

    with requests.Session() as session:
        sp = spotipy.Spotify(
            auth_manager=auth_manager,
            requests_session=session,
            requests_timeout=20,
            retries=2,
            status_retries=2,
            backoff_factor=0.5,
        )

        metadata = sp.playlist(
            playlist_id,
            fields="name",
        )

        playlist_name = clean_inline(
            metadata.get("name") or "Spotify Playlist"
        )

        page = sp.playlist_items(
            playlist_id,
            limit=50,
            offset=0,
            additional_types=("track",),
        )

        # تمام صفحات خوانده می‌شوند؛ محدود به ۵۰ یا ۱۰۰ ترک نیست.
        while page is not None:
            for entry in page.get("items", []):
                if not isinstance(entry, dict):
                    skipped_items += 1
                    continue

                # پشتیبانی از هر دو نام فیلد پاسخ
                track = entry.get("item") or entry.get("track")

                if (
                    not isinstance(track, dict)
                    or track.get("type") != "track"
                    or entry.get("is_local")
                    or track.get("is_local")
                ):
                    skipped_items += 1
                    continue

                raw_name = track.get("name")

                if not isinstance(raw_name, str) or not raw_name.strip():
                    skipped_items += 1
                    continue

                name = clean_inline(raw_name)

                artist_names = [
                    clean_inline(artist["name"])
                    for artist in (track.get("artists") or [])
                    if isinstance(artist, dict)
                    and isinstance(artist.get("name"), str)
                    and artist["name"].strip()
                ]

                artists = (
                    ", ".join(artist_names)
                    or "خوانندهٔ نامشخص"
                )

                # ترتیب و ترک‌های تکراری حفظ می‌شوند.
                tracks.append(
                    f"{len(tracks) + 1}. {name} — {artists}"
                )

            if not page.get("next"):
                break

            page = sp.next(page)

    if not tracks:
        raise EmptyPlaylistError(
            "این پلی‌لیست خالی است یا هیچ ترک قابل‌خواندنی ندارد."
        )

    return PlaylistResult(
        name=playlist_name,
        tracks=tracks,
        skipped_items=skipped_items,
    )


def build_output(result: PlaylistResult) -> str:
    sections = [
        f"پلی‌لیست: {result.name}",
        f"تعداد ترک‌های قابل‌خواندن: {len(result.tracks)}",
    ]

    if result.skipped_items:
        sections.append(
            "آیتم‌های ردشده، محلی یا بدون اطلاعات قابل‌خواندن: "
            f"{result.skipped_items}"
        )

    return (
        "\n".join(sections)
        + "\n\n"
        + "\n".join(result.tracks)
    )


def telegram_text_length(text: str) -> int:
    """محاسبهٔ محافظه‌کارانه برای متن‌های دارای ایموجی."""
    return len(text.encode("utf-16-le")) // 2


async def ensure_allowed(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    settings: Settings = context.bot_data["settings"]

    if settings.allowed_user_id is None:
        return True

    user = update.effective_user

    if user is not None and user.id == settings.allowed_user_id:
        return True

    if update.effective_message is not None:
        await update.effective_message.reply_text(
            "این بات خصوصی است و برای حساب شما فعال نشده است.",
            parse_mode=None,
        )

    return False


async def show_id(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """شناسهٔ عددی تلگرامِ درخواست‌کننده را نشان می‌دهد."""
    if (
        update.effective_message is None
        or update.effective_user is None
    ):
        return

    await update.effective_message.reply_text(
        f"شناسهٔ عددی تلگرام شما: {update.effective_user.id}",
        parse_mode=None,
    )


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await ensure_allowed(update, context):
        return

    if update.effective_message is None:
        return

    await update.effective_message.reply_text(
        "سلام! لینک کامل یک پلی‌لیست Spotify را بفرست.\n"
        "فهرست شماره‌گذاری‌شدهٔ ترک‌ها و خواننده‌ها را می‌فرستم.\n\n"
        "پلی‌لیست باید برای حساب Spotify متصل به بات قابل‌خواندن باشد.\n"
        "خروجی طولانی به‌صورت فایل TXT ارسال می‌شود.",
        parse_mode=None,
    )


async def handle_playlist(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await ensure_allowed(update, context):
        return

    message = update.effective_message

    if message is None or message.text is None:
        return

    try:
        playlist_id = extract_playlist_id(message.text)
    except InvalidPlaylistURL as exc:
        await message.reply_text(str(exc), parse_mode=None)
        return

    auth_manager: SpotifyOAuth = context.bot_data["spotify_auth"]
    spotify_lock: asyncio.Lock = context.bot_data["spotify_lock"]

    await message.reply_text(
        "در حال دریافت ترک‌های پلی‌لیست…",
        parse_mode=None,
    )

    try:
        # مدیر توکن مشترک، هم‌زمان از چند thread استفاده نمی‌شود.
        async with spotify_lock:
            result = await asyncio.to_thread(
                fetch_playlist,
                playlist_id,
                auth_manager,
            )

    except EmptyPlaylistError as exc:
        await message.reply_text(str(exc), parse_mode=None)
        return

    except spotipy.SpotifyException as exc:
        logger.warning(
            "Spotify HTTP status: %s",
            exc.http_status,
        )

        errors = {
            400: "Spotify نتوانست درخواست این پلی‌لیست را پردازش کند.",
            401: (
                "Spotify احراز هویت کاربر را نپذیرفت. "
                "مدیر بات باید ورود OAuth و مجوزهای اپ را بررسی کند."
            ),
            403: (
                "Spotify اجازهٔ خواندن این پلی‌لیست را نداد. "
                "ممکن است حساب یا اپ محدودیت دسترسی داشته باشد."
            ),
            404: (
                "پلی‌لیست پیدا نشد یا برای حساب متصل به بات "
                "قابل‌دسترسی نیست."
            ),
            429: (
                "محدودیت تعداد درخواست‌های Spotify فعال شده است. "
                "لطفاً کمی بعد دوباره امتحان کن."
            ),
        }

        await message.reply_text(
            errors.get(
                exc.http_status,
                "خطایی در دریافت اطلاعات از Spotify رخ داد. "
                "لطفاً کمی بعد دوباره امتحان کن.",
            ),
            parse_mode=None,
        )
        return

    except spotipy.SpotifyOauthError:
        logger.warning("Spotify OAuth token operation failed.")

        await message.reply_text(
            "دریافت یا تمدید توکن Spotify ناموفق بود. "
            "مدیر بات باید برنامه را دوباره اجرا و وارد Spotify شود.",
            parse_mode=None,
        )
        return

    except requests.exceptions.RequestException:
        logger.warning("Spotify connection failed.")

        await message.reply_text(
            "اتصال به Spotify برقرار نشد یا زمان پاسخ‌گویی تمام شد. "
            "لطفاً دوباره امتحان کن.",
            parse_mode=None,
        )
        return

    except Exception as exc:
        # فقط نوع خطا ثبت می‌شود؛ نه توکن یا اطلاعات درخواست.
        logger.error(
            "Unexpected playlist error: %s",
            type(exc).__name__,
        )

        await message.reply_text(
            "خطای غیرمنتظره‌ای هنگام پردازش پلی‌لیست رخ داد.",
            parse_mode=None,
        )
        return

    output = build_output(result)

    if telegram_text_length(output) < MESSAGE_THRESHOLD:
        await message.reply_text(
            output,
            parse_mode=None,
        )
        return

    filename = f"spotify_playlist_{playlist_id}.txt"

    # فایل فقط در حافظه ساخته می‌شود.
    with io.BytesIO(output.encode("utf-8")) as document:
        document.name = filename
        document.seek(0)

        await message.reply_document(
            document=document,
            filename=filename,
            caption=(
                f"فهرست {len(result.tracks)} ترک آماده است.\n"
                "به‌دلیل طولانی بودن، به‌صورت فایل متنی ارسال شد."
            ),
            parse_mode=None,
            read_timeout=60,
            write_timeout=60,
        )


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    error = context.error

    logger.error(
        "Unhandled bot error: %s",
        type(error).__name__,
    )

    if isinstance(error, TelegramError):
        logger.warning(
            "Telegram request failed; a reply may not have been delivered."
        )
        return

    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text(
                "خطایی رخ داد؛ لطفاً دوباره تلاش کن.",
                parse_mode=None,
            )
        except TelegramError:
            logger.warning("Could not deliver the error message.")


def main() -> None:
    settings = Settings.from_env()

    # ورود Spotify پیش از شروع event loop تلگرام انجام می‌شود.
    auth_manager = authorize_spotify(settings)

    if settings.allowed_user_id is None:
        print(
            "WARNING: The bot is not restricted to a Telegram user.\n"
            "All playlist requests use YOUR Spotify account permissions.\n"
            "Use /id in a private chat with the bot, then set\n"
            "TELEGRAM_ALLOWED_USER_ID and restart the bot.\n"
        )

    application = (
        Application.builder()
        .token(settings.telegram_token)
        .concurrent_updates(4)
        .build()
    )

    application.bot_data["settings"] = settings
    application.bot_data["spotify_auth"] = auth_manager
    application.bot_data["spotify_lock"] = asyncio.Lock()

    application.add_handler(CommandHandler("id", show_id))
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_playlist,
        )
    )

    application.add_error_handler(error_handler)

    print("Starting Telegram bot...")
    application.run_polling()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBot stopped.")
    except spotipy.SpotifyOauthError:
        print(
            "\nSpotify OAuth failed. Check the app credentials, "
            "redirect URI, and account access."
        )
    except requests.exceptions.RequestException:
        print("\nConnection failed or timed out during startup.")
    except (RuntimeError, ValueError) as exc:
        print(f"\nStartup failed: {exc}")