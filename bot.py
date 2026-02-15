import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message
from aiogram.types.input_file import FSInputFile
from dotenv import load_dotenv
from PIL import Image

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


def load_config() -> Config:
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is not set")
    return Config(token=token)


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
    result = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0


def convert_webm_to_video_emoji(
    input_path: Path,
    output_path: Path,
    size_limit_bytes: int = 256 * 1024,
) -> bool:
    if not command_exists("ffmpeg"):
        return False

    # Telegram video emoji requirements:
    # - webm (VP9), no audio, 100x100, <=3s, <=30fps, <=256KB
    attempts = [
        {"crf": "32", "speed": "4", "fps": "30"},
        {"crf": "36", "speed": "4", "fps": "30"},
        {"crf": "40", "speed": "6", "fps": "30"},
        {"crf": "40", "speed": "6", "fps": "24"},
    ]

    for attempt in attempts:
        ok = run_cmd(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(input_path),
                "-t",
                "3",
                "-vf",
                (
                    "scale=100:100:flags=lanczos:force_original_aspect_ratio=decrease,"
                    "pad=100:100:(ow-iw)/2:(oh-ih)/2:color=black,"
                    f"fps={attempt['fps']}"
                ),
                "-an",
                "-c:v",
                "libvpx-vp9",
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


def convert_video_to_video_emoji(
    input_path: Path,
    output_path: Path,
    size_limit_bytes: int = 256 * 1024,
) -> bool:
    if not command_exists("ffmpeg"):
        return False

    attempts = [
        {"crf": "32", "speed": "4", "fps": "30"},
        {"crf": "36", "speed": "4", "fps": "30"},
        {"crf": "40", "speed": "6", "fps": "30"},
        {"crf": "40", "speed": "6", "fps": "24"},
    ]

    for attempt in attempts:
        ok = run_cmd(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(input_path),
                "-t",
                "3",
                "-vf",
                (
                    "scale=100:100:flags=lanczos:force_original_aspect_ratio=decrease,"
                    "pad=100:100:(ow-iw)/2:(oh-ih)/2:color=black,"
                    f"fps={attempt['fps']}"
                ),
                "-an",
                "-c:v",
                "libvpx-vp9",
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


def convert_image_to_png_emoji(input_path: Path, output_path: Path) -> bool:
    try:
        with Image.open(input_path) as img:
            img = img.convert("RGBA")
            img.thumbnail((100, 100), Image.LANCZOS)
            canvas = Image.new("RGBA", (100, 100), (0, 0, 0, 255))
            x = (100 - img.width) // 2
            y = (100 - img.height) // 2
            canvas.paste(img, (x, y), img)
            canvas.save(output_path, format="PNG")
        return True
    except Exception:
        return False


def convert_webm_to_mp4(input_path: Path, output_path: Path) -> bool:
    if not command_exists("ffmpeg"):
        return False
    return run_cmd(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-movflags",
            "+faststart",
            "-pix_fmt",
            "yuv420p",
            str(output_path),
        ]
    )


def convert_webm_to_gif(input_path: Path, output_path: Path) -> bool:
    if not command_exists("ffmpeg"):
        return False
    return run_cmd(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-vf",
            "fps=15,scale=256:-1:flags=lanczos",
            str(output_path),
        ]
    )


async def handle_start(message: Message) -> None:
    await message.answer(
        "Supported formats: sticker, animated sticker, image, GIF, video."
    )


async def handle_unsupported(message: Message) -> None:
    await message.answer(
        "Supported formats: sticker, animated sticker, image, GIF, video."
    )


async def handle_sticker(message: Message, bot: Bot) -> None:
    sticker = message.sticker
    if not sticker:
        return

    if sticker.is_video:
        file = await bot.get_file(sticker.file_id)
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            input_webm = tmpdir_path / "input.webm"
            output_webm = tmpdir_path / "emoji.webm"
            output_mp4 = tmpdir_path / "sticker.mp4"
            output_gif = tmpdir_path / "sticker.gif"

            await bot.download_file(file.file_path, destination=input_webm)

            webm_ok = convert_webm_to_video_emoji(input_webm, output_webm)
            if webm_ok:
                await message.answer_document(
                    FSInputFile(str(output_webm)),
                    caption=(
                        "Done. .webm file for video emoji."
                    ),
                )
            else:
                mp4_ok = convert_webm_to_mp4(input_webm, output_mp4)
                if mp4_ok:
                    await message.answer_document(
                        FSInputFile(str(output_mp4)),
                        caption="Could not meet video emoji limits. .mp4 file.",
                    )
                    return

                gif_ok = convert_webm_to_gif(input_webm, output_gif)
                if gif_ok:
                    await message.answer_document(
                        FSInputFile(str(output_gif)),
                        caption="Could not meet video emoji limits. .gif file.",
                    )
                    return

                await message.answer(
                    "Could not process video. Make sure `ffmpeg` is installed."
                )
        return

    if not sticker.is_animated:
        file = await bot.get_file(sticker.file_id)
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            input_webp = tmpdir_path / "input.webp"
            output_png = tmpdir_path / "emoji.png"

            await bot.download_file(file.file_path, destination=input_webp)
            ok = convert_image_to_png_emoji(input_webp, output_png)
            if ok:
                await message.answer_document(
                    FSInputFile(str(output_png)),
                    caption="Done. 100x100 .png file for static emoji.",
                )
                return

        await message.answer("Could not process this sticker.")
        return

    file = await bot.get_file(sticker.file_id)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        input_path = tmpdir_path / "input.tgs"
        output_path = tmpdir_path / "emoji.tgs"

        await bot.download_file(file.file_path, destination=input_path)
        used_lottie = convert_tgs_to_emoji(input_path, output_path)

        caption = (
            "Done. .tgs file for an emoji set."
            if used_lottie
            else "Done. Original .tgs (lottie module is not installed)."
        )

        await message.answer_document(FSInputFile(str(output_path)), caption=caption)


