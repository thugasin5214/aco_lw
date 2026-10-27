"""素材整理（制片的 brief 阶段）：把一组来源整理成一份素材包，供后面的 skill 读取。

来源：
- text：小说原文或一句话构想
- image：你提供的图片（upload ID 或本地路径），带名字、类型（character / location / style / prop）
  和用法（stylize：以它为参考按片子风格重画；as_is：原样作为定妆图）
- url：网页——正文（trafilatura，不执行页面脚本）+ 主图（og:image）
- youtube：标题、简介、章节、字幕（人工或自动）、缩略图；可选按设置抽几帧作风格参考

抓取在编排器里做：有超时与大小上限；某个来源失败只标出原因，不影响其他来源。
"""

from __future__ import annotations

import contextlib
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

import httpx

MAX_PAGE_BYTES = 5 * 2**20
MAX_IMAGE_BYTES = 15 * 2**20
MAX_TEXT_CHARS = 20000
UA = "aco_lw/0.1 (local video agent; fetches pages the user links)"  # 如实标明，不伪装浏览器
YT_HOSTS = ("youtube.com", "youtu.be", "m.youtube.com", "www.youtube.com", "music.youtube.com")
SUB_LANGS = ["zh-Hans", "zh-CN", "zh", "zh-Hant", "en", "en-US"]
ROLES = ("character", "location", "style", "prop")


