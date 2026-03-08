import asyncio
import os
from typing import Any, Dict

from astrbot.api import logger
from astrbot.api.all import Star

from .constant import (
    BANNER_PATH,
    CARD_TEMPLATES,
    DEFAULT_TEMPLATE,
    LOGO_PATH,
    MAX_ATTEMPTS,
    RETRY_DELAY,
    get_template_path,
)
from .utils import *


def load_template(style: str) -> str:
    """加载指定样式的模板内容"""
    path = get_template_path(style)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class Renderer:
    """
    负责将动态数据渲染成图片。
    """

    def __init__(self, star_instance: Star, rai: bool, style: str = DEFAULT_TEMPLATE):
        """
        初始化渲染器。
        """
        self.star = star_instance
        self.rai = rai
        self.style = style
        # 预加载所有模板
        self._templates: Dict[str, str] = {}
        self._load_all_templates()

    def _load_all_templates(self):
        """预加载所有注册的模板"""
        for template_id in CARD_TEMPLATES:
            try:
                self._templates[template_id] = load_template(template_id)
            except Exception as e:
                logger.warning(f"加载模板 {template_id} 失败: {e}")

    def reload_templates(self):
        """重新加载所有模板（用于热更新）"""
        self._templates.clear()
        self._load_all_templates()

    def get_template(self, style: str | None = None) -> str:
        """获取指定样式的模板内容"""
        target_style = style or self.style
        if target_style not in self._templates:
            target_style = DEFAULT_TEMPLATE
        return self._templates.get(target_style, "")

    async def render_dynamic(
        self, render_data: Dict[str, Any], style: str | None = None
    ):
        """
        将渲染数据字典渲染成最终图片。
        这是该类的主要入口方法。
        """
        # options = {"full_page": True, "type": "png", "quality": None, "scale": "device"}
        options = {
            "full_page": True,
            "type": "jpeg",
            "quality": 95,
            "scale": "device",
            "device_scale_factor_level": "ultra",
        }

        tmpl = self.get_template(style)

        for attempt in range(1, MAX_ATTEMPTS + 1):
            render_output = None
            try:
                render_output = await self.star.html_render(
                    tmpl=tmpl,
                    data=render_data,
                    return_url=False,
                    options=options,
                )
                if (
                    render_output
                    and os.path.exists(render_output)
                    and os.path.getsize(render_output) > 4096
                ):
                    return render_output  # 成功，直接返回渲染结果
            except Exception as e:
                logger.error(f"渲染图片失败 (尝试次数: {attempt}): {e}")

            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(RETRY_DELAY)

        return None  # 所有尝试都失败

    async def build_render_data(
        self, item: Dict, is_forward: bool = False
    ) -> Dict[str, Any]:
        """
        根据从B站API获取的单个动态项目，构建用于渲染的字典。
        is_forward: 标记是否正在处理转发动态
        """
        render_data = await create_render_data()
        render_data["banner"] = await image_to_base64(BANNER_PATH)
        # 用户名称、头像、挂件
        author_module = item.get("modules", {}).get("module_author") or {}
        render_data["name"] = author_module.get("name")
        render_data["avatar"] = author_module.get("face")
        render_data["pendant"] = (author_module.get("pendant") or {}).get("image")
        render_data["type"] = item.get("type")

        # 根据不同动态类型填充数据
        if item.get("type") == "DYNAMIC_TYPE_AV":
            # 视频动态
            archive = item["modules"]["module_dynamic"]["major"]["archive"]
            title = archive["title"]
            bv = archive["bvid"]
            cover_url = archive["cover"]

            try:
                content_text = item["modules"]["module_dynamic"]["desc"]["text"]
            except (TypeError, KeyError):
                content_text = None  # 或默认值

            if content_text:
                rich_text = await parse_rich_text(
                    item["modules"]["module_dynamic"]["desc"],
                    item["modules"]["module_dynamic"]["topic"],
                )
                render_data["text"] = f"投稿了新视频<br>{rich_text}"
            else:
                render_data["text"] = f"投稿了新视频<br>"
            render_data["title"] = title
            render_data["image_urls"] = [cover_url]
            if not is_forward:
                url = f"https://www.bilibili.com/video/{bv}"
                render_data["qrcode"] = await create_qrcode(url)
                render_data["url"] = url
            # logger.info(f"返回视频动态 {dyn_id}。")
            return render_data
        elif item.get("type") in (
            "DYNAMIC_TYPE_DRAW",
            "DYNAMIC_TYPE_WORD",
            "DYNAMIC_TYPE_ARTICLE",
        ):
            # 图文动态（兼容 opus 缺失场景）
            module_dynamic = item.get("modules", {}).get("module_dynamic", {}) or {}
            major = module_dynamic.get("major", {}) or {}
            opus = major.get("opus") or {}
            desc = module_dynamic.get("desc", {}) or {}
            topic = module_dynamic.get("topic")

            summary = opus.get("summary") or {
                "text": desc.get("text", ""),
                "rich_text_nodes": desc.get("rich_text_nodes", []) or [],
            }
            jump_url = opus.get("jump_url", "")
            render_data["summary"] = summary.get("text", "")
            render_data["text"] = await parse_rich_text(summary, topic)
            render_data["title"] = opus.get("title", "")

            pics = []
            for pic in (opus.get("pics") or [])[:9]:
                if isinstance(pic, dict):
                    url = pic.get("url")
                    if url:
                        pics.append(url)
            if not pics:
                draw = major.get("draw", {}) or {}
                for pic in (draw.get("items") or [])[:9]:
                    if isinstance(pic, dict):
                        url = pic.get("src") or pic.get("url")
                        if url:
                            pics.append(url)
            render_data["image_urls"] = pics
            if not render_data["image_urls"] and self.rai:
                render_data["image_urls"] = [await image_to_base64(LOGO_PATH)]
            if not is_forward:
                if jump_url:
                    url = (
                        jump_url
                        if jump_url.startswith(("http://", "https://"))
                        else f"https:{jump_url}"
                    )
                else:
                    dyn_id = item.get("id_str", "")
                    url = f"https://t.bilibili.com/{dyn_id}" if dyn_id else ""
                render_data["qrcode"] = await create_qrcode(url)
                render_data["url"] = url
            # logger.info(f"返回图文动态 {dyn_id}。")
            return render_data
        elif item.get("type") == "DYNAMIC_TYPE_FORWARD":
            # 转发动态
            try:
                content_text = item["modules"]["module_dynamic"]["desc"]["text"]
            except (TypeError, KeyError):
                content_text = None
            if content_text:
                rich_text = await parse_rich_text(
                    item["modules"]["module_dynamic"]["desc"],
                    item["modules"]["module_dynamic"]["topic"],
                )
                render_data["text"] = f"{rich_text}"
            return render_data

        return render_data
