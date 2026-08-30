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
   - video (as a file, video message, or round video note)
   - a public http(s) link to an image, GIF, or video
2. The bot returns a ready file:
   - `.tgs` is sent as `emoji.tgs.zip` so Telegram keeps it downloadable. Unzip before uploading to @Stickers.
   - `.webm` (VP9, 100x100, <=3s, <=30fps, <=256KB) for video emoji
   - `.png` 100x100 for static emoji
   - if video emoji limits are not met — `.mp4` or `.gif`
3. Add the file to an emoji set via @Stickers.

### Presets

There are a lot of settings now, so they can be saved as named recipes:

```
/preset list              built-in ones and yours
/preset use retro         apply it
/preset save neon         remember the current look
/preset share neon        get a code to pass around
/preset import <code>     apply someone else's code
/preset delete neon
```

Built-in: `sticker`, `video`, `boomerang`, `retro`, `noir`.

A preset carries the **look** — fit, background, filter, outline, motion,
output, fps, duration — and deliberately not the caption, trim or sheet
grid, which belong to the job you set them for. Imported codes are filtered
down to known style fields, so a code can only change the look.

Presets are saved to `PRESETS_FILE` and survive restarts.

### Tweaking the result without re-uploading

Every converted file comes back with buttons under it — background, fit,
output, filter, outline, and 🔁 Again. Pressing one re-renders **the same
source** with the new setting, so you can dial an emoji in without sending
the file again. Video sources get a motion button instead of output.

The source is kept for 30 minutes and then discarded; after that the bot
asks for the file again.

### Building a real emoji pack

The bot can assemble the pack itself, so you never have to visit @Stickers:

```
/pack new Halloween     start collecting
  ... send images, videos, albums, sheets ...
/pack emoji 🔥          tag the next additions with this emoji
/pack status            what is in it so far
/pack finish            close it and get the link
```

While a pack is open, every emoji the bot produces is appended to a real
Telegram custom emoji set created in your name, and you get a
`t.me/addemoji/...` link you can share. `.png`, `.webm` and `.tgs` results are
added; `.mp4`/`.gif` fallbacks and archives are skipped, since Telegram does
not accept them as emoji.

`/pack cancel` stops adding but keeps whatever was already put in the set.

### Sending a link

Paste a public `http(s)` link to an image, GIF, or video and the bot fetches
it and converts it like an uploaded file. Only public addresses are accepted:
links resolving to loopback, private, link-local or otherwise reserved ranges
are refused, redirects are re-checked at every hop, and the download stops at
`MAX_FILE_MB`.

### Inline mode

With inline mode enabled for the bot in @BotFather (`/setinline`), typing
`@yourbot` in any chat lists the last 10 emoji you made so you can send one
straight into the conversation. The list lives in memory and is per user.

### Sprite sheets and whole packs

- **`/sheet 4x3`** cuts one grid image into cells (row by row) and animates
  them into a single emoji — handy for sprite sheets exported from an
  animation tool. Up to 8x8, 36 cells. `/sheet off` disables it.
- **`Album: zip`** (`/settings`) turns an album into a finished set: every
  photo becomes its own emoji and they all come back as one `emoji_pack.zip`
  with a short README inside. The archive holds `.png` emoji, or `.webm`
  video emoji when `Image output` is `video`.

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
- `/sheet` - slice a grid image into animation frames (`/sheet 4x3`, `/sheet off`).
- `/pack` - build a real Telegram emoji pack (`/pack new <title>`, `/pack finish`).
- `/preset` - save and reuse a look (`/preset use retro`, `/preset save neon`, `/preset share neon`).
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

Per-user settings are available via `/settings`. They are saved to
`SETTINGS_FILE` and restored on startup, so a restart does not reset anyone's
configuration.


- Video FPS: 24 or 30
- Video duration: 1, 2, or 3 seconds
- Video fit: `pad` or `crop`
- Video background: `black` or `transparent`
- Image fit: `pad` or `crop`
- Image background: `black` or `transparent`
- Image output: `static` (100x100 `.png`) or `video` (looping `.webm` video emoji built from the image)
- Album: `animate` (several photos become one animated emoji), `separate` (one emoji per photo), or `zip` (the whole set as one archive)
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
- `SETTINGS_FILE` (default: `settings.json`) - path to the JSON file where per-user settings are stored. Empty disables saving them (in-memory only).
- `PRESETS_FILE` (default: `presets.json`) - path to the JSON file where user presets are stored. Empty disables saving them.
- `STATS_FILE` (default: `stats.json`) - path to the JSON file where usage stats are persisted. Set to an empty value to disable persistence (in-memory only).

## Notes

- Supported: animated `.tgs` stickers, `.webm` video stickers, static stickers, images, GIFs, videos, round video notes, and public image/video links.
- `lottie` is required to scale `.tgs` correctly.
- `ffmpeg` is required for video emoji (`.webm` VP9, 100x100, <=3s, <=30fps, <=256KB).
- `Pillow` is required for images and static stickers. For WebP on macOS, install `webp` via Homebrew and reinstall Pillow.
- If video emoji limits are not met, the bot returns `.mp4` or `.gif` as a fallback.
- Per-user settings and presets are persisted next to the stats, under `SETTINGS_FILE` and `PRESETS_FILE`. Settings are flushed on a timer and only when something actually changed, so button taps do not each cause a write.
- Usage stats (per-user request counts and aggregate counters) are persisted to `STATS_FILE` (default `stats.json`) and reloaded on startup, so they survive restarts. In Docker, mount a volume for this file to keep stats across container recreation.
