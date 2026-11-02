"""备忘录图片视图、渲染适配器与磁盘缓存。

本模块只依赖 Python 标准库，方便单元测试与后续替换渲染器：

- :func:`build_memo_view` 把条目规范化为稳定的展示模型（不包含“几分钟后”等相对时间）。
- :func:`render_memo_image` 负责把展示模型交给可替换的渲染器，并返回图片 bytes。
- :class:`MemoImageCache` 负责插件数据目录内的图片缓存、原子写入、容量与 TTL 清理。

图片渲染器只需要实现 ``render_custom_template(template, data, return_url=False)``，
或者提供一个可调用对象。默认由 main.py 注入 AstrBot 的 ``context.html_renderer``。
"""

from __future__ import annotations

import hashlib
import html
import inspect
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Callable
from typing import Any

VIEW_SCHEMA_VERSION = "memo-view-v1"
RENDER_TEMPLATE_VERSION = "memo-html-v1"
DEFAULT_STYLE_VERSION = "1"
IMAGE_CACHE_SUBDIR = "image_cache"

# t2i 服务的设备像素比等级（见 astrbot-t2i-service ScreenshotOptions）。
IMAGE_SCALE_LEVELS = ("normal", "high", "ultra")
DEFAULT_SCALE_LEVEL = "high"

# ``full_page`` 截图的高度是 ``max(内容高度, 视口高度)``，而 t2i 服务默认视口高 720px。
# 条目被删减后内容常常不足 720px，多出来的部分就成了图片底部的空白，看着像“没有同步变化”。
# 因此把视口高度压到远小于任何真实内容的值，让截图高度只由内容决定；宽度不覆盖，
# 沿用服务端默认视口（800px），模板布局与改动前逐像素一致。
MIN_VIEWPORT_HEIGHT = 1

# AstrBot 默认使用 JPEG quality=40，文字边缘容易出现压缩模糊；改用无损 PNG。
# ``scale="device"`` 必须配合 ``device_scale_factor_level`` 才会真正按高分屏渲染，
# 否则 device_scale_factor 恒为 1，截出来的图在手机上放大就发虚。
DEFAULT_RENDER_OPTIONS = {
    "full_page": True,
    "type": "png",
    "scale": "device",
    "device_scale_factor_level": DEFAULT_SCALE_LEVEL,
    "viewport_height": MIN_VIEWPORT_HEIGHT,
}

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    padding: 24px;
    background: #eef1f5;
    color: #20242a;
    font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC",
                 "Source Han Sans SC", "WenQuanYi Micro Hei", sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .memo-card {
    width: 760px;
    max-width: calc(100vw - 48px);
    margin: 0 auto;
    padding: 30px 32px 24px;
    background: #ffffff;
    border: 1px solid #e3e7ec;
    border-radius: 18px;
    box-shadow: 0 8px 28px rgba(31, 45, 61, 0.08);
  }
  .memo-header {
    padding-bottom: 18px;
    border-bottom: 1px solid #edf0f3;
  }
  .memo-title {
    font-size: 26px;
    line-height: 1.35;
    font-weight: 700;
    letter-spacing: 0.5px;
    overflow-wrap: anywhere;
    word-break: break-word;
  }
  .memo-subtitle {
    margin-top: 7px;
    color: #7b8490;
    font-size: 14px;
    line-height: 1.5;
    overflow-wrap: anywhere;
    word-break: break-word;
  }
  .memo-section { margin-top: 22px; }
  .memo-section-title {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 11px;
    color: #2f5f8f;
    font-size: 17px;
    font-weight: 700;
    line-height: 1.45;
  }
  .memo-count {
    display: inline-block;
    padding: 1px 8px;
    border-radius: 10px;
    background: #eef5fb;
    color: #4a7aa8;
    font-size: 12px;
    font-weight: 600;
  }
  .memo-item {
    margin: 0 0 10px;
    padding: 13px 15px 12px;
    background: #fafbfc;
    border: 1px solid #edf0f3;
    border-left: 4px solid #7ba7cd;
    border-radius: 10px;
  }
  .memo-item-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
    color: #8a939e;
    font-size: 12px;
    line-height: 1.4;
  }
  .memo-index {
    color: #2f5f8f;
    font-weight: 700;
  }
  .memo-kind {
    padding: 1px 7px;
    border-radius: 8px;
    background: #eef5fb;
    color: #4a7aa8;
    font-weight: 600;
  }
  .memo-id {
    color: #9aa2ac;
    font-family: "Consolas", "Menlo", monospace;
  }
  .memo-item-title {
    font-size: 16px;
    line-height: 1.6;
    font-weight: 600;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    word-break: break-word;
  }
  .memo-item-meta {
    margin-top: 5px;
    color: #79828d;
    font-size: 13px;
    line-height: 1.55;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    word-break: break-word;
  }
  .memo-footer {
    margin-top: 22px;
    padding-top: 15px;
    border-top: 1px solid #edf0f3;
    color: #8a939e;
    font-size: 13px;
    line-height: 1.65;
    overflow-wrap: anywhere;
    word-break: break-word;
  }
