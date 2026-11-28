"""Read-only Docker resource probe for the resource scheduler.

Docker is a third heavy consumer of this Mac's unified memory, alongside the local LLM
(~19.5 GiB) and ComfyUI / FreeVideo renders (~54 GiB peak). On macOS the Docker Desktop
VM's footprint is already counted in psutil's system used / available, so this probe does
**not** change the admission math (subtracting it again would double-count). It *attributes*
how much of the used memory Docker holds, for the console resource curve, the gate's
`at_submit` record, and future policy.

Safety (non-negotiable — matches both this project's and flawDetection's rules):
- Read-only only. `READONLY_SUBCOMMANDS` is the complete set of docker subcommands this
  module may ever run; anything else raises `DockerUnsafe`.
- Never starts the Docker daemon, never starts / stops / kills / removes a container.
- Daemon down or docker CLI missing is a normal state -> ``available=False``, no exception.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

GIB = 2**30

# The ONLY docker subcommands this module is allowed to run. All strictly read-only.
READONLY_SUBCOMMANDS = frozenset({"stats", "ps", "info", "version"})

# CompletedProcess-like runner: (argv, timeout) -> result with .returncode/.stdout/.stderr
Runner = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]


class DockerUnsafe(RuntimeError):
    """Raised if code ever asks this module to run a non-read-only docker command."""


def _default_runner(args: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


def _docker_readonly(
    args: Sequence[str], timeout: float, runner: Runner
) -> subprocess.CompletedProcess[str]:
    if not args or args[0] not in READONLY_SUBCOMMANDS:
        raise DockerUnsafe(f"refused non-read-only docker command: {list(args)!r}")
    return runner(["docker", *args], timeout)


@dataclass
class DockerContainer:
    name: str
    mem_gib: float
    cpu_pct: float


@dataclass
class DockerSnapshot:
    available: bool
    total_gib: float = 0.0
    containers: list[DockerContainer] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "available": self.available,
            "total_gib": round(self.total_gib, 2),
            "containers": [
                {"name": c.name, "mem_gib": round(c.mem_gib, 2), "cpu_pct": round(c.cpu_pct, 1)}
                for c in self.containers
            ],
            "error": self.error,
        }


_UNIT_TO_GIB = {
    "b": 1 / GIB,
    "kb": 1e3 / GIB,
    "mb": 1e6 / GIB,
    "gb": 1e9 / GIB,
    "kib": 1 / 1024 / 1024,
    "mib": 1 / 1024,
    "gib": 1.0,
}


def _split_num_unit(token: str) -> tuple[float, str]:
    num, unit = "", ""
    for ch in token.strip():
        if ch.isdigit() or ch == ".":
            num += ch
        else:
            unit += ch
    try:
        return float(num), unit.strip().lower()
    except ValueError:
        return 0.0, ""


def _parse_mem_usage(usage: str) -> float:
    """``'1.35GiB / 3.825GiB'`` -> ``1.35`` (GiB). The part before ``/`` is current usage."""
    value, unit = _split_num_unit(usage.split("/")[0])
    return value * _UNIT_TO_GIB.get(unit, 0.0)


def _parse_pct(text: str) -> float:
    value, _ = _split_num_unit(text.replace("%", ""))
    return value


def docker_snapshot(runner: Runner = _default_runner, timeout: float = 8.0) -> dict:
    """Current per-container memory / CPU via ``docker stats --no-stream`` (read-only).

    Returns a plain, JSON-friendly dict (see ``DockerSnapshot.as_dict``). Never raises:
    a missing CLI or a stopped daemon yields ``{"available": False, ...}``.
    """
    if shutil.which("docker") is None:
        return DockerSnapshot(False, error="docker CLI not found").as_dict()
    try:
        proc = _docker_readonly(
            ["stats", "--no-stream", "--format", "{{json .}}"], timeout, runner
        )
    except subprocess.TimeoutExpired:
        return DockerSnapshot(False, error="docker stats timed out").as_dict()
    except (OSError, DockerUnsafe) as e:
        return DockerSnapshot(False, error=str(e)).as_dict()
    if proc.returncode != 0:
        # daemon not running is the common, expected case: keep it quiet
        tail = (proc.stderr or "").strip().splitlines()
        return DockerSnapshot(
            False, error=(tail[-1] if tail else f"docker exited {proc.returncode}")
        ).as_dict()

    containers: list[DockerContainer] = []
    total = 0.0
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        mem = _parse_mem_usage(row.get("MemUsage", ""))
        containers.append(
            DockerContainer(
                name=row.get("Name") or row.get("Container") or "?",
                mem_gib=mem,
                cpu_pct=_parse_pct(row.get("CPUPerc", "")),
            )
        )
        total += mem
    return DockerSnapshot(True, total_gib=total, containers=containers).as_dict()
