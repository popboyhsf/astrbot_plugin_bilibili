import asyncio
import json
import os
import re
import tempfile
from typing import List

from astrbot.api import AstrBotConfig, logger
from astrbot.api.all import *
from astrbot.api.event import AstrMessageEvent, MessageChain, MessageEventResult
from astrbot.api.event.filter import (
    EventMessageType,
    PermissionType,
    command,
    event_message_type,
    permission_type,
    regex,
)
from astrbot.api.message_components import Image, Plain
from astrbot.core.star.filter.command import GreedyStr
from bilibili_api import login_v2

from .bili_client import BiliClient
from .constant import (
    BV,
    CARD_TEMPLATES,
    DEFAULT_TEMPLATE,
    LOGO_PATH,
    VALID_FILTER_TYPES,
    get_template_names,
)
from .data_manager import DataManager
from .listener import DynamicListener
from .renderer import Renderer
from .tools.bangumi import BangumiTool
from .utils import *


@register("astrbot_plugin_bilibili", "Soulter", "", "", "")
class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.cfg = config
        self.context = context

        self.rai = self.cfg.get("rai", True)
        self.merge_card_mode = self.cfg.get("merge_card_mode", True)
        self.enable_parse_miniapp = self.cfg.get("enable_parse_miniapp", True)
        self.enable_parse_BV = self.cfg.get("enable_parse_BV", True)
        self.proxy = (self.cfg.get("proxy", "") or "").strip()
        # 读取样式配置
        self.style = self.cfg.get("renderer_template", DEFAULT_TEMPLATE)

        self.data_manager = DataManager()
        self.renderer = Renderer(self, self.rai, self.style)

        # 优先使用 DataManager 中的凭据
        saved_credential = self.data_manager.get_credential()
        if saved_credential:
            self.bili_client = BiliClient(
                credential_dict=saved_credential, proxy=self.proxy
            )
        else:
            self.bili_client = BiliClient(
                sessdata=self.cfg.get("sessdata"), proxy=self.proxy
            )

        self.dynamic_listener = DynamicListener(
            context=self.context,
            data_manager=self.data_manager,
            bili_client=self.bili_client,
            renderer=self.renderer,
            cfg=self.cfg,
        )
        self.context.add_llm_tools(BangumiTool())
        self._start_tasks()

    def _start_tasks(self):
        """启动或重启后台任务。"""
        if hasattr(self, "dynamic_listener_task") and self.dynamic_listener_task:
            self.dynamic_listener_task.cancel()

        self.dynamic_listener_task = asyncio.create_task(self.dynamic_listener.start())

    @command("bili_login")
    @permission_type(PermissionType.ADMIN)
    async def bili_login(self, event: AstrMessageEvent):
        """扫码登录 Bilibili。"""
        if event.get_group_id():
            return MessageEventResult().message(
                "仅支持管理员在私聊中使用'/bili_login'指令。"
            )

        login_obj = login_v2.QrCodeLogin()
        await login_obj.generate_qrcode()

        # 获取二维码图片路径
        qr_path = os.path.join(tempfile.gettempdir(), "qrcode.png")

        await event.send(
            MessageChain()
            .message("请使用 Bilibili App 扫描下方二维码登录：")
            .file_image(qr_path)
        )

        # 轮询状态
        try:
            while True:
                state = await login_obj.check_state()
                if state == login_v2.QrCodeLoginEvents.DONE:
                    credential = login_obj.get_credential()
                    # 保存凭据
                    self.bili_client.credential = credential
                    cred_dict = self.bili_client.get_credential_dict()
                    if cred_dict is not None:
                        await self.data_manager.set_credential(cred_dict)
                        self._start_tasks()
                        await event.send(MessageChain().message("✅ 登录成功！"))
                    else:
                        await event.send(
                            MessageChain().message("❌ 登录失败：无法获取凭据。")
                        )
                    break
                elif state == login_v2.QrCodeLoginEvents.TIMEOUT:
                    await event.send(
                        MessageChain().message("❌ 登录超时，请重新执行 /bili_login。")
                    )
                    break

                await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"登录过程中发生错误: {e}")
            await event.send(MessageChain().message(f"❌ 登录失败: {str(e)}"))

    @command("bili_logout")
    @permission_type(PermissionType.ADMIN)
    async def bili_logout(self, event: AstrMessageEvent):
        """登出 Bilibili，清除凭据。"""
        self.bili_client.credential = None
        await self.data_manager.clear_credential()
        self.bili_client = BiliClient(
            sessdata=self.cfg.get("sessdata"), proxy=self.proxy
        )
        self.dynamic_listener.bili_client = self.bili_client
        self._start_tasks()
        return MessageEventResult().message("✅ 已登出 Bilibili，凭据已清除。")

    @command("bili_card_style", alias={"卡片样式"})
    @permission_type(PermissionType.ADMIN)
    async def switch_style(self, event: AstrMessageEvent, style: str | None = None):
        """切换动态卡片样式。不带参数可以查看可用的卡片样式列表。"""
        available = get_template_names()

        # 不带参数：显示可用样式列表
        if not style:
            lines = ["📋 可用的卡片样式："]
            for tid in available:
                info = CARD_TEMPLATES[tid]
                current = " ← 当前" if tid == self.style else ""
                lines.append(f"  • {tid}: {info['name']}{current}")
                lines.append(f"    {info['description']}")
            lines.append(f"\n使用 /卡片样式 <样式名> 切换")
            return MessageEventResult().message("\n".join(lines))

        # 带参数：切换样式
        if style not in available:
            return MessageEventResult().message(
                f"样式 '{style}' 不存在。可用样式：{', '.join(available)}"
            )

        self.style = style
        self.renderer.style = style

        info = CARD_TEMPLATES[style]
        self.cfg["renderer_template"] = style
        self.cfg.save_config()
        return MessageEventResult().message(
            f"✅ 已切换样式为：{info['name']} ({style})"
        )

    @command("bili_merge_card")
    @permission_type(PermissionType.ADMIN)
    async def switch_merge_card_mode(
        self, event: AstrMessageEvent, mode: str | None = None
    ):
        """Toggle push format: merged card or element file group."""
        current = bool(self.cfg.get("merge_card_mode", True))
        if not mode:
            status = "on" if current else "off"
            return MessageEventResult().message(
                f"merge_card_mode: {status}\\nUse /bili_merge_card on|off"
            )

        mode_norm = mode.strip().lower()
        if mode_norm == "on":
            target = True
        elif mode_norm == "off":
            target = False
        else:
            return MessageEventResult().message("Invalid arg. Use on/off.")

        self.merge_card_mode = target
        self.dynamic_listener.merge_card_mode = target
        self.cfg["merge_card_mode"] = target
        self.cfg.save_config()
        status = "on" if target else "off"
        return MessageEventResult().message(f"merge_card_mode set to: {status}")
    @regex(BV)
    async def get_video_info(self, event: AstrMessageEvent):
        if self.enable_parse_BV:
            match_ = re.search(BV, event.message_str, re.IGNORECASE)
            if not match_:
                return
            # 匹配到短链接
            if match_.group(2):
                full_link = match_.group(0)
                converted_url = await self.bili_client.b23_to_bv(full_link)
                if not converted_url:
                    return
                match_bv = re.search(r"(BV[a-zA-Z0-9]+)", converted_url, re.IGNORECASE)
                if match_bv:
                    bvid = match_bv.group(1)
                else:
                    return
            # 匹配到长链接
            elif match_.group(1):
                bvid = match_.group(1)
            # 匹配到纯 BV 号
            elif match_.group(0):
                bvid = match_.group(0)

            video_data = await self.bili_client.get_video_info(bvid=bvid)
            if not video_data:
                return await event.send(
                    MessageChain().message("获取视频信息失败了 (´;ω;`)")
                )
            info = video_data["info"]
            online = video_data["online"]

            render_data = await create_render_data()
            render_data["name"] = "AstrBot"
            render_data["avatar"] = await image_to_base64(LOGO_PATH)
            render_data["title"] = info["title"]
            render_data["text"] = (
                f"UP 主: {info['owner']['name']}<br>"
                f"播放量: {info['stat']['view']}<br>"
                f"点赞: {info['stat']['like']}<br>"
                f"投币: {info['stat']['coin']}<br>"
                f"总共 {online['total']} 人正在观看"
            )
            render_data["image_urls"] = [info["pic"]]

            img_path = await self.renderer.render_dynamic(render_data)
            if img_path:
                await event.send(MessageChain().file_image(img_path))
            else:
                msg = "渲染图片失败了 (´;ω;`)"
                text = "\n".join(
                    filter(None, render_data.get("text", "").split("<br>"))
                )
                await event.send(
                    MessageChain().message(msg).message(text).url_image(info["pic"])
                )

    @command("bili_sub", alias={"订阅动态"})
    async def dynamic_sub(self, event: AstrMessageEvent, uid: str, input: GreedyStr):
        args = input.strip().split(" ") if input.strip() else []

        filter_types: List[str] = []
        filter_regex: List[str] = []
        for arg in args:
            if arg in VALID_FILTER_TYPES:
                filter_types.append(arg)
            else:
                filter_regex.append(arg)

        sub_user = event.unified_msg_origin
        if not uid.isdigit():
            return MessageEventResult().message("UID 格式错误")

        # 检查是否已经存在该订阅
        if await self.data_manager.update_subscription(
            sub_user, int(uid), filter_types, filter_regex
        ):
            # 如果已存在，更新其过滤条件
            return MessageEventResult().message("该动态已订阅，已更新过滤条件。")
        # 以下为新增订阅
        try:
            # 构造新的订阅数据结构
            _sub_data = {
                "uid": int(uid),
                "last": "",
                "is_live": False,
                "filter_types": filter_types,
                "filter_regex": filter_regex,
                "recent_ids": [],
            }
            # 获取最新一条动态 (用于初始化 last_id)
            dyn = await self.bili_client.get_latest_dynamics(int(uid))
            if dyn:
                await self.data_manager.add_subscription(sub_user, _sub_data)
                result_list = await self.dynamic_listener._parse_and_filter_dynamics(
                    dyn, _sub_data
                )
                for render_data, dyn_id in reversed(result_list):
                    if dyn_id:
                        await self.data_manager.update_last_dynamic_id(
                            sub_user, int(uid), dyn_id
                        )
        except Exception as e:
            logger.error(f"获取初始动态失败: {e}")
        finally:
            # 保存配置
            await self.data_manager.add_subscription(sub_user, _sub_data)
        # 获取用户信息(可能412，故后置)
        try:
            usr_info, msg = await self.bili_client.get_user_info(int(uid))
            if usr_info:
                mid = usr_info["mid"]
                name = usr_info["name"]
                sex = usr_info["sex"]
                avatar = usr_info["face"]
        except Exception as e:
            logger.error(f"获取用户信息失败: {e}")

        try:
            filter_desc = ""
            if filter_types:
                filter_desc += f"<br>过滤类型: {', '.join(filter_types)}"
            if filter_regex:
                filter_desc += f"<br>过滤正则: {filter_regex}"

            render_data = await create_render_data()
            render_data["uid"] = uid
            render_data["name"] = "AstrBot"
            render_data["avatar"] = await image_to_base64(LOGO_PATH)
            render_data["text"] = (
                f"📣 订阅成功！<br>"
                f"UP 主: {name} | 性别: {sex}"
                f"{filter_desc}"  # 显示过滤信息
            )
            render_data["image_urls"] = [avatar]
            render_data["url"] = f"https://space.bilibili.com/{mid}"
            render_data["qrcode"] = await create_qrcode(render_data["url"])
            if self.rai:
                img_path = await self.renderer.render_dynamic(render_data)
                if img_path:
                    await event.send(
                        MessageChain().file_image(img_path).message(render_data["url"])
                    )
                else:
                    msg = "渲染图片失败了 (´;ω;`)"
                    text = "\n".join(
                        filter(None, render_data.get("text", "").split("<br>"))
                    )
                    await event.send(
                        MessageChain().message(msg).message(text).url_image(avatar)
                    )
            else:
                chain = [
                    Plain(render_data["text"]),
                    Image.fromURL(avatar),
                ]
                return MessageEventResult(chain=chain, use_t2i_=False)
        except Exception as e:
            logger.warning(f"订阅出现问题: {e}")
            return MessageEventResult().message(f"订阅成功！但是:{e}")

    @command("bili_sub_list", alias={"订阅列表"})
    async def sub_list(self, event: AstrMessageEvent):
        """查看 bilibili 动态监控列表"""
        sub_user = event.unified_msg_origin
        ret = """订阅列表：\n"""
        subs = self.data_manager.get_subscriptions_by_user(sub_user)

        if not subs:
            return MessageEventResult().message("无订阅")
        else:
            for idx, uid_sub_data in enumerate(subs):
                uid = uid_sub_data["uid"]
                info, _ = await self.bili_client.get_user_info(int(uid))
                if not info:
                    ret += f"{idx + 1}. {uid} - 无法获取 UP 主信息\n"
                else:
                    name = info["name"]
                    ret += f"{idx + 1}. {uid} - {name}\n"
            return MessageEventResult().message(ret)

    @command("bili_sub_del", alias={"订阅删除"})
    async def sub_del(self, event: AstrMessageEvent, uid: str):
        """删除 bilibili 动态监控"""
        sub_user = event.unified_msg_origin
        if not uid or not uid.isdigit():
            return MessageEventResult().message("参数错误，请提供正确的UID。")

        uid2del = int(uid)

        if await self.data_manager.remove_subscription(sub_user, uid2del):
            return MessageEventResult().message("删除成功")
        else:
            return MessageEventResult().message("未找到指定的订阅")

    @permission_type(PermissionType.ADMIN)
    @command("bili_global_del", alias={"全局删除"})
    async def global_sub_del(self, event: AstrMessageEvent, umo: str = ""):
        """管理员指令。通过 UMO 删除某一个群聊或者私聊的所有订阅。"""
        if not is_valid_umo(umo):
            return MessageEventResult().message(
                "通过 UMO 删除某一个群聊或者私聊的所有订阅。使用 /sid 指令查看当前会话的 UMO 或参考 WebUI-自定义规则。"
            )

        msg = await self.data_manager.remove_all_for_user(umo)
        return MessageEventResult().message(msg)

    @permission_type(PermissionType.ADMIN)
    @command("bili_global_sub", alias={"全局订阅"})
    async def global_sub_add(
        self, event: AstrMessageEvent, umo: str, uid: str, input: GreedyStr
    ):
        """管理员指令。通过 UID 添加某一个用户的所有订阅。"""
        if not is_valid_umo(umo) or not uid.isdigit():
            return MessageEventResult().message(
                "请提供正确的UMO与UID。使用 /sid 指令查看当前会话的 UMO 或参考 WebUI-自定义规则。"
            )
        args = input.strip().split(" ") if input.strip() else []
        filter_types: List[str] = []
        filter_regex: List[str] = []
        for arg in args:
            if arg in VALID_FILTER_TYPES:
                filter_types.append(arg)
            else:
                filter_regex.append(arg)

        if await self.data_manager.update_subscription(
            umo, int(uid), filter_types, filter_regex
        ):
            return MessageEventResult().message("该动态已订阅，已更新过滤条件")

        try:
            _sub_data = {
                "uid": int(uid),
                "last": "",
                "is_live": False,
                "filter_types": filter_types,
                "filter_regex": filter_regex,
                "recent_ids": [],
            }

            dyn = await self.bili_client.get_latest_dynamics(int(uid))
            if dyn:
                await self.data_manager.add_subscription(umo, _sub_data)
                result_list = await self.dynamic_listener._parse_and_filter_dynamics(
                    dyn, _sub_data
                )
                for _, dyn_id in reversed(result_list):
                    if dyn_id:
                        await self.data_manager.update_last_dynamic_id(
                            umo, int(uid), dyn_id
                        )

            usr_info, msg = await self.bili_client.get_user_info(int(uid))
        except Exception as e:
            logger.error(f"获取初始动态失败: {e}")
        finally:
            # 保存配置
            await self.data_manager.add_subscription(umo, _sub_data)
            if not usr_info:
                return MessageEventResult().message(msg)
            else:
                return MessageEventResult().message(
                    f"订阅完成，已为{umo}添加订阅{uid}，详情见日志。"
                )

    @permission_type(PermissionType.ADMIN)
    @command("bili_global_list", alias={"全局列表"})
    async def global_list(self, event: AstrMessageEvent):
        """管理员指令。查看所有订阅者"""
        ret = "订阅会话列表：\n"
        all_subs = self.data_manager.get_all_subscriptions()
        if not all_subs:
            return MessageEventResult().message("没有任何会话订阅过。")

        for sub_user in all_subs:
            ret += f"- {sub_user}\n"
            for sub in all_subs[sub_user]:
                uid = sub.get("uid")
                ret += f"  - {uid}\n"
        return MessageEventResult().message(ret)

    @event_message_type(EventMessageType.ALL)
    async def parse_miniapp(self, event: AstrMessageEvent):
        if self.enable_parse_miniapp:
            for msg_element in event.message_obj.message:
                if (
                    hasattr(msg_element, "type")
                    and msg_element.type == "Json"
                    and hasattr(msg_element, "data")
                ):
                    json_string = msg_element.data

                    try:
                        if isinstance(json_string, dict):
                            parsed_data = json_string
                        else:
                            parsed_data = json.loads(json_string)
                        meta = parsed_data.get("meta", {})
                        detail_1 = meta.get("detail_1", {})
                        title = detail_1.get("title")
                        qqdocurl = detail_1.get("qqdocurl")
                        desc = detail_1.get("desc")

                        if title == "哔哩哔哩" and qqdocurl:
                            if "https://b23.tv" in qqdocurl:
                                qqdocurl = await self.bili_client.b23_to_bv(qqdocurl)
                            ret = f"标题: {desc}\n链接: {qqdocurl}"
                            await event.send(MessageChain().message(ret))
                        news = meta.get("news", {})
                        tag = news.get("tag", "")
                        jumpurl = news.get("jumpUrl", "")
                        title = news.get("title", "")
                        if tag == "哔哩哔哩" and jumpurl:
                            if "https://b23.tv" in jumpurl:
                                jumpurl = await self.bili_client.b23_to_bv(jumpurl)
                            ret = f"标题: {title}\n链接: {jumpurl}"
                            await event.send(MessageChain().message(ret))
                    except json.JSONDecodeError:
                        logger.error(f"Failed to decode JSON string: {json_string}")
                    except Exception as e:
                        logger.error(f"An error occurred during JSON processing: {e}")

    @command("bili_sub_test", alias={"订阅测试"})
    async def sub_test(self, event: AstrMessageEvent, uid: str):
        """测试订阅功能。仅测试获取动态与渲染图片功能，不保存订阅信息。"""
        sub_user = event.unified_msg_origin
        try:
            uid_int = int(uid)
        except (TypeError, ValueError):
            return MessageEventResult().message("UID 必须是数字。")

        dyn = await self.bili_client.get_latest_dynamics(uid_int)
        if not dyn:
            return MessageEventResult().message("未获取到动态数据，请稍后重试。")

        result_list = await self.dynamic_listener._parse_and_filter_dynamics(
            dyn,
            {
                "uid": uid,
                "filter_types": [],
                "filter_regex": [],
                "last": "",
                "recent_ids": [],
            },
        )

        render_data = None
        dyn_id = None
        for maybe_render_data, maybe_dyn_id in result_list or []:
            if maybe_render_data:
                render_data = maybe_render_data
                dyn_id = maybe_dyn_id
                break

        if not render_data:
            return MessageEventResult().message(
                "没有可用于测试推送的动态（可能没有新动态、都被过滤掉，或动态类型暂不支持）。"
            )

        await self.dynamic_listener._handle_new_dynamic(sub_user, render_data, dyn_id)

        return None


    @staticmethod
    def _extract_dynamic_id_from_url(url: str) -> str | None:
        target = (url or "").strip()
        if not target:
            return None
        if target.isdigit():
            return target

        patterns = [
            r"(?:t\.bilibili\.com|www\.bilibili\.com/opus|m\.bilibili\.com/dynamic)/(\d+)",
            r"[?&]id=(\d+)",
        ]
        for pattern in patterns:
            match_ = re.search(pattern, target, re.IGNORECASE)
            if match_:
                return match_.group(1)
        return None

    @command("bili_sub_opus")
    async def sub_opus(self, event: AstrMessageEvent, url: str):
        """Ignore cache, fetch one dynamic by URL and push it."""
        sub_user = event.unified_msg_origin
        if "b23.tv" in (url or ""):
            url = await self.bili_client.b23_to_bv(url) or url

        dyn_id = self._extract_dynamic_id_from_url(url)
        if not dyn_id:
            return MessageEventResult().message("Failed to parse dynamic id from url.")

        item = await self.bili_client.get_dynamic_detail_by_id(dyn_id)
        if not item:
            return MessageEventResult().message("Failed to fetch dynamic detail.")

        dyn = {"items": [item]}
        result_list = await self.dynamic_listener._parse_and_filter_dynamics(
            dyn,
            {
                "uid": "",
                "filter_types": [],
                "filter_regex": [],
                "last": "",
                "recent_ids": [],
            },
        )

        render_data = None
        parsed_dyn_id = dyn_id
        for maybe_render_data, maybe_dyn_id in result_list or []:
            if maybe_render_data:
                render_data = maybe_render_data
                if maybe_dyn_id:
                    parsed_dyn_id = maybe_dyn_id
                break

        if not render_data:
            return MessageEventResult().message("Dynamic parse unsupported or failed.")

        await self.dynamic_listener._handle_new_dynamic(
            sub_user, render_data, parsed_dyn_id, ignore_cache=True
        )
        return None

    async def terminate(self):
        if (
            hasattr(self, "dynamic_listener_task")
            and self.dynamic_listener_task
            and not self.dynamic_listener_task.done()
        ):
            self.dynamic_listener_task.cancel()
            try:
                await self.dynamic_listener_task
            except asyncio.CancelledError:
                logger.info(
                    "bilibili dynamic_listener task was successfully cancelled during terminate."
                )
            except Exception as e:
                logger.error(
                    f"Error awaiting cancellation of dynamic_listener task: {e}"
                )
