"""Agent runtime (A1 + A2): the LLM emits tool calls, the orchestrator executes them, results go back to the LLM when resources allow.

One run:
    your words → [LLM → tool_calls → orchestrator executes → result stored]* → final reply
Every message (user / assistant+tool_calls / tool) is stored verbatim: keeps the prefix cache extend-only and lets a run continue after a crash.

Pause and resume (a run holds no thread; all state is in the database):
- waiting_resources: all results are in, but now is not a good time to wake the LLM (a video render holds the memory) → resume when the render ends.
- waiting_event: waiting for a round to finish rendering (wait_for_round) → resume with the result when that round enters review.
- waiting_user: waiting for your approval (pick / lock / unmeasured video tier) or an answer → resume when you press a button or reply.

Tool classes: read-only (immediate), action (via Studio, with idempotency records), wait (woken by events), needs approval (buttons).
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema

from .config import ROOT
from .freevideo import FreeVideoError
from .i18n import tr
from .mcp_client import MCPError
from .planner import AGENT_CLIS, ONLINE_APIS
from .policy import Policies, classify_round
from .production import ProductionError
from .render import ParamError
from .studio import StudioError

log = logging.getLogger("aco_lw.agent")
RULES_FILE = ROOT / "prompts" / "agent_rules.md"
TERMINAL_RUN = {"done", "failed", "cancelled"}
MAX_RESULT_CHARS = 4000
FREEVIDEO_ENV = (
    "## FreeVideo agent\nYou are the FreeVideo agent for this request: make the video with the "
    "`freevideo` MCP tools (read its guide / schema first; plan before generating; prefer drafts "
    "before long or high-quality renders). FreeVideo queues and runs generation itself. Use the "
    "aco_lw tools to talk to the user: `send_file` for results to look at, `ask_user` for "
    "decisions, `run_command` / `load_skill` / `mcp_call` for downloads and research. Finish with "
    "a short report: what you made, where it is (FreeVideo library / project) and what to decide next."
)


def _json_args(raw: str | None) -> str:
    """Tool-call arguments as a JSON object string; anything else becomes "{}" (old truncated calls)."""
    try:
        if isinstance(json.loads(raw or ""), dict):
            return raw
    except json.JSONDecodeError:
        pass
    return "{}"


# ---------------- tool definitions ----------------
@dataclass
class Done:
    result: dict


@dataclass
class Pending:
    kind: str  # event | approval | user
    key: str  # round:<id> | approval:<msg id filled in by the runtime> | ask:<run id>
    message: str | None = None  # message sent to you (approval / question)
    buttons: list | None = None
    on_approve: dict | None = None  # action to run once approved {"tool": ..., "args": ...}
    info: dict = field(default_factory=dict)
    # background work started only after the waiting message is stored (so an early result is not
    # lost)
    start: Callable[[], None] | None = None
    # the "always allow" rule for this approval (e.g. cmd:ffmpeg, mcp:exa.web_search_exa)
    rule: str | None = None
    # whether bypass mode may skip it (installing nodes / downloading models may not: hard rule,
    # always asks)
    bypassable: bool = True


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    kind: str  # read | act | wait | approval | user
    handler: Callable


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


S = {"type": "string"}
I = {"type": "integer"}
N = {"type": "number"}
B = {"type": "boolean"}

LLM_CMD = re.compile(r"\b(?:lms\s+load|va\s+llm\s+(?:use|load|reload))\s+([^\s;&|]+)?")


def command_estimate(cmd: str, a: dict, need_gib=None) -> dict:
    """Memory the command will use: the agent's own estimate (memory_gib / exclusive), or — for
    loading an LLM — the model size (more reliable). `va llm` switches are gated by the daemon itself."""
    est = {
        "gib": float(a.get("memory_gib") or 0),
        "exclusive": bool(a.get("exclusive")),
        "minutes": a.get("minutes"),
        "source": "agent" if a.get("memory_gib") or a.get("exclusive") else None,
        "gate": True,
    }
    m = LLM_CMD.search(cmd)
    if m:
        if "va" in m.group(0).split()[0]:
            est["gate"] = False  # LLMControl checks memory, waits for answers, unloads the old one
        if need_gib and m.group(1):
            # unknown model: keep the agent's estimate
            with contextlib.suppress(Exception):
                est["gib"], est["source"] = need_gib(m.group(1)), "model size"
    return est


def need_gib_fn(rt):
    ctl = getattr(rt, "llm_control", None)
    return (lambda name: ctl.need_gib(ctl.resolve(name))) if ctl else None


def gated_command(rt, ctx, a, key):
    """An approved command: wait until resources allow (by its estimate), run it, release."""
    tb, rm = rt.toolbox, rt.studio.orch.resources
    timeout = int(a.get("timeout_s") or 600)
    est = command_estimate(a["command"], a, need_gib_fn(rt))
    if not est["gate"] or not (est["gib"] or est["exclusive"]):
        return tb.run_command(ctx["run_id"], a["command"], timeout)
    label = a["command"].strip().splitlines()[0][:60]
    gate = rm.admit_command(key, est["gib"], est["exclusive"], label)
    info = {**{k: v for k, v in est.items() if k != "gate"}, **gate}
    if not gate["ok"]:
        return {"error": f"not started: {gate['reason']}", "resources": info}
    try:
        out = tb.run_command(ctx["run_id"], a["command"], timeout)
    finally:
        rm.command_done(key)
    return {**out, "resources": info}


def _round_view(store, rnd: dict) -> dict:
    tasks = store.round_tasks(rnd["id"])
    return {
        "round_id": rnd["id"],
        "state": rnd["state"],
        "template": rnd["template"],
        "tier": rnd["tier"],
        "title": rnd["title"],
        "decision": rnd["decision"] or None,
        "prompt": (rnd["base_params"].get("prompt") or "")[:600],
        "candidates": [
            {
                "idx": t["idx"],
                "task_id": t["id"],
                "state": t["state"],
                "outputs": [o["kind"] for o in t["outputs"]],
                "error": t["error"],
                "exec_s": (t["metrics"] or {}).get("exec_s"),
                "seed": t["params"].get("seed"),
            }
            for t in tasks
        ],
    }


VIDEO_EXT = (".mp4", ".mov", ".webm", ".mkv", ".m4v")


# the same call (same tool, same arguments) in this many assistant turns in a row is refused: a
# local model can loop on one command until the step budget is gone
REPEAT_LIMIT = 3
REPEAT_ERROR = (
    "not run: you made this exact call 3 times in a row and the result will not change. "
    "Try a different approach, or tell the user what is missing."
)


def attachment_note(attachments: list[dict] | None) -> str:
    """How uploads the user attached are shown to the LLM: the exact @upload id to use."""
    out = ""
    for a in attachments or []:
        name = a.get("name", "")
        kind = "video" if name.lower().endswith(VIDEO_EXT) else "image"
        out += f"\n[attached {kind} @upload:{a['id']} {name}]"
    return out


def park_reason(why: str) -> str:
    """llm_admit's internal reason → one human-readable sentence."""
    if "film" in why:
        return tr("park_film")
    if "render" in why:
        return tr("park_render")
    if "pressure" in why:
        return tr("park_pressure")
    return why


def explicit_pick(text: str, idx: int) -> bool:
    """You named which candidate to pick / lock explicitly in your message (e.g. "就用 #2", "2号锁成角色")."""
    t = text.lower()
    named = re.search(rf"(#\s*{idx}\b|{idx}\s*号|no\.?\s*{idx}\b|number\s*{idx}\b)", t)
    verbs = ("选", "锁", "用", "就要", "pick", "lock", "use", "choose", "select")
    return bool(named) and any(v in t for v in verbs)


