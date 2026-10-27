"""FFmpeg 执行端（E2）：按时间线（EDL JSON）裁切、统一尺寸帧率、拼接、可选转场与字幕。

LLM / skill 只产出时间线，命令行由这里确定性地拼出来——不让模型直接写 ffmpeg 参数。
CPU 资源类：不经内存闸门，不卸载 LLM，不释放 ComfyUI。

时间线格式：
    {"width": 1280, "height": 720, "fps": 24, "transition": {"type": "fade", "seconds": 0.3},
     "clips": [{"file": "/abs/a.mp4", "in": 0.0, "out": 2.5}, ...],
     "subtitles": [{"start": 0.0, "end": 2.0, "text": "……"}, ...]}
字幕：本机 ffmpeg 没有 libass / drawtext，写成软字幕轨（mp4 mov_text）+ 同名 .srt。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import Poll, Prepared

TRANSITIONS = {"fade", "dissolve", "wipeleft", "wiperight", "slideleft", "slideright"}


def probe_seconds(path: str | Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return float(out.stdout.strip())
    except ValueError:
        raise ValueError(f"ffprobe 读不出时长：{path}") from None


def _srt_time(s: float) -> str:
    ms = round(s * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{sec:02},{ms:03}"


def to_srt(subs: list[dict]) -> str:
    return "\n".join(
        f"{i}\n{_srt_time(s['start'])} --> {_srt_time(s['end'])}\n{s['text']}\n"
        for i, s in enumerate(subs, 1)
    )


def build_command(tl: dict, out: Path, srt: Path | None) -> list[str]:
    """时间线 → ffmpeg 参数（纯函数，便于测试）。"""
    clips = tl["clips"]
    w, h, fps = int(tl.get("width", 1280)), int(tl.get("height", 720)), int(tl.get("fps", 24))
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for c in clips:
        cmd += ["-i", c["file"]]
    if srt:
        cmd += ["-i", str(srt)]
    parts = []
    for i, c in enumerate(clips):
        trim = f"trim=start={c.get('in', 0)}" + (f":end={c['out']}" if c.get("out") else "")
        parts.append(
            f"[{i}:v]{trim},setpts=PTS-STARTPTS,"
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p[v{i}]"
        )
    tr = tl.get("transition") or {}
    if len(clips) > 1 and tr.get("type") in TRANSITIONS and tr.get("seconds"):
        d = float(tr["seconds"])
        acc, prev = 0.0, "v0"
        for i in range(1, len(clips)):
            acc += clips[i - 1]["_len"] - d
            parts.append(
                f"[{prev}][v{i}]xfade=transition={tr['type']}:duration={d}:offset={acc:.3f}[x{i}]"
            )
            prev = f"x{i}"
        last = prev
    else:
        parts.append(
            "".join(f"[v{i}]" for i in range(len(clips))) + f"concat=n={len(clips)}:v=1:a=0[out]"
        )
        last = "out"
    cmd += ["-filter_complex", ";".join(parts), "-map", f"[{last}]"]
    if srt:
        cmd += ["-map", f"{len(clips)}:s", "-c:s", "mov_text"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-movflags", "+faststart"]
    return cmd + [str(out)]


class FFmpegExecutor:
    name = "ffmpeg"

    def __init__(self, cfg):
        self.cfg = cfg
        self.work = Path(tempfile.gettempdir()) / "aco_lw_ffmpeg"
        self.jobs: dict[str, subprocess.Popen] = {}

    def prepare(self, template, task: dict) -> Prepared:
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("需要 ffmpeg（brew install ffmpeg）")
        tl = json.loads(Path(task["params"]["timeline"]).read_text())
        if not tl.get("clips"):
            raise ValueError("时间线里没有片段")
        for c in tl["clips"]:
            if not Path(c["file"]).is_file():
                raise FileNotFoundError(f"找不到片段 {c['file']}")
            full = probe_seconds(c["file"])
            end = min(float(c.get("out") or full), full)
            if end <= float(c.get("in", 0)):
                raise ValueError(f"片段裁切区间为空：{c}")
            c["out"], c["_len"] = end, end - float(c.get("in", 0))
        subs = tl.get("subtitles") if task["params"].get("subtitles", True) else None
        return Prepared(
            payload={"timeline": tl, "subtitles": subs or []},
            artifacts={"timeline.json": json.dumps(tl, ensure_ascii=False, indent=2)},
            note=f"{len(tl['clips'])} 个片段 · 共 {sum(c['_len'] for c in tl['clips']):.1f} s",
        )

    def submit(self, prepared: Prepared, job_id: str) -> None:
        d = self.work / job_id
        d.mkdir(parents=True, exist_ok=True)
        srt = None
        if prepared.payload["subtitles"]:
            srt = d / "final.srt"
            srt.write_text(to_srt(prepared.payload["subtitles"]))
        cmd = build_command(prepared.payload["timeline"], d / "final.mp4", srt)
        (d / "command.json").write_text(json.dumps(cmd, ensure_ascii=False))
        log = (d / "ffmpeg.log").open("w")
        self.jobs[job_id] = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)

    def poll(self, job_id: str) -> Poll:
        p = self.jobs.get(job_id)
        if p is None:  # 进程重启后找不到：便宜，按丢失处理（不自动重提）
            return Poll("lost")
        rc = p.poll()
        if rc is None:
            return Poll("running")
        return Poll("done" if rc == 0 else "failed", result={"rc": rc})

    def failure_detail(self, result: Any) -> str:
        return f"ffmpeg exit {result.get('rc')}"  # 详细输出在 aco_lw_ffmpeg/<job>/ffmpeg.log

    def collect(self, job_id: str, result: Any, dest: Path, metrics: dict) -> list[dict]:
        d = self.work / job_id
        outputs = []
        for name, kind in (("final.mp4", "video"), ("final.srt", "subtitle")):
            if (d / name).exists():
                shutil.move(str(d / name), dest / name)
                outputs.append({"file": name, "kind": kind})
        self.jobs.pop(job_id, None)
        return outputs

    def cancel(self, job_id: str) -> None:
        p = self.jobs.get(job_id)
        if p and p.poll() is None:
            p.terminate()

    def queued_payloads(self) -> list[dict]:
        return []

    def busy(self) -> bool:
        return any(p.poll() is None for p in self.jobs.values())

    def release(self) -> bool:
        return True
