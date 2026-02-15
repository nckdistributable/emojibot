# EmojiBot

This bot accepts stickers, images, and videos and returns files suitable for creating Telegram emoji.

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

## Usage

1. Open the bot chat and send one of the supported formats:
   - static sticker
   - animated sticker (`.tgs`)
   - video sticker (`.webm`)
   - image (photo or `image/*` document)
   - GIF (as a file or animation)
   - video (as a file or video message)
2. The bot returns a ready file:
   - `.tgs` for animated stickers
   - `.webm` (VP9, 100x100, <=3s, <=30fps, <=256KB) for video emoji
   - `.png` 100x100 for static emoji
   - if video emoji limits are not met — `.mp4` or `.gif`
3. Add the file to an emoji set via @Stickers.

## Notes

- Supported: animated `.tgs` stickers, `.webm` video stickers, static stickers, images, and videos.
- `lottie` is required to scale `.tgs` correctly.
- `ffmpeg` is required for video emoji (`.webm` VP9, 100x100, <=3s, <=30fps, <=256KB).
- If video emoji limits are not met, the bot returns `.mp4` or `.gif` as a fallback.