def build_tools(rt: AgentRuntime) -> dict[str, Tool]:
    studio, store = rt.studio, rt.studio.store

    def get_status(ctx, a):
        rm = studio.orch.resources
        snap = rm.snap()
        plan = rm.plan(store.by_state("draft"))[:6]
        return Done(
            {
                "memory_free_gib": snap["avail_gib"],
                "pressure": snap["pressure"],
                "render_active": rm.render_active,
                "queued": len(store.by_state("draft")),
                "queue_eta": [
                    {
                        "task_id": p["task_id"],
                        "template": p["template"],
                        "tier": p["tier"],
                        "est_s": p["exec_s"],
                    }
                    for p in plan
                ],
                "llm_mode": rt.planner.mode,
            }
        )

    def list_templates(ctx, a):
        out = []
        for t in studio.orch.registry.builtin_and_user().values():
            out.append(
                {
                    "name": t.name,
                    "kind": t.kind,
                    "title": t.title,
                    "tiers": {
                        k: {**v.values(), "measured": v.verified} for k, v in t.tiers.items()
                    },
                    "params": [n for n in t.slots if n not in ("width", "height", "seconds")],
                }
            )
        return Done({"templates": out})

    def get_project(ctx, a):
        pid = ctx["project_id"]
        pack = store.pack(pid)
        return Done(
            {
                "project": store.get_project(pid)["name"],
                "baseline_pack": {
                    r: {"task_id": x["task_id"], "kind": x["kind"]}
                    for r, x in (pack["assets"] if pack else {}).items()
                },
                "memory": [m["text"] for m in store.memories(pid, "active")],
                "rounds": [_round_view(store, r) for r in store.rounds(pid)[:6]],
            }
        )

    def get_round(ctx, a):
        rnd = store.get_round(a["round_id"])
        if rnd is None:
            return Done({"error": f"round {a['round_id']} not found"})
        return Done(_round_view(store, rnd))

    def queue_round(ctx, a):
        tpl = studio.orch.registry.template(a["template"])
        params = {"prompt": a["prompt"]} if a.get("prompt") else {}
        if a.get("image"):
            params["image"] = a["image"]
        if a.get("seed"):
            params["seed"] = a["seed"]
        params.update(a.get("params") or {})
        tier = a.get("tier") or None
        if tier and tier not in tpl.tiers:
            return Done({"error": f"unknown tier {tier}; options: {list(tpl.tiers)}"})
        unmeasured = bool(tier) and not tpl.tiers[tier].verified
        from .daemon import explicit_tier

        rule = rt.policies.approval("queue_round", "unmeasured_video")
        named = explicit_tier(ctx["origin_text"], tier) if tier else False
        if (
            tpl.kind == "video"
            and unmeasured
            and (rule == "always" or (rule == "unless_named" and not named))
        ):
            return Pending(
                "approval",
                "",
                tr("agent_tier_approval", t=tier, tpl=tpl.name),
                on_approve={"tool": "queue_round_approved", "args": a},
                rule=f"round:{tpl.name}",
            )
        return Done(_queue(ctx, a, params, tier, tpl.kind == "image" or unmeasured))

    def _queue(ctx, a, params, tier, allow_unverified):
        rnd = studio.start_round(
            a["template"],
            params,
            a.get("n") or None,
            tier,
            a.get("title"),
            origin=ctx["channel"],
            request=f"agent:{ctx['run_id']}:{ctx['call_id']}",
            parent_round=a.get("parent_round"),
            project_id=ctx["project_id"],
            allow_unverified=allow_unverified,
        )
        est = studio.orch.resources.estimator.estimate(
            studio.orch.registry.template(rnd["template"]), rnd["tier"]
        )
        return {
            "round_id": rnd["id"],
            "n": rnd["n"],
            "tier": rnd["tier"],
            "estimated_seconds_each": round(est.exec_s),
        }

    def queue_round_approved(ctx, a):
        params = {"prompt": a["prompt"]} if a.get("prompt") else {}
        if a.get("image"):
            params["image"] = a["image"]
        if a.get("seed"):
            params["seed"] = a["seed"]
        params.update(a.get("params") or {})
        return Done(_queue(ctx, a, params, a.get("tier"), True))

    def wait_for_round(ctx, a):
        rnd = store.get_round(a["round_id"])
        if rnd is None:
            return Done({"error": f"round {a['round_id']} not found"})
        if rnd["state"] != "generating":
            return Done(_round_view(store, rnd))
        return Pending("event", f"round:{rnd['id']}")

    def redo_round(ctx, a):
        new = studio.redo(a["round_id"], origin=ctx["channel"])
        return Done({"new_round_id": new["id"], "n": new["n"]})

    def refine_candidate(ctx, a):
        new = studio.refine(
            a["round_id"], a["idx"], {"prompt": a["prompt"]}, a.get("note"), origin=ctx["channel"]
        )
        return Done({"new_round_id": new["id"], "n": new["n"], "kept_seed_of": a["idx"]})

    def reject_round(ctx, a):
        rnd = studio.reject(a["round_id"], a.get("note"), origin=ctx["channel"])
        return Done({"round_id": rnd["id"], "state": rnd["state"]})

    def propose_selection(ctx, a):
        rnd = store.get_round(a["round_id"])
        if rnd is None or rnd["state"] != "review":
            return Done({"error": "round is not waiting for review"})
        role = a.get("role") or None
        rule = rt.policies.approval("propose_selection")
        if rule == "never" or (
            rule == "unless_named" and explicit_pick(ctx["origin_text"], a["idx"])
        ):
            # policy allows it / you named it in your message → counts as approved
            return Done(_select(ctx, a))
        txt = tr(
            "agent_select_approval",
            idx=a["idx"],
            t=rnd["title"] or rnd["template"],
            r=(tr("log_lock_suffix", r=role) if role else ""),
            why=a.get("reason") or "",
        )
        return Pending(
            "approval",
            "",
            txt,
            on_approve={"tool": "select_approved", "args": a},
            info={"round_id": rnd["id"]},
        )

    def _select(ctx, a):
        rnd = studio.select(
            a["round_id"], a["idx"], role=a.get("role") or None, origin=ctx["channel"]
        )
        return {"selected": a["idx"], "locked_as": a.get("role"), "round_id": rnd["id"]}

    def select_approved(ctx, a):
        return Done(_select(ctx, a))

    def propose_memory(ctx, a):
        mems = studio.propose_memories([a["text"]], {"run": ctx["run_id"]}, ctx["project_id"])
        for m in mems:
            ctx["say"](
                tr("mem_offer", t=m["text"]),
                [
                    [
                        (tr("mem_remember"), f"m:{m['id']}:accept"),
                        (tr("mem_ignore"), f"m:{m['id']}:ignore"),
                    ]
                ],
            )
        return Done({"proposed": [m["text"] for m in mems], "note": "applies after user confirms"})

    def queue_pipeline(ctx, a):
        steps = a.get("steps") or []
        if not steps:
            return Done({"error": "steps is empty"})
        for st in steps:  # validate everything first, so we do not find a missing template halfway
            studio.orch.registry.template(st["template"])
        rnd = _queue_step(ctx, steps[0], None)
        pipe = {"steps": steps, "index": 0, "rounds": [rnd["id"]]}
        return Pending("event", f"round:{rnd['id']}", info={"pipeline": pipe})

    def _queue_step(ctx, st, prev_round):
        params = {"prompt": st["prompt"]} if st.get("prompt") else {}
        image = st.get("image")
        if image == "@prev" and prev_round:  # the first successful output of the previous step
            ok = next(t for t in store.round_tasks(prev_round) if t["outputs"])
            image = f"@task:{ok['id']}"
        if image:
            params["image"] = image
        return studio.start_round(
            st["template"],
            params,
            st.get("n") or None,
            st.get("tier") or None,
            st.get("title"),
            origin=ctx["channel"],
            request=f"pipeline:{ctx['run_id']}",
            parent_round=prev_round,
            project_id=ctx["project_id"],
            allow_unverified=True,
        )

    rt._queue_step = _queue_step

    def ask_user(ctx, a):
        return Pending("user", f"ask:{ctx['run_id']}", a["question"])

    def delegate_freevideo_approved(ctx, a):
        parent = rt.store.get_run(ctx["run_id"])
        child = rt.delegate_freevideo(
            a["task"], ctx["channel"], ctx["project_id"], parent["session_id"], start=False
        )
        return Pending(
            "event", f"run:{child['id']}", start=lambda: rt._delegate(child, "freevideo")
        )

    def delegate_agent_approved(ctx, a):
        parent = rt.store.get_run(ctx["run_id"])
        child = rt.delegate_to(
            a["agent"], a["task"], ctx["channel"], ctx["project_id"], parent["session_id"], False
        )
        return Pending("event", f"run:{child['id']}", start=lambda: rt._delegate(child, a["agent"]))

    def mcp_call_approved(ctx, a):
        tb = rt.toolbox
        key = f"job:{ctx['run_id']}:{ctx['call_id']}"
        return Pending(
            "event",
            key,
            start=lambda: tb.jobs.start(
                key, lambda: tb.mcp.call(a["server"], a["tool"], a.get("args") or {})
            ),
        )

    def run_command_approved(ctx, a):
        key = f"job:{ctx['run_id']}:{ctx['call_id']}"
        return Pending(
            "event",
            key,
            start=lambda: rt.toolbox.jobs.start(key, lambda: gated_command(rt, ctx, a, key)),
        )

    # ---- production pipeline (stage 3): the agent only reads the plan, runs it as planned or
    # relays your review; no state machine of its own ----
    def _prod():
        prod = getattr(rt, "production", None)
        if prod is None:
            raise ValueError("production engine not available (runs inside va daemon)")
        return prod

    def _plan_view(plan: dict) -> dict:
        return {
            "production_id": plan["production"]["id"],
            "title": plan["production"]["title"],
            "workflow": plan["workflow"]["name"],
            "next_action": plan["next_action"],
            "stages": [
                {"id": s["id"], "state": s["state"], "counts": s["counts"], "confirm": s["confirm"]}
                for s in plan["stages"]
            ],
        }

    def list_workflows(ctx, a):
        return Done(
            {
                "workflows": [
                    {
                        "name": w.name,
                        "title": w.title,
                        "description": w.description,
                        "settings": w.settings,
                        "stages": [s.id for s in w.stages],
                    }
                    for w in _prod().workflows.values()
                ],
                "productions": [
                    {
                        "id": p["id"],
                        "title": p["title"],
                        "workflow": p["workflow"],
                        "state": p["state"],
                    }
                    for p in store.productions(ctx["project_id"])
                ][:10],
            }
        )

    def create_production(ctx, a):
        source = {"text": a.get("source") or "", "sources": a.get("sources") or []}
        prod = _prod().create(
            a["workflow"],
            a["title"],
            {"source": source},
            a.get("settings") or {},
            ctx["project_id"],
        )
        return Done(_plan_view(_prod().plan(prod["id"])))

    def get_production_plan(ctx, a):
        return Done(_plan_view(_prod().plan(a["production_id"])))

    def run_stage(ctx, a):
        p = _prod()
        plan = p.plan(a["production_id"])
        st = next((s for s in plan["stages"] if s["id"] == a["stage"]), None)
        if st and st["confirm"] and not a.get("user_confirmed"):
            return Done(
                {
                    "error": f"stage {a['stage']} needs the user's go-ahead (long renders); "
                    "ask them, then call again with user_confirmed=true"
                }
            )
        made = p.run_stage(a["production_id"], a["stage"], a.get("keys"), a.get("note"), "agent")
        return Done({"started": len(made), "plan": _plan_view(p.plan(a["production_id"]))})

    def review_stage(ctx, a):
        p = _prod()
        art = p.review(
            a["production_id"],
            a["stage"],
            a["action"],
            a.get("note"),
            None,
            a.get("key") or "main",
            ctx["channel"],
        )
        return Done(
            {
                "artifact": {k: art[k] for k in ("stage", "key", "version", "status")},
                "plan": _plan_view(p.plan(a["production_id"])),
            }
        )

    # ---- ComfyUI exploration / building (artokun/comfyui-mcp, scheduled by the orchestrator; see
    # comfy_bridge.py) ----
    def _bridge():
        b = getattr(rt, "comfy", None)
        if b is None:
            raise ValueError("ComfyUI agent tools are not installed (data/tools/comfyui-mcp)")
        return b

    def comfy_catalog(ctx, a):
        return Done(
            {
                "tools": _bridge().catalog(),
                "note": "read = allowed; run = use run_comfy_workflow; approve = asks the user; deny = not allowed",
            }
        )

    def comfy_describe(ctx, a):
        return Done(_bridge().describe(a["tool"]))

    def _run_graph(ctx, graph: dict, title: str | None):
        rnd = studio.start_round(
            "comfy-raw",
            {"graph": json.dumps(graph), "title": title or "agent workflow"},
            1,
            "trial",
            title or "agent workflow",
            origin="agent",
            request=f"comfy:{ctx['run_id']}",
            project_id=ctx["project_id"],
        )
        return {
            "queued_round": rnd["id"],
            "hint": "call wait_for_round with this round_id for the result",
        }

    def comfy(ctx, a):
        from .comfy_bridge import classify, extract_workflow

        tool, args = a["tool"], a.get("args") or {}
        if isinstance(args, str):  # some models send the nested object as a JSON string
            try:
                args = json.loads(args)
            except json.JSONDecodeError as e:
                return Done({"error": f"args is not a JSON object: {e}"})
        if not isinstance(args, dict):
            return Done({"error": "args must be an object"})
        kind = classify(tool, args)
        if kind == "read":
            return Done(_bridge().call(tool, args))
        if kind == "run":
            graph = extract_workflow(tool, args)
            if graph is None:
                return Done(
                    {
                        "error": "runs go through our orchestrator: build an API-format workflow "
                        "(create_workflow), then call run_comfy_workflow with it"
                    }
                )
            return Done(_run_graph(ctx, graph, a.get("title")))
        if kind == "approve":
            txt = tr(
                "comfy_approval",
                t=tool,
                a=json.dumps(args, ensure_ascii=False)[:600],
                why=a.get("reason") or "",
            )
            return Pending(
                "approval",
                "",
                txt,
                on_approve={"tool": "comfy_approved", "args": a},
                bypassable=False,
            )
        return Done(
            {
                "error": f"{tool} {args.get('action') or ''} is not allowed (restart / delete / queue "
                "changes / memory / settings / cloud are reserved for the user)"
            }
        )

    def comfy_approved(ctx, a):
        return Done(_bridge().call(a["tool"], a.get("args") or {}))

    def run_comfy_workflow(ctx, a):
        graph = a["workflow"]
        if isinstance(graph, str):
            graph = json.loads(graph)
        return Done(_run_graph(ctx, graph, a.get("title")))

    def update_production_settings(ctx, a):
        prod = _prod().update_settings(a["production_id"], a["settings"])
        return Done({"settings": prod["settings"]})

    round_id = {"round_id": S}
    tools = [
        Tool(
            "get_status",
            "Memory, queue with ETA, active render, LLM mode.",
            _obj({}),
            "read",
            get_status,
        ),
        Tool(
            "list_templates",
            "Available templates, their tiers (resolution × duration; "
            "measured or not) and parameters.",
            _obj({}),
            "read",
            list_templates,
        ),
        Tool(
            "get_project",
            "Current project: baseline pack, confirmed memory, recent rounds with candidates.",
            _obj({}),
            "read",
            get_project,
        ),
        Tool(
            "get_round",
            "One round with its candidates.",
            _obj(round_id, ["round_id"]),
            "read",
            get_round,
        ),
        Tool(
            "queue_round",
            "Queue a round of candidates (renders in the background).",
            _obj(
                {
                    "template": S,
                    "prompt": S,
                    "image": S,
                    "tier": S,
                    "n": I,
                    "seed": I,
                    "title": S,
                    "parent_round": S,
                    "params": {
                        "type": "object",
                        "description": "other template slots by name, e.g. for freevideo-ref "
                        '{"ref_video": "@upload:ID", "ref_image": "@upload:ID"}',
                    },
                },
                ["template", "prompt"],
            ),
            "act",
            queue_round,
        ),
        Tool(
            "wait_for_round",
            "Wait until a round has finished rendering and get its candidates. "
            "You are paused meanwhile.",
            _obj(round_id, ["round_id"]),
            "wait",
            wait_for_round,
        ),
        Tool(
            "redo_round",
            "Discard a round under review and render it again with new seeds.",
            _obj(round_id, ["round_id"]),
            "act",
            redo_round,
        ),
        Tool(
            "refine_candidate",
            "Keep candidate idx's seed, change the prompt, new round.",
            _obj({"round_id": S, "idx": I, "prompt": S, "note": S}, ["round_id", "idx", "prompt"]),
            "act",
            refine_candidate,
        ),
        Tool(
            "reject_round",
            "Reject a round under review with a note.",
            _obj({"round_id": S, "note": S}, ["round_id"]),
            "act",
            reject_round,
        ),
        Tool(
            "propose_selection",
            "Propose selecting candidate idx (optionally locking it into the "
            "baseline pack as role). The user approves.",
            _obj({"round_id": S, "idx": I, "role": S, "reason": S}, ["round_id", "idx"]),
            "approval",
            propose_selection,
        ),
        Tool(
            "propose_memory",
            "Propose a durable project fact the user stated.",
            _obj({"text": S}, ["text"]),
            "act",
            propose_memory,
        ),
        Tool(
            "queue_pipeline",
            "Run several rounds in order without consulting you between them (per policy): each "
            'step {template, prompt, image, tier, n, title}; image "@prev" uses the previous '
            "step's first output. Returns when all steps finish or one fails.",
            _obj(
                {
                    "steps": {
                        "type": "array",
                        "items": _obj(
                            {"template": S, "prompt": S, "image": S, "tier": S, "n": I, "title": S},
                            ["template", "prompt"],
                        ),
                    }
                },
                ["steps"],
            ),
            "wait",
            queue_pipeline,
        ),
        Tool(
            "list_workflows",
            "Production workflows (e.g. novel-to-film) and this project's productions.",
            _obj({}),
            "read",
            list_workflows,
        ),
        Tool(
            "create_production",
            "Start a multi-stage production (e.g. novel → film) from a story or description, plus "
            "optional links (web pages, YouTube) and the user's pictures (e.g. their own casting "
            "photos). Planning stages start automatically; every stage waits for the user's review.",
            _obj(
                {
                    "workflow": S,
                    "title": S,
                    "source": S,
                    "sources": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "{url} for a web page or YouTube link; {kind: image, upload: "
                        "<upload id>, label, role: character|location|style|prop, use: "
                        "stylize|as_is, note} for the user's pictures",
                    },
                    "settings": {
                        "type": "object",
                        "description": "e.g. video_tier, image_candidates, subtitles, language",
                    },
                },
                ["workflow", "title"],
            ),
            "act",
            create_production,
        ),
        Tool(
            "get_production_plan",
            "The authoritative plan of a production: each stage's state and the next action. "
            "Always read it instead of guessing progress.",
            _obj({"production_id": S}, ["production_id"]),
            "read",
            get_production_plan,
        ),
        Tool(
            "run_stage",
            "Run (or re-run) a production stage — by default only missing / stale / failed items. "
            "Stages marked confirm (videos) need user_confirmed=true, set only when the user said so.",
            _obj(
                {
                    "production_id": S,
                    "stage": S,
                    "keys": {"type": "array", "items": S},
                    "note": S,
                    "user_confirmed": {"type": "boolean"},
                },
                ["production_id", "stage"],
            ),
            "act",
            run_stage,
        ),
        Tool(
            "review_stage",
            "Relay the user's review of a planning stage: approve, redo (with note), reject, or "
            "keep (still valid although upstream changed). A change that concerns many shots (e.g. "
            "'all shots in Mandarin') is a redo of the storyboard stage with that note; only shots "
            "whose text changes get re-rendered. Render stages are reviewed on their rounds.",
            _obj(
                {
                    "production_id": S,
                    "stage": S,
                    "action": {"type": "string", "enum": ["approve", "redo", "reject", "keep"]},
                    "note": S,
                    "key": S,
                },
                ["production_id", "stage", "action"],
            ),
            "act",
            review_stage,
        ),
        Tool(
            "comfy_catalog",
            "ComfyUI tools you can use to explore models, nodes, workflows and model-family guides, "
            "and to build workflows; grouped by what the policy allows.",
            _obj({}),
            "read",
            comfy_catalog,
        ),
        Tool(
            "comfy_describe",
            "Full description and argument schema of one ComfyUI tool. Read it before first use.",
            _obj({"tool": S}, ["tool"]),
            "read",
            comfy_describe,
        ),
        Tool(
            "comfy",
            "Call a ComfyUI tool from comfy_catalog with its args (e.g. {action: ...}). Reads and "
            "workflow building run directly; installs / downloads ask the user; runs go through "
            "run_comfy_workflow; restarts, deletions, queue changes and cloud are refused.",
            _obj({"tool": S, "args": {"type": "object"}, "title": S, "reason": S}, ["tool"]),
            "act",
            comfy,
        ),
        Tool(
            "run_comfy_workflow",
            "Queue an API-format ComfyUI workflow you built as a tracked trial render (our queue, "
            "pause and memory gates apply). Keep trials small (lowest resolution / few steps).",
            _obj({"workflow": {"type": "object"}, "title": S}, ["workflow"]),
            "act",
            run_comfy_workflow,
        ),
        Tool(
            "update_production_settings",
            "Change a production's settings (e.g. language for dialogue, video_tier). Applies to "
            "items run from now on; approved ones are not re-rendered automatically.",
            _obj(
                {"production_id": S, "settings": {"type": "object"}}, ["production_id", "settings"]
            ),
            "act",
            update_production_settings,
        ),
        Tool(
            "ask_user",
            "Ask the user one short question and wait for the answer.",
            _obj({"question": S}, ["question"]),
            "user",
            ask_user,
        ),
    ]
    tools += general_tools(rt)
    hidden = [
        Tool("run_command_approved", "", _obj({}), "act", run_command_approved),
        Tool("mcp_call_approved", "", _obj({}), "act", mcp_call_approved),
        Tool("delegate_freevideo_approved", "", _obj({}), "act", delegate_freevideo_approved),
        Tool("delegate_agent_approved", "", _obj({}), "act", delegate_agent_approved),
        Tool("queue_round_approved", "", _obj({}), "act", queue_round_approved),
        Tool("select_approved", "", _obj({}), "act", select_approved),
        Tool("comfy_approved", "", _obj({}), "act", comfy_approved),
    ]
    return {t.name: t for t in tools + hidden}


