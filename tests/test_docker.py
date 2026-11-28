"""Read-only Docker probe: parsing, daemon-down handling, and the safety allowlist."""

import json
import subprocess

import pytest

from video_agent import docker


def _runner(stdout="", returncode=0, stderr=""):
    def run(args, timeout):
        assert args[0] == "docker"
        return subprocess.CompletedProcess(list(args), returncode, stdout, stderr)

    return run


def _stats_line(name, mem_usage, cpu="0.00%"):
    return json.dumps({"Name": name, "MemUsage": mem_usage, "CPUPerc": cpu})


def test_snapshot_parses_containers_and_total(monkeypatch):
    monkeypatch.setattr(docker.shutil, "which", lambda _: "/usr/local/bin/docker")
    out = "\n".join(
        [
            _stats_line("lab", "1.35GiB / 3.825GiB", "2.5%"),
            _stats_line("gdk", "512MiB / 3.825GiB", "0.0%"),
        ]
    )
    snap = docker.docker_snapshot(runner=_runner(stdout=out))
    assert snap["available"] is True
    names = {c["name"]: c for c in snap["containers"]}
    assert names["lab"]["mem_gib"] == 1.35
    assert names["lab"]["cpu_pct"] == 2.5
    assert names["gdk"]["mem_gib"] == 0.5
    assert snap["total_gib"] == 1.85  # 1.35 + 0.5


def test_snapshot_daemon_down_is_normal(monkeypatch):
    monkeypatch.setattr(docker.shutil, "which", lambda _: "/usr/local/bin/docker")
    snap = docker.docker_snapshot(
        runner=_runner(returncode=1, stderr="Cannot connect to the Docker daemon")
    )
    assert snap["available"] is False
    assert snap["total_gib"] == 0.0
    assert "Docker daemon" in snap["error"]


def test_snapshot_cli_missing(monkeypatch):
    monkeypatch.setattr(docker.shutil, "which", lambda _: None)
    snap = docker.docker_snapshot(runner=_runner(stdout="should not run"))
    assert snap["available"] is False
    assert "not found" in snap["error"]


def test_snapshot_timeout_is_handled(monkeypatch):
    monkeypatch.setattr(docker.shutil, "which", lambda _: "/usr/local/bin/docker")

    def boom(args, timeout):
        raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)

    snap = docker.docker_snapshot(runner=boom)
    assert snap["available"] is False and "timed out" in snap["error"]


@pytest.mark.parametrize(
    "usage,expected",
    [
        ("1.35GiB / 3.825GiB", 1.35),
        ("512MiB / 3.825GiB", 0.5),
        ("900KiB / 3.825GiB", round(900 / 1024 / 1024, 2)),
        ("2.5GB / 8GB", round(2.5e9 / 2**30, 2)),
        ("0B / 0B", 0.0),
    ],
)
def test_parse_mem_usage_units(usage, expected):
    assert round(docker._parse_mem_usage(usage), 2) == round(expected, 2)


def test_readonly_allowlist_refuses_mutating_commands():
    calls = []

    def run(args, timeout):
        calls.append(args)
        return subprocess.CompletedProcess(list(args), 0, "", "")

    # read-only is allowed through
    docker._docker_readonly(["stats", "--no-stream"], 5.0, run)
    assert calls and calls[0][:2] == ["docker", "stats"]

    for danger in (["start", "lab"], ["stop", "lab"], ["rm", "-f", "lab"], ["run", "x"], ["kill", "lab"]):
        with pytest.raises(docker.DockerUnsafe):
            docker._docker_readonly(danger, 5.0, run)
    # nothing mutating ever reached the runner
    assert all(a[1] in docker.READONLY_SUBCOMMANDS for a in calls)