async def handle_photo(message: Message, bot: Bot) -> None:
    photo = message.photo[-1] if message.photo else None
    if not photo:
        return
    file = await bot.get_file(photo.file_id)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        input_img = tmpdir_path / "input"
        output_png = tmpdir_path / "emoji.png"

        await bot.download_file(file.file_path, destination=input_img)
        ok = convert_image_to_png_emoji(input_img, output_png)
        if ok:
            await message.answer_document(
                FSInputFile(str(output_png)),
                caption="Done. 100x100 .png file for static emoji.",
            )
            return

    await message.answer("Could not process the image.")


async def handle_animation(message: Message, bot: Bot) -> None:
    animation = message.animation
    if not animation:
        return
    file = await bot.get_file(animation.file_id)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        input_anim = tmpdir_path / "input.anim"
        output_webm = tmpdir_path / "emoji.webm"
        output_mp4 = tmpdir_path / "sticker.mp4"
        output_gif = tmpdir_path / "sticker.gif"

        await bot.download_file(file.file_path, destination=input_anim)
        webm_ok = convert_video_to_video_emoji(input_anim, output_webm)
        if webm_ok:
            await message.answer_document(
                FSInputFile(str(output_webm)),
                caption="Done. .webm file for video emoji.",
            )
            return

        mp4_ok = convert_webm_to_mp4(input_anim, output_mp4)
        if mp4_ok:
            await message.answer_document(
                FSInputFile(str(output_mp4)),
                caption="Could not meet video emoji limits. .mp4 file.",
            )
            return

        gif_ok = convert_webm_to_gif(input_anim, output_gif)
        if gif_ok:
            await message.answer_document(
                FSInputFile(str(output_gif)),
                caption="Could not meet video emoji limits. .gif file.",
            )
            return

    await message.answer("Could not process the animation.")


async def handle_video(message: Message, bot: Bot) -> None:
    video = message.video
    if not video:
        return
    file = await bot.get_file(video.file_id)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        input_video = tmpdir_path / "input.video"
        output_webm = tmpdir_path / "emoji.webm"
        output_mp4 = tmpdir_path / "sticker.mp4"
        output_gif = tmpdir_path / "sticker.gif"

        await bot.download_file(file.file_path, destination=input_video)
        webm_ok = convert_video_to_video_emoji(input_video, output_webm)
        if webm_ok:
            await message.answer_document(
                FSInputFile(str(output_webm)),
                caption="Done. .webm file for video emoji.",
            )
            return

        mp4_ok = convert_webm_to_mp4(input_video, output_mp4)
        if mp4_ok:
            await message.answer_document(
                FSInputFile(str(output_mp4)),
                caption="Could not meet video emoji limits. .mp4 file.",
            )
            return

        gif_ok = convert_webm_to_gif(input_video, output_gif)
        if gif_ok:
            await message.answer_document(
                FSInputFile(str(output_gif)),
                caption="Could not meet video emoji limits. .gif file.",
            )
            return

    await message.answer("Could not process the video.")


async def handle_document(message: Message, bot: Bot) -> None:
    doc = message.document
    if not doc:
        return
    mime = (doc.mime_type or "").lower()
    file = await bot.get_file(doc.file_id)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        input_doc = tmpdir_path / "input.doc"
        await bot.download_file(file.file_path, destination=input_doc)

        if mime.startswith("image/"):
            output_png = tmpdir_path / "emoji.png"
            ok = convert_image_to_png_emoji(input_doc, output_png)
            if ok:
                await message.answer_document(
                    FSInputFile(str(output_png)),
                    caption="Done. 100x100 .png file for static emoji.",
                )
                return
        elif mime.startswith("video/"):
            output_webm = tmpdir_path / "emoji.webm"
            output_mp4 = tmpdir_path / "sticker.mp4"
            output_gif = tmpdir_path / "sticker.gif"

            webm_ok = convert_video_to_video_emoji(input_doc, output_webm)
            if webm_ok:
                await message.answer_document(
                    FSInputFile(str(output_webm)),
                    caption="Done. .webm file for video emoji.",
                )
                return

            mp4_ok = convert_webm_to_mp4(input_doc, output_mp4)
            if mp4_ok:
                await message.answer_document(
                    FSInputFile(str(output_mp4)),
                    caption="Could not meet video emoji limits. .mp4 file.",
                )
                return

            gif_ok = convert_webm_to_gif(input_doc, output_gif)
            if gif_ok:
                await message.answer_document(
                    FSInputFile(str(output_gif)),
                    caption="Could not meet video emoji limits. .gif file.",
                )
                return

    await message.answer(
        "Supported formats: sticker, animated sticker, image, GIF, video."
    )


async def main() -> None:
    print("Starting bot...")
    logging.basicConfig(level=logging.INFO)
    config = load_config()

    bot = Bot(token=config.token)
    dp = Dispatcher()

    dp.message.register(handle_start, CommandStart())
    dp.message.register(handle_sticker, F.sticker)
    dp.message.register(handle_photo, F.photo)
    dp.message.register(handle_animation, F.animation)
    dp.message.register(handle_video, F.video)
    dp.message.register(handle_document, F.document)
    dp.message.register(handle_unsupported)

    await dp.start_polling(bot)
    print("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