def command_rule(cmd: str) -> str | None:
    """A single command gets a rule by program name (cmd:ffmpeg); ones with pipes / chaining / redirects cannot be "always allowed"."""
    import re

    c = cmd.strip()
    if not c or re.search(r"[;&|`$<>]|\n", c):
        return None
    return f"cmd:{c.split()[0]}"


def general_tools(rt: AgentRuntime) -> list[Tool]:
    """Generic tools (see toolbox.py): slow ones go to the background tool queue; commands need your approval first."""
    from .toolbox import blocked

    S, I = {"type": "string"}, {"type": "integer"}

    def tb():
        return rt.toolbox

    def job(ctx, fn) -> Pending:
        key = f"job:{ctx['run_id']}:{ctx['call_id']}"
        return Pending("event", key, start=lambda: tb().jobs.start(key, fn))

    def web_fetch(ctx, a):
        return job(ctx, lambda: tb().web_fetch(ctx["run_id"], a["url"], a.get("max_chars") or 0))

    def run_command(ctx, a):
        cmd = a["command"].strip()
        why = blocked(cmd)
        if why:
            return Done({"error": f"refused: {why}"})
        a = {**a, "command": cmd}
        if tb().auto_allowed(cmd):
            key = f"job:{ctx['run_id']}:{ctx['call_id']}"
            return Pending(
                "event",
                key,
                start=lambda: tb().jobs.start(key, lambda: gated_command(rt, ctx, a, key)),
            )
        reason = f"\n{a['reason']}" if a.get("reason") else ""
        est = command_estimate(cmd, a, need_gib_fn(rt))
        if est["gib"] or est["exclusive"]:
            reason += "\n" + tr(
                "agent_cmd_estimate",
                g=round(est["gib"], 1),
                x=tr("agent_cmd_exclusive") if est["exclusive"] else "",
                src=est["source"] or "",
            )
        return Pending(
            "approval",
            "",
            tr("agent_cmd_approval", cmd=cmd[:1500], why=reason),
            on_approve={"tool": "run_command_approved", "args": {**a, "command": cmd}},
            rule=command_rule(cmd),
        )

    def read_file(ctx, a):
        return Done(
            tb().read_file(ctx["run_id"], a["path"], a.get("offset") or 0, a.get("limit") or 400)
        )

    def write_file(ctx, a):
        return Done(tb().write_file(ctx["run_id"], a["path"], a["content"]))

    def list_skills(ctx, a):
        return Done({"skills": tb().library().list()})

    def load_skill(ctx, a):
        return Done(tb().load_skill(a["name"]))

    def memory_read(ctx, a):
        return Done(tb().memory_read(a.get("name")))

    def memory_write(ctx, a):
        return Done(tb().memory_write(a["name"], a.get("description", ""), a.get("content", "")))

    def add_media(ctx, a):
        return Done(tb().add_media(ctx["run_id"], a["path"], ctx["project_id"]))

    def mcp_list(ctx, a):
        return job(ctx, lambda: tb().mcp.catalog(a.get("server")))

    def mcp_call(ctx, a):
        server, name, args = a["server"], a["tool"], a.get("args") or {}
        rule = (rt.policies.data.get("mcp") or {}).get("servers", {}).get(server) or (
            rt.policies.data.get("mcp") or {}
        ).get("approval", "unless_read_only")
        if rule == "deny":
            return Done({"error": f"MCP server {server} is denied by policy"})
        need = rule == "always" or (
            rule == "unless_read_only" and not tb().mcp.read_only(server, name)
        )
        if not need:
            return job(ctx, lambda: tb().mcp.call(server, name, args))
        shown = json.dumps(args, ensure_ascii=False)[:1200]
        reason = f"\n{a['reason']}" if a.get("reason") else ""
        return Pending(
            "approval",
            "",
            tr("agent_mcp_approval", tool=f"{server}.{name}", args=shown, why=reason),
            on_approve={"tool": "mcp_call_approved", "args": a},
            rule=f"mcp:{server}.{name}",
        )

    def delegate_freevideo(ctx, a):
        if not rt.freevideo_online_ok():
            return Done({"error": tr("fv_offline")})
        return Pending(
            "approval",
            "",
            tr("fv_delegate_approval", task=a["task"][:1500]),
            on_approve={"tool": "delegate_freevideo_approved", "args": a},
            rule="delegate:freevideo",
        )

    def delegate_agent(ctx, a):
        if a.get("agent") not in AGENT_CLIS:
            return Done({"error": f"agent must be one of {', '.join(AGENT_CLIS)}"})
        if not rt.cfg.allow_online:
            return Done({"error": tr("agent_online_off")})
        return Pending(
            "approval",
            "",
            tr("agent_delegate_approval", agent=a["agent"], task=a["task"][:1500]),
            on_approve={"tool": "delegate_agent_approved", "args": a},
            rule=f"delegate:{a['agent']}",
        )

    def look(ctx, a):
        refs = a.get("paths") or []
        if isinstance(refs, str):
            refs = [refs]
        if not refs:
            return Done({"error": "give paths: file paths, @task:ID, @round:ID or @upload:ID"})
        return job(ctx, lambda: tb().look(ctx["run_id"], refs))

    def send_file(ctx, a):
        p = tb().readable(ctx["run_id"], a["path"])
        if not p.is_file():
            return Done({"error": f"not a file: {p}"})
        rt.send_files(ctx["channel"], [(p, a.get("caption") or p.name)])
        return Done({"sent": str(p)})

    return [
        Tool(
            "web_fetch",
            "Read a web page as text (no login, no JavaScript). Runs in the background; you get "
            "the text when it is done. For logged-in sites (Xiaohongshu, X, ...) or downloads, "
            "use a skill (e.g. agent-reach) with run_command.",
            _obj({"url": S, "max_chars": I}, ["url"]),
            "wait",
            web_fetch,
        ),
        Tool(
            "run_command",
            "Run a shell command (zsh) in this run's own working directory. The user approves "
            "every command (shown verbatim with your reason) unless policy pre-approves it; then "
            "the orchestrator runs it in the background when resources allow, and you get exit "
            "code, output tail and new files. For heavy commands give memory_gib (peak unified "
            "memory you expect it to use) and exclusive=true if nothing else big may run "
            "alongside (the LLM is unloaded meanwhile); the orchestrator waits for memory before "
            "starting it. To switch your own local model use `va llm use <name>` (`va llm list` "
            "shows models). Never use it to reach ComfyUI/FreeVideo (use the generation tools).",
            _obj(
                {
                    "command": S,
                    "reason": S,
                    "timeout_s": I,
                    "memory_gib": N,
                    "exclusive": B,
                    "minutes": N,
                },
                ["command", "reason"],
            ),
            "approval",
            run_command,
        ),
        Tool(
            "read_file",
            "Read a text file or list a directory (under data/ or a skill directory; relative "
            "paths are in this run's working directory). offset/limit are line numbers.",
            _obj({"path": S, "offset": I, "limit": I}, ["path"]),
            "read",
            read_file,
        ),
        Tool(
            "write_file",
            "Write a text file (script, prompt, notes) in this run's working directory.",
            _obj({"path": S, "content": S}, ["path", "content"]),
            "act",
            write_file,
        ),
        Tool(
            "list_skills",
            "Skills you can load (name + when to use).",
            _obj({}),
            "read",
            list_skills,
        ),
        Tool(
            "load_skill",
            "Load a skill's full instructions before doing a task it covers; follow them with "
            "your tools.",
            _obj({"name": S}, ["name"]),
            "read",
            load_skill,
        ),
        Tool(
            "memory_read",
            "Your own long-term memory: without name, the index; with name, one memory.",
            _obj({"name": S}),
            "read",
            memory_read,
        ),
        Tool(
            "memory_write",
            "Save (or with empty content, delete) one memory: a durable lesson, user preference "
            "or how-to you will need again. One fact per memory; update instead of duplicating.",
            _obj({"name": S, "description": S, "content": S}, ["name", "description", "content"]),
            "act",
            memory_write,
        ),
        Tool(
            "add_media",
            "Register a downloaded / produced image or video so generation tools can use it; "
            "returns an @upload:ID reference.",
            _obj({"path": S}, ["path"]),
            "act",
            add_media,
        ),
        Tool(
            "mcp_list",
            "MCP servers the orchestrator can use for you. Without server: each server's tools "
            "in one line; with server: that server's tools with full argument schemas (read it "
            "before the first mcp_call).",
            _obj({"server": S}),
            "wait",
            mcp_list,
        ),
        Tool(
            "mcp_call",
            "Call a tool of a configured MCP server. Read-only tools run directly; others ask "
            "the user first (per policy). Runs in the background; you get its result.",
            _obj(
                {"server": S, "tool": S, "args": {"type": "object"}, "reason": S},
                ["server", "tool"],
            ),
            "approval",
            mcp_call,
        ),
        Tool(
            "delegate_freevideo",
            "Hand a whole video task to the FreeVideo agent (Claude Code with FreeVideo's own MCP "
            "tools: films, cast cards, timeline editing, references). Use it when the task needs "
            "FreeVideo features our templates don't cover. The user approves; you get its report.",
            _obj({"task": S}, ["task"]),
            "approval",
            delegate_freevideo,
        ),
        Tool(
            "delegate_agent",
            "Hand a sub-task to a stronger online agent and wait for its report: claude_code "
            "(Claude Code) or codex (Codex, the user's ChatGPT account). Use it for hard "
            "reasoning, long research, debugging or writing code. It works through the same "
            "aco_lw tools (commands still need the user's approval). The user approves each "
            "hand-over; needs allow_online.",
            _obj(
                {"agent": {"type": "string", "enum": list(AGENT_CLIS)}, "task": S},
                ["agent", "task"],
            ),
            "act",
            delegate_agent,
        ),
        Tool(
            "look",
            "See images or videos yourself (you are a vision model): candidates you generated, "
            "reference pictures, downloaded videos (6 frames each). Use it to judge your own "
            "results against the brief / references before showing the user. Up to 8 images "
            "per call; they arrive right after this tool result.",
            _obj(
                {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "file paths, @task:ID, @round:ID (all outputs) or @upload:ID",
                    },
                },
                ["paths"],
            ),
            "wait",
            look,
        ),
        Tool(
            "send_file",
            "Send an image / video / file to the user (e.g. a contact sheet for them to judge).",
            _obj(
                {
                    "path": {
                        "type": "string",
                        "description": "a file path, @task:ID or @upload:ID",
                    },
                    "caption": S,
                },
                ["path"],
            ),
            "act",
            send_file,
        ),
    ]


