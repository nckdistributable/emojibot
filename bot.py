import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message, User
from aiogram.types.input_file import FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

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
    stats_file: str = "stats.json"


@dataclass
class UserSettings:
    video_fps: int = 30
    video_duration: int = 3
    video_fit: str = "pad"
    video_bg: str = "black"
    image_fit: str = "pad"
    image_bg: str = "black"
    image_output: str = "static"
    album_mode: str = "animate"
    motion: str = "normal"
    long_video: str = "trim"
    trim_start: int = 0
    trim_end: int = 0
    image_filter: str = "none"
    outline: str = "off"
    cut_bg: str = "off"
    text: str = ""
    sheet_cols: int = 0
    sheet_rows: int = 0


MAX_ACTIVE_DAYS = 60
NIGHT_HOUR_END = 6
MILESTONES = (1, 10, 50, 100, 250, 500, 1000)
TYPE_LABELS = {
    "sticker": "stickers",
    "photo": "photos",
    "animation": "GIFs",
    "video": "videos",
    "document": "files",
    "album": "photo series",
}
# Formats that count toward the "tried every format" achievement. Albums are
# deliberately excluded so the badge keeps the meaning it had when it shipped.
CORE_INPUT_TYPES = ("sticker", "photo", "animation", "video", "document")
# Telegram delivers an album as separate messages sharing a media_group_id,
# so frames are buffered until this long passes without a new one arriving.
ALBUM_DEBOUNCE_SECONDS = 2.0
MAX_ALBUM_FRAMES = 10
MAX_SHEET_SIDE = 8
MAX_SHEET_CELLS = 36
PACK_README = (
    "Telegram emoji pack\n"
    "\n"
    "Every file here is a ready 100x100 emoji.\n"
    "Open @Stickers in Telegram, send /newemojipack and upload the files.\n"
)


def parse_day(value: str) -> Optional[date]:
    try:
        return date.fromisoformat(value)
    except Exception:
        return None


@dataclass
class UserStat:
    user_id: int
    username: str = ""
    full_name: str = ""
    count: int = 0
    last_seen: float = 0.0
    first_seen: float = 0.0
    night_count: int = 0
    types: dict[str, int] = field(default_factory=dict)
    days: list[str] = field(default_factory=list)


