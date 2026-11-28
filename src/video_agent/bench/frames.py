"""Frames and contact sheets from delivered videos (ffmpeg)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .export import probe_seconds


def sample(video: Path, out_dir: Path, n: int = 12) -> list[Path]:
    """n frames spread evenly over the video (skipping the very first / last 3%)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dur = probe_seconds(video) or 0
    frames = []
    for i in range(n):
        t = dur * (0.03 + 0.94 * i / max(n - 1, 1))
        f = out_dir / f"{video.stem}_{i:02d}.jpg"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-ss",
                f"{t:.2f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-q:v",
                "3",
                str(f),
            ],
            check=False,
        )
        if f.exists():
            frames.append(f)
    return frames


def contact_sheet(frames: list[Path], out: Path, cols: int = 4, width: int = 360) -> Path | None:
    """Tile frames into one image (row-major, in time order) for a judge to look at."""
    if not frames:
        return None
    n = len(frames)
    inputs = [x for f in frames for x in ("-i", str(f))]
    scaled = "".join(f"[{i}:v]scale={width}:-2,setsar=1[v{i}];" for i in range(n))
    layout = "|".join(
        f"{'+'.join(['w0'] * (i % cols)) or '0'}_{'+'.join(['h0'] * (i // cols)) or '0'}"
        for i in range(n)
    )
    tiles = "".join(f"[v{i}]" for i in range(n))
    graph = f"{scaled}{tiles}xstack=inputs={n}:layout={layout}:fill=black[out]"
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            *inputs,
            "-filter_complex",
            graph,
            "-map",
            "[out]",
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(out),
        ],
        check=False,
    )
    return out if out.exists() else None
