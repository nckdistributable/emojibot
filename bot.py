import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message, User
from aiogram.types.input_file import FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

try:
    from PIL import Image, ImageOps

    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False

try:
    from lottie.exporters.tgs import export_tgs
    from lottie.importers.tgs import import_tgs
    from lottie.transform import scale_to_fit

    LOTTIE_AVAILABLE = True
except Exception:
    LOTTIE_AVAILABLE = False


@dataclass(frozen=True)
class Config:
    token: str
    log_level: str
    max_file_bytes: int
    max_duration_seconds: int
    allowed_user_ids: set[int] = field(default_factory=set)
    admin_user_ids: set[int] = field(default_factory=set)
    concurrency: int = 2


@dataclass
class UserSettings:
    video_fps: int = 30
    video_duration: int = 3
    video_fit: str = "pad"
    video_bg: str = "black"
    image_fit: str = "pad"
    image_bg: str = "black"


@dataclass
class UserStat:
    user_id: int
    username: str = ""
    full_name: str = ""
    count: int = 0
    last_seen: float = 0.0


@dataclass
class Stats:
    start_time: float
    counts: dict[str, int] = field(default_factory=dict)
    users: dict[int, UserStat] = field(default_factory=dict)

    def inc(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1

    def record_user(self, user: Optional[User]) -> None:
        if user is None:
            return
        stat = self.users.get(user.id)
        if stat is None:
            stat = UserStat(user_id=user.id)
            self.users[user.id] = stat
        if user.username:
            stat.username = user.username
        if user.full_name:
            stat.full_name = user.full_name
        stat.count += 1
        stat.last_seen = time.time()


SUPPORTED_FORMATS_TEXT = (
    "Send: sticker, animated sticker, image, GIF, or video. "
    "I will return an emoji-ready file. 👻✨"
)
user_settings: dict[int, UserSettings] = {}
stats = Stats(start_time=time.monotonic())
semaphore: Optional[asyncio.Semaphore] = None
app_config: Optional[Config] = None


def parse_int(value: str, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def parse_id_list(value: str) -> set[int]:
    ids: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except Exception:
            continue
    return ids


def load_config() -> Config:
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is not set")

    log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    max_file_mb = parse_int(os.getenv("MAX_FILE_MB", "20"), 20)
    max_duration_seconds = parse_int(os.getenv("MAX_DURATION_SECONDS", "10"), 10)
    allowed_user_ids = parse_id_list(os.getenv("ALLOWED_USER_IDS", ""))
    admin_user_ids = parse_id_list(os.getenv("ADMIN_USER_IDS", ""))
    concurrency = parse_int(os.getenv("CONCURRENCY", "2"), 2)

    return Config(
        token=token,
        log_level=log_level,
        max_file_bytes=max_file_mb * 1024 * 1024,
        max_duration_seconds=max_duration_seconds,
        allowed_user_ids=allowed_user_ids,
        admin_user_ids=admin_user_ids,
        concurrency=max(1, concurrency),
    )


def convert_tgs_to_emoji(input_path: Path, output_path: Path) -> bool:
    if not LOTTIE_AVAILABLE:
        shutil.copyfile(input_path, output_path)
        return False

    animation = import_tgs(str(input_path))
    scale_to_fit(animation, 100, 100)
    export_tgs(animation, str(output_path))
    return True


def command_exists(command: str) -> bool:
    return shutil.which(command) is not None


def run_cmd(args: list[str]) -> bool:
    result = subprocess.run(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return result.returncode == 0


def build_video_filter(settings: UserSettings) -> str:
    if settings.video_fit == "crop":
        scale = (
            "scale=100:100:flags=lanczos:force_original_aspect_ratio=increase"
        )
        crop = "crop=100:100"
        filters = [scale, crop]
    else:
        color = "0x00000000" if settings.video_bg == "transparent" else "black"
        scale = (
            "scale=100:100:flags=lanczos:force_original_aspect_ratio=decrease"
        )
        pad = f"pad=100:100:(ow-iw)/2:(oh-ih)/2:color={color}"
        filters = [scale, pad]

    filters.append(f"fps={settings.video_fps}")
    return ",".join(filters)


def convert_video_to_video_emoji(
    input_path: Path,
    output_path: Path,
    settings: UserSettings,
    size_limit_bytes: int = 256 * 1024,
) -> bool:
    if not command_exists("ffmpeg"):
        return False

    duration = max(1, min(3, settings.video_duration))
    video_filter = build_video_filter(settings)
    pix_fmt = "yuva420p" if settings.video_bg == "transparent" else "yuv420p"

    attempts = [
        {"crf": "32", "speed": "4"},
        {"crf": "36", "speed": "4"},
        {"crf": "40", "speed": "6"},
    ]

    for attempt in attempts:
        ok = run_cmd(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(input_path),
                "-t",
                str(duration),
                "-vf",
                video_filter,
                "-an",
                "-c:v",
                "libvpx-vp9",
                "-pix_fmt",
                pix_fmt,
                "-auto-alt-ref",
                "0",
                "-b:v",
                "0",
                "-crf",
                attempt["crf"],
                "-speed",
                attempt["speed"],
                str(output_path),
            ]
        )
        if not ok or not output_path.exists():
            continue
        if output_path.stat().st_size <= size_limit_bytes:
            return True

    return False


def convert_image_to_png_emoji(
    input_path: Path, output_path: Path, settings: UserSettings
) -> bool:
    if not PIL_AVAILABLE:
        return False
    try:
        with Image.open(input_path) as img:
            img = img.convert("RGBA")
            bg = (
                (0, 0, 0, 0)
                if settings.image_bg == "transparent"
                else (0, 0, 0, 255)
            )
            if settings.image_fit == "crop":
                result = ImageOps.fit(
                    img,
                    (100, 100),
                    method=Image.LANCZOS,
                    centering=(0.5, 0.5),
                )
            else:
                img.thumbnail((100, 100), Image.LANCZOS)
                result = Image.new("RGBA", (100, 100), bg)
                x = (100 - img.width) // 2
                y = (100 - img.height) // 2
                result.paste(img, (x, y), img)
            result.save(output_path, format="PNG")
        return True
    except Exception:
        return False


def convert_webm_to_mp4(
    input_path: Path, output_path: Path, settings: UserSettings
) -> bool:
    if not command_exists("ffmpeg"):
        return False
    duration = max(1, min(3, settings.video_duration))
    video_filter = build_video_filter(settings)
    return run_cmd(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-t",
            str(duration),
            "-vf",
            video_filter,
            "-movflags",
            "+faststart",
            "-pix_fmt",
            "yuv420p",
            str(output_path),
        ]
    )


def convert_webm_to_gif(
    input_path: Path, output_path: Path, settings: UserSettings
) -> bool:
    if not command_exists("ffmpeg"):
        return False
    duration = max(1, min(3, settings.video_duration))
    video_filter = build_video_filter(settings)
    return run_cmd(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-t",
            str(duration),
            "-vf",
            video_filter,
            str(output_path),
        ]
    )