@dataclass
class Stats:
    start_time: float
    counts: dict[str, int] = field(default_factory=dict)
    users: dict[int, UserStat] = field(default_factory=dict)
    path: Optional[Path] = None

    def inc(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1
        self.save()

    def record_user(self, user: Optional[User], kind: str = "") -> None:
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

        now = time.time()
        if stat.first_seen <= 0:
            stat.first_seen = now
        stat.count += 1
        stat.last_seen = now
        if kind:
            stat.types[kind] = stat.types.get(kind, 0) + 1

        local = time.localtime(now)
        if local.tm_hour < NIGHT_HOUR_END:
            stat.night_count += 1
        today = time.strftime("%Y-%m-%d", local)
        if not stat.days or stat.days[-1] != today:
            stat.days.append(today)
            del stat.days[:-MAX_ACTIVE_DAYS]

        self.save()

    def to_dict(self) -> dict:
        return {
            "counts": self.counts,
            "users": {
                str(uid): {
                    "user_id": u.user_id,
                    "username": u.username,
                    "full_name": u.full_name,
                    "count": u.count,
                    "last_seen": u.last_seen,
                    "first_seen": u.first_seen,
                    "night_count": u.night_count,
                    "types": u.types,
                    "days": u.days,
                }
                for uid, u in self.users.items()
            },
        }

    def save(self) -> None:
        if self.path is None:
            return
        try:
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(
                json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except Exception:
            logging.getLogger("emojibot").warning(
                "Failed to persist stats to %s", self.path, exc_info=True
            )


def load_stats(path: Optional[Path]) -> Stats:
    stats = Stats(start_time=time.monotonic(), path=path)
    if path is None:
        return stats
    if path.parent and not path.parent.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    if not path.exists():
        return stats
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logging.getLogger("emojibot").warning(
            "Failed to load stats from %s; starting fresh", path, exc_info=True
        )
        return stats

    counts = data.get("counts")
    if isinstance(counts, dict):
        for key, value in counts.items():
            try:
                stats.counts[str(key)] = int(value)
            except Exception:
                continue

    users = data.get("users")
    if isinstance(users, dict):
        for raw_id, info in users.items():
            if not isinstance(info, dict):
                continue
            try:
                user_id = int(info.get("user_id", raw_id))
                types: dict[str, int] = {}
                raw_types = info.get("types")
                if isinstance(raw_types, dict):
                    for key, value in raw_types.items():
                        try:
                            types[str(key)] = int(value)
                        except Exception:
                            continue
                days: list[str] = []
                raw_days = info.get("days")
                if isinstance(raw_days, list):
                    for value in raw_days:
                        if isinstance(value, str) and parse_day(value) is not None:
                            days.append(value)
                    days = sorted(set(days))[-MAX_ACTIVE_DAYS:]
                stats.users[user_id] = UserStat(
                    user_id=user_id,
                    username=str(info.get("username", "")),
                    full_name=str(info.get("full_name", "")),
                    count=int(info.get("count", 0)),
                    last_seen=float(info.get("last_seen", 0.0)),
                    first_seen=float(info.get("first_seen", 0.0)),
                    night_count=int(info.get("night_count", 0)),
                    types=types,
                    days=days,
                )
            except Exception:
                continue
    return stats


SUPPORTED_FORMATS_TEXT = (
    "Send: sticker, animated sticker, image, GIF, or video. "
    "Send several photos at once and I will animate them into one emoji. "
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
    stats_file = os.getenv("STATS_FILE", "stats.json").strip()

    return Config(
        token=token,
        log_level=log_level,
        max_file_bytes=max_file_mb * 1024 * 1024,
        max_duration_seconds=max_duration_seconds,
        allowed_user_ids=allowed_user_ids,
        admin_user_ids=admin_user_ids,
        concurrency=max(1, concurrency),
        stats_file=stats_file,
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


def build_scale_filters(fit: str, bg: str) -> list[str]:
    if fit == "crop":
        return [
            "scale=100:100:flags=lanczos:force_original_aspect_ratio=increase",
            "crop=100:100",
        ]
    color = "0x00000000" if bg == "transparent" else "black"
    return [
        "scale=100:100:flags=lanczos:force_original_aspect_ratio=decrease",
        f"pad=100:100:(ow-iw)/2:(oh-ih)/2:color={color}",
    ]


def build_video_filter(settings: UserSettings) -> str:
    filters = build_scale_filters(settings.video_fit, settings.video_bg)
    filters.append(f"fps={settings.video_fps}")
    return ",".join(filters)


def video_color_filter(name: str) -> str:
    """ffmpeg equivalent of the image filters, for real video sources."""
    if name == "bw":
        return "hue=s=0"
    if name == "invert":
        return "negate"
    if name == "sepia":
        return (
            "colorchannelmixer="
            ".393:.769:.189:0:.349:.686:.168:0:.272:.534:.131"
        )
    if name == "pixel":
        return "scale=20:20:flags=neighbor,scale=100:100:flags=neighbor"
    return ""


def motion_filter(motion: str) -> str:
    if motion == "reverse":
        return "reverse"
    if motion == "boomerang":
        # Play the clip, then play it backwards, as one stream.
        return "split[m0][m1];[m1]reverse[m1r];[m0][m1r]concat=n=2:v=1"
    return ""


def probe_duration(path: Path) -> Optional[float]:
    if not command_exists("ffprobe"):
        return None
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            capture_output=True,
            text=True,
        )
        return float(result.stdout.strip())
    except Exception:
        return None


def target_seconds(settings: UserSettings) -> float:
    target = float(max(1, min(3, settings.video_duration)))
    if settings.motion == "boomerang":
        # The clip is played forward then backward, so source only needs half.
        target /= 2
    return target


def build_encode_plan(
    input_path: Path, settings: UserSettings, config: Config
) -> tuple[list[str], str]:
    """Build the ffmpeg args that go before -i, plus the -vf filter chain."""
    target = target_seconds(settings)
    pre_args: list[str] = []

    start = max(0, settings.trim_start)
    if start:
        pre_args += ["-ss", str(start)]
    window: Optional[float] = None
    if settings.trim_end > start:
        window = float(settings.trim_end - start)

    filters = build_scale_filters(settings.video_fit, settings.video_bg)

    if settings.long_video == "speedup":
        source = probe_duration(input_path)
        available = None
        if source is not None:
            available = max(0.0, source - start)
            if window is not None:
                available = min(available, window)
            available = min(available, float(config.max_duration_seconds))
        if available and available > target:
            # Squeeze the whole clip into the target length instead of cutting.
            filters.append(f"setpts=PTS/{available / target:.6f}")
            pre_args += ["-t", f"{available:.3f}"]
        else:
            pre_args += ["-t", f"{target:.3f}"]
    else:
        limit = target if window is None else min(target, window)
        pre_args += ["-t", f"{limit:.3f}"]

    color = video_color_filter(settings.image_filter)
    if color:
        filters.append(color)
    filters.append(f"fps={settings.video_fps}")
    motion = motion_filter(settings.motion)
    if motion:
        filters.append(motion)
    return pre_args, ",".join(filters)


def convert_video_to_video_emoji(
    input_path: Path,
    output_path: Path,
    settings: UserSettings,
    config: Config,
    size_limit_bytes: int = 256 * 1024,
) -> bool:
    if not command_exists("ffmpeg"):
        return False

    pre_args, video_filter = build_encode_plan(input_path, settings, config)
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
                *pre_args,
                "-i",
                str(input_path),
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


FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
)
IMAGE_FILTERS = ("none", "bw", "invert", "sepia", "pixel")
OUTLINE_MODES = ("off", "white", "black")


def load_font(size: int):
    for candidate in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size)
    except Exception:
        return ImageFont.load_default()


def remove_background(img: Image.Image, tolerance: int = 40) -> Image.Image:
    """Clear pixels connected to a corner that match that corner's color."""
    img = img.convert("RGBA")
    width, height = img.size
    pixels = img.load()
    limit = tolerance * 3
    visited = bytearray(width * height)
    queue: list[tuple[int, int, tuple[int, int, int]]] = []

    for corner in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
        red, green, blue, alpha = pixels[corner]
        if alpha:
            queue.append((corner[0], corner[1], (red, green, blue)))

    while queue:
        x, y, reference = queue.pop()
        if x < 0 or y < 0 or x >= width or y >= height:
            continue
        index = y * width + x
        if visited[index]:
            continue
        red, green, blue, alpha = pixels[x, y]
        if alpha == 0:
            visited[index] = 1
            continue
        distance = (
            abs(red - reference[0])
            + abs(green - reference[1])
            + abs(blue - reference[2])
        )
        if distance > limit:
            continue
        visited[index] = 1
        pixels[x, y] = (red, green, blue, 0)
        queue.extend(
            [
                (x + 1, y, reference),
                (x - 1, y, reference),
                (x, y + 1, reference),
                (x, y - 1, reference),
            ]
        )
    return img


def apply_image_filter(img: Image.Image, name: str) -> Image.Image:
    if name == "none" or name not in IMAGE_FILTERS:
        return img
    alpha = img.getchannel("A")
    rgb = img.convert("RGB")
    if name == "bw":
        rgb = ImageOps.grayscale(rgb).convert("RGB")
    elif name == "invert":
        rgb = ImageOps.invert(rgb)
    elif name == "sepia":
        rgb = ImageOps.colorize(
            ImageOps.grayscale(rgb), black=(30, 15, 0), white=(255, 220, 170)
        )
    elif name == "pixel":
        blocks = (20, 20)
        rgb = rgb.resize(blocks, Image.NEAREST).resize(img.size, Image.NEAREST)
        alpha = alpha.resize(blocks, Image.NEAREST).resize(img.size, Image.NEAREST)
    result = rgb.convert("RGBA")
    result.putalpha(alpha)
    return result


def add_outline(img: Image.Image, mode: str) -> Image.Image:
    if mode not in ("white", "black"):
        return img
    color = (255, 255, 255, 255) if mode == "white" else (0, 0, 0, 255)
    dilated = img.getchannel("A").filter(ImageFilter.MaxFilter(5))
    layer = Image.new("RGBA", img.size, color)
    layer.putalpha(dilated)
    return Image.alpha_composite(layer, img)


def draw_caption(img: Image.Image, text: str) -> Image.Image:
    text = text.strip()
    if not text:
        return img
    draw = ImageDraw.Draw(img)
    width, height = img.size
    size = 26
    font = load_font(size)
    while size > 8:
        font = load_font(size)
        box = draw.textbbox((0, 0), text, font=font, stroke_width=2)
        if box[2] - box[0] <= width - 6 and box[3] - box[1] <= height // 2:
            break
        size -= 2
    box = draw.textbbox((0, 0), text, font=font, stroke_width=2)
    x = (width - (box[2] - box[0])) // 2 - box[0]
    y = height - (box[3] - box[1]) - box[1] - 4
    draw.text(
        (x, y),
        text,
        font=font,
        fill=(255, 255, 255, 255),
        stroke_width=2,
        stroke_fill=(0, 0, 0, 255),
    )
    return img


def apply_image_effects(img: Image.Image, settings: UserSettings) -> Image.Image:
    if settings.cut_bg == "on":
        img = remove_background(img)
    img = apply_image_filter(img, settings.image_filter)
    img = add_outline(img, settings.outline)
    if settings.text:
        img = draw_caption(img, settings.text)
    return img


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
            result = apply_image_effects(result, settings)
            result.save(output_path, format="PNG")
        return True
    except Exception:
        return False


def build_image_video_filter(settings: UserSettings) -> str:
    if settings.image_fit == "crop":
        scale = (
            "scale=100:100:flags=lanczos:force_original_aspect_ratio=increase"
        )
        crop = "crop=100:100"
        filters = [scale, crop]
    else:
        color = "0x00000000" if settings.image_bg == "transparent" else "black"
        scale = (
            "scale=100:100:flags=lanczos:force_original_aspect_ratio=decrease"
        )
        pad = f"pad=100:100:(ow-iw)/2:(oh-ih)/2:color={color}"
        filters = [scale, pad]

    filters.append(f"fps={settings.video_fps}")
    return ",".join(filters)


def convert_image_to_video_emoji(
    input_path: Path,
    output_path: Path,
    settings: UserSettings,
    size_limit_bytes: int = 256 * 1024,
) -> bool:
    if not command_exists("ffmpeg"):
        return False

    duration = max(1, min(3, settings.video_duration))
    video_filter = build_image_video_filter(settings)
    pix_fmt = "yuva420p" if settings.image_bg == "transparent" else "yuv420p"

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
                "-loop",
                "1",
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


def convert_frames_to_video_emoji(
    pattern: str,
    frame_count: int,
    output_path: Path,
    settings: UserSettings,
    size_limit_bytes: int = 256 * 1024,
) -> bool:
    if not command_exists("ffmpeg") or frame_count < 1:
        return False

    target = target_seconds(settings)
    # Spread the frames evenly over the clip: N frames in `target` seconds.
    input_rate = f"{frame_count / target:.6f}"
    frame_filters = [f"fps={settings.video_fps}"]
    motion = motion_filter(settings.motion)
    if motion:
        frame_filters.append(motion)
    frame_filter = ",".join(frame_filters)
    pix_fmt = "yuva420p" if settings.image_bg == "transparent" else "yuv420p"

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
                "-framerate",
                input_rate,
                "-i",
                pattern,
                "-vf",
                frame_filter,
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


def convert_frames_to_gif(
    pattern: str,
    frame_count: int,
    output_path: Path,
    settings: UserSettings,
) -> bool:
    if not command_exists("ffmpeg") or frame_count < 1:
        return False
    target = target_seconds(settings)
    args = ["ffmpeg", "-y", "-framerate", f"{frame_count / target:.6f}", "-i", pattern]
    motion = motion_filter(settings.motion)
    if motion:
        args += ["-vf", motion]
    args += ["-loop", "0", str(output_path)]
    return run_cmd(args)


def convert_webm_to_mp4(
    input_path: Path, output_path: Path, settings: UserSettings, config: Config
) -> bool:
    if not command_exists("ffmpeg"):
        return False
    pre_args, video_filter = build_encode_plan(input_path, settings, config)
    return run_cmd(
        [
            "ffmpeg",
            "-y",
            *pre_args,
            "-i",
            str(input_path),
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
    input_path: Path, output_path: Path, settings: UserSettings, config: Config
) -> bool:
    if not command_exists("ffmpeg"):
        return False
    pre_args, video_filter = build_encode_plan(input_path, settings, config)
    return run_cmd(
        [
            "ffmpeg",
            "-y",
            *pre_args,
            "-i",
            str(input_path),
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
    builder.button(
        text=f"Image Output: {settings.image_output}",
        callback_data="set:image_output",
    )
    builder.button(
        text=f"Album: {settings.album_mode}",
        callback_data="set:album_mode",
    )
    builder.button(
        text=f"Motion: {settings.motion}", callback_data="set:motion"
    )
    builder.button(
        text=f"Long video: {settings.long_video}",
        callback_data="set:long_video",
    )
    builder.button(
        text=f"Filter: {settings.image_filter}",
        callback_data="set:image_filter",
    )
    builder.button(
        text=f"Outline: {settings.outline}", callback_data="set:outline"
    )
    builder.button(
        text=f"Cut BG: {settings.cut_bg}", callback_data="set:cut_bg"
    )
    builder.button(text="Reset", callback_data="set:reset")
    builder.adjust(2, 2, 2, 2, 2, 2, 1, 1)
    return builder


def build_help_keyboard() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Open @Stickers", url="https://t.me/stickers")
    return builder


def format_trim(settings: UserSettings) -> str:
    if settings.trim_end > settings.trim_start:
        return f"{settings.trim_start}-{settings.trim_end}s"
    if settings.trim_start:
        return f"from {settings.trim_start}s"
    return "off"


def format_sheet(settings: UserSettings) -> str:
    if settings.sheet_cols > 1 or settings.sheet_rows > 1:
        return f"{settings.sheet_cols}x{settings.sheet_rows}"
    return "off"


def parse_sheet(raw: str) -> Optional[tuple[int, int]]:
    """Parse '4x3', '4 3' or '4' (square) into (cols, rows)."""
    text = raw.strip().lower().replace("x", " ").replace("*", " ")
    parts = text.split()
    if not parts or len(parts) > 2:
        return None
    try:
        values = [int(part) for part in parts]
    except ValueError:
        return None
    cols = values[0]
    rows = values[1] if len(values) == 2 else values[0]
    if not (1 <= cols <= MAX_SHEET_SIDE and 1 <= rows <= MAX_SHEET_SIDE):
        return None
    if cols * rows < 2:
        return None
    return cols, rows


def parse_trim(raw: str) -> Optional[tuple[int, int]]:
    """Parse '12', '2-5' or '2 5' into (start, end). None means invalid."""
    text = raw.strip().replace("-", " ").replace(":", " ")
    if not text:
        return None
    parts = text.split()
    if len(parts) > 2:
        return None
    try:
        values = [int(part) for part in parts]
    except ValueError:
        return None
    if any(value < 0 for value in values):
        return None
    start = values[0]
    end = values[1] if len(values) == 2 else 0
    if end and end <= start:
        return None
    return start, end


def format_settings(settings: UserSettings) -> str:
    return (
        "Current settings:\n"
        f"- Video FPS: {settings.video_fps}\n"
        f"- Video Duration: {settings.video_duration}s\n"
        f"- Video Fit: {settings.video_fit}\n"
        f"- Video Background: {settings.video_bg}\n"
        f"- Image Fit: {settings.image_fit}\n"
        f"- Image Background: {settings.image_bg}\n"
        f"- Image Output: {settings.image_output} "
        "(static = .png, video = .webm)\n"
        f"- Album: {settings.album_mode} "
        "(animate = one animated emoji, separate = one emoji per image, "
        "zip = the whole set as an archive)\n"
        f"- Motion: {settings.motion} "
        "(normal, reverse, or boomerang)\n"
        f"- Long video: {settings.long_video} "
        "(trim = cut, speedup = squeeze it all in)\n"
        f"- Trim: {format_trim(settings)} (set with /trim)\n"
        f"- Filter: {settings.image_filter} "
        "(none, bw, invert, sepia, pixel)\n"
        f"- Outline: {settings.outline}\n"
        f"- Cut background: {settings.cut_bg}\n"
        f"- Text: {settings.text or 'off'} (set with /text)\n"
        f"- Sheet: {format_sheet(settings)} (set with /sheet)\n"
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


async def process_image_as_video(
    message: Message,
    input_path: Path,
    settings: UserSettings,
    config: Config,
) -> None:
    if not command_exists("ffmpeg"):
        await message.answer("Video mode not available. 👻✨")
        return

    # Render through the image pipeline first so effects (filter, outline,
    # background removal, caption) land on the frame before it is encoded.
    source = input_path
    if PIL_AVAILABLE:
        rendered = input_path.with_name("rendered.png")
        if convert_image_to_png_emoji(input_path, rendered, settings):
            source = rendered

    output_webm = input_path.with_name("emoji.webm")
    ok = convert_image_to_video_emoji(source, output_webm, settings)
    if ok:
        await send_file(
            message,
            output_webm,
            "Here is your .webm video emoji made from a static image (VP9). 👻✨",
        )
        stats.inc("image_video_emoji")
        return

    output_png = input_path.with_name("emoji.png")
    if PIL_AVAILABLE and convert_image_to_png_emoji(
        input_path, output_png, settings
    ):
        await send_file(
            message,
            output_png,
            "Could not fit a video emoji under the limits. Here is a static "
            ".png emoji instead. 👻✨",
        )
        stats.inc("image")
        return

    await message.answer("Could not process the image. Try another file. 👻✨")


@dataclass
class AlbumBuffer:
    message: Message
    file_ids: list[str] = field(default_factory=list)
    task: Optional[asyncio.Task] = None


album_buffers: dict[str, AlbumBuffer] = {}


async def buffer_album_photo(message: Message, bot: Bot, file_id: str) -> None:
    group_id = message.media_group_id or ""
    buffer = album_buffers.get(group_id)
    if buffer is None:
        buffer = AlbumBuffer(message=message)
        album_buffers[group_id] = buffer
    if len(buffer.file_ids) < MAX_ALBUM_FRAMES:
        buffer.file_ids.append(file_id)
    if buffer.task is not None:
        buffer.task.cancel()
    buffer.task = asyncio.create_task(flush_album_later(group_id, bot))


async def flush_album_later(group_id: str, bot: Bot) -> None:
    try:
        await asyncio.sleep(ALBUM_DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return
    buffer = album_buffers.pop(group_id, None)
    if buffer is None:
        return
    try:
        await process_album(buffer, bot)
    except Exception:
        logging.getLogger("emojibot").warning(
            "Failed to process album %s", group_id, exc_info=True
        )
        await buffer.message.answer(
            "Could not build an emoji from those images. 👻✨"
        )


async def send_album_pack(
    message: Message,
    work_dir: Path,
    frames: int,
    settings: UserSettings,
) -> None:
    """Pack each frame as its own emoji and send the set as one archive."""
    stats.inc("pack_in")
    await track_request(message, "album")

    members: list[tuple[Path, str]] = []
    as_video = settings.image_output == "video" and command_exists("ffmpeg")
    for index in range(frames):
        frame_path = work_dir / f"frame_{index:03d}.png"
        if as_video:
            clip_path = work_dir / f"emoji_{index + 1:02d}.webm"
            if convert_image_to_video_emoji(frame_path, clip_path, settings):
                members.append((clip_path, clip_path.name))
                continue
        members.append((frame_path, f"emoji_{index + 1:02d}.png"))

    zip_path = work_dir / "emoji_pack.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, name in members:
            archive.write(path, arcname=name)
        archive.writestr("README.txt", PACK_README)

    kind = "video" if as_video else "static"
    await send_file(
        message,
        zip_path,
        f"Here is your pack of {len(members)} {kind} emoji. Unzip it and "
        "upload the files in @Stickers. 👻✨",
        filename="emoji_pack.zip",
        disable_content_type_detection=True,
    )
    stats.inc("pack_zip")


async def process_album(buffer: AlbumBuffer, bot: Bot) -> None:
    message = buffer.message
    config = get_config()
    user_id = message.from_user.id if message.from_user else 0
    settings = get_settings(user_id)

    if not PIL_AVAILABLE:
        await message.answer("Image mode not available. 👻✨")
        return
    if not command_exists("ffmpeg"):
        await message.answer("Video mode not available. 👻✨")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        frames = 0
        for index, file_id in enumerate(buffer.file_ids):
            file = await bot.get_file(file_id)
            raw_path = tmpdir_path / f"raw_{index:03d}"
            await bot.download_file(file.file_path, destination=raw_path)
            frame_path = tmpdir_path / f"frame_{frames:03d}.png"
            if convert_image_to_png_emoji(raw_path, frame_path, settings):
                frames += 1

        if frames == 0:
            await message.answer(
                "Could not read those images. Try other files. 👻✨"
            )
            return
        if frames == 1:
            # Not really a series - fall back to the single image pipeline.
            stats.inc("photo_in")
            await track_request(message, "photo")
            async with semaphore:
                await process_image_file(
                    message, tmpdir_path / "frame_000.png", settings, config
                )
            return

        if settings.album_mode == "zip":
            await send_album_pack(message, tmpdir_path, frames, settings)
            return

        stats.inc("album_in")
        await track_request(message, "album")

        pattern = str(tmpdir_path / "frame_%03d.png")
        output_webm = tmpdir_path / "emoji.webm"
        async with semaphore:
            ok = convert_frames_to_video_emoji(
                pattern, frames, output_webm, settings
            )
        if ok:
            await send_file(
                message,
                output_webm,
                f"Here is your animated emoji from {frames} images "
                "(VP9 .webm). 👻✨",
            )
            stats.inc("album_video_emoji")
            return

        output_gif = tmpdir_path / "emoji.gif"
        async with semaphore:
            gif_ok = convert_frames_to_gif(
                pattern, frames, output_gif, settings
            )
        if gif_ok:
            await send_file(
                message,
                output_gif,
                "Video emoji limits were too tight. Here is a .gif "
                "instead. 👻✨",
            )
            stats.inc("album_fallback_gif")
            return

        await message.answer(
            "Could not build an emoji from those images. 👻✨"
        )


def slice_sheet(
    input_path: Path, out_dir: Path, settings: UserSettings
) -> int:
    """Cut a grid image into cells and normalize each into a frame."""
    cols = max(1, settings.sheet_cols)
    rows = max(1, settings.sheet_rows)
    frames = 0
    with Image.open(input_path) as sheet:
        sheet = sheet.convert("RGBA")
        cell_w = sheet.width / cols
        cell_h = sheet.height / rows
        for row in range(rows):
            for col in range(cols):
                if frames >= MAX_SHEET_CELLS:
                    break
                box = (
                    int(col * cell_w),
                    int(row * cell_h),
                    int((col + 1) * cell_w),
                    int((row + 1) * cell_h),
                )
                if box[2] - box[0] < 1 or box[3] - box[1] < 1:
                    continue
                cell_path = out_dir / f"cell_{frames:03d}.png"
                sheet.crop(box).save(cell_path, format="PNG")
                frame_path = out_dir / f"frame_{frames:03d}.png"
                if convert_image_to_png_emoji(cell_path, frame_path, settings):
                    frames += 1
    return frames


async def process_sprite_sheet(
    message: Message,
    input_path: Path,
    settings: UserSettings,
    config: Config,
) -> None:
    if not PIL_AVAILABLE:
        await message.answer("Image mode not available. 👻✨")
        return
    if not command_exists("ffmpeg"):
        await message.answer("Video mode not available. 👻✨")
        return

    out_dir = input_path.parent
    try:
        frames = slice_sheet(input_path, out_dir, settings)
    except Exception:
        logging.getLogger("emojibot").warning(
            "Failed to slice sheet", exc_info=True
        )
        frames = 0

    if frames < 2:
        await message.answer(
            "Could not cut that sheet. Check the grid with /sheet. 👻✨"
        )
        return

    stats.inc("sheet_in")
    pattern = str(out_dir / "frame_%03d.png")
    output_webm = out_dir / "sheet.webm"
    ok = convert_frames_to_video_emoji(pattern, frames, output_webm, settings)
    if ok:
        await send_file(
            message,
            output_webm,
            f"Here is your animated emoji from a {settings.sheet_cols}x"
            f"{settings.sheet_rows} sheet ({frames} frames). 👻✨",
        )
        stats.inc("sheet_video_emoji")
        return

    output_gif = out_dir / "sheet.gif"
    if convert_frames_to_gif(pattern, frames, output_gif, settings):
        await send_file(
            message,
            output_gif,
            "Video emoji limits were too tight. Here is a .gif instead. 👻✨",
        )
        stats.inc("sheet_fallback_gif")
        return

    await message.answer("Could not build an emoji from that sheet. 👻✨")


async def process_image_file(
    message: Message,
    input_path: Path,
    settings: UserSettings,
    config: Config,
) -> None:
    if settings.sheet_cols > 1 or settings.sheet_rows > 1:
        await process_sprite_sheet(message, input_path, settings, config)
        return
    if settings.image_output == "video":
        await process_image_as_video(message, input_path, settings, config)
        return
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

    ok = convert_video_to_video_emoji(input_path, output_webm, settings, config)
    if ok:
        await send_file(
            message,
            output_webm,
            f"Here is your .webm for video emoji (VP9).{trimmed_note} 👻✨",
        )
        stats.inc("video_emoji")
        return

    mp4_ok = convert_webm_to_mp4(input_path, output_mp4, settings, config)
    if mp4_ok:
        await send_file(
            message,
            output_mp4,
            f"Video emoji limits were too tight. Here is .mp4 instead.{trimmed_note} 👻✨",
        )
        stats.inc("video_fallback_mp4")
        return

    gif_ok = convert_webm_to_gif(input_path, output_gif, settings, config)
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
        "Send several photos in one album and I will turn them into a single "
        "animated emoji. 👻✨\n"
        "I will return a file you can upload to @Stickers. 👻✨\n"
        "Use /settings to tweak fit, background, FPS, and duration. 👻✨\n"
        "Use /me for your own record and /top for the leaderboard. 👻✨"
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


def format_duration(seconds: float) -> str:
    total = int(max(0, seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    # Seconds only matter below the hour mark; above that they are just noise.
    if (secs and total < 3600) or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def format_last_seen(last_seen: float) -> str:
    if last_seen <= 0:
        return "never"
    delta = time.time() - last_seen
    if delta < 60:
        return "just now"
    return f"{format_duration(delta)} ago"


def current_streak(days: list[str]) -> int:
    parsed = {d for d in (parse_day(v) for v in days) if d is not None}
    if not parsed:
        return 0
    today = date.today()
    cursor = today
    if cursor not in parsed:
        # A streak stays alive until the day after the last active day.
        cursor = today - timedelta(days=1)
        if cursor not in parsed:
            return 0
    streak = 0
    while cursor in parsed:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def favorite_type(stat: UserStat) -> str:
    if not stat.types:
        return "nothing yet"
    kind, hits = max(stat.types.items(), key=lambda item: item[1])
    return f"{TYPE_LABELS.get(kind, kind)} ({hits})"


def ranked_users() -> list[UserStat]:
    return sorted(
        stats.users.values(), key=lambda u: (u.count, u.last_seen), reverse=True
    )


def user_rank(user_id: int) -> Optional[int]:
    for index, stat in enumerate(ranked_users(), start=1):
        if stat.user_id == user_id:
            return index
    return None


def achievements(stat: UserStat) -> list[str]:
    earned: list[str] = []
    if stat.count >= 1:
        earned.append("🎬 Debut - first emoji made")
    if stat.count >= 10:
        earned.append("🔟 Regular - 10 emoji")
    if stat.count >= 100:
        earned.append("💯 Centurion - 100 emoji")
    if stat.count >= 500:
        earned.append("👑 Emoji royalty - 500 emoji")
    if all(stat.types.get(kind) for kind in CORE_INPUT_TYPES):
        earned.append("🎭 Versatile - tried every format")
    if stat.types.get("album"):
        earned.append("🎞 Animator - built an emoji from a photo series")
    if stat.night_count >= 5:
        earned.append("🦇 Night owl - 5 requests after midnight")
    streak = current_streak(stat.days)
    if streak >= 3:
        earned.append(f"🔥 On fire - {streak} days in a row")
    if stat.first_seen > 0 and time.time() - stat.first_seen >= 30 * 86400:
        earned.append("🧙 Veteran - a month with me")
    return earned


def milestone_text(count: int) -> str:
    if count == 1:
        return "🎉 Your very first emoji! Welcome aboard. 👻✨"
    return f"🎉 That is your {count}th emoji! Keep summoning. 👻✨"


async def track_request(message: Message, kind: str) -> None:
    user = message.from_user
    stats.record_user(user, kind)
    if user is None:
        return
    stat = stats.users.get(user.id)
    if stat and stat.count in MILESTONES:
        await message.answer(milestone_text(stat.count))


async def handle_trim(message: Message, command: CommandObject) -> None:
    config = get_config()
    if await reject_if_not_allowed(message, config):
        return
    user_id = message.from_user.id if message.from_user else 0
    settings = get_settings(user_id)
    raw = (command.args or "").strip()

    if not raw or raw.lower() in {"off", "reset", "none"}:
        settings.trim_start = 0
        settings.trim_end = 0
        await message.answer(
            "Trim is off. I will take the clip from the start. 👻✨"
        )
        return

    parsed = parse_trim(raw)
    if parsed is None:
        await message.answer(
            "Use /trim 2-5 to take seconds 2 to 5, /trim 2 to start at 2s, "
            "or /trim off. 👻✨"
        )
        return

    start, end = parsed
    settings.trim_start = start
    settings.trim_end = end
    await message.answer(
        f"Trim set to {format_trim(settings)}. Send a video or GIF. 👻✨"
    )


async def handle_text(message: Message, command: CommandObject) -> None:
    config = get_config()
    if await reject_if_not_allowed(message, config):
        return
    user_id = message.from_user.id if message.from_user else 0
    settings = get_settings(user_id)
    raw = (command.args or "").strip()

    if not raw or raw.lower() in {"off", "reset", "none"}:
        settings.text = ""
        await message.answer("Caption is off. 👻✨")
        return

    settings.text = raw[:32]
    await message.answer(
        f"Caption set to “{settings.text}”. Send an image. 👻✨"
    )


async def handle_sheet(message: Message, command: CommandObject) -> None:
    config = get_config()
    if await reject_if_not_allowed(message, config):
        return
    user_id = message.from_user.id if message.from_user else 0
    settings = get_settings(user_id)
    raw = (command.args or "").strip()

    if not raw or raw.lower() in {"off", "reset", "none"}:
        settings.sheet_cols = 0
        settings.sheet_rows = 0
        await message.answer("Sheet slicing is off. 👻✨")
        return

    parsed = parse_sheet(raw)
    if parsed is None:
        await message.answer(
            f"Use /sheet 4x3 for a grid (up to {MAX_SHEET_SIDE}x"
            f"{MAX_SHEET_SIDE}), or /sheet off. 👻✨"
        )
        return

    settings.sheet_cols, settings.sheet_rows = parsed
    await message.answer(
        f"Sheet set to {format_sheet(settings)}. Send the sheet image and I "
        "will animate its cells. 👻✨"
    )


async def handle_stats(message: Message) -> None:
    config = get_config()
    if not is_admin(message.from_user.id if message.from_user else None, config):
        return
    uptime = time.monotonic() - stats.start_time
    total_requests = sum(u.count for u in stats.users.values())
    lines = [
        f"Uptime: {format_duration(uptime)}",
        f"unique_users: {len(stats.users)}",
    ]
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
    ranked = ranked_users()
    total = sum(u.count for u in ranked)
    header = f"Users: {len(ranked)} | Requests: {total}"
    lines = [header]
    limit = 50
    for stat in ranked[:limit]:
        lines.append(
            f"{format_user_label(stat)} (id {stat.user_id}): {stat.count}"
            f" | last seen {format_last_seen(stat.last_seen)}"
        )
    if len(ranked) > limit:
        lines.append(f"... and {len(ranked) - limit} more")
    await message.answer("\n".join(lines) + "\nWho's been summoning me? 👻✨")


async def handle_me(message: Message) -> None:
    config = get_config()
    if await reject_if_not_allowed(message, config):
        return
    user_id = message.from_user.id if message.from_user else 0
    stat = stats.users.get(user_id)
    if stat is None or stat.count == 0:
        await message.answer(
            "You have not summoned anything yet. Send me a sticker, image, "
            "GIF, or video to get started. 👻✨"
        )
        return

    rank = user_rank(user_id)
    lines = [f"{format_user_label(stat)} - your spooky record 👻"]
    if rank:
        lines.append(f"Emoji made: {stat.count} (#{rank} of {len(stats.users)})")
    else:
        lines.append(f"Emoji made: {stat.count}")
    if stat.first_seen > 0:
        lines.append(f"With me for: {format_duration(time.time() - stat.first_seen)}")
    lines.append(f"Last seen: {format_last_seen(stat.last_seen)}")
    lines.append(f"Favorite format: {favorite_type(stat)}")
    lines.append(f"Active days: {len(stat.days)}")
    streak = current_streak(stat.days)
    if streak:
        day_word = "day" if streak == 1 else "days"
        lines.append(f"Current streak: {streak} {day_word} 🔥")

    earned = achievements(stat)
    if earned:
        lines.append("")
        lines.append("Achievements:")
        lines.extend(earned)

    next_milestone = next((m for m in MILESTONES if m > stat.count), None)
    if next_milestone:
        lines.append("")
        lines.append(
            f"{next_milestone - stat.count} more to reach {next_milestone}. 👻✨"
        )

    await message.answer("\n".join(lines))


async def handle_top(message: Message) -> None:
    config = get_config()
    if await reject_if_not_allowed(message, config):
        return
    ranked = ranked_users()
    if not ranked:
        await message.answer("Nobody on the board yet. Be the first. 👻✨")
        return

    user_id = message.from_user.id if message.from_user else 0
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = ["Top summoners 👻"]
    top_n = 10
    for index, stat in enumerate(ranked[:top_n], start=1):
        marker = medals.get(index, f"{index}.")
        suffix = " ← you" if stat.user_id == user_id else ""
        lines.append(f"{marker} {format_user_label(stat)} - {stat.count}{suffix}")

    rank = user_rank(user_id)
    if rank and rank > top_n:
        own = stats.users[user_id]
        lines.append("...")
        lines.append(f"{rank}. {format_user_label(own)} - {own.count} ← you")

    await message.answer("\n".join(lines) + "\nUse /me for your own record. 👻✨")


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
    elif action == "image_output":
        settings.image_output = (
            "video" if settings.image_output == "static" else "static"
        )
    elif action == "album_mode":
        order = ["animate", "separate", "zip"]
        index = (
            order.index(settings.album_mode)
            if settings.album_mode in order
            else 0
        )
        settings.album_mode = order[(index + 1) % len(order)]
    elif action == "motion":
        order = ["normal", "reverse", "boomerang"]
        index = order.index(settings.motion) if settings.motion in order else 0
        settings.motion = order[(index + 1) % len(order)]
    elif action == "long_video":
        settings.long_video = (
            "speedup" if settings.long_video == "trim" else "trim"
        )
    elif action == "image_filter":
        index = (
            IMAGE_FILTERS.index(settings.image_filter)
            if settings.image_filter in IMAGE_FILTERS
            else 0
        )
        settings.image_filter = IMAGE_FILTERS[(index + 1) % len(IMAGE_FILTERS)]
    elif action == "outline":
        index = (
            OUTLINE_MODES.index(settings.outline)
            if settings.outline in OUTLINE_MODES
            else 0
        )
        settings.outline = OUTLINE_MODES[(index + 1) % len(OUTLINE_MODES)]
    elif action == "cut_bg":
        settings.cut_bg = "on" if settings.cut_bg == "off" else "off"
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

    await track_request(message, "sticker")
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
    user_id = message.from_user.id if message.from_user else 0
    settings = get_settings(user_id)

    if message.media_group_id and settings.album_mode in ("animate", "zip"):
        # Part of an album: collect the frames and handle them together.
        await buffer_album_photo(message, bot, photo.file_id)
        return

    stats.inc("photo_in")
    await track_request(message, "photo")
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
    await track_request(message, "animation")
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
    await track_request(message, "video")
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
    await track_request(message, "document")
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

    global app_config, semaphore, stats
    app_config = config
    semaphore = asyncio.Semaphore(config.concurrency)

    stats_path = Path(config.stats_file) if config.stats_file else None
    stats = load_stats(stats_path)
    if stats_path is not None:
        logger.info("Stats persistence enabled at %s", stats_path)

    bot = Bot(token=config.token)
    dp = Dispatcher()

    dp.message.register(handle_start, CommandStart())
    dp.message.register(handle_help, Command("help"))
    dp.message.register(handle_settings, Command("settings"))
    dp.message.register(handle_trim, Command("trim"))
    dp.message.register(handle_text, Command("text"))
    dp.message.register(handle_sheet, Command("sheet"))
    dp.message.register(handle_me, Command("me"))
    dp.message.register(handle_top, Command("top"))
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
