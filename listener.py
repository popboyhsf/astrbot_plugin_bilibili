import asyncio
import re
import time
import traceback
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.all import *
from astrbot.api.event import MessageEventResult
from astrbot.api.message_components import File, Node, Plain

from .bili_client import BiliClient
from .constant import BANNER_PATH, LOGO_PATH
from .data_manager import DataManager
from .renderer import Renderer
from .utils import create_qrcode, create_render_data, download_url_to_temp_file, image_to_base64


class DynamicListener:
    """
    负责后台轮询检查B站动态和直播，并推送更新。
    """

    def __init__(
        self,
        context: Context,
        data_manager: DataManager,
        bili_client: BiliClient,
        renderer: Renderer,
        cfg: dict,
    ):
        self.context = context
        self.data_manager = data_manager
        self.bili_client = bili_client
        self.renderer = renderer
        self.interval_mins = self._parse_float(
            cfg.get("interval_mins"), 20, minimum=0.1
        )
        self.interval_secs = self.interval_mins * 60
        self.task_gap_secs = self._parse_float(cfg.get("task_gap_secs"), 20, minimum=0)
        self.rai = cfg.get("rai", True)
        self.node = cfg.get("node", False)
        self.dynamic_limit = cfg.get("dynamic_limit", 5)
        self.render_cache: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self.render_cache_limit = int(cfg.get("render_cache_limit", 32))

    async def start(self):
        """启动后台监听循环（按 UID 任务池调度）。"""
        uid_states: Dict[int, float] = {}
        next_dispatch_at = 0.0

        while True:
            try:
                if self.bili_client.credential is None:
                    logger.warning(
                        "Bilibili 凭据未设置，无法获取动态。请使用 /bili_login 登录或在配置中设置 sessdata。"
                    )
                    await asyncio.sleep(self.interval_secs)
                    continue

                uid_targets = self._build_uid_targets()
                current_uids = set(uid_targets.keys())
                now = time.monotonic()

                for uid in list(uid_states):
                    if uid not in current_uids:
                        uid_states.pop(uid, None)

                for uid in current_uids:
                    uid_states.setdefault(uid, now)

                if not current_uids:
                    await asyncio.sleep(2)
                    continue

                due_uids = [uid for uid in current_uids if uid_states[uid] <= now]
                if not due_uids:
                    next_due_at = min(uid_states[uid] for uid in current_uids)
                    wait_secs = min(max(next_due_at - now, 0.2), 2.0)
                    await asyncio.sleep(wait_secs)
                    continue

                if now < next_dispatch_at:
                    wait_secs = min(max(next_dispatch_at - now, 0.2), 2.0)
                    await asyncio.sleep(wait_secs)
                    continue

                run_uid = min(due_uids, key=lambda uid: (uid_states[uid], uid))
                await self._run_uid_task(run_uid, uid_targets.get(run_uid, []))

                finished_at = time.monotonic()
                uid_states[run_uid] = finished_at + self.interval_secs
                next_dispatch_at = finished_at + self.task_gap_secs
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"UID任务池调度异常: {e}\n{traceback.format_exc()}")
                await asyncio.sleep(1)

    @staticmethod
    def _parse_float(value: Any, default: float, minimum: float = 0) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        return max(parsed, minimum)

    def _build_uid_targets(self) -> Dict[int, List[Tuple[str, Dict[str, Any]]]]:
        """构建 UID -> 订阅目标列表 的映射，用于 UID 级去重请求。"""
        uid_targets: Dict[int, List[Tuple[str, Dict[str, Any]]]] = {}
        all_subs = self.data_manager.get_all_subscriptions()

        for sub_user, sub_list in all_subs.items():
            for sub_data in sub_list or []:
                uid = sub_data.get("uid")
                try:
                    uid_int = int(uid)
                except (TypeError, ValueError):
                    continue

                uid_targets.setdefault(uid_int, []).append((sub_user, sub_data))

        return uid_targets

    async def _run_uid_task(
        self, uid: int, targets: List[Tuple[str, Dict[str, Any]]]
    ) -> None:
        """执行单个 UID 的任务：动态/直播仅请求一次，再按订阅分发。"""
        if not targets:
            return

        try:
            dyn = await self.bili_client.get_latest_dynamics(uid)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"拉取 UID={uid} 动态失败: {e}\n{traceback.format_exc()}")
            dyn = None

        should_check_live = any(
            "live" not in (sub_data.get("filter_types") or [])
            for _, sub_data in targets
        )
        live_room = None
        if should_check_live:
            try:
                live_room = await self.bili_client.get_live_info_by_uids([uid])
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    f"拉取 UID={uid} 直播状态失败: {e}\n{traceback.format_exc()}"
                )
                live_room = None

        for sub_user, sub_data in targets:
            try:
                await self._check_single_up(
                    sub_user=sub_user,
                    sub_data=sub_data,
                    dyn=dyn,
                    live_room=live_room,
                    shared_payload=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    f"处理订阅者 {sub_user} 的 UP主 {sub_data.get('uid', '未知UID')} 时发生未知错误: {e}\n{traceback.format_exc()}"
                )

    async def _check_single_up(
        self,
        sub_user: str,
        sub_data: Dict[str, Any],
        dyn: Optional[Dict[str, Any]] = None,
        live_room: Optional[Dict[str, Any]] = None,
        shared_payload: bool = False,
    ):
        """检查单个订阅的UP主是否有更新。"""
        uid = sub_data.get("uid")
        if uid is None:
            return

        try:
            uid = int(uid)
        except (TypeError, ValueError):
            return

        # 检查动态更新
        if dyn is None and not shared_payload:
            dyn = await self.bili_client.get_latest_dynamics(uid)
        if dyn:
            result_list = await self._parse_and_filter_dynamics(dyn, sub_data)
            sent = 0
            for render_data, dyn_id in reversed(result_list):
                if render_data:
                    if sent < self.dynamic_limit:
                        sent += 1
                        await self._handle_new_dynamic(sub_user, render_data, dyn_id)
                    await self.data_manager.update_last_dynamic_id(
                        sub_user, uid, dyn_id
                    )

                elif dyn_id:  # 动态被过滤，只更新ID
                    await self.data_manager.update_last_dynamic_id(
                        sub_user, uid, dyn_id
                    )

        # 检查直播状态
        if "live" in sub_data.get("filter_types", []):
            return

        if live_room is None and not shared_payload:
            # lives = await self.bili_client.get_live_info(uid)
            live_room = await self.bili_client.get_live_info_by_uids([uid])
        if live_room:
            await self._handle_live_status(sub_user, sub_data, live_room)

    def _compose_plain_dynamic(
        self, render_data: Dict[str, Any], render_fail: bool = False
    ):
        """转换为纯文本消息链。"""
        name = render_data.get("name")
        summary = render_data.get("summary", "")
        prefix_fail = [Plain("渲染图片失败了 (´;ω;`)\n")] if render_fail else []
        ls = [
            *prefix_fail,
            Plain(f"📣 UP 主 「{name}」 发布了新图文动态:\n"),
            Plain(summary),
        ]
        return ls

    async def _send_dynamic(
        self, sub_user: str, chain_parts: list, send_node: bool = False
    ):
        if self.node or send_node:
            qqNode = Node(
                uin=0,
                name="AstrBot",
                content=chain_parts,
            )
            await self.context.send_message(
                sub_user, MessageEventResult(chain=[qqNode])
            )
        else:
            await self.context.send_message(
                sub_user, MessageEventResult(chain=chain_parts).use_t2i(False)
            )

    def _cache_render(self, dyn_id: Optional[str], chain_parts: list, send_node: bool):
        """缓存渲染结果，避免同一动态在不同会话重复渲染。"""
        if not dyn_id:
            return
        self.render_cache[dyn_id] = {"chain": chain_parts, "send_node": send_node}
        while len(self.render_cache) > self.render_cache_limit:
            self.render_cache.popitem(last=False)

    async def _handle_new_dynamic(
        self,
        sub_user: str,
        render_data: Optional[Dict[str, Any]],
        dyn_id: Optional[str] = None,
        ignore_cache: bool = False,
    ):
        """处理并发送新的动态通知。"""
        if not render_data:
            return

        cached = self.render_cache.get(dyn_id) if (dyn_id and not ignore_cache) else None
        if cached:
            await self._send_dynamic(sub_user, cached["chain"], cached["send_node"])
            return

        send_node_flag = self.node
        # 非图文混合模式
        if not self.rai and render_data.get("type") in (
            "DYNAMIC_TYPE_DRAW",
            "DYNAMIC_TYPE_WORD",
        ):
            ls = self._compose_plain_dynamic(render_data)
            await self._send_dynamic(sub_user, ls, send_node_flag)
            if not ignore_cache:
                self._cache_render(dyn_id, ls, send_node_flag)
            return

        img_path = await self.renderer.render_dynamic(render_data)
        if img_path:
            url = render_data.get("url", "")
            timestamp = int(time.time())
            filename = f"bilibili_dynamic_{timestamp}.jpg"
            ls = [File(file=img_path, name=filename)]
            ls.append(Plain(f"\n{url}"))
            await self._send_dynamic(sub_user, ls, send_node_flag)
            if not ignore_cache:
                self._cache_render(dyn_id, ls, send_node_flag)
            return

        logger.error("渲染图片失败，尝试发送纯文本消息")
        ls = self._compose_plain_dynamic(render_data, render_fail=True)
        await self._send_dynamic(sub_user, ls, send_node=True)

    async def _handle_live_status(self, sub_user: str, sub_data: Dict, live_room: Dict):
        """处理并发送直播状态变更通知。"""
        is_live = sub_data.get("is_live", False)

        live_name = live_room.get("title", "Unknown")
        user_name = live_room.get("uname", "Unknown")
        cover_url = live_room.get("cover_from_user", "")
        room_id = live_room.get("room_id", 0)
        link = f"https://live.bilibili.com/{room_id}"

        render_data = await create_render_data()
        render_data["banner"] = await image_to_base64(BANNER_PATH)
        render_data["name"] = "AstrBot"
        render_data["avatar"] = await image_to_base64(LOGO_PATH)
        render_data["title"] = live_name
        render_data["url"] = link
        render_data["image_urls"] = [cover_url]
        # live_status: 0：未开播    1：正在直播     2：轮播中
        if live_room.get("live_status", "") == 1 and not is_live:
            render_data["text"] = f"📣 你订阅的UP 「{user_name}」 开播了！"
            await self.data_manager.update_live_status(sub_user, sub_data["uid"], True)
        if live_room.get("live_status", "") != 1 and is_live:
            render_data["text"] = f"📣 你订阅的UP 「{user_name}」 下播了！"
            await self.data_manager.update_live_status(sub_user, sub_data["uid"], False)
        if render_data["text"]:
            render_data["qrcode"] = await create_qrcode(link)
            img_path = await self.renderer.render_dynamic(render_data)
            if img_path:
                timestamp = int(time.time())
                filename = f"bilibili_live_{timestamp}.jpg"
                await self.context.send_message(
                    sub_user,
                    MessageEventResult(
                        chain=[File(file=img_path, name=filename), Plain(render_data["url"])]
                    ).use_t2i(False),
                )
            else:
                text = "\n".join(filter(None, render_data.get("text", "").split("\n")))
                chain_parts = [Plain("渲染图片失败了 (´;ω;`)") , Plain(text)]
                cover_path, cover_name = await download_url_to_temp_file(
                    cover_url, filename_prefix="bili_live_cover_"
                )
                if cover_path and cover_name:
                    chain_parts.append(File(file=cover_path, name=cover_name))
                await self.context.send_message(
                    sub_user,
                    MessageEventResult(chain=chain_parts).use_t2i(False),
                )

    async def _get_dynamic_items(self, dyn: Dict, data: Dict):
        """获取动态条目列表。"""
        last = data["last"]
        items = dyn["items"]
        recent_ids = data.get("recent_ids", []) or []
        known_ids = {x for x in ([last] + recent_ids) if x}
        new_items = []

        for item in items:
            if "modules" not in item:
                continue
            # 过滤置顶
            if (
                item["modules"].get("module_tag")
                and item["modules"]["module_tag"].get("text") == "置顶"
            ):
                continue

            if item["id_str"] in known_ids:
                break
            new_items.append(item)

        return new_items

    def _match_filter_regex(
        self, text: Optional[str], filter_regex: List[str], log_template: str
    ) -> bool:
        """检测文本是否命中过滤正则"""
        if not text or not filter_regex:
            return False

        for regex_pattern in filter_regex:
            try:
                if re.search(regex_pattern, text):
                    logger.info(log_template.format(regex_pattern=regex_pattern))
                    return True
            except re.error:
                logger.warning(f"无效的正则表达式: {regex_pattern}")
                continue

        return False

    async def _parse_and_filter_dynamics(self, dyn: Dict, data: Dict):
        """
        解析并过滤动态。
        """
        filter_types = data.get("filter_types", [])
        filter_regex = data.get("filter_regex", [])
        uid = data.get("uid", "")
        items = await self._get_dynamic_items(dyn, data)  # 不含last及置顶的动态列表
        result_list = []
        # 无新动态
        if not items:
            result_list.append((None, None))

        for item in items:
            dyn_id = item["id_str"]
            item_type = item.get("type")

            if item_type == "DYNAMIC_TYPE_FORWARD":
                result = await self._handle_forward_dynamic(
                    item, dyn_id, uid, filter_types, filter_regex
                )
            elif item_type in ("DYNAMIC_TYPE_DRAW", "DYNAMIC_TYPE_WORD"):
                result = await self._handle_draw_or_word_dynamic(
                    item, dyn_id, uid, filter_types, filter_regex
                )
            elif item_type == "DYNAMIC_TYPE_AV":
                result = await self._handle_video_dynamic(
                    item, dyn_id, uid, filter_types
                )
            elif item_type == "DYNAMIC_TYPE_ARTICLE":
                result = await self._handle_article_dynamic(
                    item, dyn_id, uid, filter_types
                )
            else:
                result = (None, None)

            result_list.append(result)

        return result_list

    async def _handle_forward_dynamic(
        self,
        item: Dict,
        dyn_id: str,
        uid: str,
        filter_types: List[str],
        filter_regex: List[str],
    ) -> tuple:
        """处理转发动态的过滤与渲染数据准备。"""
        try:
            is_forward_lottery = (
                item["orig"]["modules"]["module_dynamic"]["major"]["opus"]["summary"][
                    "rich_text_nodes"
                ][0].get("text")
                == "互动抽奖"
            )
        except (KeyError, TypeError):
            is_forward_lottery = False

        if "forward_lottery" in filter_types and is_forward_lottery:
            logger.info(f"转发互动抽奖在过滤列表 {filter_types} 中。")
            return (None, dyn_id)

        if "forward" in filter_types:
            logger.info(f"转发类型在过滤列表 {filter_types} 中。")
            return (None, dyn_id)

        try:
            content_text = item["modules"]["module_dynamic"]["desc"]["text"]
        except (TypeError, KeyError):
            content_text = ""

        if "lottery" in filter_types and re.search(
            r"恭喜.*等\d+位同学中奖，已私信通知，详情请点击抽奖查看。",
            content_text,
        ):
            logger.info(f"转发内容为抽奖在过滤列表 {filter_types} 中。")
            return (None, dyn_id)

        if self._match_filter_regex(
            content_text, filter_regex, "转发内容匹配正则 {regex_pattern}。"
        ):
            return (None, dyn_id)

        render_data = await self.renderer.build_render_data(item)
        render_data["uid"] = uid
        render_data["url"] = f"https://t.bilibili.com/{dyn_id}"
        render_data["qrcode"] = await create_qrcode(render_data["url"])

        render_forward = await self.renderer.build_render_data(
            item.get("orig", {}), is_forward=True
        )
        if render_forward.get("image_urls"):
            render_forward["image_urls"] = [render_forward["image_urls"][0]]
        render_data["forward"] = render_forward
        return (render_data, dyn_id)

    async def _handle_draw_or_word_dynamic(
        self,
        item: Dict,
        dyn_id: str,
        uid: str,
        filter_types: List[str],
        filter_regex: List[str],
    ) -> tuple:
        """处理图文/文字动态。"""
        if "draw" in filter_types:
            logger.info(f"图文类型在过滤列表 {filter_types} 中。")
            return (None, dyn_id)

        major = item.get("modules", {}).get("module_dynamic", {}).get("major", {})
        if major.get("type") == "MAJOR_TYPE_BLOCKED":
            logger.info(f"图文动态 {dyn_id} 为充电专属。")
            return (None, dyn_id)

        opus = major.get("opus", {})
        summary = opus.get("summary", {})
        summary_text = summary.get("text", "")
        rich_nodes = summary.get("rich_text_nodes", [])
        first_node_text = rich_nodes[0].get("text") if rich_nodes else ""

        if first_node_text == "互动抽奖" and "lottery" in filter_types:
            logger.info(f"互动抽奖在过滤列表 {filter_types} 中。")
            return (None, dyn_id)

        if self._match_filter_regex(
            summary_text,
            filter_regex,
            f"图文动态 {dyn_id} 的 summary 匹配正则 '{{regex_pattern}}'。",
        ):
            return (None, dyn_id)

        render_data = await self.renderer.build_render_data(item)
        render_data["uid"] = uid
        return (render_data, dyn_id)

    async def _handle_video_dynamic(
        self, item: Dict, dyn_id: str, uid: str, filter_types: List[str]
    ) -> tuple:
        """处理视频动态。"""
        if "video" in filter_types:
            logger.info(f"视频类型在过滤列表 {filter_types} 中。")
            return (None, dyn_id)

        render_data = await self.renderer.build_render_data(item)
        render_data["uid"] = uid
        return (render_data, dyn_id)

    async def _handle_article_dynamic(
        self, item: Dict, dyn_id: str, uid: str, filter_types: List[str]
    ) -> tuple:
        """处理专栏文章动态。"""
        if "article" in filter_types:
            logger.info(f"文章类型在过滤列表 {filter_types} 中。")
            return (None, dyn_id)

        major = item.get("modules", {}).get("module_dynamic", {}).get("major", {})
        if major.get("type") == "MAJOR_TYPE_BLOCKED":
            logger.info(f"文章 {dyn_id} 为充电专属。")
            return (None, dyn_id)

        render_data = await self.renderer.build_render_data(item)
        render_data["uid"] = uid
        return (render_data, dyn_id)