def get_settings(user_id: int) -> UserSettings:
    settings = user_settings.get(user_id)
    if not settings:
        settings = UserSettings()
        user_settings[user_id] = settings
    return settings


def is_allowed(user_id: Optional[int], config: Config) -> bool:
    if user_id is None:
        return False
    if not config.allowed_user_ids:
        return True
    return user_id in config.allowed_user_ids


def is_admin(user_id: Optional[int], config: Config) -> bool:
    if user_id is None:
        return False
    if not config.admin_user_ids:
        return False
    return user_id in config.admin_user_ids


def file_too_large(file_size: Optional[int], config: Config) -> bool:
    return file_size is not None and file_size > config.max_file_bytes


def duration_too_long(duration: Optional[int], config: Config) -> bool:
    return duration is not None and duration > config.max_duration_seconds


def build_settings_keyboard(settings: UserSettings) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"FPS: {settings.video_fps}", callback_data="set:video_fps"
    )
    builder.button(
        text=f"Duration: {settings.video_duration}s",
        callback_data="set:video_duration",
    )
    builder.button(
        text=f"Video Fit: {settings.video_fit}",
        callback_data="set:video_fit",
    )
    builder.button(
        text=f"Video BG: {settings.video_bg}", callback_data="set:video_bg"
    )
    builder.button(
        text=f"Image Fit: {settings.image_fit}",
        callback_data="set:image_fit",
    )
    builder.button(
        text=f"Image BG: {settings.image_bg}", callback_data="set:image_bg"
    )
    builder.button(text="Reset", callback_data="set:reset")
    builder.adjust(2, 2, 2, 1)
    return builder