def classify(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return "youtube" if any(host == h or host.endswith("." + h) for h in YT_HOSTS) else "url"


def vtt_to_text(vtt: str) -> str:
    """字幕转纯文本：去掉时间轴与样式标签，合并自动字幕里逐词滚动造成的重复行。"""
    out: list[str] = []
    for line in vtt.splitlines():
        line = line.strip()
        if not line or "-->" in line or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        if line.isdigit():
            continue
        line = re.sub(r"<[^>]+>", "", line).strip()
        if line and (not out or out[-1] != line):
            out.append(line)
    return "\n".join(out)


class Ingest:
    def __init__(
        self,
        work_dir: Path,
        resolve_upload: Callable[[str], str] | None = None,
        http: httpx.Client | None = None,
        ytdl: Callable[[dict], object] | None = None,
    ):
        self.work_dir = work_dir
        self.resolve_upload = resolve_upload or (lambda x: x)
        self.http = http or httpx.Client(
            timeout=20, follow_redirects=True, headers={"User-Agent": UA}
        )
        self.ytdl = ytdl  # 测试注入；默认用 yt_dlp.YoutubeDL

    def __call__(self, source, settings: dict) -> dict:
        """source：旧格式是一段文字；新格式 {"text": ..., "sources": [...]}。"""
        if isinstance(source, str):
            source = {"text": source, "sources": []}
        self.work_dir.mkdir(parents=True, exist_ok=True)
        texts, refs, report = [], [], []
        if (source.get("text") or "").strip():
            texts.append(("Brief / source text", source["text"].strip()))
            report.append({"kind": "text", "status": "ok", "chars": len(source["text"])})
        for i, src in enumerate(source.get("sources") or [], 1):
            kind = src.get("kind") or (
                "image" if src.get("upload") else classify(src.get("url", ""))
            )
            entry = {"kind": kind, "status": "ok"}
            try:
                if kind == "text":
                    texts.append((src.get("title") or f"Note {i}", src["text"].strip()))
                elif kind == "image":
                    refs.append(self._image(src, len(refs) + 1))
                    entry["ref"] = refs[-1]["id"]
                elif kind == "url":
                    title, text, img = self._page(src["url"], i)
                    entry.update(url=src["url"], title=title, chars=len(text))
                    texts.append((f"Web page: {title or src['url']}", text))
                    if img:
                        refs.append(
                            self._ref(
                                len(refs) + 1,
                                img,
                                title or "web image",
                                "style",
                                "stylize",
                                src["url"],
                            )
                        )
                elif kind == "youtube":
                    title, text, imgs = self._youtube(src["url"], i, settings)
                    entry.update(url=src["url"], title=title, chars=len(text), frames=len(imgs))
                    texts.append((f"YouTube: {title}", text))
                    for img in imgs:
                        refs.append(
                            self._ref(
                                len(refs) + 1,
                                img,
                                f"{title} (frame)",
                                "style",
                                "stylize",
                                src["url"],
                            )
                        )
                else:
                    raise ValueError(f"unknown source kind {kind}")
            except Exception as e:  # noqa: BLE001 — 单个来源失败不影响其他来源
                entry.update(status="failed", error=str(e)[:300], url=src.get("url"))
            report.append(entry)
        if not texts and not refs:
            raise ValueError("no usable source (all failed or empty)")
        body = "\n\n".join(f"## {h}\n\n{t}" for h, t in texts)
        return {"text": body[: MAX_TEXT_CHARS * 2], "references": refs, "sources": report}

    # ---- 图片 ----
    def _ref(self, n: int, path: str, label: str, role: str, use: str, origin: str = "") -> dict:
        return {
            "id": f"r{n}",
            "label": label,
            "role": role,
            "use": use,
            "path": str(path),
            "from": origin,
        }

    def _image(self, src: dict, n: int) -> dict:
        path = self.resolve_upload(src["upload"]) if src.get("upload") else src["path"]
        if not Path(path).is_file():
            raise FileNotFoundError(f"image not found: {path}")
        role = src.get("role") if src.get("role") in ROLES else "character"
        use = "as_is" if src.get("use") == "as_is" else "stylize"
        ref = self._ref(n, path, src.get("label") or Path(path).stem, role, use, "upload")
        if src.get("note"):
            ref["note"] = src["note"]
        return ref

    def _download(self, url: str, dest: Path) -> str | None:
        with self.http.stream("GET", url) as r:
            r.raise_for_status()
            if not r.headers.get("content-type", "").startswith("image/"):
                return None
            data = b""
            for chunk in r.iter_bytes():
                data += chunk
                if len(data) > MAX_IMAGE_BYTES:
                    return None
        ext = {"image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(
            r.headers.get("content-type", "").split(";")[0], ".jpg"
        )
        out = dest.with_suffix(ext)
        out.write_bytes(data)
        return str(out)

    # ---- 网页 ----
    def _page(self, url: str, n: int) -> tuple[str, str, str | None]:
        import trafilatura

        with self.http.stream("GET", url) as r:
            r.raise_for_status()
            ctype = r.headers.get("content-type", "")
            if "html" not in ctype and "text" not in ctype:
                raise ValueError(f"not a web page ({ctype or 'unknown type'})")
            raw = b""
            for chunk in r.iter_bytes():
                raw += chunk
                if len(raw) > MAX_PAGE_BYTES:
                    raise ValueError("page larger than 5 MB")
        html = raw.decode(r.encoding or "utf-8", errors="replace")
        text = trafilatura.extract(html, include_comments=False, include_tables=False) or ""
        meta = trafilatura.extract_metadata(html)
        title = (meta.title if meta else None) or ""
        if not text.strip():
            raise ValueError("no readable text on the page")
        m = re.search(
            r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)',
            html,
            re.IGNORECASE,
        ) or re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image', html, re.IGNORECASE
        )
        img = None
        if m:
            try:
                img = self._download(
                    str(httpx.URL(url).join(m.group(1))), self.work_dir / f"web{n}"
                )
            except Exception:  # noqa: BLE001 — 主图拿不到不影响正文
                img = None
        return title, text[:MAX_TEXT_CHARS], img

    # ---- YouTube ----
    def _youtube(self, url: str, n: int, settings: dict) -> tuple[str, str, list[str]]:
        d = self.work_dir / f"yt{n}"
        d.mkdir(parents=True, exist_ok=True)
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": SUB_LANGS,
            "subtitlesformat": "vtt",
            "outtmpl": str(d / "%(id)s.%(ext)s"),
            "noplaylist": True,
            "ignore_no_formats_error": True,  # 只要元数据与字幕，不因视频格式不可用而失败
        }
        info = self._ydl(opts).extract_info(url, download=True)
        title = info.get("title") or url
        parts = [f"Title: {title}", f"Channel: {info.get('uploader') or ''}"]
        if info.get("duration"):
            parts.append(f"Duration: {info['duration']} s")
        if info.get("description"):
            parts.append("Description:\n" + info["description"][:4000])
        if info.get("chapters"):
            parts.append(
                "Chapters:\n"
                + "\n".join(
                    f"- {c.get('start_time', 0):.0f}s {c.get('title', '')}"
                    for c in info["chapters"]
                )
            )
        subs = sorted(
            d.glob("*.vtt"),
            key=lambda p: next(
                (i for i, lang in enumerate(SUB_LANGS) if f".{lang}." in p.name), 99
            ),
        )
        if subs:
            parts.append(
                "Transcript:\n" + vtt_to_text(subs[0].read_text(errors="replace"))[:MAX_TEXT_CHARS]
            )
        imgs: list[str] = []
        if info.get("thumbnail"):
            with contextlib.suppress(Exception):  # 缩略图拿不到不影响文字
                img = self._download(info["thumbnail"], d / "thumbnail")
                if img:
                    imgs.append(img)
        frames = int(settings.get("youtube_frames") or 0)
        if frames > 0:
            try:
                imgs += self._frames(url, d, frames, info.get("duration") or 0)
            except Exception as e:  # noqa: BLE001 — 抽帧失败不影响文字与缩略图
                parts.append(f"(Frames unavailable: {str(e)[:200]})")
        return title, "\n\n".join(parts), imgs

    def _ydl(self, opts: dict):
        if self.ytdl:
            return self.ytdl(opts)
        import yt_dlp

        return yt_dlp.YoutubeDL(opts)

    def _frames(self, url: str, d: Path, n: int, duration: float) -> list[str]:
        """下载低清纯视频流（≤ 360p、≤ 200 MB），均匀抽 n 帧作风格参考，用完即删。"""
        opts = {
            "quiet": True,
            "no_warnings": True,
            # 抽帧只要画面：≤ 360p 的纯视频流（YouTube 多数只提供音视频分离的流）
            "format": "bv*[height<=360][ext=mp4]/bv*[height<=360]/wv*/worst",
            "max_filesize": 200 * 2**20,
            "noprogress": True,
            "outtmpl": str(d / "video.%(ext)s"),
            "noplaylist": True,
        }
        self._ydl(opts).download([url])
        video = next(iter(sorted(d.glob("video.*"))), None)
        if video is None or not duration:
            return []
        out = []
        for i in range(n):
            t = duration * (i + 0.5) / n
            f = d / f"frame{i + 1}.jpg"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{t:.2f}",
                    "-i",
                    str(video),
                    "-frames:v",
                    "1",
                    str(f),
                ],
                check=False,
            )
            if f.exists():
                out.append(str(f))
        video.unlink(missing_ok=True)
        return out
