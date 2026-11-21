# Upfern

Upfern finds short clips in long videos and gives you a browser editor for framing, captions, timeline edits, covers, and export.

- `app/web/` contains the redesigned website, editor, and public demo assets.
- `app/studio/` contains the FastAPI clipping engine.
- `ShabiClips-Extension/` contains the desktop Chrome Auto-Post extension. The same extension is available from Settings in the running app.

## Run the app

Install Python, FFmpeg and FFprobe on PATH. Install Deno or Node.js if using YouTube ingest. From the `app` folder, install dependencies with `python -m pip install -r requirements.txt`. Use `.env.example` to configure secrets privately, then run `python -m studio.server`. The default local address is http://127.0.0.1:8767.

Highlight selection can use a hosted AI provider. The engine also supports local Parakeet transcription when the workflow needs it. Run `python -m studio.fetch_model` from `app` to install those weights. The cover cutout model is fetched by the engine when needed. Gameplay backgrounds can be added separately to `app/data/gameplay/`. The starter sound pack is fetched by the app.

## Deployment

Upload the extracted contents to a private GitHub repository. This source package includes the web interface and engine, plus demo assets. Install runtime tools and model weights on the backend host. Use persistent data storage and enable account authentication with `PEAKCUT_ACCOUNTS=1` before making an instance public. A Vercel frontend requires a reachable HTTPS backend for sign-in, clip processing, media and exports.

The desktop Auto-Post extension uses the local Upfern app. Supported mobile browsers can share rendered MP4 files through the system share sheet.
