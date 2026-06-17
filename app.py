import html
import json
import mimetypes
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import TimeoutError, ThreadPoolExecutor, as_completed
from pathlib import Path
from tkinter import Canvas, END, IntVar, Menu, StringVar, Text, Tk, filedialog, messagebox
from tkinter import font as tkfont
from tkinter import ttk


API_BASE = "https://api.weixin.qq.com"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
OPENAI_MODEL_OPTIONS = ("gpt-4.1-mini", "gpt-4.1", "gpt-5-mini", "gpt-5")
APP_VERSION = "0.9.18"

UI_PALETTE = {
    "bg": "#f6f2ea",
    "panel": "#fffaf2",
    "panel_alt": "#eef8f5",
    "field": "#fffdf8",
    "header": "#0f766e",
    "header_dark": "#115e59",
    "text": "#1f2937",
    "muted": "#64748b",
    "border": "#e8dacb",
    "accent": "#0f766e",
    "accent_hover": "#0d9488",
    "accent_soft": "#dff5ef",
    "warning": "#f59e0b",
    "warning_soft": "#fff4d8",
    "danger": "#dc2626",
}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
IMAGE_PLACEHOLDER_PATTERN = re.compile(r"\[\[\s*(?:图片|图)\s*([0-9０-９一二三四五六七八九十]+)\s*]]")
APP_DIR = Path(os.getenv("APPDATA", str(Path.home()))) / "WechatAutoPublisher"
CONFIG_PATH = APP_DIR / "config.json"
DRAFTS_DIR = APP_DIR / "drafts"
PUBLIC_IP_ENDPOINTS = [
    "https://api.ipify.org",
    "https://icanhazip.com",
    "https://ident.me",
    "https://ifconfig.me/ip",
]
ARTICLE_TEMPLATES = {
    "日常分享": (
        "## 今天想和你聊聊\n"
        "这里写开场，可以是一件小事、一个观察，或者今天想分享的主题。\n\n"
        "> 可以放一句最核心的话，让读者先抓住重点。\n\n"
        "## 主要内容\n"
        "第一段写背景：为什么这件事值得聊。\n\n"
        "第二段写你的观点：可以结合经历、案例或数据。\n\n"
        "[[图片1]]\n\n"
        "### 最后想说\n"
        "这里写总结和行动建议，让读者读完知道下一步可以做什么。\n"
    ),
    "活动通知": (
        "## 活动亮点\n"
        "这里写活动最吸引人的地方，例如福利、嘉宾、主题或限时权益。\n\n"
        "- 活动时间：\n"
        "- 活动地点：\n"
        "- 适合人群：\n\n"
        "[[图片1]]\n\n"
        "## 参与方式\n"
        "这里写报名方式、注意事项和截止时间。\n\n"
        "> 重要提醒：名额有限，建议尽早报名。\n"
    ),
    "产品种草": (
        "## 为什么推荐它\n"
        "先写用户痛点：用户遇到了什么问题，为什么需要这个产品。\n\n"
        "## 体验亮点\n"
        "- 亮点一：\n"
        "- 亮点二：\n"
        "- 亮点三：\n\n"
        "[[图片1]]\n\n"
        "### 适合谁\n"
        "这里写适合的人群、使用场景和购买建议。\n\n"
        "### 小结\n"
        "用一两句话收束观点，也可以放优惠、原文链接或联系方式。\n"
    ),
    "知识干货": (
        "## 先说结论\n"
        "用一段话直接说明本文最重要的结论。\n\n"
        "## 背景问题\n"
        "解释为什么这个问题重要，读者通常会卡在哪里。\n\n"
        "## 解决方法\n"
        "- 第一步：\n"
        "- 第二步：\n"
        "- 第三步：\n\n"
        "[[图片1]]\n\n"
        "### 注意事项\n"
        "> 这里写容易踩坑的点，或者你希望读者特别注意的提醒。\n\n"
        "### 总结\n"
        "复盘关键点，并给出一个可以马上执行的小建议。\n"
    ),
}


class WechatApiError(RuntimeError):
    pass


def explain_wechat_error(exc):
    text = str(exc)
    seen_ip = extract_wechat_seen_ip(exc)
    explanations = {
        "40013": "AppID 可能填写错误。",
        "40125": "AppSecret 可能填写错误，或 AppSecret 已重置。",
        "40164": "微信接口看到的出口 IP 不在公众号后台 IP 白名单中。请优先复制“微信看到 IP”加入白名单。",
        "48001": "公众号未开通或没有对应接口权限。",
        "89503": "IP 白名单配置可能未生效，请稍等后重试。",
    }
    for code, message in explanations.items():
        if code in text:
            if code == "40164" and seen_ip:
                return f"{text}\n\n微信接口看到的 IP：{seen_ip}\n\n可能原因：{message}"
            return f"{text}\n\n可能原因：{message}"
    if "timed out" in text.lower() or "timeout" in text.lower():
        return f"{text}\n\n可能原因：网络超时，请检查当前网络后重试。"
    if "urlopen error" in text.lower():
        return f"{text}\n\n可能原因：网络无法连接微信接口，请检查网络、防火墙或代理。"
    return text


def extract_ips(text):
    return sorted(set(re.findall(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)", text)))


def extract_wechat_seen_ip(exc):
    text = str(exc)
    ips = extract_ips(text)
    if not ips:
        return ""
    return ips[0]


def normalize_image_number(value):
    value = str(value).strip()
    full_width = str.maketrans("０１２３４５６７８９", "0123456789")
    value = value.translate(full_width)
    if value.isdigit():
        return int(value)
    chinese_numbers = {
        "一": 1,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    if value in chinese_numbers:
        return chinese_numbers[value]
    if value.startswith("十") and len(value) == 2 and value[1] in chinese_numbers:
        return 10 + chinese_numbers[value[1]]
    if value.endswith("十") and len(value) == 2 and value[0] in chinese_numbers:
        return chinese_numbers[value[0]] * 10
    if "十" in value:
        left, right = value.split("十", 1)
        if left in chinese_numbers and right in chinese_numbers:
            return chinese_numbers[left] * 10 + chinese_numbers[right]
    return 0


def canonical_image_placeholder(value):
    index = normalize_image_number(value)
    return f"[[图片{index}]]" if index > 0 else ""


def split_inline_placeholders(line):
    parts = []
    last = 0
    for match in IMAGE_PLACEHOLDER_PATTERN.finditer(line):
        before = line[last:match.start()].strip()
        if before:
            parts.append(before)
        placeholder = canonical_image_placeholder(match.group(1))
        if placeholder:
            parts.append(placeholder)
        last = match.end()
    after = line[last:].strip()
    if after:
        parts.append(after)
    return parts or [line]


def normalize_article_image_markers(text):
    normalized_lines = []
    single_marker = re.compile(
        r"^\s*[\[【(（]?\s*(?:第\s*)?([0-9０-９一二三四五六七八九十]+)\s*(?:张)?\s*(?:图|图片|配图|照片)\s*[\]】)）]?\s*$"
    )
    reverse_marker = re.compile(
        r"^\s*[\[【(（]?\s*(?:图|图片|配图|照片)\s*([0-9０-９一二三四五六七八九十]+)\s*[\]】)）]?\s*$"
    )
    for line in text.splitlines():
        if IMAGE_PLACEHOLDER_PATTERN.search(line):
            normalized_lines.extend(split_inline_placeholders(line))
            continue
        clean = line.strip()
        match = single_marker.fullmatch(clean) or reverse_marker.fullmatch(clean)
        if match:
            placeholder = canonical_image_placeholder(match.group(1))
            if placeholder:
                normalized_lines.append(placeholder)
                continue
        normalized_lines.append(line)
    return "\n".join(normalized_lines)


def load_config():
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(data):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def http_json(method, url, payload=None, timeout=30):
    body = None
    headers = {}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        text = response.read().decode("utf-8")

    data = json.loads(text)
    if data.get("errcode") not in (None, 0):
        raise WechatApiError(f"{data.get('errcode')}: {data.get('errmsg')}")
    return data


def extract_openai_text(data):
    if data.get("output_text"):
        return data["output_text"].strip()

    chunks = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if isinstance(content, dict):
                text = content.get("text")
                if text:
                    chunks.append(text)
    return "\n".join(chunks).strip()


def openai_generate_article(api_key, model, user_prompt, timeout=90):
    payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": (
                    "你是一名专业微信公众号编辑。请输出适合直接导入公众号排版软件的中文文章。"
                    "格式必须包含三部分：标题、摘要、正文。正文使用 Markdown 风格，"
                    "可以使用 ## 小标题、### 小标题、> 引用、- 列表、[[图片1]] 图片占位符。"
                    "整体风格要干净、自然、像人工编辑，不要堆叠表情符号，不要使用花哨分隔线，"
                    "不要频繁使用口号式短句或过多文本框提示。不要输出解释、不要输出代码块。"
                ),
            },
            {"role": "user", "content": user_prompt},
        ],
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        OPENAI_RESPONSES_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"AI 写作请求失败：HTTP {exc.code}\n{detail}") from exc

    text = extract_openai_text(data)
    if not text:
        raise RuntimeError("AI 已返回结果，但没有解析到正文文本。")
    return text