</style>
</head>
<body>
{{ body | safe }}
</body>
</html>
"""


class MemoImageError(RuntimeError):
    """图片渲染或缓存过程中可预期的错误。"""


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def normalize_scale_level(value: Any) -> str:
    """把配置里的清晰度等级规范化为 t2i 服务支持的三个值之一。"""
    level = str(value or "").strip().lower()
    return level if level in IMAGE_SCALE_LEVELS else DEFAULT_SCALE_LEVEL


def build_render_options(config: dict | None = None) -> dict:
    """按配置生成渲染选项。

    只把会影响成图像素的字段放进这里：这些字段同时参与缓存 key 计算，
    因此调整清晰度后旧图会自动失效，不会继续复用模糊的缓存。
    """
    config = config or {}
    return {
        "scale": "device",
        "device_scale_factor_level": normalize_scale_level(
            config.get("image_scale_level", DEFAULT_SCALE_LEVEL)
        ),
    }


def _merge_render_options(options: dict | None = None, config: dict | None = None) -> dict:
    merged = dict(DEFAULT_RENDER_OPTIONS)
    merged.update(build_render_options(config))
    if options:
        merged.update(options)
    return merged


def _callable_accepts_keyword(func: Any, name: str) -> bool:
    """判断渲染器是否接受某个关键字参数，兼容旧的两参数渲染器。"""
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if parameter.name == name and parameter.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            return True
    return False


def normalize_text(value: Any, limit: int = 200) -> str:
    """清理用户文本：统一换行、移除控制字符、截断超长内容。"""
    text = str(value if value is not None else "")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    text = _CONTROL_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if limit > 0 and len(text) > limit:
        text = text[: max(1, limit - 1)].rstrip() + "…"
    return text


def escape_html(value: Any) -> str:
    """转义所有用户可控文本，防止 HTML 注入。"""
    return html.escape(str(value if value is not None else ""), quote=True)


def _format_timestamp(timestamp: Any) -> str:
    try:
        value = float(timestamp)
    except (TypeError, ValueError):
        return "未知时间"
    if value <= 0:
        return "未设置"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))
    except (OSError, OverflowError, ValueError):
        return "未知时间"


def _scope_label(data: dict) -> str:
    raw = str(data.get("target_umo") or "").strip()
    if data.get("is_global") or raw.upper() == "GLOBAL":
        return "全局"
    if raw:
        return f"作用域：{raw}"
    return "当前会话"


def _status_memo_item(index: int, key: str, data: dict, max_chars: int) -> dict:
    expire_timestamp = data.get("expire_timestamp", 0)
    try:
        permanent = float(expire_timestamp) <= 0
    except (TypeError, ValueError):
        permanent = True
    expire_text = "永久有效" if permanent else f"有效期至 {_format_timestamp(expire_timestamp)}"
    return {
        "index": index,
        "short_id": str(key)[:8],
        "kind": "状态备忘",
        "title": normalize_text(data.get("content", ""), max_chars) or "（空内容）",
        "meta": f"{expire_text} · {_scope_label(data)}",
    }


def _task_item(index: int, key: str, data: dict, max_chars: int) -> dict:
    type_cn = {
        "one_off": "单次",
        "daily": "每日",
        "workday": "工作日",
        "interval": "间隔循环",
    }
    status_cn = {
        "pending": "等待生成",
        "generating": "正在生成",
        "ready": "就绪",
        "failed": "失败",
    }
    task_type = str(data.get("type", "one_off"))
    status = str(data.get("status", "pending"))
    return {
        "index": index,
        "short_id": str(key)[:8],
        "kind": "定时提醒",
        "title": normalize_text(data.get("task_description", ""), max_chars) or "（空描述）",
        "meta": (
            f"{type_cn.get(task_type, task_type)} · "
            f"下次触发：{_format_timestamp(data.get('trigger_timestamp', 0))} · "
            f"状态：{status_cn.get(status, status)} · {_scope_label(data)}"
        ),
    }


def _keyword_item(index: int, key: str, data: dict, max_chars: int) -> dict:
    return {
        "index": index,
        "short_id": str(key)[:8],
        "kind": "关键词监听",
        "title": normalize_text(data.get("task_description", ""), max_chars) or "（空设定）",
        "meta": (
            f"触发词：{normalize_text(data.get('keyword', ''), max_chars)} · "
            f"{_scope_label(data)}"
        ),
    }


def build_memo_view(items: list, config: dict | None = None) -> dict:
    """把 ``_get_all_items()`` 的结果转换为稳定的图片展示模型。

    展示模型只使用绝对时间 / 永久状态，不包含“还有几分钟”这类每次查看都会变化的
    相对时间，因此可以直接参与缓存 key 的规范化计算。
    """
    config = config or {}
    max_items = _as_int(config.get("image_max_items", 50), 50, 1, 200)
    max_chars = _as_int(config.get("image_item_max_chars", 160), 160, 20, 2000)

    section_defs = [
        ("status_memo", "状态 / 人设备忘录", _status_memo_item),
        ("task", "定时提醒与周期计划", _task_item),
        ("keyword_trigger", "关键词主动监听", _keyword_item),
    ]
    sections = {key: {"title": title, "items": []} for key, title, _ in section_defs}
    builders = {key: builder for key, _, builder in section_defs}

    total = len(items)
    shown = 0
    for index, item in enumerate(items, start=1):
        if shown >= max_items:
            break
        item_type = item.get("type") if isinstance(item, dict) else None
        builder = builders.get(item_type)
        if not builder:
            continue
        data = item.get("data") or {}
        key = str(item.get("key") or "")
        sections[item_type]["items"].append(builder(index, key, data, max_chars))
        shown += 1

    truncated = shown < total
    subtitle = f"共 {total} 条"
    if truncated:
        subtitle += f" · 图片仅显示前 {shown} 条"

    footer = "使用 /memo del <序号> 删除条目，/memo edit <序号> <属性> <新值> 修改属性。"
    if truncated:
        footer += " 完整列表请发送 /memo list text。"

    return {
        "title": "AI 备忘录",
        "subtitle": subtitle,
        "sections": [section for section in sections.values() if section["items"]],
        "footer": footer,
        "total": total,
        "shown": shown,
        "truncated": truncated,
    }


def _build_item_html(item: dict) -> str:
    return (
        '<div class="memo-item">'
        '<div class="memo-item-head">'
        f'<span class="memo-index">#{escape_html(item.get("index", ""))}</span>'
        f'<span class="memo-kind">{escape_html(item.get("kind", ""))}</span>'
        f'<span class="memo-id">ID {escape_html(item.get("short_id", ""))}</span>'
        "</div>"
        f'<div class="memo-item-title">{escape_html(item.get("title", ""))}</div>'
        f'<div class="memo-item-meta">{escape_html(item.get("meta", ""))}</div>'
        "</div>"
    )


def build_memo_image_body(view: dict) -> str:
    """生成卡片正文 HTML；所有用户内容均已转义。"""
    parts = [
        '<div class="memo-card">',
        '<div class="memo-header">',
        f'<div class="memo-title">{escape_html(view.get("title", ""))}</div>',
        f'<div class="memo-subtitle">{escape_html(view.get("subtitle", ""))}</div>',
        "</div>",
    ]

    for section in view.get("sections", []):
        items = section.get("items", [])
        if not items:
            continue
        parts.append('<div class="memo-section">')
        parts.append(
            '<div class="memo-section-title">'
            f'{escape_html(section.get("title", ""))}'
            f'<span class="memo-count">{len(items)}</span>'
            "</div>"
        )
        parts.extend(_build_item_html(item) for item in items)
        parts.append("</div>")

    parts.append(f'<div class="memo-footer">{escape_html(view.get("footer", ""))}</div>')
    parts.append("</div>")
    return "".join(parts)


def image_cache_payload(view: dict, config: dict | None = None, options: dict | None = None) -> dict:
    """构造缓存 key 的规范化载荷（仅包含展示内容与影响显示的配置）。"""
    config = config or {}
    return {
        "schema": VIEW_SCHEMA_VERSION,
        "template": RENDER_TEMPLATE_VERSION,
        "style_version": str(config.get("image_style_version", DEFAULT_STYLE_VERSION)),
        "max_items": _as_int(config.get("image_max_items", 50), 50, 1, 200),
        "item_max_chars": _as_int(config.get("image_item_max_chars", 160), 160, 20, 2000),
        "render_options": _merge_render_options(options, config),
        "view": view,
    }


def image_cache_key(view: dict, config: dict | None = None, options: dict | None = None) -> str:
    payload = image_cache_payload(view, config, options)
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def detect_image_suffix(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    return ".png"


def _looks_like_image(data: bytes) -> bool:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if data.startswith(b"\xff\xd8\xff"):
        return True
    if data.startswith((b"GIF87a", b"GIF89a")):
        return True
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return data.startswith(b"BM")


def _coerce_renderer_result(result: Any) -> bytes:
    if isinstance(result, bytes):
        data = result
    elif isinstance(result, bytearray):
        data = bytes(result)
    elif isinstance(result, (str, os.PathLike)):
        path = os.fspath(result)
        if str(path).startswith(("http://", "https://")):
            raise MemoImageError("渲染器返回了 URL，但这里需要本地图片或 bytes")
        try:
            with open(path, "rb") as image_file:
                data = image_file.read()
        except OSError as exc:
            raise MemoImageError(f"读取渲染器输出图片失败: {exc}") from exc
    else:
        raise MemoImageError(f"不支持的渲染器返回值类型: {type(result).__name__}")

    if not data:
        raise MemoImageError("渲染器返回了空图片")
    if not _looks_like_image(data):
        raise MemoImageError("渲染器返回值不是受支持的图片格式")
    return data


async def render_memo_image(
    data: dict,
    config: dict | None = None,
    renderer: Any = None,
    options: dict | None = None,
) -> bytes:
    """把展示模型渲染为图片 bytes。

    ``renderer`` 可以是实现了 ``render_custom_template(template, data, return_url=False)``
    的对象（例如 AstrBot 的 ``context.html_renderer``），也可以是
    ``renderer(template, data)`` 形式的可调用对象。渲染失败时抛出
    :class:`MemoImageError`，由调用方决定回退文字。

    ``options`` 会合并进默认渲染选项。默认使用无损 PNG，避免 AstrBot 默认
    ``jpeg quality=40`` 造成的文字发糊。
    """
    if renderer is None:
        raise MemoImageError("未提供图片渲染器")
    if not isinstance(data, dict):
        raise MemoImageError("展示模型必须是 dict")

    body = build_memo_image_body(data)
    template_data = {"body": body}
    render_options = _merge_render_options(options, config)

    try:
        if hasattr(renderer, "render_custom_template"):
            render_method = renderer.render_custom_template
            if _callable_accepts_keyword(render_method, "options"):
                result = render_method(
                    HTML_TEMPLATE,
                    template_data,
                    return_url=False,
                    options=render_options,
                )
            else:
                result = render_method(
                    HTML_TEMPLATE,
                    template_data,
                    return_url=False,
                )
        elif callable(renderer):
            if _callable_accepts_keyword(renderer, "options"):
                result = renderer(HTML_TEMPLATE, template_data, options=render_options)
            else:
                result = renderer(HTML_TEMPLATE, template_data)
        else:
            raise MemoImageError("渲染器既没有 render_custom_template，也不可调用")

        if inspect.isawaitable(result):
            result = await result
    except MemoImageError:
        raise
    except Exception as exc:
        raise MemoImageError(f"图片渲染失败: {exc}") from exc

    return _coerce_renderer_result(result)


class MemoImageCache:
    """插件数据目录内的图片缓存。

    - 文件名是规范化展示内容的 SHA-256，内容或样式变化时自动换 key。
    - 写入使用同目录临时文件 + ``os.replace`` 原子替换。
    - 命中前检查文件存在、是普通文件且大小大于 0。
    - 每次写入后按 TTL 与总容量清理旧文件。
    """

    def __init__(
        self,
        directory: str,
        max_bytes: int = 32 * 1024 * 1024,
        ttl_seconds: int = 7 * 24 * 3600,
        enabled: bool = True,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.directory = str(directory)
        self.max_bytes = max(1, int(max_bytes))
        self.ttl_seconds = max(0, int(ttl_seconds))
        self.enabled = bool(enabled)
        self._log = log or logging.getLogger(__name__).warning
        self._init_error: BaseException | None = None

        if self.enabled:
            try:
                os.makedirs(self.directory, exist_ok=True)
            except OSError as exc:
                self._init_error = exc
                self._log(f"[InstantMemo] 初始化图片缓存目录失败: {exc}")

    def make_key(self, view: dict, config: dict | None = None, options: dict | None = None) -> str:
        return image_cache_key(view, config, options)

    def _path_candidates(self, key: str) -> list:
        return [os.path.join(self.directory, key + suffix) for suffix in sorted(_IMAGE_SUFFIXES)]

    def _find_path(self, key: str) -> str | None:
        for path in self._path_candidates(key):
            if os.path.isfile(path):
                return path
        return None

    def get(self, key: str) -> str | None:
        if not self.enabled:
            return None
        if self._init_error:
            raise MemoImageError(f"图片缓存不可用: {self._init_error}") from self._init_error

        path = self._find_path(key)
        if not path:
            return None

        try:
            stat = os.stat(path)
            if not os.path.isfile(path) or stat.st_size <= 0:
                self._remove_file(path)
                self._log(f"[InstantMemo] 图片缓存文件为空或非法，已忽略: {os.path.basename(path)}")
                return None
            if self.ttl_seconds > 0 and (time.time() - stat.st_mtime) > self.ttl_seconds:
                self._remove_file(path)
                return None
            return path
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise MemoImageError(f"读取图片缓存失败: {exc}") from exc

    def put(self, key: str, data: bytes) -> str:
        if not self.enabled:
            raise MemoImageError("图片缓存未启用")
        if self._init_error:
            raise MemoImageError(f"图片缓存不可用: {self._init_error}") from self._init_error
        if not data:
            raise MemoImageError("拒绝写入空图片缓存")

        raw = bytes(data)
        suffix = detect_image_suffix(raw)
        final_path = os.path.join(self.directory, key + suffix)
        temp_fd = None
        temp_path = None
        try:
            os.makedirs(self.directory, exist_ok=True)
            temp_fd, temp_path = tempfile.mkstemp(
                prefix=f".{key}.",
                suffix=".tmp",
                dir=self.directory,
            )
            with os.fdopen(temp_fd, "wb") as temp_file:
                temp_fd = None
                temp_file.write(raw)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, final_path)
            temp_path = None
        except OSError as exc:
            raise MemoImageError(f"写入图片缓存失败: {exc}") from exc
        finally:
            if temp_fd is not None:
                try:
                    os.close(temp_fd)
                except OSError:
                    pass
            if temp_path:
                self._remove_file(temp_path)

        # 同一 key 的旧格式文件不应继续占用空间。
        for old_path in self._path_candidates(key):
            if old_path != final_path:
                self._remove_file(old_path)

        self.cleanup()
        return final_path

    def cleanup(self) -> None:
        """删除过期/临时文件，并按总容量淘汰最旧的缓存。"""
        if not self.enabled or self._init_error:
            return
        try:
            entries = list(os.scandir(self.directory))
        except OSError as exc:
            self._log(f"[InstantMemo] 扫描图片缓存目录失败: {exc}")
            return

        now = time.time()
        cached = []
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
                name = entry.name
                if name.startswith(".") and name.endswith(".tmp"):
                    stat = entry.stat()
                    if now - stat.st_mtime > 3600:
                        self._remove_file(entry.path)
                    continue
                if os.path.splitext(name)[1].lower() not in _IMAGE_SUFFIXES:
                    continue
                stat = entry.stat()
                if self.ttl_seconds > 0 and now - stat.st_mtime > self.ttl_seconds:
                    self._remove_file(entry.path)
                    continue
                cached.append((stat.st_mtime, stat.st_size, entry.path))
            except OSError:
                continue

        total_size = sum(size for _, size, _ in cached)
        if total_size <= self.max_bytes:
            return

        cached.sort(key=lambda item: item[0])
        # 至少保留最新的一个文件，避免单张图片大于上限时反复渲染。
        while total_size > self.max_bytes and len(cached) > 1:
            _, size, path = cached.pop(0)
            self._remove_file(path)
            total_size -= size

    def clear(self) -> None:
        """清空所有缓存图片。"""
        if not self.enabled:
            return
        try:
            entries = list(os.scandir(self.directory))
        except OSError as exc:
            self._log(f"[InstantMemo] 清空图片缓存失败: {exc}")
            return
        for entry in entries:
            try:
                if entry.is_file() and (
                    os.path.splitext(entry.name)[1].lower() in _IMAGE_SUFFIXES
                    or entry.name.endswith(".tmp")
                ):
                    self._remove_file(entry.path)
            except OSError:
                continue

    def _remove_file(self, path: str) -> None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            pass