def _first_line(p: Pending) -> str:
    lines = [x for x in (p.message or "").splitlines() if x.strip()]
    return " ".join(lines[:2])[:300]


def tools_spec(tools: dict[str, Tool]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
        }
        for t in tools.values()
        if t.description
    ]


# ---------------- runtime ----------------
class AgentRuntime:
    def __init__(
        self,
        studio,
        planner,
        enqueue: Callable[[Callable[[], None]], None],
        say_for: Callable[[str], Callable],
        max_steps: int = 16,
    ):
        """enqueue: put a step into the planner queue (single thread); say_for(channel): a function that sends a message to that channel."""
        self.studio, self.planner, self.store = studio, planner, studio.store
        self.enqueue, self.say_for, self.max_steps = enqueue, say_for, max_steps
        self.cfg = studio.orch.cfg
        self.policies = Policies.load(studio.orch.cfg.data_dir)
        from .toolbox import Toolbox

        self.toolbox = Toolbox(self.cfg, self)
        self.llm_control = None  # injected by Actions (switching / loading the local LLM)
        # sending you files (injected per channel by the daemon; by default just a message with the
        # paths)
        self.send_files: Callable[[str, list], None] = lambda ch, items: self.say_for(ch)(
            "\n".join(f"📎 {cap}: {p}" for p, cap in items)
        )
        self.tools = build_tools(self)
        self.spec = tools_spec(self.tools)
        self._lock = threading.RLock()
        studio.orch.resources.listeners.append(self.on_resources_freed)

    # ---- context (stable prefix + verbatim replay) ----
    def system_prompt(self, project_id: str, session: dict | None) -> str:
        # agent rules + the same template docs, project memory and session summary as the planner
        # (stable prefix)
        rules = RULES_FILE.read_text().strip()
        skills = self.toolbox.skills_index()
        if skills:
            rules += "\n\n## Skills (load_skill before a task one covers)\n" + skills
        mem = self.toolbox.memory_index()
        if mem:
            rules += "\n\n## Your memory index (memory_read for details)\n" + mem
        return self.planner.system_prompt(project_id, session, rules)

    @staticmethod
    def _wire(m: dict) -> dict:
        if m["role"] == "assistant":
            out = {"role": "assistant", "content": m["content"]}
            # within this run, replay the thinking verbatim (as generated)
            if m["meta"].get("reasoning"):
                out["reasoning_content"] = m["meta"]["reasoning"]
            # a turn Claude generated: replay its raw content blocks unchanged
            if m["meta"].get("anthropic"):
                out["_anthropic"] = m["meta"]["anthropic"]
            if m["tool_calls"]:
                out["tool_calls"] = [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {"name": c["name"], "arguments": _json_args(c["arguments"])},
                    }
                    for c in m["tool_calls"]
                ]
            return out
        if m["role"] == "tool":
            return {"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]}
        return {"role": m["role"], "content": m["content"]}

    def _compact(self, run: dict) -> list[dict]:
        """Finished runs are folded into two stable messages (your words + final reply).

        Measured: the model's chat template drops earlier turns' thinking once a new user message arrives, so the prefix
        cannot be extended verbatim across runs anyway; in the stable form, only the last run's two messages are recomputed and everything earlier hits the cache.
        """
        final = next(
            (
                m["content"]
                for m in reversed(self.store.agent_msgs(run["id"]))
                if m["role"] == "assistant" and not m["tool_calls"] and m["content"]
            ),
            f"(run {run['status']})",
        )
        return [
            {"role": "user", "content": f"## User\n{run['origin_text']}"},
            {"role": "assistant", "content": final},
        ]

    def messages_for(self, run: dict) -> list[dict]:
        session = self.store.get_session(run["session_id"]) if run["session_id"] else None
        msgs = [{"role": "system", "content": self.system_prompt(run["project_id"], session)}]
        upto = (session and self.store.get_kv(f"agent_upto:{session['id']}")) or 0
        for r in self.store.runs(run["session_id"]):
            if r["id"] == run["id"]:
                break
            if r["created"] <= upto:
                continue
            msgs += self._compact(r)
        pics: list[dict] = []
        for m in self.store.agent_msgs(run["id"]):
            if m["role"] != "tool" and pics:  # images go right after this group of tool results
                msgs.append({"role": "user", "content": pics})
                pics = []
            msgs.append(self._wire(m))
            if m["role"] == "tool" and m["name"] == "look" and m["status"] == "done":
                pics += self._look_parts(m["content"])
        if pics:
            msgs.append({"role": "user", "content": pics})
        return msgs

    @staticmethod
    def _look_parts(content: str) -> list[dict]:
        """look results → a vision message (OpenAI-style image_url data URLs; the Claude backend converts them)."""
        import base64

        try:
            images = json.loads(content).get("images") or []
        except (ValueError, AttributeError):
            return []
        parts: list[dict] = []
        for im in images:
            try:
                data = base64.b64encode(Path(im["file"]).read_bytes()).decode()
            except OSError:
                continue
            parts.append({"type": "text", "text": f"[look] {im['ref']}"})
            parts.append(
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}}
            )
        return parts

    # ---- start ----
    def start(
        self,
        text: str,
        channel: str,
        project_id: str,
        session_id: str,
        attachments: list[dict] | None = None,
    ) -> dict:
        run = self.store.create_run(project_id, session_id, channel, text)
        att = attachment_note(attachments)
        live = self.planner.live_state(project_id)
        content = f"## Live state\n{live}\n\n## User\n{text}{att}"
        self.store.add_agent_msg(run["id"], "user", content)
        self.enqueue(lambda: self.step(run["id"]))
        return run

    # ---- one step ----
    # ---- backend choice: local first; when short of resources / local unavailable, fall back to
    # online models or agent CLIs as configured ----
    def _api_backend(self, name: str):
        cache = self.__dict__.setdefault("_api_backends", {})
        if name not in cache:
            from .planner import make_named_backend

            cache[name] = make_named_backend(self.cfg, self.store, name)
        return cache[name]

    def _fallback(self, run: dict, why: str):
        """Returns ("backend", backend) | ("delegate", CLI name) | ("park", reason)."""
        chain = list(self.cfg.llm_fallback or [])
        if not chain:
            return "park", why
        if not self.cfg.allow_online:  # privacy switch off: nothing goes out
            return "park", f"{why}; online fallback is configured but allow_online is off"
        # not started yet: can be handed to a CLI as a whole
        fresh = len(self.store.agent_msgs(run["id"])) == 1
        for name in chain:
            if name in ONLINE_APIS:
                return "backend", self._api_backend(name)
            if name in ("claude_code", "codex") and fresh:
                return "delegate", name
        return "park", why

    def brain(self) -> str:
        """The brain chosen with /model: local (= the configured primary backend) | an online API | an agent CLI."""
        return self.store.get_kv("agent_brain", "local") or "local"

    def _chosen_brain(self, run: dict):
        name = self.brain()
        if name == "local":
            return None
        if not self.cfg.allow_online:  # privacy switch off: fall back to local
            return None
        # hand the whole request to the CLI; runs already started locally stay local
        if name in AGENT_CLIS:
            return ("delegate", name) if len(self.store.agent_msgs(run["id"])) == 1 else None
        try:
            return "backend", self._api_backend(name)
        except Exception as e:  # noqa: BLE001 — no key / model configured: stop and say why
            return "park", f"{name}: {e}"

    def _choose(self, run: dict):
        picked = self._chosen_brain(run)
        if picked:
            return picked
        primary = self.planner.backend
        if not getattr(primary, "local", False):
            # the primary backend is online itself: uses no local memory, no gate needed
            return "backend", primary
        ok, why = self.studio.orch.resources.llm_admit()
        if ok:
            return "backend", primary
        return self._fallback(run, why)

    def step(self, run_id: str) -> None:
        with self._lock:
            run = self.store.get_run(run_id)
            if run is None or run["status"] in TERMINAL_RUN or run["status"] == "delegated":
                return
            if self._unresolved(run_id):
                return  # some tool results are still out; wait for the event / approval
            if run["steps"] >= self.max_steps:
                self._finish(run, tr("agent_step_budget", n=self.max_steps), "done")
                return
            kind, what = self._choose(run)
            parked = run["status"] == "waiting_resources"
            if kind == "park":  # park the results; hand them to the LLM once resources allow
                self.store.update_run(run_id, status="waiting_resources", waiting={"reason": what})
                # tell you only once, when it starts waiting: what it waits for and what will wake
                # it
                if not parked:
                    self.say_for(run["channel"])(tr("agent_parked", why=park_reason(what)))
                return
            if kind == "delegate":
                self._delegate(run, what)
                return
            self.store.update_run(run_id, status="running", waiting={})
        backend = what
        say = self.say_for(run["channel"])
        if parked:
            say(tr("agent_resumed"))
        rm = self.studio.orch.resources
        try:
            res = self._call(backend, run, rm)
        # local LLM error / unavailable: fall back once as configured, otherwise mark failed
        except Exception as e:
            kind, alt = self._fallback(run, f"{backend.name} failed: {e}")
            if kind == "backend" and alt is not backend:
                log.warning(
                    "agent backend %s failed, falling back to %s: %s", backend.name, alt.name, e
                )
                try:
                    backend, res = alt, self._call(alt, run, rm)
                except Exception as e2:
                    log.exception("agent fallback call failed")
                    self._finish(run, tr("planner_error", e=e2), "failed")
                    return
            elif kind == "delegate":
                self._delegate(run, alt)
                return
            else:
                log.exception("agent LLM call failed")
                self._finish(run, tr("planner_error", e=e), "failed")
                return
        if backend is not self.planner.backend:
            self._log_policy(
                run, f"local LLM unavailable → this step used {res.backend or backend.name}"
            )
        calls = [self._check_args(c, res.truncated) for c in res.tool_calls]
        extra = (
            {"anthropic": res.raw_content} if (res.backend or "").startswith("anthropic") else {}
        )
        self.store.add_agent_msg(
            run_id,
            "assistant",
            res.text or None,
            tool_calls=calls or None,
            reasoning=res.reasoning,
            **extra,
            **res.stats(),
        )
        self.store.update_run(run_id, steps=run["steps"] + 1)
        if not calls:
            self.studio.log(
                "llm",
                "out",
                res.text or "",
                run["project_id"],
                backend.name,
                run_id=run_id,
                purpose="agent",
                **res.stats(),
            )
            self._finish(run, None, "done", say_text=res.text)
            return
        for c in calls:
            if self._repeats(run_id, c) >= REPEAT_LIMIT:
                self._tool_done(run, c, {}, {"error": REPEAT_ERROR})
                continue
            self._execute(run, c, say)
        self._advance(run_id)

    @staticmethod
    def _call_key(c: dict) -> str:
        try:
            args = json.dumps(json.loads(c.get("arguments") or "{}"), sort_keys=True)
        except (json.JSONDecodeError, TypeError):
            args = str(c.get("arguments"))
        return f"{c.get('name')}:{args}"

    def _repeats(self, run_id: str, call: dict) -> int:
        """How many of the latest assistant turns in a row (this one included) made this same call."""
        key, n = self._call_key(call), 0
        for m in reversed(self.store.agent_msgs(run_id)):
            if m["role"] != "assistant":
                continue
            if not any(self._call_key(c) == key for c in m["tool_calls"] or []):
                break
            n += 1
        return n

    def _call(self, backend, run: dict, rm):
        inflight = (
            rm.llm.inference() if getattr(backend, "local", False) else contextlib.nullcontext()
        )
        with inflight:
            return backend.chat(
                self.messages_for(run),
                None,
                max_tokens=self.cfg.agent_max_tokens,
                tools=self.spec,
            )

    # ---- hand off to a local agent CLI (Claude Code / Codex) ----
    def _delegate(self, run: dict, name: str) -> None:
        self.store.update_run(run["id"], status="delegated", waiting={"agent": name})
        self._log_policy(run, f"local LLM unavailable → whole request delegated to {name}")
        threading.Thread(
            target=self._run_cli, args=(run["id"], name), daemon=True, name=f"agent-cli-{run['id']}"
        ).start()

    def cli_prompt(self, run: dict) -> str:
        session = self.store.get_session(run["session_id"]) if run["session_id"] else None
        parts = [self.system_prompt(run["project_id"], session)]
        for r in self.store.runs(run["session_id"]):
            if r["id"] == run["id"]:
                break
            if r["status"] in TERMINAL_RUN:
                c = self._compact(r)
                parts.append(f"{c[0]['content']}\n## Your earlier answer\n{c[1]['content']}")
        parts.append(self.store.agent_msgs(run["id"])[0]["content"])
        if run["waiting"].get("agent") == "freevideo":
            parts.append(FREEVIDEO_ENV)
        parts.append(
            "## Environment\nYou run inside aco_lw. Use only the aco_lw tools (commands, "
            "files, web pages and skills go through run_command / read_file / web_fetch / "
            "load_skill, which the orchestrator executes); do not use your own shell or file "
            "tools. When finished, reply with the final message for the user (it is shown to "
            "them as-is)."
        )
        return "\n\n".join(parts)

    def _run_cli(self, run_id: str, name: str) -> None:

        run = self.store.get_run(run_id)
        url = f"http://127.0.0.1:{self.cfg.web_port}"
        try:
            text, stats = self.cli_runner(name).run(run_id, self.cli_prompt(run), url)
            status = "done"
        except Exception as e:  # CLI failed: tell you why and mark the run failed
            log.exception("agent CLI %s failed", name)
            text, stats, status = tr("planner_error", e=e), {"via": name}, "failed"
        self.store.add_agent_msg(run_id, "assistant", text, **stats)
        self.studio.log(
            "llm", "out", text, run["project_id"], name, run_id=run_id, purpose="agent", **stats
        )
        self.store.update_run(run_id, status="running")  # leave delegated before finishing
        self._finish(self.store.get_run(run_id), None, status, say_text=text)
        self.resolve(f"run:{run_id}", {"status": status, "reply": text})  # wake runs waiting for it

    def cli_runner(self, name: str):
        from .agent_cli import AgentCLI

        if name == "freevideo":
            return AgentCLI(name, self.cfg, self.freevideo_mcp())
        return AgentCLI(name, self.cfg)

    def freevideo_mcp(self) -> dict:
        """FreeVideo's own MCP launch config (/freevideo/agent/connect); the token does not pass through here."""
        from .freevideo import FreeVideo

        fv = FreeVideo(self.cfg.comfy_url)
        r = fv.http.get(
            self.cfg.comfy_url.rstrip("/") + "/freevideo/agent/connect", headers=fv._headers()
        )
        r.raise_for_status()
        return r.json()["mcp_json"]["mcpServers"]

    # ---- hand off to FreeVideo's agent (Claude Code + FreeVideo MCP) ----
    def freevideo_online_ok(self) -> bool:
        return bool(self.cfg.allow_online or getattr(self.cfg, "freevideo_agent_online", False))

    def delegate_freevideo(
        self, text: str, channel: str, project_id: str, session_id: str | None, start: bool = True
    ) -> dict:
        """Start a new run handed entirely to the FreeVideo agent; when it ends, `run:<id>` wakes runs waiting for it."""
        if not self.freevideo_online_ok():
            raise ValueError(tr("fv_offline"))
        return self.delegate_to("freevideo", text, channel, project_id, session_id, start)

    def delegate_to(
        self,
        name: str,
        text: str,
        channel: str,
        project_id: str,
        session_id: str | None,
        start: bool = True,
    ) -> dict:
        """Start a new run handed to an agent CLI (claude_code / codex / freevideo); wakes `run:<id>` when it ends."""
        run = self.store.create_run(project_id, session_id, channel, text)
        self.store.add_agent_msg(run["id"], "user", f"## User\n{text}")
        if start:
            self._delegate(run, name)
        return run

    def external_tool(self, run_id: str, name: str, args: dict, timeout: float = 1800) -> dict:
        """Tool calls from a CLI agent over MCP: same execution policy, approvals and waits, only the result is returned synchronously."""
        run = self.store.get_run(run_id)
        if run is None or run["status"] != "delegated":
            return {"error": "this run is not delegated to an external agent"}
        call = {
            "id": f"ext_{uuid.uuid4().hex[:10]}",
            "name": name,
            "arguments": json.dumps(args, ensure_ascii=False),
        }
        self._execute(run, call, self.say_for(run["channel"]))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            m = next(
                (
                    x
                    for x in self.store.agent_msgs(run_id)
                    if x["role"] == "tool" and x["tool_call_id"] == call["id"]
                ),
                None,
            )
            if m and m["status"] == "done":
                try:
                    return json.loads(m["content"])
                except (TypeError, json.JSONDecodeError):
                    return {"result": m["content"]}
            time.sleep(1)
        return {
            "pending": True,
            "note": "still waiting (render or user decision); "
            "call the same tool again to keep waiting",
        }

    def _check_args(self, call: dict, truncated: bool) -> dict:
        """Arguments that are not valid JSON (usually cut off at max_tokens) are stored as "{}" plus
        an error, so the history replays cleanly and the tool is not run with missing parameters."""
        raw = call.get("arguments") or ""
        if not truncated and not raw.strip():
            return {**call, "arguments": "{}"}
        try:
            if isinstance(json.loads(raw), dict):  # complete even if the turn was cut off later
                return call
        except json.JSONDecodeError:
            pass
        if truncated:
            err = (
                f"output hit max_tokens ({self.cfg.agent_max_tokens}) and the tool call was cut off "
                f"after {len(raw)} characters of arguments; nothing was run. Split large content "
                "into several smaller calls (e.g. several smaller files)."
            )
        else:
            err = f"arguments are not valid JSON ({len(raw)} characters); nothing was run"
        return {**call, "arguments": "{}", "bad_args": err}

    def _execute(self, run: dict, call: dict, say) -> None:
        if call.get("bad_args"):
            return self._tool_done(run, call, {}, {"error": call["bad_args"]})
        tool = self.tools.get(call["name"])
        ctx = {
            "run_id": run["id"],
            "call_id": call["id"],
            "channel": run["channel"],
            "project_id": run["project_id"],
            "origin_text": run["origin_text"] or "",
            "say": say,
        }
        try:
            args = json.loads(call["arguments"] or "{}")
        except json.JSONDecodeError as e:
            return self._tool_done(run, call, {}, {"error": f"arguments are not valid JSON: {e}"})
        if tool is None or not tool.description:
            return self._tool_done(run, call, args, {"error": f"unknown tool {call['name']}"})
        denied = self.policies.check(call["name"], args)
        if denied:  # execution policy: only refuses, never rewrites your parameters
            return self._tool_done(run, call, args, {"error": denied})
        try:
            out = tool.handler(ctx, args)
        except (
            StudioError,
            ParamError,
            KeyError,
            ValueError,
            ProductionError,
            FreeVideoError,
            OSError,
            MCPError,
        ) as e:
            out = Done({"error": str(e)})
        except jsonschema.ValidationError as e:
            out = Done({"error": f"schema: {e.message}"})
        # any other bug in a tool handler: report it to the agent instead of killing the step,
        # which would leave the run stuck in "running" with a call that never gets a result
        except Exception as e:
            log.exception("tool %s failed", call["name"])
            out = Done({"error": f"{type(e).__name__}: {e}"})
        if isinstance(out, Pending) and out.kind == "approval" and out.on_approve:
            how = self.auto_approval(out.rule, out.bypassable)
            # bypass mode or an "always allow" rule you set: do not ask, run the approved action
            # directly
            if how:
                say(tr("agent_auto_approved", how=tr(f"perm_how_{how}"), what=_first_line(out)))
                out = self._run_approved(ctx, out.on_approve, how)
        if isinstance(out, Done):
            return self._tool_done(run, call, args, out.result)
        mid = self.store.add_agent_msg(
            run["id"],
            "tool",
            None,
            tool_call_id=call["id"],
            name=call["name"],
            status="pending",
            kind=out.kind,
            key=out.key,
            on_approve=out.on_approve,
            info=out.info,
            rule=out.rule,
        )
        if out.kind == "approval":
            key = f"approval:{mid}"
            self.store.update_agent_msg(mid, meta={"key": key})
            row = [(tr("btn_approve"), f"a:{mid}:yes")]
            if out.rule:
                row.append((tr("btn_always", r=out.rule), f"a:{mid}:always"))
            row.append((tr("btn_decline"), f"a:{mid}:no"))
            say(out.message, [row])
        elif out.kind == "user":
            say(out.message)
        if out.start:
            out.start()
        self._log_step(run, call, args, "…")

    def _tool_done(self, run: dict, call: dict, args: dict, result: dict) -> None:
        text = json.dumps(result, ensure_ascii=False)
        # overly long results are truncated before handing back; the full text stays in meta
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + "…(truncated)"
        self.store.add_agent_msg(
            run["id"], "tool", text, tool_call_id=call["id"], name=call["name"], full=result
        )
        self._log_step(run, call, args, text)

    def _log_step(self, run: dict, call: dict, args, result: str) -> None:
        brief = ", ".join(
            f"{k}={str(v)[:40]}" for k, v in (args or {}).items() if k not in ("prompt",)
        )
        self.studio.log(
            "agent",
            "out",
            f"🔧 {call['name']}({brief}) → {result[:160]}",
            run["project_id"],
            run_id=run["id"],
            tool=call["name"],
        )

    def _unresolved(self, run_id: str) -> list[dict]:
        return [
            m
            for m in self.store.agent_msgs(run_id)
            if m["role"] == "tool" and m["status"] != "done"
        ]

    def _advance(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        if run and run["status"] == "delegated":
            # the CLI agent is waiting for this result itself (external_tool polling); do not queue
            # a local step
            return
        pending = self._unresolved(run_id)
        if not pending:
            self.store.update_run(run_id, status="running", waiting={})
            self.enqueue(lambda: self.step(run_id))
            return
        kinds = {m["meta"].get("kind") for m in pending}
        status = "waiting_user" if kinds & {"approval", "user"} else "waiting_event"
        self.store.update_run(
            run_id, status=status, waiting={"keys": [m["meta"].get("key") for m in pending]}
        )

    def _finish(self, run: dict, note: str | None, status: str, say_text: str | None = None):
        self.store.update_run(run["id"], status=status)
        text = say_text if say_text is not None else note
        if text:
            self.say_for(run["channel"])(text)
        if run["session_id"]:
            self.enqueue(lambda: self.prewarm(run["session_id"]))

    def prewarm(self, session_id: str) -> None:
        """Prewarm the cache for the next message.

        Measured (G0 baseline): the engine keeps a checkpoint "before the last message" of each request; a new request reuses it only if it
        matches an old request all the way up to there. Here we send [stable prefix + compacted history + placeholder user message] once,
        so your next message hits the checkpoint "before the placeholder". Skipped when resources do not allow (never competes with a render).
        """
        rm = self.studio.orch.resources
        if not rm.llm_admit()[0] or not self.planner.available():
            return
        session = self.store.get_session(session_id)
        if session is None:
            return
        msgs = [{"role": "system", "content": self.system_prompt(session["project_id"], session)}]
        upto = self.store.get_kv(f"agent_upto:{session_id}") or 0
        for r in self.store.runs(session_id):
            if r["created"] > upto and r["status"] in TERMINAL_RUN:
                msgs += self._compact(r)
        msgs.append({"role": "user", "content": "## Live state\n(warm-up)"})
        try:
            with rm.llm.inference():
                self.planner.backend.prewarm(msgs, tools=self.spec)
        except Exception:  # a failed prewarm affects no result
            log.warning("agent prewarm failed", exc_info=True)

    # ---- recovery and wake-up ----
    def resolve(self, key: str, result: dict) -> int:
        """Fill in waiting tool results (events / approvals / answers) and queue runs that can continue."""
        n = 0
        for m in self.store.pending_tool_msgs():
            if m["meta"].get("key") != key:
                continue
            self.store.update_agent_msg(
                m["id"],
                content=json.dumps(result, ensure_ascii=False),
                status="done",
                meta={"full": result},
            )
            n += 1
            self._advance(m["run_id"])
        return n

    def resolve_msg(self, m: dict, result: dict) -> None:
        self.store.update_agent_msg(
            m["id"],
            content=json.dumps(result, ensure_ascii=False),
            status="done",
            meta={"full": result},
        )
        self._advance(m["run_id"])

    def on_round_ready(self, rnd: dict) -> None:
        """A render result arrived: by continuation policy, hand it back to the LLM, auto next step, or bounded retry."""
        key = f"round:{rnd['id']}"
        for m in self.store.pending_tool_msgs():
            if m["meta"].get("key") != key:
                continue
            run = self.store.get_run(m["run_id"])
            pipe = (m["meta"].get("info") or {}).get("pipeline")
            outcome = classify_round(self.store, rnd["id"])
            retries = m["meta"].get("retries", 0)
            last = bool(pipe) and pipe["index"] >= len(pipe["steps"]) - 1
            action = self.policies.on_round(outcome, retries, bool(pipe), last)
            if action == "retry":
                new = self._retry(rnd, run)
                if pipe:
                    pipe["rounds"][-1] = new["id"]
                self.store.update_agent_msg(
                    m["id"],
                    meta={
                        "key": f"round:{new['id']}",
                        "retries": retries + 1,
                        "info": {"pipeline": pipe} if pipe else {},
                    },
                )
                self._log_policy(run, f"{outcome} → retry {retries + 1} (same parameters)")
                continue
            if action == "auto_next":
                pipe["index"] += 1
                nxt = self._queue_step(
                    {
                        "channel": run["channel"],
                        "project_id": run["project_id"],
                        "run_id": run["id"],
                    },
                    pipe["steps"][pipe["index"]],
                    rnd["id"],
                )
                pipe["rounds"].append(nxt["id"])
                self.store.update_agent_msg(
                    m["id"],
                    meta={"key": f"round:{nxt['id']}", "retries": 0, "info": {"pipeline": pipe}},
                )
                self._log_policy(
                    run,
                    f"step {pipe['index']} of {len(pipe['steps'])} done "
                    "→ next step queued (no LLM)",
                )
                continue
            if pipe:
                result = {
                    "pipeline": [
                        _round_view(self.store, self.store.get_round(r)) for r in pipe["rounds"]
                    ],
                    "finished": action == "finished",
                    "outcome": outcome,
                }
            else:
                result = {**_round_view(self.store, rnd), "outcome": outcome}
            self.resolve_msg(m, result)

    def _retry(self, rnd: dict, run: dict) -> dict:
        tasks = self.store.round_tasks(rnd["id"])
        seeds = [t["params"].get("seed") for t in tasks]
        return self.studio.start_round(
            rnd["template"],
            rnd["base_params"],
            rnd["n"],
            rnd["tier"],
            rnd["title"],
            origin=run["channel"],
            request="policy retry",
            parent_round=rnd["id"],
            project_id=rnd["project_id"],
            allow_unverified=True,
            seeds=seeds if all(isinstance(x, int) for x in seeds) else None,
        )

    def _log_policy(self, run: dict, text: str) -> None:
        self.studio.log("agent", "out", f"⚙️ policy: {text}", run["project_id"], run_id=run["id"])

    def on_round_decided(self, rnd: dict) -> None:
        """You decided with the normal review buttons: the approval request waiting on this round now has its answer."""
        d = rnd["decision"] or {}
        for m in self.store.pending_tool_msgs():
            if (
                m["meta"].get("kind") == "approval"
                and m["meta"].get("info", {}).get("round_id") == rnd["id"]
            ):
                self.resolve(
                    m["meta"]["key"],
                    {
                        "user_decision": d.get("action"),
                        "selected": d.get("idx"),
                        "role": d.get("role"),
                    },
                )
        self.on_round_ready(rnd)

    # ---- permissions: default (approve each) | bypass (no approvals); "always allow" rules ----
    def permission_mode(self) -> str:
        return self.store.get_kv("permission_mode") or getattr(
            self.cfg, "permission_mode", "default"
        )

    def allow_rules(self) -> list[str]:
        return list(self.store.get_kv("perm_allow") or [])

    def remember_rule(self, rule: str) -> None:
        rules = self.allow_rules()
        if rule not in rules:
            self.store.set_kv("perm_allow", [*rules, rule])

    def auto_approval(self, rule: str | None, bypassable: bool = True) -> str | None:
        if bypassable and self.permission_mode() == "bypass":
            return "bypass"
        if rule and rule in self.allow_rules():
            return "rule"
        return None

    def _run_approved(self, ctx: dict, act: dict, how: str):
        try:
            out = self.tools[act["tool"]].handler(ctx, act["args"])
        except (StudioError, ParamError, KeyError, ValueError, OSError, MCPError) as e:
            return Done({"approved": True, "auto": how, "error": str(e)})
        if isinstance(out, Done):
            return Done({"approved": True, "auto": how, **out.result})
        # runs in the background: noted in the message, the result comes back as usual
        out.info = {**out.info, "auto": how}
        return out

    def on_approval(self, msg_id: int, approve: bool, remember: bool = False) -> str:
        m = self.store.get_agent_msg(msg_id)
        if m is None or m["status"] == "done":
            return tr("agent_already_decided")
        run = self.store.get_run(m["run_id"])
        if approve and remember and m["meta"].get("rule"):
            self.remember_rule(m["meta"]["rule"])
        if not approve:
            self.resolve(m["meta"]["key"], {"approved": False, "note": "user declined"})
            return tr("agent_declined")
        act = m["meta"].get("on_approve") or {}
        ctx = {
            "run_id": run["id"],
            "call_id": m["tool_call_id"],
            "channel": run["channel"],
            "project_id": run["project_id"],
            "origin_text": run["origin_text"] or "",
            "say": self.say_for(run["channel"]),
        }
        try:
            out = self.tools[act["tool"]].handler(ctx, act["args"])
            # runs in the background after approval: switch to waiting for its result first, then
            # start
            if isinstance(out, Pending):
                self.store.update_agent_msg(
                    msg_id, meta={"key": out.key, "kind": out.kind, "approved": True}
                )
                self._advance(run["id"])
                if out.start:
                    out.start()
                return tr("agent_approved")
            result = {"approved": True, **out.result}
        except (StudioError, ParamError, KeyError, ValueError) as e:
            result = {"approved": True, "error": str(e)}
        self.resolve(m["meta"]["key"], result)
        return tr("agent_approved")

    def answer_pending_question(
        self, channel: str, session_id: str, text: str, attachments: list[dict] | None = None
    ) -> bool:
        answer = text + attachment_note(attachments)
        for run in self.store.runs(session_id, "waiting_user", "delegated"):
            if run["channel"] != channel:
                continue
            if self.resolve(f"ask:{run['id']}", {"answer": answer}):
                return True
        return False

    def on_resources_freed(self) -> None:
        for run in self.store.runs(None, "waiting_resources"):
            self.enqueue(lambda rid=run["id"]: self.step(rid))

    def recover(self) -> None:
        """Daemon restart: runs that can continue do; if the round a run waits for has finished, fill in the result directly."""
        for m in self.store.pending_tool_msgs():
            key = m["meta"].get("key") or ""
            if key.startswith("job:") and not self.toolbox.jobs.busy(key):
                # background tools were interrupted by the daemon restart: report that honestly and
                # let the agent decide whether to redo (commands are never rerun automatically)
                self.resolve_msg(m, {"error": "interrupted by a daemon restart; not rerun"})
                continue
            if key.startswith("round:"):
                rnd = self.store.get_round(key.split(":", 1)[1])
                if rnd and rnd["state"] != "generating":
                    self.on_round_ready(rnd)
        for run in self.store.runs(None, "running", "waiting_resources"):
            if not self._unresolved(run["id"]):
                self.enqueue(lambda rid=run["id"]: self.step(rid))

    def cancel(self, run_id: str) -> None:
        self.store.update_run(run_id, status="cancelled")
        for m in self._unresolved(run_id):
            self.store.update_agent_msg(m["id"], content='{"cancelled": true}', status="done")
