import asyncio
import json
import os
import sys
import tempfile
import time
import unittest

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

from memo_image import (
    DEFAULT_RENDER_OPTIONS,
    HTML_TEMPLATE,
    MIN_VIEWPORT_HEIGHT,
    MemoImageCache,
    MemoImageError,
    build_memo_image_body,
    build_memo_view,
    build_render_options,
    image_cache_key,
    image_cache_payload,
    normalize_scale_level,
    render_memo_image,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"test-image-data"


def sample_items():
    now = time.time()
    return [
        {
            "type": "status_memo",
            "key": "11111111-2222-3333-4444-555555555555",
            "data": {
                "content": "正在打游戏",
                "expire_timestamp": now + 3600,
                "target_umo": "GLOBAL",
            },
        },
        {
            "type": "task",
            "key": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "data": {
                "type": "daily",
                "task_description": "提醒我喝水",
                "trigger_timestamp": now + 7200,
                "status": "ready",
                "target_umo": "GroupMessage:123456",
            },
        },
        {
            "type": "keyword_trigger",
            "key": "99999999-8888-7777-6666-555555555555",
            "data": {
                "keyword": "吃饭",
                "task_description": "自然地问问对方吃了什么",
                "is_global": True,
                "target_umo": "GLOBAL",
            },
        },
    ]


class MemoViewTests(unittest.TestCase):
    def test_view_is_stable_and_has_no_relative_time(self):
        config = {"image_max_items": 50, "image_item_max_chars": 160}
        first = build_memo_view(sample_items(), config)
        second = build_memo_view(sample_items(), config)

        self.assertEqual(image_cache_key(first, config), image_cache_key(second, config))
        payload_text = json.dumps(image_cache_payload(first, config), ensure_ascii=False)
        self.assertNotIn("分钟后", payload_text)
        self.assertIn("有效期至", payload_text)

    def test_cache_key_ignores_unrelated_config_and_changes_with_content_or_style(self):
        base = {
            "poll_interval": 15,
            "image_style_version": "1",
            "image_max_items": 50,
            "image_item_max_chars": 160,
        }
        view = build_memo_view(sample_items(), base)
        same_view_config = dict(base, poll_interval=99)
        self.assertEqual(image_cache_key(view, base), image_cache_key(view, same_view_config))

        changed_style = dict(base, image_style_version="2")
        self.assertNotEqual(image_cache_key(view, base), image_cache_key(view, changed_style))

        items = sample_items()
        items[0]["data"]["content"] = "正在写代码"
        changed_view = build_memo_view(items, base)
        self.assertNotEqual(image_cache_key(view, base), image_cache_key(changed_view, base))

        self.assertNotEqual(
            image_cache_key(view, base),
            image_cache_key(view, base, {"type": "jpeg", "quality": 95}),
        )

    def test_long_content_is_truncated(self):
        items = sample_items()
        items[0]["data"]["content"] = "很长的内容" * 100
        view = build_memo_view(items, {"image_item_max_chars": 30})
        title = view["sections"][0]["items"][0]["title"]
        self.assertLessEqual(len(title), 30)
        self.assertTrue(title.endswith("…"))

    def test_max_items_truncates_and_hints_text_mode(self):
        items = sample_items() * 3
        view = build_memo_view(items, {"image_max_items": 2})
        shown = sum(len(section["items"]) for section in view["sections"])
        self.assertEqual(shown, 2)
        self.assertTrue(view["truncated"])
        self.assertIn("/memo list text", view["footer"])

    def test_html_body_escapes_user_content(self):
        items = sample_items()
        items[0]["data"]["content"] = '<script>alert("x")</script>{{7*7}}&"'
        view = build_memo_view(items, {})
        body = build_memo_image_body(view)

        self.assertIn("&lt;script&gt;", body)
        self.assertNotIn("<script>", body)
        self.assertIn("&amp;", body)
        # 用户内容里的 Jinja 语法不应进入模板，只能作为 data 传入。
        self.assertIn("{{7*7}}", body)


class FakeRenderer:
    def __init__(self, result=PNG_BYTES, error=None):
        self.result = result
        self.error = error
        self.template = None
        self.data = None
        self.options = None

    async def render_custom_template(self, template, data, return_url=False, options=None):
        self.template = template
        self.data = data
        self.options = options
        if self.error:
            raise self.error
        return self.result


class TemplateTests(unittest.TestCase):
    def test_template_keeps_card_style(self):
        # 卡片样式已回退到改动前的版本，避免破坏原项目的视觉风格。
        self.assertIn(".memo-card", HTML_TEMPLATE)
        self.assertIn("background: #eef1f5", HTML_TEMPLATE)
        self.assertIn("border-radius: 18px", HTML_TEMPLATE)
        self.assertIn("background: #fafbfc", HTML_TEMPLATE)

    def test_render_options_do_not_override_viewport_width(self):
        # 卡片布局沿用模板原有视口宽度，清晰度只通过设备像素比提升。
        self.assertNotIn("viewport_width", DEFAULT_RENDER_OPTIONS)

    def test_render_options_shrink_viewport_height(self):
        # 视口高度必须远小于内容，否则 full_page 截图会被撑高，条目删减后底部留白。
        self.assertEqual(DEFAULT_RENDER_OPTIONS["viewport_height"], MIN_VIEWPORT_HEIGHT)
        self.assertLess(MIN_VIEWPORT_HEIGHT, 720)

        renderer = FakeRenderer()
        asyncio.run(render_memo_image(build_memo_view(sample_items(), {}), {}, renderer=renderer))
        self.assertEqual(renderer.options["viewport_height"], MIN_VIEWPORT_HEIGHT)


class RenderOptionTests(unittest.TestCase):
    def test_default_options_request_device_scaling(self):
        self.assertEqual(DEFAULT_RENDER_OPTIONS["scale"], "device")
        self.assertEqual(DEFAULT_RENDER_OPTIONS["device_scale_factor_level"], "high")
        self.assertEqual(DEFAULT_RENDER_OPTIONS["type"], "png")

        renderer = FakeRenderer()
        asyncio.run(render_memo_image(build_memo_view(sample_items(), {}), {}, renderer=renderer))
        self.assertEqual(renderer.options["scale"], "device")
        self.assertEqual(renderer.options["device_scale_factor_level"], "high")

    def test_scale_level_config_is_applied_and_falls_back(self):
        self.assertEqual(
            build_render_options({"image_scale_level": "ultra"})["device_scale_factor_level"],
            "ultra",
        )
        self.assertEqual(normalize_scale_level("ULTRA"), "ultra")
        self.assertEqual(normalize_scale_level("huge"), "high")
        self.assertEqual(normalize_scale_level(None), "high")

        renderer = FakeRenderer()
        asyncio.run(
            render_memo_image(
                build_memo_view(sample_items(), {}),
                {"image_scale_level": "ultra"},
                renderer=renderer,
            )
        )
        self.assertEqual(renderer.options["device_scale_factor_level"], "ultra")

    def test_scale_level_participates_in_cache_key(self):
        view = build_memo_view(sample_items(), {})
        self.assertNotEqual(
            image_cache_key(view, {"image_scale_level": "high"}),
            image_cache_key(view, {"image_scale_level": "ultra"}),
        )


class RendererTests(unittest.TestCase):
    def test_render_uses_data_channel_and_returns_bytes(self):
        items = sample_items()
        items[0]["data"]["content"] = "<b>用户内容</b>{{7*7}}"
        view = build_memo_view(items, {})
        renderer = FakeRenderer()

        result = asyncio.run(render_memo_image(view, {}, renderer=renderer))

        self.assertEqual(result, PNG_BYTES)
        self.assertIn("&lt;b&gt;用户内容&lt;/b&gt;", renderer.data["body"])
        self.assertNotIn("{{7*7}}", renderer.template)
        self.assertIn("{{7*7}}", renderer.data["body"])
        self.assertEqual(renderer.options["type"], "png")
        self.assertTrue(renderer.options["full_page"])

    def test_render_options_can_be_overridden(self):
        renderer = FakeRenderer()
        asyncio.run(
            render_memo_image(
                build_memo_view(sample_items(), {}),
                {},
                renderer=renderer,
                options={"type": "jpeg", "quality": 95},
            )
        )

        self.assertEqual(renderer.options["type"], "jpeg")
        self.assertEqual(renderer.options["quality"], 95)
        self.assertTrue(renderer.options["full_page"])

    def test_legacy_two_argument_callable_is_supported(self):
        async def legacy_renderer(template, data):
            return PNG_BYTES

        result = asyncio.run(
            render_memo_image(
                build_memo_view(sample_items(), {}),
                {},
                renderer=legacy_renderer,
            )
        )
        self.assertEqual(result, PNG_BYTES)

    def test_callable_renderer_receives_options(self):
        received = {}

        async def renderer(template, data, options=None):
            received.update(options or {})
            return PNG_BYTES

        asyncio.run(
            render_memo_image(
                build_memo_view(sample_items(), {}),
                {},
                renderer=renderer,
            )
        )
        self.assertEqual(received["type"], "png")

    def test_renderer_failure_is_wrapped(self):
        renderer = FakeRenderer(error=RuntimeError("t2i down"))
        with self.assertRaises(MemoImageError):
            asyncio.run(render_memo_image(build_memo_view(sample_items(), {}), {}, renderer=renderer))

    def test_invalid_renderer_output_is_rejected(self):
        renderer = FakeRenderer(result=b"<html>not an image</html>")
        with self.assertRaises(MemoImageError):
            asyncio.run(render_memo_image(build_memo_view(sample_items(), {}), {}, renderer=renderer))

    def test_missing_renderer_is_rejected(self):
        with self.assertRaises(MemoImageError):
            asyncio.run(render_memo_image(build_memo_view(sample_items(), {}), {}))


class CacheTests(unittest.TestCase):
    def test_put_get_and_atomic_write(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = MemoImageCache(directory, max_bytes=1024 * 1024, ttl_seconds=0)
            path = cache.put("key1", PNG_BYTES)

            self.assertTrue(os.path.isfile(path))
            self.assertGreater(os.path.getsize(path), 0)
            self.assertEqual(cache.get("key1"), path)
            self.assertFalse(any(name.endswith(".tmp") for name in os.listdir(directory)))

    def test_empty_cache_file_is_a_miss(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = MemoImageCache(
                directory,
                max_bytes=1024 * 1024,
                ttl_seconds=0,
                log=lambda _: None,
            )
            empty_path = os.path.join(directory, "empty.png")
            with open(empty_path, "wb"):
                pass

            self.assertIsNone(cache.get("empty"))
            self.assertFalse(os.path.exists(empty_path))

    def test_ttl_expiry_removes_file(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = MemoImageCache(directory, max_bytes=1024 * 1024, ttl_seconds=10)
            path = cache.put("ttl", PNG_BYTES)
            old = time.time() - 3600
            os.utime(path, (old, old))

            self.assertIsNone(cache.get("ttl"))
            self.assertFalse(os.path.exists(path))

    def test_capacity_evicts_oldest_file(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = MemoImageCache(directory, max_bytes=24, ttl_seconds=0)
            first = cache.put("first", PNG_BYTES)
            old = time.time() - 3600
            os.utime(first, (old, old))

            second = cache.put("second", PNG_BYTES)

            self.assertFalse(os.path.exists(first))
            self.assertTrue(os.path.exists(second))
            self.assertEqual(cache.get("second"), second)

    def test_clear_removes_cached_images(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = MemoImageCache(directory, max_bytes=1024 * 1024, ttl_seconds=0)
            cache.put("clear-me", PNG_BYTES)
            cache.clear()

            self.assertEqual(cache.get("clear-me"), None)
            self.assertEqual(os.listdir(directory), [])

    def test_unavailable_cache_directory_raises_for_fallback(self):
        with tempfile.NamedTemporaryFile() as occupied_path:
            cache = MemoImageCache(occupied_path.name, enabled=True, log=lambda _: None)
            with self.assertRaises(MemoImageError):
                cache.get("anything")
            with self.assertRaises(MemoImageError):
                cache.put("anything", PNG_BYTES)


if __name__ == "__main__":
    unittest.main()