def fetch_public_ip(endpoint, timeout):
    request = urllib.request.Request(endpoint, headers={"User-Agent": "WechatAutoPublisher/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        ip = response.read().decode("utf-8").strip()
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip):
        return ip
    raise RuntimeError(f"{endpoint} 返回了无效 IP：{ip}")


def get_public_ip(timeout=3):
    errors = []
    with ThreadPoolExecutor(max_workers=len(PUBLIC_IP_ENDPOINTS)) as executor:
        futures = {
            executor.submit(fetch_public_ip, endpoint, timeout): endpoint
            for endpoint in PUBLIC_IP_ENDPOINTS
        }
        try:
            for future in as_completed(futures, timeout=timeout + 1):
                try:
                    return future.result()
                except Exception as exc:
                    errors.append(f"{futures[future]}: {exc}")
        except TimeoutError:
            errors.append("所有 IP 服务响应超时")
    raise RuntimeError("无法检测公网 IP。\n" + "\n".join(errors[-3:]))


def read_image_size(path):
    data = Path(path).read_bytes()[:64]
    if len(data) < 10:
        return None

    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")

    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")

    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        full = Path(path).read_bytes()
        vp8x = full.find(b"VP8X")
        if vp8x >= 0 and len(full) >= vp8x + 18:
            width = 1 + int.from_bytes(full[vp8x + 12:vp8x + 15], "little")
            height = 1 + int.from_bytes(full[vp8x + 15:vp8x + 18], "little")
            return width, height
        vp8 = full.find(b"VP8 ")
        if vp8 >= 0 and len(full) >= vp8 + 30:
            return int.from_bytes(full[vp8 + 26:vp8 + 28], "little") & 0x3FFF, int.from_bytes(full[vp8 + 28:vp8 + 30], "little") & 0x3FFF
        vp8l = full.find(b"VP8L")
        if vp8l >= 0 and len(full) >= vp8l + 10:
            b0, b1, b2, b3 = full[vp8l + 5:vp8l + 9]
            width = 1 + (((b1 & 0x3F) << 8) | b0)
            height = 1 + (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6))
            return width, height

    if data.startswith(b"\xff\xd8"):
        full = Path(path).read_bytes()
        index = 2
        while index + 9 < len(full):
            if full[index] != 0xFF:
                index += 1
                continue
            marker = full[index + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                height = int.from_bytes(full[index + 5:index + 7], "big")
                width = int.from_bytes(full[index + 7:index + 9], "big")
                return width, height
            segment_length = int.from_bytes(full[index + 2:index + 4], "big")
            index += 2 + segment_length
    return None


def build_multipart_file(field_name, file_path):
    boundary = f"----wechat-publisher-{int(time.time() * 1000)}"
    path = Path(file_path)
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    file_bytes = path.read_bytes()

    parts = [
        f"--{boundary}\r\n".encode("utf-8"),
        (
            f'Content-Disposition: form-data; name="{field_name}"; '
            f'filename="{path.name}"\r\n'
        ).encode("utf-8"),
        f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"),
        file_bytes,
        f"\r\n--{boundary}--\r\n".encode("utf-8"),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def http_upload(url, file_path, timeout=60):
    body, content_type = build_multipart_file("media", file_path)
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": content_type},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        text = response.read().decode("utf-8")

    data = json.loads(text)
    if data.get("errcode") not in (None, 0):
        raise WechatApiError(f"{data.get('errcode')}: {data.get('errmsg')}")
    return data


class WechatPublisher:
    def __init__(self):
        self.access_token = None
        self.expires_at = 0
        self.token_appid = ""
        self.token_secret = ""

    def get_access_token(self, appid, secret):
        now = time.time()
        if (
            self.access_token
            and self.token_appid == appid
            and self.token_secret == secret
            and now < self.expires_at - 120
        ):
            return self.access_token

        params = urllib.parse.urlencode(
            {
                "grant_type": "client_credential",
                "appid": appid,
                "secret": secret,
            }
        )
        data = http_json("GET", f"{API_BASE}/cgi-bin/token?{params}")
        self.access_token = data["access_token"]
        self.token_appid = appid
        self.token_secret = secret
        self.expires_at = now + int(data.get("expires_in", 7200))
        return self.access_token

    def upload_cover(self, appid, secret, cover_path):
        token = self.get_access_token(appid, secret)
        params = urllib.parse.urlencode({"access_token": token, "type": "image"})
        data = http_upload(f"{API_BASE}/cgi-bin/material/add_material?{params}", cover_path)
        return data["media_id"]

    def upload_article_image(self, appid, secret, image_path):
        token = self.get_access_token(appid, secret)
        params = urllib.parse.urlencode({"access_token": token})
        data = http_upload(f"{API_BASE}/cgi-bin/media/uploadimg?{params}", image_path)
        return data["url"]

    def create_draft(self, appid, secret, article):
        token = self.get_access_token(appid, secret)
        params = urllib.parse.urlencode({"access_token": token})
        data = http_json("POST", f"{API_BASE}/cgi-bin/draft/add?{params}", {"articles": [article]})
        return data["media_id"]

    def publish_draft(self, appid, secret, media_id):
        token = self.get_access_token(appid, secret)
        params = urllib.parse.urlencode({"access_token": token})
        return http_json("POST", f"{API_BASE}/cgi-bin/freepublish/submit?{params}", {"media_id": media_id})


class AutoFormatter:
    WRAPPER_STYLE = (
        "max-width:677px;margin:0 auto;color:#222;font-size:16px;"
        "line-height:1.85;letter-spacing:0;font-family:-apple-system,"
        "BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;"
    )
    EMOJI_STYLES = {
        "温暖": {
            "h2": "&#127775;",
            "h3": "&#127804;",
            "quote": "&#128172;",
            "list": "&#10024;",
            "important": "&#128161;",
            "time": "&#9200;",
            "success": "&#9989;",
            "money": "&#128176;",
            "default": "",
        },
        "活泼": {
            "h2": "&#128640;",
            "h3": "&#127881;",
            "quote": "&#128173;",
            "list": "&#128073;",
            "important": "&#128293;",
            "time": "&#9203;",
            "success": "&#127919;",
            "money": "&#128184;",
            "default": "&#10024;",
        },
        "商务": {
            "h2": "&#128200;",
            "h3": "&#128313;",
            "quote": "&#128221;",
            "list": "&#8226;",
            "important": "&#128161;",
            "time": "&#128197;",
            "success": "&#10004;",
            "money": "&#128202;",
            "default": "",
        },
    }
    THEMES = {
        "清新蓝": {
            "accent": "#2f80ed",
            "accent_soft": "#eaf3ff",
            "accent_mid": "#d8eaff",
            "text": "#1f2937",
            "muted": "#6b7280",
            "border": "#b8d7ff",
            "card": "#f7fbff",
        },
        "暖橙": {
            "accent": "#f97316",
            "accent_soft": "#fff3e8",
            "accent_mid": "#ffe4c7",
            "text": "#2b2118",
            "muted": "#7a6758",
            "border": "#ffd0a3",
            "card": "#fffaf5",
        },
        "商务灰": {
            "accent": "#4b5563",
            "accent_soft": "#f3f4f6",
            "accent_mid": "#e5e7eb",
            "text": "#1f2937",
            "muted": "#6b7280",
            "border": "#d1d5db",
            "card": "#fafafa",
        },
    }

    def __init__(self, title, subtitle="", auto_emoji=True, emoji_style="温暖", layout_theme="清新蓝", layout_style="公众号图文"):
        self.title = title.strip()
        self.subtitle = subtitle.strip()
        self.auto_emoji = auto_emoji
        self.emoji_style = emoji_style if emoji_style in self.EMOJI_STYLES else "温暖"
        self.layout_theme = layout_theme if layout_theme in self.THEMES else "清新蓝"
        self.layout_style = layout_style
        self.theme = dict(self.THEMES[self.layout_theme])
        if self.layout_style == "简约线条":
            self.theme["accent_soft"] = "#ffffff"
            self.theme["card"] = "#ffffff"
        elif self.layout_style == "轻卡片":
            self.theme["card"] = self.theme["accent_soft"]
        self.paragraph_count = 0
        self.section_count = 0
        self.subsection_count = 0
        self.list_count = 0
        self.normal_paragraph_count = 0

    def is_wechat_style(self):
        return self.layout_style == "公众号图文"

    def render(self, source_text, image_urls=None, local_preview=False):
        source_text = normalize_article_image_markers(source_text)
        image_urls = image_urls or {}
        blocks = [self.header()]
        for raw_line in source_text.splitlines():
            line = raw_line.strip()
            if not line:
                blocks.append('<p style="height:10px;margin:0;"></p>')
                continue
            blocks.append(self.render_line(line, image_urls, local_preview))
        blocks.append(self.footer())
        return f'<section style="{self.WRAPPER_STYLE}">' + "\n".join(blocks) + "</section>"

    def inline_html(self, text):
        escaped = html.escape(text)
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
        escaped = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", escaped)
        return escaped

    def header(self):
        if self.is_wechat_style():
            if not self.subtitle:
                return ""
            subtitle = html.escape(self.subtitle)
            return (
                f'<section style="margin:0 0 22px;padding:14px 16px;border-radius:8px;'
                f'background:{self.theme["accent_soft"]};border-left:4px solid {self.theme["accent"]};">'
                f'<p style="margin:0;color:{self.theme["text"]};font-size:15px;line-height:1.85;">'
                f'{subtitle}</p></section>'
            )

        title = html.escape(self.title or "未命名文章")
        subtitle = html.escape(self.subtitle)
        subtitle_html = ""
        if subtitle:
            subtitle_html = (
                '<p style="margin:8px 0 22px;color:#888;font-size:14px;">'
                f"{subtitle}</p>"
            )
        return (
            f'<section style="margin:0 0 28px;padding:24px 18px 22px;border-radius:12px;'
            f'background:{self.theme["accent_soft"]};'
            f'border:1px solid {self.theme["border"]};">'
            f'<h1 style="font-size:26px;line-height:1.38;margin:0;text-align:center;'
            f'font-weight:800;color:#111;">{title}</h1>'
            f'<section style="width:46px;height:3px;border-radius:99px;background:{self.theme["accent"]};'
            f'margin:16px auto 0;"></section>{subtitle_html}</section>'
        )

    def emoji(self, name):
        if self.is_wechat_style():
            return ""
        if not self.auto_emoji:
            return ""
        value = self.EMOJI_STYLES[self.emoji_style].get(name, "")
        return f"{value} " if value else ""

    def paragraph_emoji(self, line):
        if self.is_wechat_style():
            return ""
        if not self.auto_emoji:
            return ""
        checks = [
            (("重点", "重要", "注意", "提醒", "关键"), "important"),
            (("时间", "今天", "明天", "本周", "截止", "安排"), "time"),
            (("完成", "成功", "结果", "收获", "达成"), "success"),
            (("价格", "成本", "收益", "预算", "优惠", "费用"), "money"),
        ]
        for keywords, emoji_name in checks:
            if any(keyword in line for keyword in keywords):
                return self.emoji(emoji_name)
        self.paragraph_count += 1
        if self.emoji_style == "活泼" and self.paragraph_count % 4 == 1:
            return self.emoji("default")
        return ""

    def footer(self):
        if self.is_wechat_style():
            return ""
        return (
            f'<section style="margin:32px 0 0;padding:14px 0;border-top:1px solid {self.theme["accent_mid"]};'
            f'color:{self.theme["muted"]};font-size:13px;text-align:center;">'
            "本文由公众号自动发布助手排版</section>"
        )

    def label_box(self, label, text, emoji_name="important"):
        if self.is_wechat_style():
            return (
                f'<section style="margin:20px 0;padding:14px 16px;border-radius:8px;'
                f'background:{self.theme["accent_soft"]};border-left:4px solid {self.theme["accent"]};">'
                f'<p style="margin:0;color:{self.theme["text"]};font-size:15px;line-height:1.95;">'
                f'{html.escape(text)}</p></section>'
            )
        return (
            f'<section style="margin:20px 0;padding:14px 16px;border-radius:10px;'
            f'background:{self.theme["card"]};border:1px solid {self.theme["border"]};'
            f'border-left:4px solid {self.theme["accent"]};">'
            f'<p style="margin:0 0 8px;color:{self.theme["accent"]};font-size:13px;font-weight:800;">'
            f'{self.emoji(emoji_name)}{label}</p>'
            f'<p style="margin:0;color:{self.theme["text"]};font-weight:600;line-height:1.85;">'
            f'{html.escape(text)}</p></section>'
        )

    def summary_box(self, text):
        if self.is_wechat_style():
            return (
                f'<section style="margin:26px 0;padding:16px 0;border-top:1px solid {self.theme["accent_mid"]};'
                f'border-bottom:1px solid {self.theme["accent_mid"]};">'
                f'<p style="margin:0;color:{self.theme["text"]};font-size:15px;line-height:1.95;">'
                f'{html.escape(text)}</p></section>'
            )
        return (
            f'<section style="margin:24px 0;padding:18px 16px;border-radius:10px;'
            f'background:{self.theme["card"]};'
            f'border:1px solid {self.theme["border"]};">'
            f'<p style="margin:0 0 10px;color:{self.theme["accent"]};font-weight:800;'
            f'font-size:16px;text-align:center;">{self.emoji("success")}小结</p>'
            f'<section style="width:34px;height:2px;background:{self.theme["accent"]};margin:0 auto 12px;"></section>'
            f'<p style="margin:0;color:{self.theme["text"]};line-height:1.9;">{html.escape(text)}</p></section>'
        )

    def render_line(self, line, image_urls, local_preview):
        image_match = re.fullmatch(r"\[\[图片(\d+)]]", line)
        if image_match:
            index = int(image_match.group(1))
            url = image_urls.get(index)
            if not url:
                caption = f"图片{index} 将在发布前上传"
                return (
                    f'<section style="margin:18px 0;padding:22px;border:1px dashed {self.theme["border"]};'
                    f'color:{self.theme["muted"]};text-align:center;border-radius:8px;'
                    f'background:{self.theme["card"]};">{caption}</section>'
                )
            caption_html = ""
            if not self.is_wechat_style():
                caption_html = (
                    f'<p style="margin:8px 0 0;color:{self.theme["muted"]};font-size:12px;'
                    f'text-align:center;">图 {index:02d}</p>'
                )
            return (
                f'<section style="margin:22px 0;'
                f'padding:{0 if self.is_wechat_style() else 8}px;'
                f'border-radius:{0 if self.is_wechat_style() else 10}px;'
                f'background:{self.theme["card"]};'
                f'border:{0 if self.is_wechat_style() else 1}px solid {self.theme["border"]};">'
                f'<img src="{html.escape(url)}" style="display:block;max-width:100%;'
                f'border-radius:8px;margin:0 auto;" />'
                f'{caption_html}</section>'
            )

        if line == "---":
            return (
                f'<section style="margin:28px 0;height:1px;background:{self.theme["accent_mid"]};"></section>'
            )

        if line.startswith("### "):
            self.subsection_count += 1
            text = html.escape(line[4:].strip())
            if self.is_wechat_style():
                return (
                    f'<section style="margin:26px 0 10px;">'
                    f'<p style="margin:0;color:{self.theme["text"]};font-size:17px;'
                    f'font-weight:700;line-height:1.6;">'
                    f'{text}</p></section>'
                )
            return (
                f'<section style="margin:26px 0 12px;padding:0 0 8px;border-bottom:1px solid {self.theme["accent_mid"]};">'
                f'<h3 style="font-size:18px;line-height:1.55;margin:0;font-weight:700;'
                f'color:{self.theme["text"]};">{self.emoji("h3")}{text}</h3></section>'
            )

        if line.startswith("## "):
            self.section_count += 1
            self.subsection_count = 0
            text = html.escape(line[3:].strip())
            if self.is_wechat_style():
                return (
                    f'<section style="margin:34px 0 16px;padding:0 0 0 12px;'
                    f'border-left:4px solid {self.theme["accent"]};">'
                    f'<h2 style="font-size:20px;line-height:1.5;margin:0;font-weight:800;'
                    f'color:{self.theme["text"]};">{text}</h2></section>'
                )
            return (
                f'<section style="margin:34px 0 16px;padding:0 0 0 12px;'
                f'border-left:5px solid {self.theme["accent"]};">'
                f'<h2 style="font-size:21px;line-height:1.45;margin:0;font-weight:800;'
                f'color:{self.theme["text"]};">{self.emoji("h2")}{text}</h2></section>'
            )

        if line.startswith(">"):
            text = self.inline_html(line[1:].strip())
            if self.is_wechat_style():
                return (
                    f'<section style="margin:18px 0;padding:2px 0 2px 12px;'
                    f'border-left:3px solid {self.theme["accent_mid"]};">'
                    f'<p style="margin:0;color:{self.theme["muted"]};font-size:15px;line-height:1.95;">'
                    f'{text}</p></section>'
                )
            return (
                f'<section style="margin:18px 0;padding:14px 16px;border-radius:10px;'
                f'background:{self.theme["accent_soft"]};border:1px solid {self.theme["border"]};'
                f'color:{self.theme["text"]};">'
                f'<p style="margin:0;color:{self.theme["muted"]};font-size:13px;">{self.emoji("quote")}摘录</p>'
                f'<p style="margin:6px 0 0;line-height:1.9;">{text}</p></section>'
            )

        if re.match(r"^[-*]\s+", line):
            self.list_count += 1
            text = self.inline_html(re.sub(r"^[-*]\s+", "", line))
            if self.is_wechat_style():
                return (
                    f'<section style="margin:10px 0;padding:6px 0;">'
                    f'<p style="margin:0;color:{self.theme["text"]};font-size:15px;line-height:1.9;">'
                    f'<span style="color:{self.theme["accent"]};font-weight:800;margin-right:8px;">•</span>'
                    f'{text}</p></section>'
                )
            return (
                f'<section style="margin:10px 0;padding:11px 12px;border-radius:10px;'
                f'background:#ffffff;border:1px solid {self.theme["accent_mid"]};">'
                f'<p style="margin:0;color:{self.theme["text"]};">'
                f'<span style="display:inline-block;width:24px;height:24px;line-height:24px;'
                f'text-align:center;border-radius:6px;background:{self.theme["accent_soft"]};color:{self.theme["accent"]};'
                f'font-size:12px;font-weight:700;margin-right:8px;">{self.list_count}</span>{text}</p></section>'
            )

        if self.is_wechat_style():
            self.normal_paragraph_count += 1
            top_margin = 18 if self.normal_paragraph_count == 1 else 15
            return (
                f'<p style="margin:{top_margin}px 0;color:{self.theme["text"]};font-size:16px;'
                f'line-height:2.05;text-align:justify;">'
                f'{self.paragraph_emoji(line)}{self.inline_html(line)}</p>'
            )
        return (
            f'<p style="margin:14px 0;color:{self.theme["text"]};line-height:1.95;">'
            f'{self.paragraph_emoji(line)}{self.inline_html(line)}</p>'
        )


class PublisherApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"公众号自动发布助手 v{APP_VERSION}")
        self.root.geometry("1280x820")
        self.root.minsize(1040, 680)

        self.api = WechatPublisher()
        self.config = load_config()
        self.image_paths = []
        self.latest_html = ""

        self.appid = StringVar(value=self.config.get("appid", ""))
        self.secret = StringVar(value=self.config.get("appsecret", "") if self.config.get("remember_secret") else "")
        self.remember_secret = IntVar(value=int(self.config.get("remember_secret", 0)))
        self.current_ip = StringVar(value="未检测")
        self.last_ip = StringVar(value=self.config.get("last_public_ip") or "未记录")
        self.wechat_seen_ip = StringVar(value="未获取")
        self.title = StringVar(value="")
        self.author = StringVar(value=self.config.get("author", ""))
        self.digest = StringVar()
        self.source_url = StringVar()
        self.cover_path = StringVar()
        self.publish_now = IntVar(value=0)
        self.open_comment = IntVar(value=0)
        self.auto_emoji = IntVar(value=int(self.config.get("auto_emoji", 0)))
        self.auto_image_layout = IntVar(value=int(self.config.get("auto_image_layout", 1)))
        self.emoji_style = StringVar(value=self.config.get("emoji_style", "温暖"))
        self.layout_theme = StringVar(value=self.config.get("layout_theme", "清新蓝"))
        self.layout_style = StringVar(value=self.config.get("layout_style", "公众号图文"))
        self.template_name = StringVar(value=self.config.get("template_name", "日常分享"))
        saved_openai_key = self.config.get("openai_api_key", "") if self.config.get("remember_openai_key") else ""
        self.openai_key = StringVar(value=saved_openai_key or os.getenv("OPENAI_API_KEY", ""))
        self.remember_openai_key = IntVar(value=int(self.config.get("remember_openai_key", 0)))
        self.openai_model = StringVar(value=self.config.get("openai_model", OPENAI_MODEL_OPTIONS[0]))
        if self.openai_model.get() not in OPENAI_MODEL_OPTIONS:
            self.openai_model.set(OPENAI_MODEL_OPTIONS[0])
        self.ai_topic = StringVar(value=self.config.get("ai_topic", ""))
        self.ai_audience = StringVar(value=self.config.get("ai_audience", "公众号读者"))
        self.ai_tone = StringVar(value=self.config.get("ai_tone", "温暖自然"))
        self.ai_length = StringVar(value=self.config.get("ai_length", "中等篇幅"))
        self.ai_status = StringVar(value="AI 写作待命")
        self.connection_status = StringVar(value="未测试")
        self.progress_text = StringVar(value="进度：等待操作")
        self.ready_summary = StringVar(value="发布准备：正在检查...")
        self.publish_action_text = StringVar(value="")
        self.last_publish_result = StringVar(value="暂无发布结果")
        self.ready_refresh_job = None

        self.build_style()
        self.build_ui()
        self.install_change_watchers()
        self.update_publish_action_text()
        self.refresh_ready_summary()

    def build_style(self):
        palette = UI_PALETTE
        self.root.configure(bg=palette["bg"])
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TFrame", background=palette["bg"])
        style.configure("Panel.TFrame", background=palette["panel"], relief="flat")
        style.configure("Soft.TFrame", background=palette["panel_alt"], relief="flat")
        style.configure("Header.TFrame", background=palette["header"])
        style.configure("TLabel", background=palette["bg"], foreground=palette["text"])
        style.configure("Panel.TLabel", background=palette["panel"], foreground=palette["text"])
        style.configure("Soft.TLabel", background=palette["panel_alt"], foreground=palette["text"])
        style.configure("Muted.TLabel", background=palette["panel"], foreground=palette["muted"])
        style.configure("HeaderTitle.TLabel", background=palette["header"], foreground="#fffdf8", font=("Microsoft YaHei UI", 17, "bold"))
        style.configure("HeaderMuted.TLabel", background=palette["header"], foreground="#d7f3ec")
        style.configure("HeaderBadge.TLabel", background=palette["header_dark"], foreground="#fff7ed", padding=(10, 5), font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Title.TLabel", background=palette["panel"], foreground=palette["header_dark"], font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Section.TLabel", background=palette["panel"], foreground=palette["header_dark"], font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("TButton", padding=(11, 7), background="#fffdf8", foreground=palette["text"], bordercolor=palette["border"], lightcolor="#ffffff", darkcolor=palette["border"])
        style.map("TButton", background=[("active", palette["accent_soft"]), ("pressed", "#cdece4")], foreground=[("disabled", "#94a3b8")])
        style.configure("Primary.TButton", padding=(14, 8), background=palette["accent"], foreground="#ffffff", bordercolor=palette["accent"], font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Primary.TButton", background=[("active", palette["accent_hover"]), ("pressed", palette["header_dark"])], foreground=[("disabled", "#e2e8f0")])
        style.configure("Danger.TButton", padding=(12, 7), background="#fff7f7", foreground=palette["danger"], bordercolor="#fecaca")
        style.map("Danger.TButton", background=[("active", "#fee2e2"), ("pressed", "#fecaca")])
        style.configure("TCheckbutton", background=palette["panel"], foreground=palette["text"])
        style.map("TCheckbutton", background=[("active", palette["panel"])])
        style.configure("TEntry", fieldbackground=palette["field"], foreground=palette["text"], bordercolor=palette["border"], insertcolor=palette["accent"], padding=(6, 5))
        style.configure("TCombobox", fieldbackground=palette["field"], background=palette["field"], foreground=palette["text"], bordercolor=palette["border"], arrowcolor=palette["accent"], padding=(5, 4))
        style.map("TCombobox", fieldbackground=[("readonly", palette["field"])], selectbackground=[("readonly", palette["accent_soft"])], selectforeground=[("readonly", palette["text"])])
        style.configure("TNotebook", background=palette["panel"], borderwidth=0, tabmargins=(8, 8, 8, 0))
        style.configure("TNotebook.Tab", padding=(18, 9), background=palette["warning_soft"], foreground=palette["muted"], bordercolor=palette["border"], font=("Microsoft YaHei UI", 10, "bold"))
        style.map("TNotebook.Tab", background=[("selected", palette["accent_soft"]), ("active", "#fff8e8")], foreground=[("selected", palette["header_dark"]), ("active", palette["accent"])])
        style.configure("Treeview", background=palette["field"], fieldbackground=palette["field"], foreground=palette["text"], bordercolor=palette["border"], rowheight=28)
        style.configure("Treeview.Heading", background=palette["accent_soft"], foreground=palette["header_dark"], font=("Microsoft YaHei UI", 9, "bold"))
        style.map("Treeview", background=[("selected", palette["accent"])], foreground=[("selected", "#ffffff")])
        style.configure("Horizontal.TProgressbar", background=palette["warning"], troughcolor=palette["accent_soft"], bordercolor=palette["border"], lightcolor=palette["warning"], darkcolor=palette["warning"])

    def build_ui(self):
        shell = ttk.Frame(self.root, padding=14)
        shell.pack(fill="both", expand=True)

        header = ttk.Frame(shell, style="Header.TFrame", padding=(18, 16))
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text=f"公众号自动发布助手 v{APP_VERSION}", style="HeaderTitle.TLabel").pack(side="left")
        ttk.Label(
            header,
            text="账号配置 · 图文编辑 · 自动排版 · 草稿发布",
            style="HeaderMuted.TLabel",
        ).pack(side="left", padx=(16, 0))
        ttk.Label(header, text="智能排版工作台", style="HeaderBadge.TLabel").pack(side="right")

        ready_bar = ttk.Frame(shell, style="Soft.TFrame", padding=(12, 9))
        ready_bar.pack(fill="x", pady=(0, 12))
        ttk.Label(ready_bar, textvariable=self.ready_summary, style="Soft.TLabel").pack(side="left")
        ttk.Button(ready_bar, text="检查准备状态", command=self.show_publish_checklist).pack(side="right")
        ttk.Button(ready_bar, text="打开预览", command=self.preview_layout).pack(side="right", padx=(0, 8))

        body = ttk.Frame(shell)
        body.pack(fill="both", expand=True)

        left_outer = ttk.Frame(body, style="Panel.TFrame", padding=0, width=410)
        left_outer.pack(side="left", fill="y", padx=(0, 12))
        left_outer.pack_propagate(False)

        right = ttk.Frame(body, style="Panel.TFrame", padding=14)
        right.pack(side="left", fill="both", expand=True)

        self.build_sidebar(left_outer)
        self.build_editor_panel(right)

        status_bar = ttk.Frame(shell, style="Panel.TFrame", padding=(12, 8))
        status_bar.pack(fill="x", pady=(12, 0))
        self.status = StringVar(value="准备就绪。勾选“记住 AppSecret”后才会保存到本机。")
        ttk.Label(status_bar, textvariable=self.status, style="Panel.TLabel").pack(side="left")
        ttk.Button(status_bar, text="使用说明", command=self.open_readme).pack(side="right")
        ttk.Button(status_bar, text="打开软件目录", command=self.open_app_folder).pack(side="right", padx=(0, 8))
        ttk.Button(status_bar, text="打开草稿目录", command=self.open_drafts_folder).pack(side="right", padx=(0, 8))

    def scrollable_panel(self, parent):
        canvas = Canvas(parent, background=UI_PALETTE["panel"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        content = ttk.Frame(canvas, style="Panel.TFrame", padding=14)
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")

        def update_scroll_region(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def sync_width(event):
            canvas.itemconfigure(window_id, width=event.width)

        def on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        content.bind("<Configure>", update_scroll_region)
        canvas.bind("<Configure>", sync_width)
        canvas.bind("<Enter>", lambda _event: canvas.bind_all("<MouseWheel>", on_mousewheel))
        canvas.bind("<Leave>", lambda _event: canvas.unbind_all("<MouseWheel>"))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        return content

    def build_sidebar(self, parent):
        notebook = ttk.Notebook(parent)
        notebook.pack(fill="both", expand=True)

        account_tab = ttk.Frame(notebook, style="Panel.TFrame")
        article_tab = ttk.Frame(notebook, style="Panel.TFrame")
        material_tab = ttk.Frame(notebook, style="Panel.TFrame")
        ai_tab = ttk.Frame(notebook, style="Panel.TFrame")

        notebook.add(account_tab, text="账号")
        notebook.add(article_tab, text="文章")
        notebook.add(material_tab, text="素材")
        notebook.add(ai_tab, text="AI写作")

        self.build_account_panel(self.scrollable_panel(account_tab))
        self.build_article_panel(self.scrollable_panel(article_tab))
        self.build_material_panel(self.scrollable_panel(material_tab))
        self.build_ai_panel(self.scrollable_panel(ai_tab))

    def build_account_panel(self, parent):
        ttk.Label(parent, text="账号 API", style="Title.TLabel").pack(anchor="w", pady=(0, 6))
        ttk.Label(parent, text="填写公众号后台的开发者信息。", style="Muted.TLabel", wraplength=330).pack(anchor="w", pady=(0, 12))
        self.field(parent, "AppID", self.appid)
        self.field(parent, "AppSecret", self.secret, secret=True)
        secret_options = ttk.Frame(parent, style="Panel.TFrame")
        secret_options.pack(fill="x", pady=(0, 6))
        ttk.Checkbutton(secret_options, text="记住 AppSecret（仅保存在本机）", variable=self.remember_secret).pack(side="left")
        ttk.Button(secret_options, text="清除密钥", command=self.clear_saved_secret).pack(side="left", padx=(8, 0))

        actions = ttk.Frame(parent, style="Panel.TFrame")
        actions.pack(fill="x", pady=(6, 16))
        self.test_button = ttk.Button(actions, text="测试连接", command=self.test_connection)
        self.test_button.pack(side="left")
        ttk.Button(actions, text="保存常用项", command=self.save_common_fields).pack(side="left", padx=(8, 0))

        test_row = ttk.Frame(parent, style="Panel.TFrame")
        test_row.pack(fill="x", pady=(0, 8))
        ttk.Label(test_row, text="连接状态", style="Panel.TLabel", width=10).pack(side="left")
        ttk.Label(test_row, textvariable=self.connection_status, style="Panel.TLabel").pack(side="left")

        ip_box = ttk.Frame(parent, style="Panel.TFrame")
        ip_box.pack(fill="x", pady=(0, 12))
        ttk.Label(ip_box, text="当前公网 IP", style="Panel.TLabel", width=10).grid(row=0, column=0, sticky="w", pady=3)
        ttk.Label(ip_box, textvariable=self.current_ip, style="Panel.TLabel").grid(row=0, column=1, sticky="w", pady=3)
        ttk.Label(ip_box, text="上次记录 IP", style="Panel.TLabel", width=10).grid(row=1, column=0, sticky="w", pady=3)
        ttk.Label(ip_box, textvariable=self.last_ip, style="Panel.TLabel").grid(row=1, column=1, sticky="w", pady=3)
        ttk.Label(ip_box, text="微信看到 IP", style="Panel.TLabel", width=10).grid(row=2, column=0, sticky="w", pady=3)
        ttk.Label(ip_box, textvariable=self.wechat_seen_ip, style="Panel.TLabel").grid(row=2, column=1, sticky="w", pady=3)
        ip_buttons = ttk.Frame(parent, style="Panel.TFrame")
        ip_buttons.pack(fill="x", pady=(0, 8))
        self.detect_ip_button = ttk.Button(ip_buttons, text="检测 IP", command=self.detect_public_ip)
        self.detect_ip_button.pack(side="left")
        ttk.Button(ip_buttons, text="复制 IP", command=self.copy_public_ip).pack(side="left", padx=(8, 0))
        ttk.Button(ip_buttons, text="复制微信 IP", command=self.copy_wechat_seen_ip).pack(side="left", padx=(8, 0))

    def build_article_panel(self, parent):
        ttk.Label(parent, text="文章信息", style="Title.TLabel").pack(anchor="w", pady=(0, 6))
        ttk.Label(parent, text="标题和摘要会提交到公众号草稿。原文链接可留空。", style="Muted.TLabel", wraplength=330).pack(anchor="w", pady=(0, 12))
        self.field(parent, "标题", self.title)
        self.field(parent, "作者", self.author)
        self.field(parent, "摘要", self.digest)
        self.field(parent, "原文链接", self.source_url)

    def build_material_panel(self, parent):
        ttk.Label(parent, text="封面与正文图片", style="Title.TLabel").pack(anchor="w", pady=(0, 6))
        ttk.Label(parent, text="先选封面，再添加正文图片。最准确写法：单独一行 [[图片1]]、[[图片2]]；也支持“图片1放这里”。", style="Muted.TLabel", wraplength=330).pack(anchor="w", pady=(0, 12))

        cover_row = ttk.Frame(parent, style="Panel.TFrame")
        cover_row.pack(fill="x", pady=(0, 8))
        cover_entry = ttk.Entry(cover_row, textvariable=self.cover_path, width=32)
        cover_entry.pack(side="left", fill="x", expand=True)
        self.bind_text_menu(cover_entry)
        ttk.Button(cover_row, text="选择封面", command=self.choose_cover).pack(side="left", padx=(6, 0))

        image_buttons = ttk.Frame(parent, style="Panel.TFrame")
        image_buttons.pack(fill="x", pady=(4, 8))
        ttk.Button(image_buttons, text="添加图片", command=self.add_body_images).pack(side="left")
        ttk.Button(image_buttons, text="自动插图", command=self.auto_place_body_images).pack(side="left", padx=(8, 0))
        ttk.Button(image_buttons, text="移除", command=self.remove_selected_image).pack(side="left", padx=(8, 0))

        ttk.Checkbutton(
            parent,
            text="添加多张图片后自动穿插到正文段落",
            variable=self.auto_image_layout,
        ).pack(anchor="w", pady=(0, 8))

        self.image_list = ttk.Treeview(parent, columns=("placeholder", "path"), show="headings", height=8)
        self.image_list.heading("placeholder", text="占位符")
        self.image_list.heading("path", text="图片路径")
        self.image_list.column("placeholder", width=75, stretch=False)
        self.image_list.column("path", width=265)
        self.image_list.pack(fill="x")

        ttk.Label(
            parent,
            text="推荐把 [[图片1]] 单独放一行。也支持 [图片1]、[[图1]]、第1张图、图片1放这里等写法。",
            style="Muted.TLabel",
            wraplength=350,
        ).pack(anchor="w", pady=(8, 0))

    def build_ai_panel(self, parent):
        ttk.Label(parent, text="AI 写作", style="Title.TLabel").pack(anchor="w", pady=(0, 6))
        ttk.Label(parent, text="输入主题后自动生成标题、摘要和正文，也可以把 ChatGPT 复制的内容一键导入。默认生成干净正文。", style="Muted.TLabel", wraplength=330).pack(anchor="w", pady=(0, 12))

        self.field(parent, "OpenAI Key", self.openai_key, secret=True)
        secret_options = ttk.Frame(parent, style="Panel.TFrame")
        secret_options.pack(fill="x", pady=(0, 10))
        ttk.Checkbutton(secret_options, text="记住 OpenAI Key（仅保存在本机）", variable=self.remember_openai_key).pack(side="left")
        ttk.Button(secret_options, text="清除", command=self.clear_saved_openai_key).pack(side="left", padx=(8, 0))

        model_row = ttk.Frame(parent, style="Panel.TFrame")
        model_row.pack(fill="x", pady=4)
        ttk.Label(model_row, text="模型", style="Panel.TLabel", width=10).pack(side="left")
        ttk.Combobox(
            model_row,
            textvariable=self.openai_model,
            values=OPENAI_MODEL_OPTIONS,
            state="readonly",
            width=22,
        ).pack(side="left", fill="x", expand=True)

        self.field(parent, "主题", self.ai_topic)
        self.field(parent, "读者", self.ai_audience)

        tone_row = ttk.Frame(parent, style="Panel.TFrame")
        tone_row.pack(fill="x", pady=4)
        ttk.Label(tone_row, text="语气", style="Panel.TLabel", width=10).pack(side="left")
        ttk.Combobox(
            tone_row,
            textvariable=self.ai_tone,
            values=("温暖自然", "活泼种草", "专业可信", "简洁商务", "故事感"),
            state="readonly",
            width=22,
        ).pack(side="left", fill="x", expand=True)

        length_row = ttk.Frame(parent, style="Panel.TFrame")
        length_row.pack(fill="x", pady=4)
        ttk.Label(length_row, text="篇幅", style="Panel.TLabel", width=10).pack(side="left")
        ttk.Combobox(
            length_row,
            textvariable=self.ai_length,
            values=("短篇", "中等篇幅", "长篇深度"),
            state="readonly",
            width=22,
        ).pack(side="left", fill="x", expand=True)

        ttk.Label(parent, text="补充要求", style="Section.TLabel").pack(anchor="w", pady=(12, 6))
        self.ai_instruction = Text(
            parent,
            height=7,
            wrap="word",
            font=("Microsoft YaHei UI", 10),
            padx=10,
            pady=10,
            bg=UI_PALETTE["field"],
            fg=UI_PALETTE["text"],
            insertbackground=UI_PALETTE["accent"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=UI_PALETTE["border"],
            highlightcolor=UI_PALETTE["accent"],
        )
        self.ai_instruction.insert(END, self.config.get("ai_instruction", "文章要适合公众号阅读，开头吸引人，中间有条理，结尾给出行动建议。不要添加夸张表情，不要使用过多口号和花哨分隔。需要时保留 [[图片1]] 图片位置。"))
        self.ai_instruction.pack(fill="x", pady=(0, 10))
        self.bind_text_menu(self.ai_instruction)

        actions = ttk.Frame(parent, style="Panel.TFrame")
        actions.pack(fill="x", pady=(2, 10))
        self.ai_generate_button = ttk.Button(actions, text="AI生成文章", command=self.generate_ai_article, style="Primary.TButton")
        self.ai_generate_button.pack(side="left")
        ttk.Button(actions, text="从剪贴板导入", command=self.import_from_clipboard).pack(side="left", padx=(8, 0))

        ttk.Label(parent, textvariable=self.ai_status, style="Panel.TLabel", wraplength=340).pack(anchor="w", pady=(0, 8))
        ttk.Label(
            parent,
            text="提示：OpenAI Key 和公众号 AppSecret 一样，只有勾选记住时才会保存到本机。",
            style="Muted.TLabel",
            wraplength=340,
        ).pack(anchor="w")

    def build_editor_panel(self, parent):
        top = ttk.Frame(parent, style="Panel.TFrame")
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="正文编辑", style="Title.TLabel").pack(side="left")
        ttk.Button(top, textvariable=self.publish_action_text, command=self.create_or_publish, style="Primary.TButton").pack(side="right")
        ttk.Button(top, text="预览排版", command=self.preview_layout).pack(side="right", padx=(0, 8))
        ttk.Label(
            top,
            text="直接写内容；支持小标题、引用、列表和图片占位符",
            style="Muted.TLabel",
        ).pack(side="left", padx=(16, 0))

        tools = ttk.Frame(parent, style="Panel.TFrame")
        tools.pack(fill="x", pady=(0, 10))
        ttk.Label(tools, text="模板", style="Panel.TLabel").pack(side="left")
        ttk.Combobox(
            tools,
            textvariable=self.template_name,
            values=tuple(ARTICLE_TEMPLATES.keys()),
            state="readonly",
            width=12,
        ).pack(side="left", padx=(6, 8))
        ttk.Button(tools, text="套用", command=self.apply_template).pack(side="left")
        ttk.Button(tools, text="新建文章", command=self.new_article).pack(side="left", padx=(8, 0))
        ttk.Button(tools, text="保存本地草稿", command=self.save_local_draft).pack(side="left", padx=(8, 0))
        ttk.Button(tools, text="打开本地草稿", command=self.load_local_draft).pack(side="left", padx=(8, 0))

        rich_tools = ttk.Frame(parent, style="Panel.TFrame")
        rich_tools.pack(fill="x", pady=(0, 10))
        ttk.Label(rich_tools, text="富文本", style="Panel.TLabel").pack(side="left")
        ttk.Button(rich_tools, text="大标题", command=lambda: self.apply_line_prefix("## ")).pack(side="left", padx=(6, 0))
        ttk.Button(rich_tools, text="小标题", command=lambda: self.apply_line_prefix("### ")).pack(side="left", padx=(6, 0))
        ttk.Button(rich_tools, text="加粗", command=lambda: self.wrap_selection("**", "**")).pack(side="left", padx=(6, 0))
        ttk.Button(rich_tools, text="斜体", command=lambda: self.wrap_selection("*", "*")).pack(side="left", padx=(6, 0))
        ttk.Button(rich_tools, text="引用", command=lambda: self.apply_line_prefix("> ")).pack(side="left", padx=(6, 0))
        ttk.Button(rich_tools, text="列表", command=lambda: self.apply_line_prefix("- ")).pack(side="left", padx=(6, 0))
        ttk.Button(rich_tools, text="插入图片位", command=self.insert_next_image_placeholder).pack(side="left", padx=(6, 0))
        ttk.Button(rich_tools, text="清除格式", command=self.clear_selected_formatting).pack(side="left", padx=(6, 0))

        panes = ttk.PanedWindow(parent, orient="horizontal")
        panes.pack(fill="both", expand=True)

        editor_frame = ttk.Frame(panes, style="Panel.TFrame")
        preview_frame = ttk.Frame(panes, style="Panel.TFrame")
        panes.add(editor_frame, weight=3)
        panes.add(preview_frame, weight=2)

        self.content = Text(
            editor_frame,
            wrap="word",
            undo=True,
            font=("Microsoft YaHei UI", 11),
            padx=14,
            pady=14,
            bg=UI_PALETTE["field"],
            fg=UI_PALETTE["text"],
            insertbackground=UI_PALETTE["accent"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=UI_PALETTE["border"],
            highlightcolor=UI_PALETTE["accent"],
        )
        self.configure_rich_text_tags()
        self.content.insert(END, self.sample_content())
        self.content.pack(fill="both", expand=True)
        self.content.bind("<KeyRelease>", self.on_content_changed)
        self.bind_text_menu(self.content)
        self.refresh_editor_styles()

        ttk.Label(preview_frame, text="操作反馈", style="Title.TLabel").pack(anchor="w", pady=(0, 6))
        ttk.Label(
            preview_frame,
            text="这里会显示预览、上传、创建草稿和错误提示。",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(0, 8))
        self.preview = Text(
            preview_frame,
            wrap="word",
            font=("Microsoft YaHei UI", 10),
            padx=12,
            pady=12,
            bg=UI_PALETTE["panel_alt"],
            fg=UI_PALETTE["text"],
            insertbackground=UI_PALETTE["accent"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=UI_PALETTE["border"],
            highlightcolor=UI_PALETTE["accent"],
        )
        self.preview.pack(fill="both", expand=True)
        self.bind_text_menu(self.preview, editable=False)
        self.log_message("准备就绪。默认只创建公众号后台草稿；勾选“创建草稿后立即提交发布”才会真正提交发布。")

        bottom = ttk.Frame(parent, style="Panel.TFrame")
        bottom.pack(fill="x", pady=(10, 0))
        settings_row = ttk.Frame(bottom, style="Panel.TFrame")
        settings_row.pack(fill="x")
        actions_row = ttk.Frame(bottom, style="Panel.TFrame")
        actions_row.pack(fill="x", pady=(8, 0))
        progress_row = ttk.Frame(bottom, style="Panel.TFrame")
        progress_row.pack(fill="x", pady=(8, 0))

        ttk.Checkbutton(settings_row, text="开启评论", variable=self.open_comment).pack(side="left")
        ttk.Checkbutton(settings_row, text="装饰表情（默认版式会尽量克制）", variable=self.auto_emoji).pack(side="left", padx=(14, 0))
        ttk.Label(settings_row, text="风格", style="Panel.TLabel").pack(side="left", padx=(14, 4))
        ttk.Combobox(
            settings_row,
            textvariable=self.emoji_style,
            values=("温暖", "活泼", "商务"),
            state="readonly",
            width=7,
        ).pack(side="left")
        ttk.Label(settings_row, text="主题", style="Panel.TLabel").pack(side="left", padx=(12, 4))
        ttk.Combobox(
            settings_row,
            textvariable=self.layout_theme,
            values=("清新蓝", "暖橙", "商务灰"),
            state="readonly",
            width=8,
        ).pack(side="left")
        ttk.Label(settings_row, text="版式", style="Panel.TLabel").pack(side="left", padx=(12, 4))
        ttk.Combobox(
            settings_row,
            textvariable=self.layout_style,
            values=("公众号图文", "杂志风", "轻卡片", "简约线条"),
            state="readonly",
            width=9,
        ).pack(side="left")
        ttk.Button(settings_row, text="使用干净排版", command=self.apply_clean_layout).pack(side="left", padx=(10, 0))
        ttk.Checkbutton(
            actions_row,
            text="创建草稿后立即提交发布（不勾选只进草稿箱）",
            variable=self.publish_now,
            command=self.update_publish_action_text,
        ).pack(side="left")
        ttk.Button(actions_row, text="打开排版预览", command=self.preview_layout).pack(side="right")
        ttk.Button(actions_row, textvariable=self.publish_action_text, command=self.create_or_publish, style="Primary.TButton").pack(side="right", padx=(0, 8))
        ttk.Label(progress_row, textvariable=self.progress_text, style="Panel.TLabel").pack(side="left")
        ttk.Button(progress_row, text="复制结果", command=self.copy_last_publish_result).pack(side="right", padx=(8, 0))
        self.progress_bar = ttk.Progressbar(progress_row, mode="determinate", maximum=100)
        self.progress_bar.pack(side="right", fill="x", expand=True, padx=(12, 0))

    def field(self, parent, label, variable, secret=False):
        row = ttk.Frame(parent, style="Panel.TFrame")
        row.pack(fill="x", pady=4)
        ttk.Label(row, text=label, style="Panel.TLabel", width=10).pack(side="left")
        entry = ttk.Entry(row, textvariable=variable, width=34, show="*" if secret else "")
        entry.pack(side="left", fill="x", expand=True)
        self.bind_text_menu(entry)

    def bind_text_menu(self, widget, editable=True):
        menu = Menu(widget, tearoff=0)
        if editable:
            menu.add_command(label="剪切", command=lambda: widget.event_generate("<<Cut>>"))
        menu.add_command(label="复制", command=lambda: widget.event_generate("<<Copy>>"))
        if editable:
            menu.add_command(label="粘贴", command=lambda: widget.event_generate("<<Paste>>"))
        menu.add_separator()
        menu.add_command(label="全选", command=lambda: self.select_all_text(widget))

        def popup(event):
            try:
                widget.focus_set()
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()
            return "break"

        widget.bind("<Button-3>", popup, add="+")
        widget.bind("<Control-Button-1>", popup, add="+")

    def select_all_text(self, widget):
        try:
            if isinstance(widget, Text):
                widget.tag_add("sel", "1.0", END)
                widget.mark_set("insert", "1.0")
                widget.see("insert")
            else:
                widget.selection_range(0, END)
                widget.icursor(END)
            widget.focus_set()
        except Exception:
            pass
        return "break"

    def configure_rich_text_tags(self):
        base_font = tkfont.Font(family="Microsoft YaHei UI", size=11)
        h2_font = tkfont.Font(family="Microsoft YaHei UI", size=15, weight="bold")
        h3_font = tkfont.Font(family="Microsoft YaHei UI", size=13, weight="bold")
        bold_font = tkfont.Font(family="Microsoft YaHei UI", size=11, weight="bold")
        italic_font = tkfont.Font(family="Microsoft YaHei UI", size=11, slant="italic")
        self.content.configure(font=base_font)
        self.content.tag_configure("rich_h2", font=h2_font, foreground=UI_PALETTE["header_dark"], spacing1=10, spacing3=6)
        self.content.tag_configure("rich_h3", font=h3_font, foreground=UI_PALETTE["text"], spacing1=8, spacing3=4)
        self.content.tag_configure("rich_quote", foreground=UI_PALETTE["muted"], lmargin1=18, lmargin2=18, spacing1=4, spacing3=4)
        self.content.tag_configure("rich_list", lmargin1=18, lmargin2=34)
        self.content.tag_configure("rich_image", foreground=UI_PALETTE["accent"], background=UI_PALETTE["accent_soft"], justify="center", spacing1=8, spacing3=8)
        self.content.tag_configure("rich_bold", font=bold_font)
        self.content.tag_configure("rich_italic", font=italic_font)

    def on_content_changed(self, _event=None):
        self.schedule_ready_summary_refresh()
        self.root.after_idle(self.refresh_editor_styles)

    def refresh_editor_styles(self):
        if not hasattr(self, "content"):
            return
        for tag in ("rich_h2", "rich_h3", "rich_quote", "rich_list", "rich_image", "rich_bold", "rich_italic"):
            self.content.tag_remove(tag, "1.0", END)

        text = self.content.get("1.0", "end-1c")
        for line_no, line in enumerate(text.splitlines(), start=1):
            clean = line.strip()
            line_start = f"{line_no}.0"
            line_end = f"{line_no}.end"
            if clean.startswith("## "):
                self.content.tag_add("rich_h2", line_start, line_end)
            elif clean.startswith("### "):
                self.content.tag_add("rich_h3", line_start, line_end)
            elif clean.startswith(">"):
                self.content.tag_add("rich_quote", line_start, line_end)
            elif re.match(r"^\s*[-*]\s+", line):
                self.content.tag_add("rich_list", line_start, line_end)
            elif IMAGE_PLACEHOLDER_PATTERN.fullmatch(clean):
                self.content.tag_add("rich_image", line_start, line_end)

            for match in re.finditer(r"\*\*(.+?)\*\*", line):
                self.content.tag_add("rich_bold", f"{line_no}.{match.start(1)}", f"{line_no}.{match.end(1)}")
            for match in re.finditer(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", line):
                self.content.tag_add("rich_italic", f"{line_no}.{match.start(1)}", f"{line_no}.{match.end(1)}")

    def wrap_selection(self, prefix, suffix):
        try:
            start = self.content.index("sel.first")
            end = self.content.index("sel.last")
            selected = self.content.get(start, end)
            self.content.delete(start, end)
            self.content.insert(start, f"{prefix}{selected}{suffix}")
        except Exception:
            self.content.insert("insert", f"{prefix}{suffix}")
            self.content.mark_set("insert", f"insert-{len(suffix)}c")
        self.content.focus_set()
        self.on_content_changed()

    def apply_line_prefix(self, prefix):
        try:
            start_line = int(self.content.index("sel.first").split(".")[0])
            end_line = int(self.content.index("sel.last").split(".")[0])
        except Exception:
            start_line = end_line = int(self.content.index("insert").split(".")[0])

        for line_no in range(start_line, end_line + 1):
            line_start = f"{line_no}.0"
            line_text = self.content.get(line_start, f"{line_no}.end")
            clean = re.sub(r"^\s*(#{2,3}\s+|>\s+|[-*]\s+)", "", line_text)
            self.content.delete(line_start, f"{line_no}.end")
            self.content.insert(line_start, prefix + clean)
        self.content.focus_set()
        self.on_content_changed()

    def insert_next_image_placeholder(self):
        existing = [
            normalize_image_number(item)
            for item in IMAGE_PLACEHOLDER_PATTERN.findall(self.content.get("1.0", END))
        ]
        next_index = 1
        while next_index in existing:
            next_index += 1
        if self.image_paths:
            next_index = min(next_index, len(self.image_paths))
        placeholder = f"\n[[图片{next_index}]]\n"
        self.content.insert("insert", placeholder)
        self.content.focus_set()
        self.on_content_changed()

    def clear_selected_formatting(self):
        try:
            start = self.content.index("sel.first")
            end = self.content.index("sel.last")
        except Exception:
            start = self.content.index("insert linestart")
            end = self.content.index("insert lineend")
        text = self.content.get(start, end)
        text = re.sub(r"(?m)^\s*(#{2,3}\s+|>\s+|[-*]\s+)", "", text)
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", text)
        self.content.delete(start, end)
        self.content.insert(start, text)
        self.content.focus_set()
        self.on_content_changed()

    def install_change_watchers(self):
        variables = (
            self.appid,
            self.secret,
            self.current_ip,
            self.title,
            self.cover_path,
            self.source_url,
        )
        for variable in variables:
            variable.trace_add("write", lambda *_args: self.schedule_ready_summary_refresh())

    def schedule_ready_summary_refresh(self):
        if self.ready_refresh_job:
            try:
                self.root.after_cancel(self.ready_refresh_job)
            except Exception:
                pass
        self.ready_refresh_job = self.root.after(250, self.refresh_ready_summary)

    def quick_publish_status(self):
        missing = []
        tips = []
        if not self.appid.get().strip():
            missing.append("AppID")
        if not self.secret.get().strip():
            missing.append("AppSecret")
        if not self.title.get().strip():
            missing.append("标题")
        content = self.content.get("1.0", END).strip() if hasattr(self, "content") else ""
        if not content:
            missing.append("正文")
        cover = self.cover_path.get().strip()
        if not cover:
            missing.append("封面")
        elif not Path(cover).exists():
            missing.append("封面文件不存在")
        missing_images = [index for index, path in enumerate(self.image_paths, start=1) if not Path(path).exists()]
        if missing_images:
            missing.append(f"{len(missing_images)}张正文图片不存在")
        if self.current_ip.get() in ("未检测", "检测失败"):
            tips.append("建议发布前检测 IP")
        if self.image_paths and not self.has_manual_image_placeholders():
            tips.append("正文图片将自动插入")
        return missing, tips

    def refresh_ready_summary(self):
        self.ready_refresh_job = None
        if not hasattr(self, "ready_summary"):
            return
        missing, tips = self.quick_publish_status()
        if missing:
            text = f"发布准备：还差 {len(missing)} 项 - " + "、".join(missing[:4])
            if len(missing) > 4:
                text += "..."
        elif tips:
            text = "发布准备：基础信息完整，提醒 - " + "、".join(tips[:2])
        else:
            text = "发布准备：基础信息已完整，可以预览或发布草稿。"
        self.ready_summary.set(text)

    def show_publish_checklist(self):
        errors, warnings = self.publish_checklist()
        self.refresh_ready_summary()
        if errors:
            message = "发布前检查发现以下问题：\n\n" + "\n".join(f"- {item}" for item in errors)
            if warnings:
                message += "\n\n提醒：\n" + "\n".join(f"- {item}" for item in warnings)
            self.log_message("已执行发布前检查：未通过。")
            messagebox.showerror("发布前检查未通过", message)
            return
        if warnings:
            message = "发布前检查通过，但有以下提醒：\n\n" + "\n".join(f"- {item}" for item in warnings)
            self.log_message("已执行发布前检查：有提醒。")
            messagebox.showwarning("发布前检查提醒", message)
            return
        self.log_message("已执行发布前检查：全部通过。")
        messagebox.showinfo("发布前检查通过", "账号、文章、封面、正文和图片检查均已通过。")

    def open_path(self, path, label):
        target = Path(path)
        try:
            if target.suffix:
                target.parent.mkdir(parents=True, exist_ok=True)
            else:
                target.mkdir(parents=True, exist_ok=True)
            os.startfile(str(target))
            self.status.set(f"已打开{label}。")
        except Exception as exc:
            messagebox.showerror("打开失败", f"无法打开{label}：\n{exc}")

    def open_drafts_folder(self):
        self.open_path(DRAFTS_DIR, "草稿目录")

    def open_app_folder(self):
        self.open_path(Path(__file__).resolve().parent, "软件目录")

    def open_readme(self):
        readme = Path(__file__).resolve().parent / "README.md"
        self.open_path(readme, "使用说明")

    def log_message(self, message):
        timestamp = time.strftime("%H:%M:%S")
        self.preview.insert(END, f"[{timestamp}] {message}\n")
        self.preview.see(END)

    def update_publish_action_text(self):
        if self.publish_now.get():
            self.publish_action_text.set("创建并发布")
            self.status.set("当前模式：创建草稿后立即提交发布。")
        else:
            self.publish_action_text.set("创建草稿")
            self.status.set("当前模式：只创建公众号后台草稿，不会立即发布。")

    def copy_last_publish_result(self):
        result = self.last_publish_result.get().strip()
        if not result or result == "暂无发布结果":
            messagebox.showinfo("复制结果", "还没有可复制的发布结果。")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(result)
        self.status.set("已复制最后一次发布结果。")

    def record_publish_result(self, payload):
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            history_path = APP_DIR / "publish_history.jsonl"
            history_path.write_text("", encoding="utf-8") if not history_path.exists() else None
            with history_path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def set_progress(self, percent, message):
        def update():
            self.progress_bar["value"] = percent
            self.progress_text.set(f"进度：{message}（{percent}%）")
            self.status.set(message)
            self.log_message(message)

        self.root.after(0, update)

    def sample_content(self):
        return (
            "## 文章小标题\n"
            "把正文直接写在这里。默认排版会保持干净，不会因为普通关键词自动加框或乱加符号。\n\n"
            "> 如果需要引用，可以这样单独写一行。\n\n"
            "- 第一个要点\n"
            "- 第二个要点\n\n"
            "[[图片1]]\n\n"
            "### 结尾小标题\n"
            "点击“预览排版”可以先看效果，确认图片和段落位置后再创建草稿。\n"
        )

    def replace_content(self, text):
        self.content.delete("1.0", END)
        self.content.insert(END, text)
        self.on_content_changed()

    def apply_template(self):
        template = ARTICLE_TEMPLATES.get(self.template_name.get())
        if not template:
            return
        current = self.content.get("1.0", END).strip()
        if current and not messagebox.askyesno("套用模板", "套用模板会覆盖当前正文，确定继续吗？"):
            return
        self.replace_content(template)
        self.status.set(f"已套用“{self.template_name.get()}”模板。")

    def new_article(self):
        current = self.content.get("1.0", END).strip()
        has_article = any(
            (
                self.title.get().strip(),
                self.digest.get().strip(),
                self.cover_path.get().strip(),
                self.image_paths,
                current,
            )
        )
        if has_article and not messagebox.askyesno("新建文章", "新建文章会清空当前标题、正文、封面和正文图片，确定继续吗？"):
            return
        self.title.set("")
        self.digest.set("")
        self.source_url.set("")
        self.cover_path.set("")
        self.image_paths = []
        self.refresh_image_list()
        self.replace_content("")
        self.status.set("已新建空白文章。")
        self.log_message("已新建空白文章。")

    def apply_clean_layout(self):
        self.layout_style.set("公众号图文")
        self.auto_emoji.set(0)
        self.status.set("已切换为干净排版：不自动加文字框，不乱加表情。")
        self.log_message("已切换为干净排版。建议点击“预览排版”查看效果。")

    def draft_payload(self):
        return {
            "title": self.title.get().strip(),
            "author": self.author.get().strip(),
            "digest": self.digest.get().strip(),
            "source_url": self.source_url.get().strip(),
            "cover_path": self.cover_path.get().strip(),
            "image_paths": self.image_paths,
            "content": self.content.get("1.0", END).strip(),
            "auto_emoji": int(self.auto_emoji.get()),
            "auto_image_layout": int(self.auto_image_layout.get()),
            "emoji_style": self.emoji_style.get(),
            "layout_theme": self.layout_theme.get(),
            "layout_style": self.layout_style.get(),
            "open_comment": int(self.open_comment.get()),
            "publish_now": int(self.publish_now.get()),
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def safe_draft_name(self):
        title = self.title.get().strip() or "未命名草稿"
        title = re.sub(r'[\\/:*?"<>|]+', "_", title)
        title = title[:40].strip() or "未命名草稿"
        return f"{time.strftime('%Y%m%d_%H%M%S')}_{title}.json"

    def save_local_draft(self):
        try:
            DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
            path = filedialog.asksaveasfilename(
                title="保存本地草稿",
                initialdir=str(DRAFTS_DIR),
                initialfile=self.safe_draft_name(),
                defaultextension=".json",
                filetypes=[("Draft files", "*.json"), ("All files", "*.*")],
            )
            if not path:
                return
            Path(path).write_text(json.dumps(self.draft_payload(), ensure_ascii=False, indent=2), encoding="utf-8")
            self.status.set(f"本地草稿已保存：{path}")
        except Exception as exc:
            self.show_error(exc)

    def load_local_draft(self):
        try:
            DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
            path = filedialog.askopenfilename(
                title="加载本地草稿",
                initialdir=str(DRAFTS_DIR),
                filetypes=[("Draft files", "*.json"), ("All files", "*.*")],
            )
            if not path:
                return
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            self.title.set(data.get("title", ""))
            self.author.set(data.get("author", ""))
            self.digest.set(data.get("digest", ""))
            self.source_url.set(data.get("source_url", ""))
            self.cover_path.set(data.get("cover_path", ""))
            self.image_paths = list(data.get("image_paths", []))
            self.auto_emoji.set(int(data.get("auto_emoji", 1)))
            self.auto_image_layout.set(int(data.get("auto_image_layout", 1)))
            self.emoji_style.set(data.get("emoji_style", "温暖"))
            self.layout_theme.set(data.get("layout_theme", "清新蓝"))
            self.layout_style.set(data.get("layout_style", "公众号图文"))
            self.open_comment.set(int(data.get("open_comment", 0)))
            self.publish_now.set(int(data.get("publish_now", 0)))
            self.replace_content(data.get("content", ""))
            self.refresh_image_list()
            self.status.set(f"已加载本地草稿：{path}")
        except Exception as exc:
            self.show_error(exc)

    def choose_cover(self):
        path = filedialog.askopenfilename(
            title="选择封面图片",
            filetypes=[("Image files", "*.jpg *.jpeg *.png *.gif *.bmp *.webp"), ("All files", "*.*")],
        )
        if path:
            self.cover_path.set(path)
            self.status.set(f"已选择封面图：{path}")
        else:
            self.status.set("未选择封面图。")

    def add_body_images(self):
        paths = filedialog.askopenfilenames(
            title="选择正文图片",
            filetypes=[("Image files", "*.jpg *.jpeg *.png *.gif *.bmp *.webp"), ("All files", "*.*")],
        )
        if not paths:
            self.status.set("未选择正文图片。")
            return

        inserted = []
        skipped = 0
        for path in paths:
            if path not in self.image_paths:
                self.image_paths.append(path)
                inserted.append(f"[[图片{len(self.image_paths)}]]")
            else:
                skipped += 1
        self.refresh_image_list()
        manual_positions = self.has_manual_image_placeholders()
        if inserted and self.auto_image_layout.get() and not manual_positions:
            self.auto_place_body_images(show_message=False, respect_manual=True)
        elif inserted:
            placeholder_text = "\n" + "\n".join(inserted) + "\n"
            if not manual_positions:
                self.content.insert("insert", placeholder_text)
                self.content.focus_set()
        message = f"已添加 {len(inserted)} 张正文图片"
        if skipped:
            message += f"，跳过 {skipped} 张重复图片"
        if manual_positions:
            message += "。已检测到正文里已有图片占位符，将按用户指定位置插图"
        elif inserted and self.auto_image_layout.get():
            message += "。已根据正文要求或段落位置自动插图"
        elif inserted:
            message += f"。已在正文光标处插入：{' '.join(inserted)}"
        self.status.set(message)

    def has_manual_image_placeholders(self):
        content = normalize_article_image_markers(self.content.get("1.0", END))
        return bool(IMAGE_PLACEHOLDER_PATTERN.search(content))

    def is_image_instruction_line(self, line):
        clean = line.strip()
        if not clean:
            return False
        if IMAGE_PLACEHOLDER_PATTERN.fullmatch(clean):
            return False
        patterns = (
            r"^(这里|此处|下面|上面|当前位置|这个位置)?.{0,10}(放|插入|添加|配|展示|放入).{0,12}(图|图片|配图|照片|截图|海报|二维码)",
            r"(图|图片|配图|照片|截图|海报|二维码).{0,14}(放这里|放在这里|插这里|插入这里|在这里|放此处|插此处)",
            r"第?\s*[0-9０-９一二三四五六七八九十]+\s*张?\s*(图|图片|配图|照片).{0,14}(这里|此处|放|插|位置)",
            r"(图|图片|配图|照片)\s*[0-9０-９一二三四五六七八九十]+.{0,14}(这里|此处|放|插|位置)",
            r"^\s*(配图|插图|图片|放图|这里放图|此处放图|放二维码|放海报)\s*[:：]?\s*$",
        )
        return any(re.search(pattern, clean) for pattern in patterns)

    def instruction_image_index(self, line):
        clean = line.strip()
        match = re.search(r"(?:第\s*)?([0-9０-９]+)\s*(?:张)?\s*(?:图|图片|配图|照片)", clean)
        if not match:
            match = re.search(r"(?:图|图片|配图|照片)\s*([0-9０-９]+)", clean)
        if match:
            index = normalize_image_number(match.group(1))
            if 1 <= index <= len(self.image_paths):
                return index

        match = re.search(r"第?\s*([一二三四五六七八九十]+)\s*张?\s*(?:图|图片|配图|照片)", clean)
        if not match:
            match = re.search(r"(?:图|图片|配图|照片)\s*([一二三四五六七八九十]+)", clean)
        if match:
            index = normalize_image_number(match.group(1))
            if 1 <= index <= len(self.image_paths):
                return index
        return 0

    def auto_place_body_images(self, show_message=True, respect_manual=True):
        if not self.image_paths:
            if show_message:
                self.status.set("请先添加正文图片。")
            return

        if respect_manual and self.has_manual_image_placeholders():
            if show_message:
                self.status.set("已检测到手动图片占位符，保持用户指定的插图位置。")
            return

        source = normalize_article_image_markers(self.content.get("1.0", END)).strip()
        lines = [
            line for line in source.splitlines()
            if not IMAGE_PLACEHOLDER_PATTERN.fullmatch(line.strip())
        ]
        if not any(line.strip() for line in lines):
            lines = []

        placeholders = [f"[[图片{index}]]" for index in range(1, len(self.image_paths) + 1)]
        if not lines:
            self.replace_content("\n\n".join(placeholders))
            if show_message:
                self.status.set(f"已自动插入 {len(placeholders)} 张图片占位符。")
            return

        next_placeholder = 0
        used_indexes = set()
        placed_lines = []
        used_instruction = False
        for line in lines:
            if self.is_image_instruction_line(line) and len(used_indexes) < len(placeholders):
                requested_index = self.instruction_image_index(line)
                if requested_index and requested_index not in used_indexes:
                    placeholder = f"[[图片{requested_index}]]"
                    used_indexes.add(requested_index)
                else:
                    while next_placeholder < len(placeholders) and (next_placeholder + 1) in used_indexes:
                        next_placeholder += 1
                    if next_placeholder >= len(placeholders):
                        placed_lines.append(line)
                        continue
                    placeholder = placeholders[next_placeholder]
                    used_indexes.add(next_placeholder + 1)
                    next_placeholder += 1
                if placed_lines and placed_lines[-1] != "":
                    placed_lines.append("")
                placed_lines.append(placeholder)
                placed_lines.append("")
                used_instruction = True
            else:
                placed_lines.append(line)

        if used_instruction:
            remaining = [
                placeholder for index, placeholder in enumerate(placeholders, start=1)
                if index not in used_indexes
            ]
            if remaining:
                lines = placed_lines
                candidates = [
                    index for index, line in enumerate(lines)
                    if line.strip()
                    and not line.strip().startswith(("## ", "### ", ">"))
                    and line.strip() != "---"
                    and not re.match(r"^[-*]\s+", line.strip())
                    and not IMAGE_PLACEHOLDER_PATTERN.fullmatch(line.strip())
                ]
                if not candidates:
                    candidates = [index for index, line in enumerate(lines) if line.strip()]
                offset = 0
                for image_index, placeholder in enumerate(remaining, start=1):
                    raw_position = round(image_index * len(candidates) / (len(remaining) + 1))
                    candidate_index = candidates[min(max(raw_position, 0), len(candidates) - 1)]
                    insert_at = candidate_index + 1 + offset
                    lines.insert(insert_at, "")
                    lines.insert(insert_at + 1, placeholder)
                    lines.insert(insert_at + 2, "")
                    offset += 3
                placed_lines = lines

            self.replace_content("\n".join(placed_lines).strip() + "\n")
            self.content.focus_set()
            if show_message:
                self.status.set("已根据正文里的插图提示放置图片。")
            return

        candidates = [
            index for index, line in enumerate(lines)
            if line.strip()
            and not line.strip().startswith(("## ", "### ", ">"))
            and line.strip() != "---"
            and not re.match(r"^[-*]\s+", line.strip())
        ]
        if not candidates:
            candidates = [index for index, line in enumerate(lines) if line.strip()]

        insertions = []
        for image_index, placeholder in enumerate(placeholders, start=1):
            raw_position = round(image_index * len(candidates) / (len(placeholders) + 1))
            candidate_index = candidates[min(max(raw_position, 0), len(candidates) - 1)]
            insertions.append((candidate_index, placeholder))

        offset = 0
        for candidate_index, placeholder in insertions:
            insert_at = candidate_index + 1 + offset
            lines.insert(insert_at, "")
            lines.insert(insert_at + 1, placeholder)
            lines.insert(insert_at + 2, "")
            offset += 3

        self.replace_content("\n".join(lines).strip() + "\n")
        self.content.focus_set()
        if show_message:
            self.status.set(f"已将 {len(placeholders)} 张正文图片自动穿插到文章段落中。")

    def missing_image_placeholders(self):
        content = normalize_article_image_markers(self.content.get("1.0", END))
        missing = []
        for index in range(1, len(self.image_paths) + 1):
            if f"[[图片{index}]]" not in content:
                missing.append(index)
        return missing

    def remove_selected_image(self):
        selected = self.image_list.selection()
        if not selected:
            self.status.set("请先在正文图片列表里选中要移除的图片。")
            return
        indexes = sorted((self.image_list.index(item) for item in selected), reverse=True)
        for index in indexes:
            if 0 <= index < len(self.image_paths):
                self.image_paths.pop(index)
        self.refresh_image_list()
        self.status.set("已从素材列表移除选中的正文图片。正文里的 [[图片N]] 占位符请按需要手动调整。")

    def refresh_image_list(self):
        for item in self.image_list.get_children():
            self.image_list.delete(item)
        for index, path in enumerate(self.image_paths, start=1):
            self.image_list.insert("", "end", values=(f"[[图片{index}]]", path))
        self.schedule_ready_summary_refresh()

    def save_common_fields(self):
        data = {
            "appid": self.appid.get().strip(),
            "remember_secret": int(self.remember_secret.get()),
            "appsecret": self.secret.get().strip() if self.remember_secret.get() else "",
            "author": self.author.get().strip(),
            "last_public_ip": self.last_ip.get() if self.last_ip.get() != "未记录" else "",
            "auto_emoji": int(self.auto_emoji.get()),
            "auto_image_layout": int(self.auto_image_layout.get()),
            "emoji_style": self.emoji_style.get(),
            "layout_theme": self.layout_theme.get(),
            "layout_style": self.layout_style.get(),
            "template_name": self.template_name.get(),
            "remember_openai_key": int(self.remember_openai_key.get()),
            "openai_api_key": self.openai_key.get().strip() if self.remember_openai_key.get() else "",
            "openai_model": self.openai_model.get(),
            "ai_topic": self.ai_topic.get().strip(),
            "ai_audience": self.ai_audience.get().strip(),
            "ai_tone": self.ai_tone.get(),
            "ai_length": self.ai_length.get(),
            "ai_instruction": self.get_ai_instruction(),
        }
        save_config(data)
        if self.remember_secret.get():
            self.status.set("已保存常用项和 AppSecret。AppSecret 仅保存在本机。")
        else:
            self.status.set("已保存常用项。AppSecret 未保存。")

    def clear_saved_secret(self):
        self.secret.set("")
        self.remember_secret.set(0)
        self.save_config_snapshot()
        self.status.set("已清除本机保存的 AppSecret。")

    def clear_saved_openai_key(self):
        self.openai_key.set("")
        self.remember_openai_key.set(0)
        self.save_config_snapshot()
        self.ai_status.set("已清除本机保存的 OpenAI Key。")
        self.status.set("已清除本机保存的 OpenAI Key。")

    def get_ai_instruction(self):
        if not hasattr(self, "ai_instruction"):
            return self.config.get("ai_instruction", "")
        return self.ai_instruction.get("1.0", END).strip()

    def build_ai_user_prompt(self):
        topic = self.ai_topic.get().strip()
        instruction = self.get_ai_instruction()
        if not topic and not instruction:
            raise ValueError("请先填写 AI 写作主题或补充要求。")
        image_hint = ""
        if self.image_paths:
            image_hint = f"\n可用正文图片数量：{len(self.image_paths)} 张。请按需要在正文中使用 [[图片1]]、[[图片2]] 这类占位符。"
        return (
            f"请写一篇微信公众号文章。\n"
            f"主题：{topic or '按补充要求拟定'}\n"
            f"目标读者：{self.ai_audience.get().strip() or '公众号读者'}\n"
            f"语气：{self.ai_tone.get()}\n"
            f"篇幅：{self.ai_length.get()}\n"
            f"{image_hint}\n"
            f"补充要求：{instruction or '无'}\n\n"
            "输出格式：\n"
            "标题：一句吸引人的标题\n"
            "摘要：不超过120字\n"
            "正文：\n"
            "正文内容"
        )

    def parse_article_text(self, text):
        clean = text.strip()
        title = ""
        digest = ""
        body = clean

        title_match = re.search(r"(?m)^#{0,3}\s*标题[：:]\s*(.+)$", clean)
        if title_match:
            title = title_match.group(1).strip().strip("# ")

        digest_match = re.search(r"(?m)^#{0,3}\s*(摘要|导语)[：:]\s*(.+)$", clean)
        if digest_match:
            digest = digest_match.group(2).strip()

        body_match = re.search(r"(?ms)^#{0,3}\s*正文[：:]\s*(.+)$", clean)
        if body_match:
            body = body_match.group(1).strip()
        else:
            lines = []
            for line in clean.splitlines():
                if re.match(r"^\s*#{0,3}\s*(标题|摘要|导语)[：:]", line):
                    continue
                lines.append(line)
            body = "\n".join(lines).strip()

        if not title:
            for line in body.splitlines():
                candidate = line.strip().strip("# ")
                if candidate:
                    title = candidate[:64]
                    break
        return title, digest, body

    def apply_article_text(self, text, source="导入内容"):
        title, digest, body = self.parse_article_text(text)
        current = self.content.get("1.0", END).strip()
        if current and not messagebox.askyesno(source, f"{source}会覆盖当前正文，确定继续吗？"):
            self.ai_status.set("已取消导入。")
            return
        if title:
            self.title.set(title)
        if digest:
            self.digest.set(digest)
        self.content.delete("1.0", END)
        self.content.insert(END, body or text.strip())
        if self.auto_image_layout.get() and self.image_paths and not self.has_manual_image_placeholders():
            self.auto_place_body_images(show_message=False)
        self.on_content_changed()
        self.ai_status.set(f"{source}已放入正文编辑区。")
        self.status.set(f"{source}已导入，可以继续排版或创建草稿。")
        self.log_message(f"{source}已导入正文。")

    def import_from_clipboard(self):
        try:
            text = self.root.clipboard_get().strip()
        except Exception:
            messagebox.showinfo("剪贴板导入", "剪贴板里没有可导入的文字。")
            return
        if not text:
            messagebox.showinfo("剪贴板导入", "剪贴板内容为空。")
            return
        self.apply_article_text(text, source="剪贴板内容")

    def generate_ai_article(self):
        api_key = self.openai_key.get().strip()
        if not api_key:
            messagebox.showinfo("AI 写作", "请先填写 OpenAI API Key。")
            return
        try:
            prompt = self.build_ai_user_prompt()
        except Exception as exc:
            messagebox.showerror("AI 写作", str(exc))
            return
        model = self.openai_model.get()
        self.ai_generate_button.configure(state="disabled", text="生成中...")
        self.ai_status.set("正在请求 AI 写作，请稍等...")
        self.status.set("AI 正在生成公众号文章...")
        self.log_message("AI 写作已开始。")
        self.save_config_snapshot()

        def task():
            try:
                result = openai_generate_article(api_key, model, prompt)
                self.root.after(0, lambda: self.apply_article_text(result, source="AI生成文章"))
            except Exception as exc:
                detail = str(exc)
                self.root.after(0, lambda: self.ai_status.set("AI 写作失败，请检查 Key、网络或模型。"))
                self.root.after(0, lambda: self.status.set("AI 写作失败。"))
                self.root.after(0, lambda: self.log_message(f"AI 写作失败：{detail.splitlines()[0]}"))
                self.root.after(0, lambda: messagebox.showerror("AI 写作失败", detail))
            finally:
                self.root.after(0, lambda: self.ai_generate_button.configure(state="normal", text="AI生成文章"))

        threading.Thread(target=task, daemon=True).start()

    def save_config_snapshot(self, public_ip=None):
        save_config(
            {
                "appid": self.appid.get().strip(),
                "remember_secret": int(self.remember_secret.get()),
                "appsecret": self.secret.get().strip() if self.remember_secret.get() else "",
                "author": self.author.get().strip(),
                "last_public_ip": public_ip if public_ip is not None else (
                    self.last_ip.get() if self.last_ip.get() != "未记录" else ""
                ),
                "auto_emoji": int(self.auto_emoji.get()),
                "auto_image_layout": int(self.auto_image_layout.get()),
                "emoji_style": self.emoji_style.get(),
                "layout_theme": self.layout_theme.get(),
                "layout_style": self.layout_style.get(),
                "template_name": self.template_name.get(),
                "remember_openai_key": int(self.remember_openai_key.get()),
                "openai_api_key": self.openai_key.get().strip() if self.remember_openai_key.get() else "",
                "openai_model": self.openai_model.get(),
                "ai_topic": self.ai_topic.get().strip(),
                "ai_audience": self.ai_audience.get().strip(),
                "ai_tone": self.ai_tone.get(),
                "ai_length": self.ai_length.get(),
                "ai_instruction": self.get_ai_instruction(),
            }
        )

    def detect_public_ip(self):
        self.current_ip.set("检测中...")
        self.status.set("正在快速检测当前公网 IP...")
        self.detect_ip_button.configure(state="disabled", text="检测中...")

        def task():
            try:
                ip = get_public_ip()
                previous = self.last_ip.get()
                self.root.after(0, lambda: self.current_ip.set(ip))
                self.root.after(0, lambda: self.last_ip.set(ip))
                self.save_config_snapshot(public_ip=ip)
                if previous not in ("未记录", "", ip):
                    message = f"当前公网 IP 是 {ip}，和上次记录的 {previous} 不同。请确认公众号后台 IP 白名单已更新。"
                else:
                    message = f"当前公网 IP 是 {ip}。请把它加入公众号后台 IP 白名单。"
                self.root.after(0, lambda: self.status.set(message))
            except Exception as exc:
                detail = (
                    f"{exc}\n\n"
                    "可以手动打开 https://ip138.com 或 https://cip.cc 查询公网 IP，"
                    "然后填入公众号后台 IP 白名单。"
                )
                self.root.after(0, lambda: self.current_ip.set("检测失败"))
                self.root.after(0, lambda: self.status.set("公网 IP 检测失败，可以稍后重试或手动查询。"))
                self.root.after(0, lambda: messagebox.showerror("检测 IP 失败", detail))
            finally:
                self.root.after(0, lambda: self.detect_ip_button.configure(state="normal", text="检测 IP"))

        threading.Thread(target=task, daemon=True).start()

    def copy_public_ip(self):
        ip = self.current_ip.get().strip()
        if ip == "未检测":
            ip = self.last_ip.get().strip()
        if not ip or ip in ("未检测", "未记录"):
            messagebox.showinfo("提示", "请先点击“检测 IP”。")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(ip)
        self.status.set(f"已复制公网 IP：{ip}")

    def copy_wechat_seen_ip(self):
        ip = self.wechat_seen_ip.get().strip()
        if not ip or ip == "未获取":
            messagebox.showinfo("提示", "还没有获取到微信看到的 IP。请先点击“测试连接”。")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(ip)
        self.status.set(f"已复制微信看到的 IP：{ip}")

    def validate_account(self):
        appid = self.appid.get().strip()
        secret = self.secret.get().strip()
        if not appid or not secret:
            raise ValueError("请填写 AppID 和 AppSecret。")
        return appid, secret

    def validate_article(self):
        if not self.title.get().strip():
            raise ValueError("请填写文章标题。")
        if not self.cover_path.get().strip():
            raise ValueError("请选择封面图。")
        if not Path(self.cover_path.get().strip()).exists():
            raise ValueError("封面图文件不存在。")
        for path in self.image_paths:
            if not Path(path).exists():
                raise ValueError(f"正文图片不存在：{path}")
        content = normalize_article_image_markers(self.content.get("1.0", END)).strip()
        if not content:
            raise ValueError("请填写正文内容。")
        return content

    def check_image_file(self, label, path, errors, warnings, cover=False):
        image_path = Path(path)
        if not path:
            errors.append(f"{label} 未选择。")
            return
        if not image_path.exists():
            errors.append(f"{label} 文件不存在：{path}")
            return
        if image_path.suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
            errors.append(f"{label} 格式可能不支持：{image_path.suffix}")
        size = image_path.stat().st_size
        if size > MAX_IMAGE_BYTES:
            warnings.append(f"{label} 文件较大：{size / 1024 / 1024:.1f} MB，上传可能较慢或失败。")
        dimensions = read_image_size(image_path)
        if not dimensions:
            warnings.append(f"{label} 无法读取图片尺寸，请确认图片文件正常。")
            return
        width, height = dimensions
        if width < 300 or height < 300:
            warnings.append(f"{label} 尺寸偏小：{width}x{height}，显示效果可能不佳。")
        if cover:
            ratio = width / max(height, 1)
            if ratio < 1.2 or ratio > 2.5:
                warnings.append(f"封面图比例为 {width}:{height}，建议使用横图，展示更稳定。")

    def publish_checklist(self):
        errors = []
        warnings = []

        if not self.appid.get().strip():
            errors.append("AppID 未填写。")
        if not self.secret.get().strip():
            errors.append("AppSecret 未填写。")
        if not self.title.get().strip():
            errors.append("文章标题未填写。")
        content = self.content.get("1.0", END).strip()
        if not content:
            errors.append("正文内容为空。")
        elif len(content) < 20:
            warnings.append("正文内容较短，请确认不是测试占位内容。")

        self.check_image_file("封面图", self.cover_path.get().strip(), errors, warnings, cover=True)
        for index, path in enumerate(self.image_paths, start=1):
            self.check_image_file(f"正文图片{index}", path, errors, warnings)

        source_url = self.source_url.get().strip()
        if source_url and not re.match(r"^https?://", source_url, re.I):
            errors.append("原文链接需要以 http:// 或 https:// 开头。")

        placeholders = sorted(
            set(
                normalize_image_number(item)
                for item in IMAGE_PLACEHOLDER_PATTERN.findall(content)
                if normalize_image_number(item) > 0
            )
        )
        for item in placeholders:
            if item < 1 or item > len(self.image_paths):
                errors.append(f"正文使用了 [[图片{item}]]，但素材列表没有第 {item} 张正文图片。")
        missing = self.missing_image_placeholders()
        if missing and self.has_manual_image_placeholders():
            warnings.append("素材列表中有图片未插入正文：" + "、".join(f"[[图片{i}]]" for i in missing))
        if self.auto_image_layout.get() and self.image_paths and not self.has_manual_image_placeholders():
            warnings.append("未检测到手动图片占位符，软件将按正文提示或段落自动插图。")
        if self.current_ip.get() in ("未检测", "检测失败"):
            warnings.append("还没有检测当前公网 IP。若白名单已配置，可继续；否则建议先检测。")
        return errors, warnings

    def confirm_publish_checklist(self):
        errors, warnings = self.publish_checklist()
        if errors:
            message = "发布前检查发现以下问题：\n\n" + "\n".join(f"- {item}" for item in errors)
            if warnings:
                message += "\n\n提醒：\n" + "\n".join(f"- {item}" for item in warnings)
            self.log_message("发布前检查未通过。")
            messagebox.showerror("发布前检查未通过", message)
            return False
        if warnings:
            message = "发布前检查通过，但有以下提醒：\n\n" + "\n".join(f"- {item}" for item in warnings)
            message += "\n\n是否继续创建公众号草稿？"
            self.log_message("发布前检查有提醒，等待用户确认。")
            return messagebox.askyesno("发布前检查提醒", message)
        self.log_message("发布前检查通过。")
        return True

    def preview_layout(self):
        formatter = AutoFormatter(
            self.title.get(),
            self.digest.get(),
            auto_emoji=bool(self.auto_emoji.get()),
            emoji_style=self.emoji_style.get(),
            layout_theme=self.layout_theme.get(),
            layout_style=self.layout_style.get(),
        )
        local_urls = {index: Path(path).as_uri() for index, path in enumerate(self.image_paths, start=1)}
        self.latest_html = formatter.render(self.content.get("1.0", END).strip(), local_urls, local_preview=True)

        self.status.set("预览已生成，已打开浏览器预览页。")
        self.log_message("已生成排版预览，并尝试打开浏览器预览页。")

        preview_file = Path(tempfile.gettempdir()) / "wechat_auto_publisher_preview.html"
        page = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>公众号排版预览</title></head><body style='background:#f5f5f5;padding:24px;'>"
            f"<main style='background:#fff;padding:28px;margin:0 auto;max-width:760px;'>{self.latest_html}</main>"
            "</body></html>"
        )
        preview_file.write_text(page, encoding="utf-8")
        try:
            webbrowser.open(preview_file.as_uri())
        except Exception:
            pass

    def run_task(self, label, func):
        self.status.set(label)
        thread = threading.Thread(target=self.safe_run, args=(func,), daemon=True)
        thread.start()

    def safe_run(self, func):
        try:
            func()
        except Exception as exc:
            self.root.after(0, lambda: self.show_error(exc))

    def show_error(self, exc):
        detail = explain_wechat_error(exc)
        self.status.set(f"失败：{str(exc).splitlines()[0]}")
        if hasattr(self, "last_publish_result"):
            self.last_publish_result.set(detail)
        if hasattr(self, "progress_text"):
            self.progress_text.set("进度：操作失败")
        if hasattr(self, "preview"):
            self.log_message(f"操作失败：{str(exc).splitlines()[0]}")
        messagebox.showerror("操作失败", detail)

    def test_connection(self):
        self.connection_status.set("测试中...")
        self.status.set("正在测试公众号 API 连接，请稍等...")
        self.test_button.configure(state="disabled", text="测试中...")

        def task():
            try:
                appid, secret = self.validate_account()
                self.api.get_access_token(appid, secret)
                self.save_config_snapshot()
                finished_at = time.strftime("%H:%M:%S")
                message = f"连接成功，已获取 access_token。测试时间：{finished_at}"
                self.root.after(0, lambda: self.wechat_seen_ip.set("已通过"))
                self.root.after(0, lambda: self.connection_status.set(f"成功 {finished_at}"))
                self.root.after(0, lambda: self.status.set(message))
                self.root.after(0, lambda: messagebox.showinfo("测试连接成功", message))
            except Exception as exc:
                detail = explain_wechat_error(exc)
                seen_ip = extract_wechat_seen_ip(exc)
                finished_at = time.strftime("%H:%M:%S")
                if seen_ip:
                    self.root.after(0, lambda ip=seen_ip: self.wechat_seen_ip.set(ip))
                self.root.after(0, lambda: self.connection_status.set(f"失败 {finished_at}"))
                self.root.after(0, lambda: self.status.set(f"测试失败：{str(exc).splitlines()[0]}"))
                self.root.after(0, lambda: messagebox.showerror("测试连接失败", detail))
            finally:
                self.root.after(0, lambda: self.test_button.configure(state="normal", text="测试连接"))

        threading.Thread(target=task, daemon=True).start()

    def check_public_ip_before_publish(self):
        ip = get_public_ip()
        previous = self.last_ip.get()
        self.root.after(0, lambda: self.current_ip.set(ip))
        self.root.after(0, lambda: self.last_ip.set(ip))
        self.save_config_snapshot(public_ip=ip)
        if previous not in ("未记录", "", ip):
            raise RuntimeError(
                f"公网 IP 已变化。\n当前公网 IP：{ip}\n上次记录 IP：{previous}\n\n"
                "请先把当前 IP 加入公众号后台白名单，然后再重新发布。"
            )
        return ip

    def upload_body_images(self, appid, secret, base_progress=45, span=25):
        image_urls = {}
        total = len(self.image_paths)
        for index, path in enumerate(self.image_paths, start=1):
            progress = base_progress + round((index - 1) * span / max(total, 1))
            self.set_progress(progress, f"正在上传正文图片 {index}/{total}...")
            image_urls[index] = self.api.upload_article_image(appid, secret, path)
        if total:
            self.set_progress(base_progress + span, "正文图片上传完成。")
        return image_urls

    def create_or_publish(self):
        if not self.confirm_publish_checklist():
            self.progress_text.set("进度：发布前检查未通过")
            return

        missing = self.missing_image_placeholders()
        if missing and self.has_manual_image_placeholders():
            self.log_message(f"提示：正文缺少这些图片占位符：{', '.join(f'[[图片{i}]]' for i in missing)}。这些图片不会出现在文章中。")
        if self.auto_image_layout.get() and missing and not self.has_manual_image_placeholders():
            self.auto_place_body_images(show_message=False)
            self.status.set("发布前已自动补齐正文图片位置。")

        def task():
            self.set_progress(5, "准备发布流程...")
            appid, secret = self.validate_account()
            content = self.validate_article()
            self.set_progress(15, "正在检查公网 IP...")
            self.check_public_ip_before_publish()
            self.save_config_snapshot()

            self.set_progress(30, "正在上传封面图...")
            thumb_media_id = self.api.upload_cover(appid, secret, self.cover_path.get().strip())

            image_urls = self.upload_body_images(appid, secret)
            self.set_progress(72, "正在生成公众号排版 HTML...")
            formatter = AutoFormatter(
                self.title.get(),
                self.digest.get(),
                auto_emoji=bool(self.auto_emoji.get()),
                emoji_style=self.emoji_style.get(),
                layout_theme=self.layout_theme.get(),
                layout_style=self.layout_style.get(),
            )
            article_html = formatter.render(content, image_urls)

            article = {
                "title": self.title.get().strip(),
                "author": self.author.get().strip(),
                "digest": self.digest.get().strip(),
                "content": article_html,
                "content_source_url": self.source_url.get().strip(),
                "thumb_media_id": thumb_media_id,
                "need_open_comment": 1 if self.open_comment.get() else 0,
                "only_fans_can_comment": 0,
            }

            self.set_progress(82, "正在创建公众号草稿...")
            draft_media_id = self.api.create_draft(appid, secret, article)

            if self.publish_now.get():
                self.set_progress(92, "正在提交发布...")
                result = self.api.publish_draft(appid, secret, draft_media_id)
                publish_id = result.get("publish_id", "已提交")
                message = (
                    "已创建草稿并提交发布。\n"
                    f"草稿 media_id: {draft_media_id}\n"
                    f"publish_id: {publish_id}\n"
                    "请到公众号后台查看发布状态。"
                )
            else:
                message = (
                    "草稿已创建，但尚未正式发布。\n"
                    f"media_id: {draft_media_id}\n"
                    "请到公众号后台：内容与互动 -> 草稿箱 查看。"
                )

            self.set_progress(100, "发布流程完成。")
            result_payload = {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "title": self.title.get().strip(),
                "mode": "publish" if self.publish_now.get() else "draft",
                "draft_media_id": draft_media_id,
                "message": message,
            }
            self.record_publish_result(result_payload)
            self.root.after(0, lambda: self.status.set(message.replace("\n", " ")))
            self.root.after(0, lambda: self.last_publish_result.set(message))
            self.root.after(0, lambda: self.log_message(message.replace("\n", " ")))
            self.root.after(0, lambda: messagebox.showinfo("完成", message))

        self.run_task("准备发布流程...", task)


if __name__ == "__main__":
    root = Tk()
    PublisherApp(root)
    root.mainloop()
