# Bundled ffmpeg for Telegram GIF->MP4

Put ffmpeg binary in this directory so the plugin can use it inside Docker.

Expected paths (priority order):
1. tools/ffmpeg/ffmpeg
2. tools/ffmpeg/ffmpeg.exe
3. system PATH `ffmpeg`

## Linux Docker
Download a static build and place executable as:
- `tools/ffmpeg/ffmpeg`

Ensure executable permission:
```bash
chmod +x tools/ffmpeg/ffmpeg
```

## Windows
Place binary as:
- `tools/ffmpeg/ffmpeg.exe`

## Verification in logs
When GIF conversion starts, log should show:
- `[tg_sender] using ffmpeg=...`

If not found:
- `[tg_sender] ffmpeg not found (bundled/system), gif stays document`
