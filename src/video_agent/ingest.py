"""Material intake (a production's brief stage): turn a set of sources into one material pack for the later skills.

Sources:
- text: novel text or a one-line idea
- image: images you provide (upload ID or local path), with name, type (character / location / style / prop)
  and usage (stylize: redraw in the film's style using it as reference; as_is: use as-is as the casting image)
- url: a web page — body text (trafilatura, page scripts are not run) + main image (og:image)
- youtube: title, description, chapters, subtitles (manual or automatic), thumbnail; optionally a few frames as style reference

Fetching happens in the orchestrator, with timeouts and size limits; one failing source is only marked with the reason and does not affect the others.
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
TEXT_KINDS = ("text", "file", "url", "youtube")
TEXT_EXT = (".txt", ".md", ".markdown", ".text")
# honest about who we are, not posing as a browser
UA = "aco_lw/0.1 (local video agent; fetches pages the user links)"
YT_HOSTS = ("youtube.com", "youtu.be", "m.youtube.com", "www.youtube.com", "music.youtube.com")
SUB_LANGS = ["zh-Hans", "zh-CN", "zh", "zh-Hant", "en", "en-US"]
ROLES = ("character", "location", "style", "prop")


def classify(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return "youtube" if any(host == h or host.endswith("." + h) for h in YT_HOSTS) else "url"


def vtt_to_text(vtt: str) -> str:
    """Subtitles to plain text: drop timings and style tags, merge the duplicate lines from word-by-word scrolling in auto subtitles."""
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
        file_roots: list[Path] | None = None,
    ):
        self.work_dir = work_dir
        # text files may be read only from inside these folders (e.g. data/: agent work dirs,
        # downloads, uploads)
        self.file_roots = [Path(r).resolve() for r in (file_roots or [])]
        self.resolve_upload = resolve_upload or (lambda x: x)
        self.http = http or httpx.Client(
            timeout=20, follow_redirects=True, headers={"User-Agent": UA}
        )
        self.ytdl = ytdl  # injected by tests; defaults to yt_dlp.YoutubeDL

    def __call__(self, source, settings: dict) -> dict:
        """source: the old format is a piece of text; the new format is {"text": ..., "sources": [...]}."""
        if isinstance(source, str):
            source = {"text": source, "sources": []}
        self.work_dir.mkdir(parents=True, exist_ok=True)
        texts, refs, report = [], [], []
        text = (source.get("text") or "").strip()
        as_file = self._as_file(text)
        if as_file is not None:  # the text is just a path to a text file: read the file
            source = {
                **source,
                "sources": [{"kind": "file", "path": text}, *(source.get("sources") or [])],
            }
        elif text:
            texts.append(("Brief / source text", text))
            report.append({"kind": "text", "status": "ok", "chars": len(text)})
        for i, src in enumerate(source.get("sources") or [], 1):
            kind = src.get("kind") or (
                "image" if src.get("upload") else classify(src.get("url", ""))
            )
            entry = {"kind": kind, "status": "ok"}
            try:
                if kind == "text":
                    texts.append((src.get("title") or f"Note {i}", src["text"].strip()))
                elif kind == "file":
                    body = self._read_file(src["path"])
                    entry.update(path=src["path"], chars=len(body))
                    texts.append(
                        (src.get("title") or f"Source text ({Path(src['path']).name})", body)
                    )
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
            except Exception as e:  # noqa: BLE001 — one failing source does not affect the others
                entry.update(status="failed", error=str(e)[:300], url=src.get("url"))
            report.append(entry)
        if not texts and not refs:
            raise ValueError("no usable source (all failed or empty)")
        failed = [e for e in report if e["status"] == "failed" and e["kind"] in TEXT_KINDS]
        if failed and settings.get("fidelity") == "faithful":
            # a faithful adaptation of half the source is a changed story: stop and say why
            raise ValueError(
                "faithful adaptation needs the whole source, but some text failed to load: "
                + "; ".join(
                    f"{e['kind']} {e.get('path') or e.get('url') or ''}: {e['error']}"
                    for e in failed
                )
            )
        body = "\n\n".join(f"## {h}\n\n{t}" for h, t in texts)
        limit = settings.get("max_source_chars", MAX_TEXT_CHARS * 2)
        out = {"text": body, "references": refs, "sources": report}
        if limit and len(body) > limit:
            out["text"] = body[:limit]
            out["truncated"] = {"chars": len(body), "kept": limit}
        return out

    # ---- text files ----
    def _as_file(self, text: str) -> Path | None:
        """A one-line text that names an existing text file inside the allowed folders."""
        if not text or "\n" in text or len(text) > 500 or not text.endswith(TEXT_EXT):
            return None
        try:
            return self._resolve(text)
        except (FileNotFoundError, PermissionError):
            return None

    def _resolve(self, path: str) -> Path:
        p = Path(path).expanduser()
        cands = [p] if p.is_absolute() else [r / p for r in self.file_roots]
        for c in cands:
            c = c.resolve()
            if c.is_file():
                if not any(c.is_relative_to(r) for r in self.file_roots):
                    raise PermissionError(f"{path} is outside the allowed folders")
                return c
        raise FileNotFoundError(f"text file {path} not found")

    def _read_file(self, path: str) -> str:
        raw = self._resolve(path).read_bytes()
        for enc in ("utf-8", "gb18030", "big5"):
            try:
                return raw.decode(enc).strip()
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace").strip()

    # ---- images ----
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

    # ---- web pages ----
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
            except Exception:  # noqa: BLE001 — a missing main image does not affect the body text
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
            # we only want metadata and subtitles; do not fail because video formats are unavailable
            "ignore_no_formats_error": True,
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
            with contextlib.suppress(Exception):  # a missing thumbnail does not affect the text
                img = self._download(info["thumbnail"], d / "thumbnail")
                if img:
                    imgs.append(img)
        frames = int(settings.get("youtube_frames") or 0)
        if frames > 0:
            try:
                imgs += self._frames(url, d, frames, info.get("duration") or 0)
            # failing to grab frames does not affect text and thumbnail
            except Exception as e:  # noqa: BLE001
                parts.append(f"(Frames unavailable: {str(e)[:200]})")
        return title, "\n\n".join(parts), imgs

    def _ydl(self, opts: dict):
        if self.ytdl:
            return self.ytdl(opts)
        import yt_dlp

        return yt_dlp.YoutubeDL(opts)

    def _frames(self, url: str, d: Path, n: int, duration: float) -> list[str]:
        """Download a low-res video-only stream (≤ 360p, ≤ 200 MB), grab n evenly spaced frames as style reference, then delete it."""
        opts = {
            "quiet": True,
            "no_warnings": True,
            # frames only need the picture: a video-only stream ≤ 360p (YouTube mostly serves audio
            # and video separately)
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
