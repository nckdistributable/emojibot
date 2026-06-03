# EmojiBot

This bot accepts stickers, images, GIFs, and videos and returns files suitable for creating Telegram emoji.

## Run

1. Create a bot via @BotFather and get a token.
2. Copy `.env.example` to `.env` and set the token.
3. Install dependencies.
4. Start the bot.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

## Docker

1. Create `.env` with `BOT_TOKEN`.
2. Build the image and run the container:

```bash
docker build -t emojibot .
docker run --env-file .env --name emojibot --rm emojibot
```

## Docker Compose

```bash
docker compose up --build
```

## Usage

1. Open the bot chat and send one of the supported formats:
   - static sticker
   - animated sticker (`.tgs`)
   - video sticker (`.webm`)
   - image (photo or `image/*` document)
   - GIF (as a file or animation)
   - video (as a file or video message)
2. The bot returns a ready file:
   - `.tgs` is sent as `emoji.tgs.zip` so Telegram keeps it downloadable. Unzip before uploading to @Stickers.
   - `.webm` (VP9, 100x100, <=3s, <=30fps, <=256KB) for video emoji
   - `.png` 100x100 for static emoji
   - if video emoji limits are not met — `.mp4` or `.gif`
3. Add the file to an emoji set via @Stickers.

## Commands

- `/start` - show supported formats.
- `/help` - short usage info and a link to @Stickers.
- `/settings` - adjust fit, background, FPS, and duration.
- `/health` - simple health check.
- `/stats` - usage stats, including unique user and total request counts (admin only).
- `/users` - per-user breakdown of who sent requests and how many times, ranked by request count (admin only).

## Settings

Per-user settings are available via `/settings`:

- Video FPS: 24 or 30
- Video duration: 1, 2, or 3 seconds
- Video fit: `pad` or `crop`
- Video background: `black` or `transparent`
- Image fit: `pad` or `crop`
- Image background: `black` or `transparent`

## Environment Variables

- `BOT_TOKEN` (required)
- `LOG_LEVEL` (default: `INFO`)
- `MAX_FILE_MB` (default: `20`)
- `MAX_DURATION_SECONDS` (default: `10`)
- `CONCURRENCY` (default: `2`)
- `ALLOWED_USER_IDS` (comma-separated)
- `ADMIN_USER_IDS` (comma-separated)

## Notes

- Supported: animated `.tgs` stickers, `.webm` video stickers, static stickers, images, GIFs, and videos.
- `lottie` is required to scale `.tgs` correctly.
- `ffmpeg` is required for video emoji (`.webm` VP9, 100x100, <=3s, <=30fps, <=256KB).
- `Pillow` is required for images and static stickers. For WebP on macOS, install `webp` via Homebrew and reinstall Pillow.
- If video emoji limits are not met, the bot returns `.mp4` or `.gif` as a fallback.
