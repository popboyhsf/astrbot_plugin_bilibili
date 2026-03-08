import base64
import io
import re
from urllib.parse import urlparse

import qrcode
import qrcode.constants
from astrbot.api import logger
from astrbot.api.all import *
from PIL import Image as PILImage


async def create_render_data() -> dict:
    return {
        "name": "",  # 图中header处用户名
        "avatar": "",  # 头像url
        "pendant": "",  # 头像框
        "text": "",  # 正文
        "image_urls": [],  # 正文图片url列表
        "qrcode": "",  # qrcode url(base64)
        "url": "",  # 用于渲染qrcode，也用于构成massagechain
        "title": "",  # 标题(视频标题、动态标题)
    }


async def image_to_base64(image_source, mime_type: str = "image/png") -> str:
    """
    将图片对象或文件路径转为Base64 Data URI
    :param image_source: PIL Image对象 或 图片文件路径
    :param mime_type: 图片MIME类型，默认image/png
    :return: Base64 Data URI字符串
    """
    buffer = io.BytesIO()

    # 处理PIL Image对象
    if hasattr(image_source, "save"):
        image_source.save(buffer, format=mime_type.split("/")[-1])
    # 处理文件路径
    elif isinstance(image_source, str):
        with open(image_source, "rb") as f:
            buffer.write(f.read())
    else:
        raise ValueError("Unsupported image source type")

    base64_str = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return f"data:{mime_type};base64,{base64_str}"


async def create_qrcode(url):
    if not is_valid_url(url):
        return ""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=1,
    )
    qr.add_data(url)
    qr.make(fit=True)
    qr_image = qr.make_image(fill_color="#fb7299", back_color="white")
    url = await image_to_base64(qr_image)
    return url


def is_valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return all([parsed.scheme, parsed.netloc])
    except ValueError:
        return False


def is_valid_umo(umo: str) -> bool:
    pattern = r"([^:]+):\s*([^:]+):\s*(.+)"
    return re.match(pattern, umo) is not None


async def parse_rich_text(summary, topic):
    text = "<br>".join(filter(None, summary["text"].split("\n")))
    # 真正的话题
    if topic:
        topic_link = f"<a href='{topic['jump_url']}'># {topic['name']}</a>"
        text = f"# {topic_link}<br>" + text
    # 获取富文本节点
    rich_text_nodes = summary["rich_text_nodes"]
    for node in rich_text_nodes:
        # 表情包
        if node["type"] == "RICH_TEXT_NODE_TYPE_EMOJI":
            emoji_info = node["emoji"]
            placeholder = emoji_info["text"]  # 例如 "[脱单doge]"
            img_tag = f"<img src='{emoji_info['icon_url']}'>"
            # 替换文本中的占位符
            text = text.replace(placeholder, img_tag)
        # 话题形如"#一个话题#"，实际是跳转搜索
        elif node["type"] == "RICH_TEXT_NODE_TYPE_TOPIC":
            topic_info = node["text"]
            topic_url = node["jump_url"]
            topic_tag = f"<a href='https:{topic_url}'>{topic_info}</a>"
            # 替换文本中的占位符
            text = text.replace(topic_info, topic_tag)

    return text


async def is_height_valid(img_path: str, max_height: int = 25000) -> bool:
    """
    检查图片高度是否在允许范围内
    :param img_path: 图片文件路径
    :param max_height: 最大允许高度
    :return: 如果图片高度小于等于max_height则返回True，否则返回False
    """

    try:
        with PILImage.open(img_path) as img:
            _, height = img.size
            return height <= max_height
    except Exception as e:
        logger.error(f"无法打开图片 {img_path} 进行高度检查: {e}")
        return False
