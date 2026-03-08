import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
from typing import Dict, List, Optional, Tuple, Union

from astrbot.api import logger
from curl_cffi import CurlMime, requests


ChatId = Union[int, str]


class TelegramSender:
    def __init__(
        self,
        bot_token: str = "",
        proxy: str = "",
        timeout_secs: int = 30,
        api_base: str = "https://api.telegram.org",
    ):
        self.bot_token = (bot_token or "").strip()
        self.proxy = (proxy or "").strip()
        self.timeout_secs = int(timeout_secs)
        self.api_base = api_base.rstrip("/")

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token)

    def parse_chat_id_from_sub_user(self, sub_user: str) -> Optional[ChatId]:
        raw = (sub_user or "").strip()
        if not raw:
            return None

        if re.fullmatch(r"-?\d+", raw):
            return int(raw)
        if raw.startswith("@"):
            return raw

        parts = [p for p in raw.split(":") if p]
        if len(parts) >= 3 and parts[1] in ("FriendMessage", "GroupMessage"):
            last = parts[-1]
            if re.fullmatch(r"-?\d+", last):
                return int(last)

        for part in reversed(parts):
            if re.fullmatch(r"-?\d+", part):
                return int(part)
            if part.startswith("@"):
                return part

        return None

    def _is_video(self, url: str) -> bool:
        return bool(re.search(r"\.(mp4|mov|m4v|webm)(?:\?|$)", url, re.IGNORECASE))

    def _is_gif(self, url: str) -> bool:
        return bool(re.search(r"\.gif(?:\?|$)", url, re.IGNORECASE))

    def _is_image(self, url: str) -> bool:
        return bool(re.search(r"\.(jpg|jpeg|png|bmp|webp)(?:\?|$)", url, re.IGNORECASE))

    def _request(self, method: str, payload: Dict, files: Optional[Dict] = None) -> Dict:
        url = f"{self.api_base}/bot{self.bot_token}/{method}"
        kwargs = {"timeout": self.timeout_secs}
        if self.proxy:
            kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}

        # Keep only this telemetry info line for media-group upload requests.
        if method == "sendMediaGroup" and bool(files):
            logger.info(
                f"[tg_sender] request method=sendMediaGroup via_proxy={bool(self.proxy)} files=True"
            )

        if files:
            mime = CurlMime()
            for key, val in payload.items():
                mime.addpart(name=str(key), data=str(val))
            for key, file_tuple in files.items():
                filename, content, content_type = file_tuple
                mime.addpart(
                    name=str(key),
                    filename=str(filename),
                    content_type=str(content_type),
                    data=content,
                )
            resp = requests.post(url, multipart=mime, **kwargs)
        else:
            resp = requests.post(url, json=payload, **kwargs)

        raw_text = getattr(resp, "text", "")
        try:
            data = resp.json()
        except Exception:
            data = {}

        if resp.status_code >= 400:
            raise RuntimeError(
                f"Telegram HTTP {resp.status_code}, method={method}, body={raw_text}"
            )
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: method={method}, data={data}")
        return data

    def _download_media_bytes(self, media_url: str) -> Tuple[str, bytes, str]:
        kwargs = {"timeout": self.timeout_secs}
        if self.proxy:
            kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}

        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.bilibili.com/",
        }
        resp = requests.get(media_url, headers=headers, **kwargs)
        resp.raise_for_status()

        content = resp.content or b""
        content_type = (
            (resp.headers.get("content-type") or "application/octet-stream")
            .split(";")[0]
            .strip()
        )

        ext = "bin"
        if self._is_gif(media_url):
            ext = "gif"
        elif self._is_video(media_url):
            ext = "mp4"
        elif self._is_image(media_url):
            ext = "jpg"

        filename = f"tg_media.{ext}"
        return filename, content, content_type

    def _resolve_ffmpeg_path(self) -> Optional[str]:
        base_dir = os.path.dirname(__file__)
        bundled_candidates = [
            os.path.join(base_dir, "ffmpeg", "ffmpeg"),
            os.path.join(base_dir, "ffmpeg", "ffmpeg.exe"),
        ]
        for candidate in bundled_candidates:
            if os.path.exists(candidate):
                return candidate
        return shutil.which("ffmpeg")

    def _gif_to_mp4(self, gif_bytes: bytes) -> Optional[bytes]:
        ffmpeg = self._resolve_ffmpeg_path()
        if not ffmpeg:
            return None

        with tempfile.TemporaryDirectory(prefix="tg_gif2mp4_") as tmpdir:
            in_path = os.path.join(tmpdir, "in.gif")
            out_path = os.path.join(tmpdir, "out.mp4")
            with open(in_path, "wb") as f:
                f.write(gif_bytes)

            cmd = [
                ffmpeg,
                "-y",
                "-i",
                in_path,
                "-movflags",
                "+faststart",
                "-pix_fmt",
                "yuv420p",
                "-vf",
                "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                out_path,
            ]
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if proc.returncode != 0 or (not os.path.exists(out_path)):
                logger.warning(
                    f"[tg_sender] gif->mp4 failed rc={proc.returncode}, stderr={proc.stderr.decode('utf-8', errors='ignore')[:200]}"
                )
                return None

            with open(out_path, "rb") as f:
                return f.read()

    def _send_single(self, chat_id: ChatId, caption: str, media_url: str) -> None:
        if self._is_gif(media_url):
            self._request(
                "sendAnimation",
                {
                    "chat_id": chat_id,
                    "animation": media_url,
                    "caption": caption or "",
                    "parse_mode": "HTML",
                },
            )
            return

        if self._is_video(media_url):
            self._request(
                "sendVideo",
                {
                    "chat_id": chat_id,
                    "video": media_url,
                    "caption": caption or "",
                    "parse_mode": "HTML",
                },
            )
            return

        if self._is_image(media_url):
            self._request(
                "sendPhoto",
                {
                    "chat_id": chat_id,
                    "photo": media_url,
                    "caption": caption or "",
                    "parse_mode": "HTML",
                },
            )
            return

        self._request(
            "sendDocument",
            {
                "chat_id": chat_id,
                "document": media_url,
                "caption": caption or "",
                "parse_mode": "HTML",
                "disable_content_type_detection": True,
            },
        )

    def _send_media_group_uploaded(self, chat_id: ChatId, caption: str, media_urls: List[str]) -> None:
        media_items: List[Dict] = []
        files: Dict[str, Tuple[str, bytes, str]] = {}

        for idx, media_url in enumerate(media_urls[:10]):
            filename, content, content_type = self._download_media_bytes(media_url)

            media_type = "document"
            if self._is_video(media_url) or content_type.startswith("video/"):
                media_type = "video"
                if not filename.lower().endswith(".mp4"):
                    filename = "tg_media.mp4"
            elif self._is_gif(media_url) or content_type == "image/gif":
                mp4 = self._gif_to_mp4(content)
                if mp4:
                    content = mp4
                    content_type = "video/mp4"
                    filename = "tg_media.mp4"
                    media_type = "video"
                else:
                    media_type = "document"
            elif self._is_image(media_url) or content_type.startswith("image/"):
                media_type = "photo"
                if not re.search(r"\.(jpg|jpeg|png|webp)$", filename, re.IGNORECASE):
                    filename = "tg_media.jpg"
                    content_type = "image/jpeg"

            file_key = f"file{idx}"
            files[file_key] = (filename, content, content_type)
            item = {"type": media_type, "media": f"attach://{file_key}"}
            if idx == 0 and caption:
                item["caption"] = caption
                item["parse_mode"] = "HTML"
            media_items.append(item)

        types = [x.get("type", "") for x in media_items]
        if "document" in types and any(t != "document" for t in types):
            for item in media_items:
                item["type"] = "document"

        payload = {
            "chat_id": str(chat_id),
            "media": json.dumps(media_items, ensure_ascii=False),
        }
        self._request("sendMediaGroup", payload, files=files)

    def send_bundle_sync(self, chat_id: ChatId, caption: str, media_urls: List[str]) -> bool:
        caption = (caption or "").strip()
        media_urls = [u.strip() for u in media_urls if (u or "").strip()]

        if not media_urls:
            self._request(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": caption or " ",
                    "disable_web_page_preview": True,
                },
            )
            return True

        if len(media_urls) == 1:
            self._send_single(chat_id, caption, media_urls[0])
            return True

        self._send_media_group_uploaded(chat_id, caption, media_urls)
        return True

    async def send_bundle(self, chat_id: ChatId, caption: str, media_urls: List[str]) -> bool:
        try:
            return await asyncio.to_thread(
                self.send_bundle_sync, chat_id, caption, media_urls
            )
        except Exception as e:
            logger.warning(f"TelegramSender send failed: {e}")
            return False