def build_help_keyboard() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Open @Stickers", url="https://t.me/stickers")
    return builder


def format_settings(settings: UserSettings) -> str:
    return (
        "Current settings:\n"
        f"- Video FPS: {settings.video_fps}\n"
        f"- Video Duration: {settings.video_duration}s\n"
        f"- Video Fit: {settings.video_fit}\n"
        f"- Video Background: {settings.video_bg}\n"
        f"- Image Fit: {settings.image_fit}\n"
        f"- Image Background: {settings.image_bg}\n"
        "\nTap buttons to change."
    )


def max_file_mb(config: Config) -> int:
    return max(1, config.max_file_bytes // (1024 * 1024))


async def process_tgs_file(
    message: Message,
    input_path: Path,
    settings: UserSettings,
    config: Config,
) -> None:
    output_path = input_path.with_name("emoji.tgs")
    used_lottie = convert_tgs_to_emoji(input_path, output_path)
    caption = (
        "Here is your .tgs for an animated emoji. Upload it in @Stickers. 👻✨"
        if used_lottie
        else "Here is the original .tgs 👻✨"
    )
    zip_path = input_path.with_name("emoji.tgs.zip")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(output_path, arcname="emoji.tgs")
    await send_file(
        message,
        zip_path,
        f"{caption} Sent as .zip so Telegram keeps it as a downloadable file. You need to download this file and unpack it to get the .tgs file. 👻✨",
        filename="emoji.tgs.zip",
        disable_content_type_detection=True,
    )
    stats.inc("tgs")


async def process_image_file(
    message: Message,
    input_path: Path,
    settings: UserSettings,
    config: Config,
) -> None:
    if not PIL_AVAILABLE:
        await message.answer(
            "Image mode not available. 👻✨"
        )
        return
    output_png = input_path.with_name("emoji.png")
    ok = convert_image_to_png_emoji(input_path, output_png, settings)
    if ok:
        await send_file(
            message,
            output_png,
            "Here is your 100x100 .png for a static emoji. 👻✨",
        )
        stats.inc("image")
        return
    await message.answer("Could not process the image. Try another file. 👻✨")


async def process_video_file(
    message: Message,
    input_path: Path,
    settings: UserSettings,
    config: Config,
    duration: Optional[int] = None,
) -> None:
    if not command_exists("ffmpeg"):
        await message.answer(
            "Video mode not available. 👻✨"
        )
        return

    output_webm = input_path.with_name("emoji.webm")
    output_mp4 = input_path.with_name("fallback.mp4")
    output_gif = input_path.with_name("fallback.gif")

    trimmed_note = ""
    if duration is not None and duration > config.max_duration_seconds:
        trimmed_note = (
            f" Input was longer than {config.max_duration_seconds}s and was trimmed."
        )

    ok = convert_video_to_video_emoji(input_path, output_webm, settings)
    if ok:
        await send_file(
            message,
            output_webm,
            f"Here is your .webm for video emoji (VP9).{trimmed_note} 👻✨",
        )
        stats.inc("video_emoji")
        return

    mp4_ok = convert_webm_to_mp4(input_path, output_mp4, settings)
    if mp4_ok:
        await send_file(
            message,
            output_mp4,
            f"Video emoji limits were too tight. Here is .mp4 instead.{trimmed_note} 👻✨",
        )
        stats.inc("video_fallback_mp4")
        return

    gif_ok = convert_webm_to_gif(input_path, output_gif, settings)
    if gif_ok:
        await send_file(
            message,
            output_gif,
            f"Video emoji limits were too tight. Here is .gif instead.{trimmed_note} 👻✨",
        )
        stats.inc("video_fallback_gif")
        return

    await message.answer("Could not process the video. Try another file. 👻✨")

async def send_file(
    message: Message,
    path: Path,
    caption: str,
    include_help: bool = True,
    filename: Optional[str] = None,
    disable_content_type_detection: bool = False,
) -> None:
    keyboard = build_help_keyboard().as_markup() if include_help else None
    await message.answer_document(
        FSInputFile(str(path), filename=filename),
        caption=caption,
        reply_markup=keyboard,
        disable_content_type_detection=disable_content_type_detection,
    )


async def reject_if_not_allowed(message: Message, config: Config) -> bool:
    user_id = message.from_user.id if message.from_user else None
    if not is_allowed(user_id, config):
        await message.answer("Access denied. The bouncer says “no”. 👻✨")
        return True
    return False


def get_config() -> Config:
    if app_config is None:
        raise RuntimeError("Config is not initialized")
    return app_config

async def handle_start(message: Message) -> None:
    config = get_config()
    if await reject_if_not_allowed(message, config):
        return
    await message.answer(SUPPORTED_FORMATS_TEXT)


async def handle_help(message: Message) -> None:
    config = get_config()
    if await reject_if_not_allowed(message, config):
        return
    text = (
        "Send a sticker, animated sticker, image, GIF, or video. 👻✨\n"
        "I will return a file you can upload to @Stickers. 👻✨\n"
        "Use /settings to tweak fit, background, FPS, and duration. 👻✨"
    )
    await message.answer(text, reply_markup=build_help_keyboard().as_markup())


async def handle_settings(message: Message) -> None:
    config = get_config()
    if await reject_if_not_allowed(message, config):
        return
    user_id = message.from_user.id if message.from_user else 0
    settings = get_settings(user_id)
    await message.answer(
        f"{format_settings(settings)}\nSettings updated. No ghosts harmed. 👻✨",
        reply_markup=build_settings_keyboard(settings).as_markup(),
    )


async def handle_stats(message: Message) -> None:
    config = get_config()
    if not is_admin(message.from_user.id if message.from_user else None, config):
        return
    uptime = int(time.monotonic() - stats.start_time)
    total_requests = sum(u.count for u in stats.users.values())
    lines = [f"Uptime: {uptime}s", f"unique_users: {len(stats.users)}"]
    if total_requests:
        lines.append(f"total_requests: {total_requests}")
    for key in sorted(stats.counts.keys()):
        lines.append(f"{key}: {stats.counts[key]}")
    await message.answer("\n".join(lines) + "\nStats are looking spooky. 👻✨")


def format_user_label(stat: UserStat) -> str:
    if stat.username:
        return f"@{stat.username}"
    if stat.full_name:
        return stat.full_name
    return f"id {stat.user_id}"


async def handle_users(message: Message) -> None:
    config = get_config()
    if not is_admin(message.from_user.id if message.from_user else None, config):
        return
    if not stats.users:
        await message.answer("No requests yet. The void stares back. 👻✨")
        return
    ranked = sorted(
        stats.users.values(), key=lambda u: (u.count, u.last_seen), reverse=True
    )
    total = sum(u.count for u in ranked)
    header = f"Users: {len(ranked)} | Requests: {total}"
    lines = [header]
    limit = 50
    for stat in ranked[:limit]:
        lines.append(f"{format_user_label(stat)} (id {stat.user_id}): {stat.count}")
    if len(ranked) > limit:
        lines.append(f"... and {len(ranked) - limit} more")
    await message.answer("\n".join(lines) + "\nWho's been summoning me? 👻✨")


async def handle_health(message: Message) -> None:
    config = get_config()
    if await reject_if_not_allowed(message, config):
        return
    await message.answer("All clear. Systems are spooky. 👻✨")


async def handle_settings_callback(query: CallbackQuery) -> None:
    config = get_config()
    user_id = query.from_user.id if query.from_user else 0
    if not is_allowed(user_id, config):
        await query.answer("Access denied. The bouncer says “no”. 👻✨", show_alert=True)
        return
    settings = get_settings(user_id)
    data = query.data or ""
    if not data.startswith("set:"):
        return
    action = data.split(":", 1)[1]
    if action == "video_fps":
        settings.video_fps = 24 if settings.video_fps == 30 else 30
    elif action == "video_duration":
        if settings.video_duration == 3:
            settings.video_duration = 2
        elif settings.video_duration == 2:
            settings.video_duration = 1
        else:
            settings.video_duration = 3
    elif action == "video_fit":
        settings.video_fit = "crop" if settings.video_fit == "pad" else "pad"
    elif action == "video_bg":
        settings.video_bg = "transparent" if settings.video_bg == "black" else "black"
    elif action == "image_fit":
        settings.image_fit = "crop" if settings.image_fit == "pad" else "pad"
    elif action == "image_bg":
        settings.image_bg = "transparent" if settings.image_bg == "black" else "black"
    elif action == "reset":
        user_settings[user_id] = UserSettings()
        settings = user_settings[user_id]

    await query.message.edit_text(
        f"{format_settings(settings)}\nSettings updated. No ghosts harmed. 👻✨",
        reply_markup=build_settings_keyboard(settings).as_markup(),
    )
    await query.answer()


async def handle_unsupported(message: Message) -> None:
    config = get_config()
    if await reject_if_not_allowed(message, config):
        return
    await message.answer(SUPPORTED_FORMATS_TEXT)


async def handle_sticker(message: Message, bot: Bot) -> None:
    config = get_config()
    if await reject_if_not_allowed(message, config):
        return
    sticker = message.sticker
    if not sticker:
        return
    if file_too_large(sticker.file_size, config):
        await message.answer(
            f"File is too large. Max {max_file_mb(config)} MB. 👻✨"
        )
        return

    stats.record_user(message.from_user)
    user_id = message.from_user.id if message.from_user else 0
    settings = get_settings(user_id)

    if sticker.is_video:
        stats.inc("sticker_video_in")
        file = await bot.get_file(sticker.file_id)
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            input_webm = tmpdir_path / "input.webm"
            await bot.download_file(file.file_path, destination=input_webm)
            async with semaphore:
                await process_video_file(
                    message,
                    input_webm,
                    settings,
                    config,
                    duration=getattr(sticker, "duration", None),
                )
        return

    if not sticker.is_animated:
        stats.inc("sticker_static_in")
        file = await bot.get_file(sticker.file_id)
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            input_webp = tmpdir_path / "input.webp"

            await bot.download_file(file.file_path, destination=input_webp)
            async with semaphore:
                await process_image_file(message, input_webp, settings, config)
        return

    stats.inc("sticker_animated_in")
    file = await bot.get_file(sticker.file_id)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        input_path = tmpdir_path / "input.tgs"

        await bot.download_file(file.file_path, destination=input_path)
        async with semaphore:
            await process_tgs_file(message, input_path, settings, config)


async def handle_photo(message: Message, bot: Bot) -> None:
    config = get_config()
    if await reject_if_not_allowed(message, config):
        return
    photo = message.photo[-1] if message.photo else None
    if not photo:
        return
    if file_too_large(photo.file_size, config):
        await message.answer(
            f"File is too large. Max {max_file_mb(config)} MB. 👻✨"
        )
        return
    stats.inc("photo_in")
    stats.record_user(message.from_user)
    user_id = message.from_user.id if message.from_user else 0
    settings = get_settings(user_id)
    file = await bot.get_file(photo.file_id)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        input_img = tmpdir_path / "input"

        await bot.download_file(file.file_path, destination=input_img)
        async with semaphore:
            await process_image_file(message, input_img, settings, config)


async def handle_animation(message: Message, bot: Bot) -> None:
    config = get_config()
    if await reject_if_not_allowed(message, config):
        return
    animation = message.animation
    if not animation:
        return
    if file_too_large(animation.file_size, config):
        await message.answer(
            f"File is too large. Max {max_file_mb(config)} MB. 👻✨"
        )
        return
    stats.inc("animation_in")
    stats.record_user(message.from_user)
    user_id = message.from_user.id if message.from_user else 0
    settings = get_settings(user_id)
    file = await bot.get_file(animation.file_id)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        input_anim = tmpdir_path / "input.anim"

        await bot.download_file(file.file_path, destination=input_anim)
        async with semaphore:
            await process_video_file(
                message, input_anim, settings, config, duration=animation.duration
            )


async def handle_video(message: Message, bot: Bot) -> None:
    config = get_config()
    if await reject_if_not_allowed(message, config):
        return
    video = message.video
    if not video:
        return
    if file_too_large(video.file_size, config):
        await message.answer(
            f"File is too large. Max {max_file_mb(config)} MB. 👻✨"
        )
        return
    stats.inc("video_in")
    stats.record_user(message.from_user)
    user_id = message.from_user.id if message.from_user else 0
    settings = get_settings(user_id)
    file = await bot.get_file(video.file_id)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        input_video = tmpdir_path / "input.video"

        await bot.download_file(file.file_path, destination=input_video)
        async with semaphore:
            await process_video_file(
                message, input_video, settings, config, duration=video.duration
            )


async def handle_document(message: Message, bot: Bot) -> None:
    config = get_config()
    if await reject_if_not_allowed(message, config):
        return
    doc = message.document
    if not doc:
        return
    if file_too_large(doc.file_size, config):
        await message.answer(
            f"File is too large. Max {max_file_mb(config)} MB. 👻✨"
        )
        return
    stats.inc("document_in")
    stats.record_user(message.from_user)
    mime = (doc.mime_type or "").lower()
    doc_duration = getattr(doc, "duration", None)
    user_id = message.from_user.id if message.from_user else 0
    settings = get_settings(user_id)
    file = await bot.get_file(doc.file_id)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        input_doc = tmpdir_path / "input.doc"
        await bot.download_file(file.file_path, destination=input_doc)

        if mime == "image/gif":
            async with semaphore:
                await process_video_file(message, input_doc, settings, config)
            return
        if mime.startswith("image/"):
            async with semaphore:
                await process_image_file(message, input_doc, settings, config)
            return
        if mime.startswith("video/"):
            async with semaphore:
                await process_video_file(
                    message, input_doc, settings, config, duration=doc_duration
                )
            return

    await message.answer(SUPPORTED_FORMATS_TEXT)


async def main() -> None:
    config = load_config()
    logging.basicConfig(level=config.log_level)
    logger = logging.getLogger("emojibot")

    if not command_exists("ffmpeg"):
        logger.warning("ffmpeg is not available. Video processing will fail.")
    if not LOTTIE_AVAILABLE:
        logger.warning("lottie is not available. .tgs scaling will be skipped.")
    if not PIL_AVAILABLE:
        logger.warning("Pillow is not available. Image processing will fail.")

    global app_config, semaphore
    app_config = config
    semaphore = asyncio.Semaphore(config.concurrency)

    bot = Bot(token=config.token)
    dp = Dispatcher()

    dp.message.register(handle_start, CommandStart())
    dp.message.register(handle_help, Command("help"))
    dp.message.register(handle_settings, Command("settings"))
    dp.message.register(handle_stats, Command("stats"))
    dp.message.register(handle_users, Command("users"))
    dp.message.register(handle_health, Command("health"))
    dp.callback_query.register(
        handle_settings_callback, F.data.startswith("set:")
    )
    dp.message.register(handle_sticker, F.sticker)
    dp.message.register(handle_photo, F.photo)
    dp.message.register(handle_animation, F.animation)
    dp.message.register(handle_video, F.video)
    dp.message.register(handle_document, F.document)
    dp.message.register(handle_unsupported)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
