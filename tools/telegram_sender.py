import asyncio
import json
import re
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
        logger.info(f"[tg_sender] parse sub_user={raw}")
        if not raw:
            logger.info("[tg_sender] sub_user empty, skip")
            return None

        if re.fullmatch(r"-?\d+", raw):
            chat_id = int(raw)
            logger.info(f"[tg_sender] parsed direct chat_id={chat_id}")
            return chat_id
        if raw.startswith("@"):
            logger.info(f"[tg_sender] parsed direct username={raw}")
            return raw

        parts = [p for p in raw.split(":") if p]
        if len(parts) >= 3 and parts[1] in ("FriendMessage", "GroupMessage"):
            last = parts[-1]
            if re.fullmatch(r"-?\d+", last):
                chat_id = int(last)
                logger.info(f"[tg_sender] parsed UMO chat_id={chat_id}")
                return chat_id

        for part in reversed(parts):
            if re.fullmatch(r"-?\d+", part):
                chat_id = int(part)
                logger.info(f"[tg_sender] parsed segmented chat_id={chat_id}")
                return chat_id
            if part.startswith("@"):
                logger.info(f"[tg_sender] parsed segmented username={part}")
                return part

        logger.info("[tg_sender] failed to parse chat_id")
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

        logger.info(
            f"[tg_sender] request method={method} via_proxy={bool(self.proxy)} files={bool(files)}"
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
        content_type = (resp.headers.get("content-type") or "application/octet-stream").split(";")[0].strip()

        ext = "bin"
        if self._is_gif(media_url):
            ext = "gif"
        elif self._is_video(media_url):
            ext = "mp4"
        elif self._is_image(media_url):
            ext = "jpg"

        filename = f"tg_media.{ext}"
        return filename, content, content_type

    def _send_single(self, chat_id: ChatId, caption: str, media_url: str) -> None:
        if self._is_gif(media_url):
            logger.info("[tg_sender] single media -> sendAnimation")
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
            logger.info("[tg_sender] single media -> sendVideo")
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
            logger.info("[tg_sender] single media -> sendPhoto")
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

        logger.info("[tg_sender] single media -> sendDocument")
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

    def _build_media_group_items(self, media_urls: List[str], caption: str) -> List[Dict]:
        items: List[Dict] = []
        for i, media_url in enumerate(media_urls[:10]):
            if self._is_video(media_url):
                media_type = "video"
            elif self._is_gif(media_url):
                media_type = "document"
            elif self._is_image(media_url):
                media_type = "photo"
            else:
                media_type = "document"

            item = {"type": media_type, "media": media_url}
            if i == 0 and caption:
                item["caption"] = caption
                item["parse_mode"] = "HTML"
            items.append(item)

        types = [x.get("type", "") for x in items]
        if "document" in types and any(t != "document" for t in types):
            for item in items:
                item["type"] = "document"

        logger.info(f"[tg_sender] media_group_types={[x.get('type') for x in items]}")
        return items

    def _send_media_group_uploaded(self, chat_id: ChatId, caption: str, media_urls: List[str]) -> None:
        logger.info("[tg_sender] media_group fallback/upload mode")
        media_items = self._build_media_group_items(media_urls, caption)

        files: Dict[str, Tuple[str, bytes, str]] = {}
        for idx, item in enumerate(media_items):
            file_key = f"file{idx}"
            filename, content, content_type = self._download_media_bytes(item["media"])
            files[file_key] = (filename, content, content_type)
            item["media"] = f"attach://{file_key}"

        payload = {
            "chat_id": str(chat_id),
            "media": json.dumps(media_items, ensure_ascii=False),
        }
        self._request("sendMediaGroup", payload, files=files)

    def send_bundle_sync(self, chat_id: ChatId, caption: str, media_urls: List[str]) -> bool:
        caption = (caption or "").strip()
        media_urls = [u.strip() for u in media_urls if (u or "").strip()]
        logger.info(f"[tg_sender] send_bundle chat_id={chat_id} media_count={len(media_urls)}")

        if not media_urls:
            logger.info("[tg_sender] no media -> sendMessage")
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

        logger.info("[tg_sender] multi media -> sendMediaGroup")
        media_group = self._build_media_group_items(media_urls, caption)
        try:
            self._request("sendMediaGroup", {"chat_id": chat_id, "media": media_group})
            return True
        except Exception as e:
            logger.warning(f"[tg_sender] sendMediaGroup by url failed, fallback upload: {e}")

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

