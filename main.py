import os
import json
import aiohttp
import asyncio
import shutil
import time
import tempfile
from io import BytesIO
from typing import Dict, List
from datetime import datetime, timezone, timedelta, date, time as dt_time
from icalendar import Calendar
from PIL import Image, ImageDraw, ImageFont
from dateutil.rrule import rrulestr
from astrbot.core.star import Star, Context, StarTools
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event.filter import event_message_type, EventMessageType
from astrbot.api.message_components import At
from astrbot.core.utils.io import download_file
from pathlib import Path


class Main(Star):
    """课程表插件"""
    # --- Group Schedule Image Styles ---
    GS_BG_COLOR = "#FFFFFF"
    GS_FONT_COLOR = "#333333"
    GS_TITLE_COLOR = "#000000"
    GS_SUBTITLE_COLOR = "#888888"
    GS_STATUS_COLORS = {
        "进行中": ("#D32F2F", "#FFFFFF"),
        "下一节": ("#1976D2", "#FFFFFF"),
        "已结束": ("#388E3C", "#FFFFFF"),
        "无课程": ("#757575", "#FFFFFF"),
    }
    GS_AVATAR_SIZE = 80
    GS_ROW_HEIGHT = 120
    GS_PADDING = 40
    GS_WIDTH = 800

    # --- User Schedule Image Styles ---
    US_BG_COLOR = "#FFFFFF"
    US_FONT_COLOR = "#333333"
    US_TITLE_COLOR = "#000000"
    US_SUBTITLE_COLOR = "#888888"
    US_COURSE_BG_COLOR = "#E3F2FD"
    US_ROW_HEIGHT = 100
    US_PADDING = 40
    US_WIDTH = 800

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self.context = context

        # StarTools 是一个独立的工具集，应该直接通过类名调用
        self.data_path: Path = StarTools.get_data_dir()
        self.ics_path: Path = self.data_path / "ics"
        self.user_data_file: Path = self.data_path / "userdata.json"

        self._init_data()
        self.user_data = self._load_user_data()
        self.binding_requests: Dict[str, Dict] = {}
        self.course_cache: Dict[str, List[Dict]] = {}
        self.reminders: Dict[str, bool] = {}  # 新增：用于存储提醒状态
        # 启动后台提醒任务
        asyncio.create_task(self._reminder_task())

    @filter.command("绑定课表")
    async def bind_schedule(self, event: AstrMessageEvent):
        """绑定课表"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("请在群聊中使用此指令。")
            return

        user_id = event.get_sender_id()
        nickname = event.get_sender_name()

        # 记录绑定请求
        request_key = f"{group_id}-{user_id}"
        self.binding_requests[request_key] = {
            "timestamp": time.time(),
            "group_id": group_id,
            "user_id": user_id,
            "nickname": nickname
        }

        yield event.plain_result("请在60秒内，在本群内直接发送你的 .ics 文件。")

    @event_message_type(EventMessageType.GROUP_MESSAGE)
    async def handle_file_message(self, event: AstrMessageEvent):
        """处理文件消息，检查是否为课表绑定请求"""
        # 只处理群消息
        group_id = event.get_group_id()
        if not group_id:
            return

        user_id = event.get_sender_id()
        request_key = f"{group_id}-{user_id}"

        # 检查是否有绑定请求
        if request_key not in self.binding_requests:
            return

        request = self.binding_requests[request_key]

        # 检查是否超时（60秒）
        if time.time() - request["timestamp"] > 60:
            del self.binding_requests[request_key]
            return

        # 获取消息链中的文件组件
        messages = event.get_messages()
        file_component = None

        for message in messages:
            if hasattr(message, "type") and message.type == "File":
                file_component = message
                break

        if not file_component:
            return


        nickname = request.get("nickname", user_id)
        ics_file_path = self.ics_path / f"{user_id}_{group_id}.ics"

        try:
            # 使用File组件的异步方法获取文件
            file_path = await file_component.get_file(allow_return_url=True)
            logger.info(f"File component returned path: {file_path}")

            # 检查返回的是字符串路径还是BytesIO对象
            if isinstance(file_path, str):
                if file_path.startswith("http"):
                    # 如果返回的是URL，下载文件
                    logger.info(f"Downloading file from URL: {file_path}")
                    await download_file(file_path, ics_file_path)
                else:
                    # 如果返回的是本地文件路径，直接复制
                    logger.info(f"Copying file from local path: {file_path}")
                    shutil.copy2(file_path, ics_file_path)
            elif hasattr(file_path, "read"):
                # 如果返回的是文件对象（如BytesIO），直接写入
                logger.info("Writing file from file object")
                with open(ics_file_path, "wb") as f:
                    f.write(file_path.read())
            else:
                raise ValueError(f"Unsupported file path type: {type(file_path)}")
        except Exception as e:
            logger.error(f"获取文件信息失败: {e}")
            yield event.plain_result(f"无法获取文件信息，绑定失败。错误：{str(e)}")
            del self.binding_requests[request_key]
            return

        # 检查下载的文件是否存在
        if not os.path.exists(ics_file_path):
            logger.error(f"文件下载失败，文件不存在: {ics_file_path}")
            yield event.plain_result("文件下载失败，请重试。")
            del self.binding_requests[request_key]
            return
        logger.info(event.message_obj.raw_message) # 平台下发的原始消息在这里
        logger.info(f"文件下载成功，文件路径: {ics_file_path}")
        logger.info(f"文件大小: {os.path.getsize(ics_file_path)} bytes")

        # 保存用户数据
        if group_id not in self.user_data:
            self.user_data[group_id] = {"umo": event.unified_msg_origin, "users": {}}
        elif "umo" not in self.user_data[group_id]:
            self.user_data[group_id]["umo"] = event.unified_msg_origin

        self.user_data[group_id]["users"][user_id] = {
            "nickname": nickname,
            "reminder": self.user_data[group_id]["users"].get(user_id, {}).get("reminder", False)
        }

        self._save_user_data()

        # 清除该用户的课表缓存
        if str(ics_file_path) in self.course_cache:
            del self.course_cache[str(ics_file_path)]

        # 删除绑定请求
        del self.binding_requests[request_key]
        yield event.plain_result(f"课表绑定成功！群号：{group_id}")

    @filter.command("开启上课提醒")
    async def enable_reminders(self, event: AstrMessageEvent):
        """开启上课提醒"""
        group_id = event.get_group_id()
        user_id = event.get_sender_id()

        if (not group_id or group_id not in self.user_data or
                user_id not in self.user_data[group_id].get("users", {})):
            yield event.plain_result("你还没有绑定课表，无法开启提醒。")
            return

        self.user_data[group_id]["users"][user_id]["reminder"] = True
        self._save_user_data()
        yield event.plain_result("上课提醒已开启。")

    @filter.command("关闭上课提醒")
    async def disable_reminders(self, event: AstrMessageEvent):
        """关闭上课提醒"""
        group_id = event.get_group_id()
        user_id = event.get_sender_id()

        if (not group_id or group_id not in self.user_data or
                user_id not in self.user_data[group_id].get("users", {})):
            yield event.plain_result("你还没有绑定课表，无法关闭提醒。")
            return

        self.user_data[group_id]["users"][user_id]["reminder"] = False
        self._save_user_data()
        yield event.plain_result("上课提醒已关闭。")
    def _parse_ics_file(self, file_path: str) -> List[Dict]:
        """解析 .ics 文件并返回课程列表，包括重复事件。使用缓存以提高性能。"""
        # 检查缓存
        if file_path in self.course_cache:
            return self.course_cache[file_path]

        courses = []
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                cal_content = f.read()
        except (FileNotFoundError, IOError) as e:
            logger.error(f"无法读取 ICS 文件 {file_path}: {e}")
            return []

        cal = Calendar.from_ical(cal_content)
        shanghai_tz = timezone(timedelta(hours=8))
        today = datetime.now(shanghai_tz).date()

        for component in cal.walk():
            if component.name == "VEVENT":
                summary = component.get("summary")
                description = component.get("description")
                location = component.get("location")
                dtstart = component.get("dtstart").dt
                dtend = component.get("dtend").dt
                rrule_str = component.get("rrule")

                if isinstance(dtstart, date) and not isinstance(dtstart, datetime):
                    dtstart = datetime.combine(dtstart, dt_time.min)
                if isinstance(dtend, date) and not isinstance(dtend, datetime):
                    dtend = datetime.combine(dtend, dt_time.min)

                dtstart = dtstart.astimezone(shanghai_tz) if dtstart.tzinfo else dtstart.replace(tzinfo=shanghai_tz)
                dtend = dtend.astimezone(shanghai_tz) if dtend.tzinfo else dtend.replace(tzinfo=shanghai_tz)

                course_duration = dtend - dtstart

                if rrule_str:
                    if "UNTIL" in rrule_str:
                        until_dt = rrule_str["UNTIL"][0]
                        if isinstance(until_dt, date) and not isinstance(until_dt, datetime):
                            until_dt = datetime.combine(until_dt, dt_time.max)
                        if until_dt.tzinfo is None:
                            until_dt = until_dt.replace(tzinfo=shanghai_tz)
                        rrule_str["UNTIL"][0] = until_dt.astimezone(timezone.utc)

                    dtstart_utc = dtstart.astimezone(timezone.utc)
                    rrule = rrulestr(rrule_str.to_ical().decode(), dtstart=dtstart_utc)

                    start_of_today_utc = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                    future_limit_utc = start_of_today_utc + timedelta(days=365)

                    for occurrence_utc in rrule.between(start_of_today_utc, future_limit_utc, inc=True):
                        occurrence_local = occurrence_utc.astimezone(shanghai_tz)
                        courses.append({
                            "summary": summary, "description": description, "location": location,
                            "start_time": occurrence_local, "end_time": occurrence_local + course_duration
                        })
                else:
                    if dtstart.date() >= today:
                        courses.append({
                            "summary": summary, "description": description, "location": location,
                            "start_time": dtstart, "end_time": dtend
                        })

        # 存入缓存
        self.course_cache[file_path] = courses
        return courses

    @filter.command("查看课表")
    async def show_today_schedule(self, event: AstrMessageEvent):
        """查看今天还有什么课"""
        user_id = event.get_sender_id()
        group_id = event.get_group_id()

        if (not group_id or group_id not in self.user_data or
                user_id not in self.user_data[group_id].get("users", {})):
            yield event.plain_result(
                "你还没有在这个群绑定课表哦，请在群内发送 /绑定课表 指令，然后发送 .ics 文件来绑定。"
            )
            return

        ics_file_path = self.ics_path / f"{user_id}_{group_id}.ics"
        if not os.path.exists(ics_file_path):
            yield event.plain_result("课表文件不存在，可能已被删除。请重新绑定。")
            return

        courses = self._parse_ics_file(ics_file_path)
        today_courses = []
        # 使用上海时区 (UTC+8)
        shanghai_tz = timezone(timedelta(hours=8))
        now = datetime.now(shanghai_tz)

        for course in courses:
            if (course["start_time"].date() == now.date() and
                    course["start_time"] > now):
                today_courses.append(course)

        if not today_courses:
            yield event.plain_result("你今天没有课啦！")
            return

        # Sort courses by start time
        today_courses.sort(key=lambda x: x["start_time"])

        # Add user_id to each course for image generation
        for course in today_courses:
            course["nickname"] = self.user_data[group_id]["users"].get(user_id, {}).get("nickname", user_id)

        image_path = await self._generate_user_schedule_image(today_courses, event.get_sender_name())
        yield event.image_result(image_path)

    @filter.command("群友在上什么课")
    async def show_group_schedule(self, event: AstrMessageEvent):
        """查看群友接下来有什么课"""
        group_id = event.get_group_id()
        if not group_id or group_id not in self.user_data:
            yield event.plain_result("本群还没有人绑定课表哦。")
            return

        # 使用上海时区 (UTC+8)
        shanghai_tz = timezone(timedelta(hours=8))
        now = datetime.now(shanghai_tz)
        next_courses = []

        group_users = self.user_data[group_id].get("users", {})
        for user_id, user_info in group_users.items():
            nickname = user_info.get("nickname", user_id)
            ics_file_path = self.ics_path / f"{user_id}_{group_id}.ics"
            if not os.path.exists(ics_file_path):
                continue

            courses = self._parse_ics_file(ics_file_path)
            user_current_course = None
            user_next_course = None

            # 只筛选当天的课程进行判断
            today_courses = [c for c in courses if c.get("start_time") and c.get("start_time").date() == now.date()]

            for course in today_courses:
                start_time = course.get("start_time")
                end_time = course.get("end_time")

                if start_time and end_time:
                    # 检查是否是正在进行的课程
                    if start_time <= now < end_time:
                        user_current_course = course
                        break  # 找到正在上的课，就不需要再找下一节了

                    # 检查是否是未来的课程
                    elif start_time > now:
                        if user_next_course is None or start_time < user_next_course.get("start_time"):
                            user_next_course = course

            # 优先显示正在上的课
            display_course = user_current_course if user_current_course else user_next_course

            # 无论用户今天是否有课，都为他创建一个条目
            if display_course:
                # 用户有课
                user_course_copy = {
                    "summary": display_course["summary"],
                    "description": display_course["description"],
                    "location": display_course["location"],
                    "start_time": display_course["start_time"],
                    "end_time": display_course["end_time"],
                    "user_id": user_id,
                    "nickname": nickname
                }
            else:
                # 用户今天没课
                user_course_copy = {
                    "summary": "今日无课",
                    "description": "",
                    "location": "",
                    "start_time": None, # 标记为无课
                    "end_time": None,
                    "user_id": user_id,
                    "nickname": nickname
                }
            next_courses.append(user_course_copy)

        if not next_courses:
            yield event.plain_result("群友们接下来都没有课啦！")
            return

        # 排序时，将无课的用户（start_time is None）排在最后
        next_courses.sort(key=lambda x: (x["start_time"] is None, x["start_time"]))

        result_str = "接下来群友们的课程有：\n"
        for course in next_courses:
            result_str += f"\n用户: {course['nickname']}\n"
            result_str += f"课程名称: {course['summary']}\n"
            if course["start_time"]:
                result_str += (f"时间: {course['start_time'].strftime('%H:%M')} - "
                              f"{course['end_time'].strftime('%H:%M')}\n")
            if course["location"]:
                result_str += f"地点: {course['location']}\n"

        # Instead of sending plain text, we will generate and send an image.
        image_bytes = await self._generate_schedule_image(next_courses)
        yield event.image_result(image_bytes)

    async def _generate_schedule_image(self, courses: List[Dict]) -> str:
        """生成课程表图片并返回临时文件路径"""
        # --- 动态字体加载 ---
        font_path = self._find_font_file()
        try:
            font_main = ImageFont.truetype(font_path, 32) if font_path else ImageFont.load_default()
            font_sub = ImageFont.truetype(font_path, 24) if font_path else ImageFont.load_default()
            font_title = ImageFont.truetype(font_path, 48) if font_path else ImageFont.load_default()
        except IOError:
            logger.warning(f"无法加载字体文件: {font_path}，将使用默认字体。")
            font_main, font_sub, font_title = ImageFont.load_default(), ImageFont.load_default(), ImageFont.load_default()

        # --- 图像尺寸计算 ---
        height = self.GS_PADDING * 2 + 120 + len(courses) * self.GS_ROW_HEIGHT
        image = Image.new("RGB", (self.GS_WIDTH, height), self.GS_BG_COLOR)
        draw = ImageDraw.Draw(image)

        # --- 绘制标题 ---
        draw.rectangle([self.GS_PADDING, self.GS_PADDING, self.GS_PADDING + 20, self.GS_PADDING + 60], fill="#26A69A")
        draw.text((self.GS_PADDING + 40, self.GS_PADDING), "“群友在上什么课?”", font=font_title, fill=self.GS_TITLE_COLOR)
        draw.rectangle([self.GS_PADDING + 40, self.GS_PADDING + 70, self.GS_PADDING + 40 + 300, self.GS_PADDING + 75], fill="#A7FFEB")

        # --- 获取头像 ---
        async def fetch_avatar(session, user_id):
            avatar_url = f"http://q.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=640&img_type=jpg"
            try:
                async with session.get(avatar_url) as response:
                    if response.status == 200:
                        return await response.read()
            except Exception as e:
                logger.error(f"Failed to download avatar for {user_id}: {e}")
            return None

        async with aiohttp.ClientSession() as session:
            tasks = [fetch_avatar(session, course.get("user_id", "N/A")) for course in courses]
            avatar_datas = await asyncio.gather(*tasks)

        # --- 绘制每一行 ---
        y_offset = self.GS_PADDING + 120
        now = datetime.now(timezone(timedelta(hours=8)))

        for i, course in enumerate(courses):
            user_id = course.get("user_id", "N/A")
            nickname = course.get("nickname", user_id)
            summary = course.get("summary", "无课程信息")
            start_time = course.get("start_time")
            end_time = course.get("end_time")

            # --- 绘制头像 ---
            avatar_data = avatar_datas[i]
            if avatar_data:
                avatar = Image.open(BytesIO(avatar_data)).convert("RGBA")
                avatar = avatar.resize((self.GS_AVATAR_SIZE, self.GS_AVATAR_SIZE))

                # 创建圆形遮罩
                mask = Image.new("L", (self.GS_AVATAR_SIZE, self.GS_AVATAR_SIZE), 0)
                mask_draw = ImageDraw.Draw(mask)
                mask_draw.ellipse((0, 0, self.GS_AVATAR_SIZE, self.GS_AVATAR_SIZE), fill=255)

                image.paste(avatar, (self.GS_PADDING, y_offset + (self.GS_ROW_HEIGHT - self.GS_AVATAR_SIZE) // 2), mask)

            # --- 绘制箭头 ---
            arrow_x = self.GS_PADDING + self.GS_AVATAR_SIZE + 20
            arrow_y = y_offset + self.GS_ROW_HEIGHT // 2
            arrow_points = [
                (arrow_x, arrow_y - 20),
                (arrow_x + 30, arrow_y),
                (arrow_x, arrow_y + 20)
            ]
            draw.polygon(arrow_points, fill="#BDBDBD")

            # --- 状态判断和绘制 ---
            status_text = ""
            detail_text = ""

            if start_time and end_time:
                if start_time <= now < end_time:
                    status_text = "进行中"
                    remaining_minutes = (end_time - now).seconds // 60
                    if remaining_minutes > 60:
                        detail_text = f"剩余 {remaining_minutes // 60} 小时 {remaining_minutes % 60} 分钟"
                    else:
                        detail_text = f"剩余 {remaining_minutes} 分钟"
                elif now < start_time:
                    status_text = "下一节"
                    delta_minutes = (start_time - now).seconds // 60
                    if delta_minutes > 60:
                        detail_text = f"{delta_minutes // 60} 小时 {delta_minutes % 60} 分钟后"
                    else:
                        detail_text = f"{delta_minutes} 分钟后"
                else:
                    status_text = "已结束"
                    detail_text = "今日所有课程已结束"
            else:
                status_text = "已结束"
                detail_text = "今日所有课程已结束"

            # --- 绘制文本 ---
            text_x = arrow_x + 50
            draw.text((text_x, y_offset + 15), str(nickname), font=font_main, fill=self.GS_FONT_COLOR)

            status_bg, status_fg = self.GS_STATUS_COLORS.get(status_text, ("#000000", "#FFFFFF"))
            draw.rectangle([text_x, y_offset + 60, text_x + 100, y_offset + 95], fill=status_bg)
            draw.text((text_x + 10, y_offset + 65), status_text, font=font_sub, fill=status_fg)

            draw.text((text_x + 120, y_offset + 65), summary, font=font_sub, fill=self.GS_FONT_COLOR)
            if start_time and end_time:
                time_str = f"{start_time.strftime('%H:%M')}-{end_time.strftime('%H:%M')}"
                draw.text((text_x + 120, y_offset + 95), f"{time_str} ({detail_text})", font=font_sub, fill=self.GS_SUBTITLE_COLOR)
            else:
                 draw.text((text_x + 120, y_offset + 95), detail_text, font=font_sub, fill=self.GS_SUBTITLE_COLOR)


            y_offset += self.GS_ROW_HEIGHT

        # --- 保存到临时文件 ---
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        temp_path = temp_file.name
        image.save(temp_path, format="PNG")
        temp_file.close()

        return temp_path

    async def _generate_user_schedule_image(self, courses: List[Dict], nickname: str) -> str:
        """为单个用户生成今日课程表图片"""
        # --- 动态字体加载 ---
        font_path = self._find_font_file()
        try:
            font_main = ImageFont.truetype(font_path, 28) if font_path else ImageFont.load_default()
            font_sub = ImageFont.truetype(font_path, 22) if font_path else ImageFont.load_default()
            font_title = ImageFont.truetype(font_path, 40) if font_path else ImageFont.load_default()
        except IOError:
            logger.warning(f"无法加载字体文件: {font_path}，将使用默认字体。")
            font_main, font_sub, font_title = ImageFont.load_default(), ImageFont.load_default(), ImageFont.load_default()

        # --- 图像尺寸计算 ---
        height = self.US_PADDING * 2 + 100 + len(courses) * self.US_ROW_HEIGHT
        image = Image.new("RGB", (self.US_WIDTH, height), self.US_BG_COLOR)
        draw = ImageDraw.Draw(image)

        # --- 绘制标题 ---
        draw.text((self.US_PADDING, self.US_PADDING), f"{nickname}的今日课程", font=font_title, fill=self.US_TITLE_COLOR)

        # --- 绘制课程 ---
        y_offset = self.US_PADDING + 100

        for course in courses:
            summary = course.get("summary", "无课程信息")
            start_time = course.get("start_time")
            end_time = course.get("end_time")
            location = course.get("location", "未知地点")

            # 绘制圆角矩形背景
            self._draw_rounded_rectangle(draw, [self.US_PADDING, y_offset, self.US_WIDTH - self.US_PADDING, y_offset + self.US_ROW_HEIGHT - 10], 10, fill=self.US_COURSE_BG_COLOR)

            # 绘制时间
            time_str = f"{start_time.strftime('%H:%M')} - {end_time.strftime('%H:%M')}"
            draw.text((self.US_PADDING + 20, y_offset + 15), time_str, font=font_main, fill=self.US_TITLE_COLOR)

            # 绘制课程名称和地点
            draw.text((self.US_PADDING + 20, y_offset + 55), f"{summary} @ {location}", font=font_sub, fill=self.US_FONT_COLOR)

            y_offset += self.US_ROW_HEIGHT

        # --- 绘制页脚 ---
        footer_text = f"生成时间: {datetime.now().strftime('%Y/%m/%d %H:%M:%S')}"
        draw.text((self.US_PADDING, height - self.US_PADDING), footer_text, font=font_sub, fill=self.US_SUBTITLE_COLOR)

        # --- 保存到临时文件 ---
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        temp_path = temp_file.name
        image.save(temp_path, format="PNG")
        temp_file.close()

        return temp_path

    def _draw_rounded_rectangle(self, draw, xy, radius, fill):
        """手动绘制圆角矩形"""
        x1, y1, x2, y2 = xy
        draw.rectangle([x1, y1 + radius, x2, y2 - radius], fill=fill)
        draw.rectangle([x1 + radius, y1, x2 - radius, y2], fill=fill)
        draw.pieslice([x1, y1, x1 + radius * 2, y1 + radius * 2], 180, 270, fill=fill)
        draw.pieslice([x2 - radius * 2, y1, x2, y1 + radius * 2], 270, 360, fill=fill)
        draw.pieslice([x1, y2 - radius * 2, x1 + radius * 2, y2], 90, 180, fill=fill)
        draw.pieslice([x2 - radius * 2, y2 - radius * 2, x2, y2], 0, 90, fill=fill)

    def _find_font_file(self) -> str:
        """在插件目录中查找第一个 .ttf 或 .otf 字体文件"""
        plugin_dir = os.path.dirname(__file__)
        for filename in os.listdir(plugin_dir):
            if filename.lower().endswith((".ttf", ".otf")):
                return os.path.join(plugin_dir, filename)
        return ""

    def _load_user_data(self) -> Dict:
        """加载用户数据"""
        try:
            with open(self.user_data_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def _save_user_data(self):
        """保存用户数据"""
        with open(self.user_data_file, "w", encoding="utf-8") as f:
            json.dump(self.user_data, f, ensure_ascii=False, indent=4)

    def _init_data(self):
        """初始化插件数据文件和目录"""
        self.data_path.mkdir(parents=True, exist_ok=True)
        self.ics_path.mkdir(exist_ok=True)
        if not self.user_data_file.exists():
            with open(self.user_data_file, "w", encoding="utf-8") as f:
                json.dump({}, f)

    async def terminate(self):
        logger.info("Course Schedule plugin terminated.")

    async def _reminder_task(self):
        """后台任务，用于检查并发送上课提醒"""
        self.sent_reminders = set()

        while True:
            try:
                shanghai_tz = timezone(timedelta(hours=8))
                now = datetime.now(shanghai_tz)

                for group_id, group_data in self.user_data.items():
                    umo = group_data.get("umo")
                    if not umo:
                        continue

                    for user_id, user_info in group_data.get("users", {}).items():
                        if not user_info.get("reminder"):
                            continue

                        ics_file_path = self.ics_path / f"{user_id}_{group_id}.ics"
                        if not os.path.exists(ics_file_path):
                            continue

                        courses = self._parse_ics_file(str(ics_file_path))
                        today_courses = [c for c in courses if c["start_time"].date() == now.date()]

                        for course in today_courses:
                            start_time = course["start_time"]
                            time_diff = (start_time - now).total_seconds()

                            # 检查是否在30分钟到31分钟之间
                            if 1800 <= time_diff < 1860:
                                reminder_key = (user_id, start_time.strftime("%Y-%m-%d %H:%M"))
                                if reminder_key in self.sent_reminders:
                                    continue

                                # --- 构建并发送消息 ---
                                course_name = course.get("summary", "未知课程")
                                location = course.get("location", "未知地点")
                                time_str = start_time.strftime("%H:%M")

                                message_chain = [
                                    At(user_id),
                                    f" 上课提醒！\n\n你接下来在 {time_str} 有一节课：\n"
                                    f"课程名称: {course_name}\n"
                                    f"上课地点: {location}\n\n"
                                    "请做好准备！"
                                ]

                                await self.context.send_message(umo, message_chain)
                                self.sent_reminders.add(reminder_key)
                                logger.info(f"Sent reminder to {user_id} in group {group_id} for course {course_name}")

            except Exception as e:
                logger.error(f"Error in reminder task: {e}")

            # 每60秒检查一次
            await asyncio.sleep(60)
