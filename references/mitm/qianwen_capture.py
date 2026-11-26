"""
mitmproxy 抓包插件：定位千问 App 渲染回答时调用的「会话接口」。

背景
----
现有流水线依赖分享链接 → `chat2-api.qianwen.com/api/v1/share/info` → 拿到
`data.session.record_list[].response_messages[].meta_data.sources[]` 与 `plan_cot` 思考块。
但「带货回答」（淘宝商品卡）不渲染分享按钮，没有 share_id，share/info 进不去。

App 渲染那条回答时本来就从后端拉过一次同构数据。这个插件在网络层把那次响应截下来，
和有没有分享按钮无关 —— 这是绕过千问反 GEO（不让带货回答外链化）的根本办法。

用法
----
    pip install mitmproxy
    mitmdump -s qianwen_capture.py -p 8080
    # 可选：自定义落盘目录与抓取范围
    mitmdump -s qianwen_capture.py -p 8080 \
        --set qw_capture_dir=outputs/mitm \
        --set qw_capture_all=false

随后让设备走这个代理并装好 mitmproxy CA 证书（见 references/docs/mitm-capture-runbook.md），
在千问 App 里发一条带货问题。命中标记的响应会：
  1. 在终端打印一行摘要（方法 / host+path / 命中的标记 / 字节数）；
  2. 完整 dump 到 outputs/mitm/<时间戳>-<host><path 安全化>.json，
     含请求方法、URL、请求头（cookie 已脱敏）、请求体、响应状态与响应体。

拿到 dump 后，找出那个非 share/info、却含 record_list/sources/plan_cot 的 endpoint，
就是要直连重放的目标接口。把它的 URL、请求体字段（session_id/conversation_id 等）
告诉我，我再把解析接进 runner（复用 extract-sources.js 的字段走法，无需分享链接）。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from mitmproxy import ctx, http

# 命中其一即认为响应携带「会话数据」，值得完整 dump。
# 这些键名直接对应 extract-sources.js 里走的字段路径。
MARKER_KEYS = (
    "record_list",
    "response_messages",
    "meta_data",
    "plan_cot",
    "multi_load",
    '"sources"',
    "deep_think",
    "deep_search",
)

# 千问/通义/阿里相关域名。share/info 在 chat2-api.qianwen.com，会话接口大概率同源。
HOST_HINTS = (
    "qianwen.com",
    "tongyi",
    "aliyun",
    "dashscope",
    "aliyuncs.com",
)

# 请求头里这些字段含敏感凭证，dump 时脱敏（保留是否存在 + 长度，便于判断登录态）。
SENSITIVE_HEADERS = {"cookie", "authorization", "x-tongyi-token", "x-xsrf-token", "set-cookie"}


class QianwenCapture:
    def load(self, loader) -> None:
        loader.add_option(
            "qw_capture_dir", str, "outputs/mitm",
            "目录：命中的响应 dump 到这里（默认 outputs/mitm，已被 .gitignore 忽略）",
        )
        loader.add_option(
            "qw_capture_all", bool, False,
            "为 true 时，dump 所有千问相关响应（不止命中标记的），用于排查 endpoint。",
        )

    def _capture_dir(self) -> Path:
        d = Path(ctx.options.qw_capture_dir)
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _host_interesting(host: str) -> bool:
        host = host.lower()
        return any(hint in host for hint in HOST_HINTS)

    @staticmethod
    def _found_markers(text: str) -> list[str]:
        return [m for m in MARKER_KEYS if m in text]

    @staticmethod
    def _safe_name(flow: http.HTTPFlow) -> str:
        host = flow.request.host
        path = flow.request.path.split("?", 1)[0]
        raw = f"{host}{path}"
        return re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("_")[:120]

    @staticmethod
    def _redact_headers(headers) -> dict:
        out = {}
        for k, v in headers.items():
            if k.lower() in SENSITIVE_HEADERS:
                out[k] = f"<redacted len={len(v)}>"
            else:
                out[k] = v
        return out

    @staticmethod
    def _pretty(text: str):
        """尽量解析为 JSON 返回（便于人读）；失败则原样返回字符串。"""
        try:
            return json.loads(text)
        except Exception:
            return text

    def response(self, flow: http.HTTPFlow) -> None:
        if not self._host_interesting(flow.request.host):
            return

        ctype = flow.response.headers.get("content-type", "")
        is_streamy = "event-stream" in ctype
        # 流式（SSE）回答生成接口也抓，原始文本里通常含 plan_cot/source 增量。
        try:
            body = flow.response.get_text(strict=False) or ""
        except Exception:
            return
        if not body:
            return

        markers = self._found_markers(body)
        if not markers and not ctx.options.qw_capture_all:
            return

        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = f"{stamp}-{self._safe_name(flow)}.json"
        out_path = self._capture_dir() / name

        req_body = None
        if flow.request.content:
            try:
                req_body = self._pretty(flow.request.get_text(strict=False) or "")
            except Exception:
                req_body = "<unreadable>"

        record = {
            "capturedAt": stamp,
            "method": flow.request.method,
            "url": flow.request.url,
            "host": flow.request.host,
            "path": flow.request.path,
            "status": flow.response.status_code,
            "contentType": ctype,
            "isEventStream": is_streamy,
            "markersFound": markers,
            "requestHeaders": self._redact_headers(flow.request.headers),
            "requestBody": req_body,
            # SSE 原始文本可能很大；JSON 接口则解析后存，方便核对 record_list 结构。
            "responseBody": body if is_streamy else self._pretty(body),
        }

        out_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        flag = "SSE" if is_streamy else "JSON"
        ctx.log.alert(
            f"[qw-capture] {flag} {flow.request.method} "
            f"{flow.request.host}{flow.request.path.split('?',1)[0]} "
            f"markers={markers} bytes={len(body)} -> {out_path}"
        )


addons = [QianwenCapture()]
