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
   - several photos in one album (animated into a single emoji)
   - GIF (as a file or animation)
   - video (as a file or video message)
2. The bot returns a ready file:
   - `.tgs` is sent as `emoji.tgs.zip` so Telegram keeps it downloadable. Unzip before uploading to @Stickers.
   - `.webm` (VP9, 100x100, <=3s, <=30fps, <=256KB) for video emoji
   - `.png` 100x100 for static emoji
   - if video emoji limits are not met — `.mp4` or `.gif`
3. Add the file to an emoji set via @Stickers.

### Image effects

Applied to images, static stickers and photo-series frames:

- **Filter** (`/settings`): `none`, `bw`, `invert`, `sepia`, `pixel`. Videos and
  GIFs get the same look through the matching ffmpeg filters.
- **Outline** (`/settings`): `white` or `black` ring around the visible pixels,
  so the emoji stays readable on any chat background.
- **Cut BG** (`/settings`): clears the background by flood-filling from the
  corners. Works on solid or near-solid backgrounds; it is a color match, not
  a cutout model.
- **`/text BOO!`** draws a caption at the bottom (white with a black stroke,
  auto-shrunk to fit). `/text off` clears it.

### Motion, speed and trimming

Video, GIF and photo-series emoji can be reshaped before encoding:

- **Motion** (`/settings`): `normal`, `reverse` (play backwards), or
  `boomerang` (forward then backwards; the source window is halved so the
  result still fits the duration limit).
- **Long video** (`/settings`): `trim` cuts the clip at `Video duration`,
  `speedup` squeezes the whole clip into it instead of cutting.
- **`/trim`** picks which part of the clip to use:
  - `/trim 2-5` - use seconds 2 to 5
  - `/trim 2` - start at 2s
  - `/trim off` - back to the start of the clip

### Animating a series of photos into one emoji

Send several photos **as one album** and the bot stitches them into a single
animated emoji instead of returning one file per photo: every frame is
normalized to 100x100 (using `Image fit` / `Image background`) and the frames
are spread evenly over `Video duration` seconds, encoded as a looping VP9
`.webm` (`<=256KB`), with a `.gif` fallback if the limits cannot be met.
Up to 10 frames are used — the size of one Telegram album.

Set `Album` to `separate` in `/settings` to get the old behaviour back (one
emoji per photo).

### Turning static images into video emoji

By default a static image (photo, `image/*` document, or static sticker) is
returned as a `.png` static emoji. To upload images into a **video** emoji set
instead, set `Image output` to `video` in `/settings`. The bot then renders the
image into a looping `.webm` video emoji (VP9, 100x100, `Video duration`
seconds long, `<=256KB`) that you can add to a video emoji pack via @Stickers.
`Image fit` and `Image background` still control framing; `Video duration` and
`FPS` control the clip length and frame rate. If the image cannot be fit under
the video-emoji limits, the bot falls back to a `.png` static emoji.

## Commands

- `/start` - show supported formats.
- `/help` - short usage info and a link to @Stickers.
- `/settings` - adjust fit, background, FPS, duration, motion, and album handling.
- `/trim` - pick which part of a clip to use (`/trim 2-5`, `/trim 2`, `/trim off`).
- `/text` - draw a caption on the emoji (`/text BOO!`, `/text off`).
- `/me` - your personal record: how many emoji you made, your rank, how long you have been using the bot, your favorite format, active days, current streak, and unlocked achievements.
- `/top` - leaderboard of the most active users, with your own position marked.
- `/health` - simple health check.
- `/stats` - usage stats, including uptime (as `2d 7h 33m`), unique user and total request counts (admin only).
- `/users` - per-user breakdown of who sent requests, how many times, and when they were last seen, ranked by request count (admin only).

## Achievements

Every user collects badges automatically, visible in `/me`:

| Badge | Unlocked by |
|---|---|
| 🎬 Debut | first emoji |
| 🔟 Regular | 10 emoji |
| 💯 Centurion | 100 emoji |
| 👑 Emoji royalty | 500 emoji |
| 🎭 Versatile | using every input format (sticker, photo, GIF, video, file) |
| 🎞 Animator | building an emoji from a photo series |
| 🦇 Night owl | 5 requests after midnight (server time) |
| 🔥 On fire | 3+ days in a row |
| 🧙 Veteran | a month since the first request |

The bot also sends a short congratulation when a user hits a milestone
(1st, 10th, 50th, 100th, 250th, 500th, 1000th emoji).

## Settings

Per-user settings are available via `/settings`:

- Video FPS: 24 or 30
- Video duration: 1, 2, or 3 seconds
- Video fit: `pad` or `crop`
- Video background: `black` or `transparent`
- Image fit: `pad` or `crop`
- Image background: `black` or `transparent`
- Image output: `static` (100x100 `.png`) or `video` (looping `.webm` video emoji built from the image)
- Album: `animate` (several photos become one animated emoji) or `separate` (one emoji per photo)
- Motion: `normal`, `reverse`, or `boomerang`
- Long video: `trim` (cut at the duration limit) or `speedup` (fit the whole clip into it)
- Filter: `none`, `bw`, `invert`, `sepia`, or `pixel`
- Outline: `off`, `white`, or `black`
- Cut BG: `off` or `on` (flood-fill the background away from the corners)

## Environment Variables

- `BOT_TOKEN` (required)
- `LOG_LEVEL` (default: `INFO`)
- `MAX_FILE_MB` (default: `20`)
- `MAX_DURATION_SECONDS` (default: `10`)
- `CONCURRENCY` (default: `2`)
- `ALLOWED_USER_IDS` (comma-separated)
- `ADMIN_USER_IDS` (comma-separated)
- `STATS_FILE` (default: `stats.json`) - path to the JSON file where usage stats are persisted. Set to an empty value to disable persistence (in-memory only).

## Notes

- Supported: animated `.tgs` stickers, `.webm` video stickers, static stickers, images, GIFs, and videos.
- `lottie` is required to scale `.tgs` correctly.
- `ffmpeg` is required for video emoji (`.webm` VP9, 100x100, <=3s, <=30fps, <=256KB).
- `Pillow` is required for images and static stickers. For WebP on macOS, install `webp` via Homebrew and reinstall Pillow.
- If video emoji limits are not met, the bot returns `.mp4` or `.gif` as a fallback.
- Usage stats (per-user request counts and aggregate counters) are persisted to `STATS_FILE` (default `stats.json`) and reloaded on startup, so they survive restarts. In Docker, mount a volume for this file to keep stats across container recreation.
