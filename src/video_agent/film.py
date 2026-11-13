"""A production's film stage: shot videos are shot as a FreeVideo multi-shot film (character cards for consistency, continuing from the previous shot for continuity).

Flow (review still happens on our side):
1. Character cards (tool: freevideo_cards): one FreeVideo character card per person — the card named in settings first,
   then an existing card in the library with the same name, otherwise a new one from the approved casting image (and reference images you provided).
2. Shoot: storyboard → script (characters point to cards, lines carry the speaker, continue means pick up from the previous shot) → FreeVideo plan validation → start.
   One artifact per shot (status running, meta records film / shot).
3. Polling: when all takes of a shot are done → download into the task directory and import as a round of candidates for review (template freevideo-take).
4. You pick one in review → FreeVideo choose (later continuation shots continue from this take's last frame).
   Redo / reject with a note → FreeVideo reshoot (the note is passed through as-is); a reject without a note also reshoots.
5. Nothing advances while paused; "stop now" cancels takes not yet shot (finished ones are kept).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import TYPE_CHECKING

from .freevideo import FreeVideoError

if TYPE_CHECKING:  # pragma: no cover
    from .production import Production, Stage

TERMINAL_JOB = ("succeeded", "failed", "cancelled", "interrupted")
TAKE_TEMPLATE = "freevideo-take"
MIN_CONTINUE_S = 10.0
# while waiting for your approval in the FreeVideo page, how often to resend the start request
RETRY_S = 60.0
_FIX = re.compile(r"Use /screenplay/shots/(\d+)/seconds ([\d.]+)")


FILM_DONE = ("cancelled", "done", "finished", "completed", "failed", "wrapped")
TAKE_ACTIVE = ("queued", "running", "waiting")


def film_active(film: dict) -> bool:
    """Still shooting (holds memory): not finished, and some take is queued / shooting, or some shot has no take yet."""
    if film.get("status") in FILM_DONE:
        return False
    shots = film.get("shots") or []
    # waiting for you to pick a take / handle an interruption: FreeVideo is idle
    if film.get("status") == "needs_choice":
        return any(t.get("state") in TAKE_ACTIVE for sh in shots for t in sh.get("takes") or [])
    return any(
        not sh.get("takes") or any(t.get("state") in TAKE_ACTIVE for t in sh["takes"])
        for sh in shots
    )


def _script_hash(sp: dict) -> str:
    return hashlib.sha256(json.dumps(sp, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


class FilmDriver:
    def __init__(self, engine: Production, client):
        self.engine, self.fv = engine, client
        self.store = engine.store
        # injected by the daemon: push an imported round to the console / Telegram
        self.notify_round = lambda rnd: None
        # injected by the daemon: send you a reminder (e.g. to go approve in FreeVideo)
        self.notify_text = lambda prod, key: None
        self._last_retry: dict[str, float] = {}
        # the daemon injects the ResourceManager: shooting holds the memory (unload the LLM before
        # shooting, keep it from loading meanwhile)
        self.resources = None

    def _make_room(self) -> None:
        if self.resources is not None:
            self.resources.prepare_film()

    def _started(self, film_id: str) -> None:
        if self.resources is not None:
            self.resources.film_started(film_id)

    # ---- character cards ----
    def cards_tool(self, prod: dict, st: Stage, inputs: dict) -> dict:
        settings = prod["settings"]
        fixed = settings.get("cards") or {}
        bible = (
            next(
                (
                    v
                    for v in inputs.values()
                    if isinstance(v, dict)
                    and "characters" in v
                    and isinstance(v["characters"], list)
                ),
                {},
            )
            or {}
        )
        sheets = inputs.get("characters") or {}  # {key: data of the approved casting artifact}
        library = {c.get("name", "").strip().lower(): c for c in self.fv.cards()}
        out: dict = {"characters": {}, "created": [], "reused": []}
        for c in bible.get("characters", []):
            key = c["id"]
            if key in fixed:
                out["characters"][key] = fixed[key]
                out["reused"].append(key)
                continue
            hit = library.get(c.get("name", "").strip().lower())
            if hit:
                out["characters"][key] = hit["id"]
                out["reused"].append(key)
                continue
            pics = [
                (o["path"], "sheet")
                for o in (sheets.get(key) or {}).get("outputs", [])
                if o.get("kind") == "image"
            ]
            if not pics:
                # no approved casting image: this person relies on text only in the shots (the plan
                # shows cast_missing)
                continue
            card = self.fv.create_card(
                c.get("name") or key, c.get("look") or c.get("name") or key, pics[:6]
            )
            out["characters"][key] = card["id"]
            out["created"].append(key)
        return out

    # ---- script ----
    def screenplay(self, prod: dict, st: Stage, ctx: dict, keys: list[str] | None = None) -> dict:
        settings = prod["settings"]
        lang = (settings.get("language") or "English").strip() or "English"
        cast_map = (ctx.get(st.cast) or {}).get("characters", {}) if st.cast else {}
        bible = ctx.get("bible") or {}
        looks = {c["id"]: c for c in bible.get("characters", [])}
        # hand-written / imported storyboards may carry film-wide style, character looks,
        # continuity, director rules and negatives: all go to FreeVideo
        board = ctx.get(st.fan_out.items.split(".")[0]) if st.fan_out else None
        board = board if isinstance(board, dict) else {}
        board_looks = board.get("characters") if isinstance(board.get("characters"), dict) else {}
        chars = {}
        for key, card in cast_map.items():
            entry = {"card": card, "language": lang}
            if looks.get(key, {}).get("voice"):
                entry["voice"] = looks[key]["voice"]
            if isinstance(board_looks.get(key), str) and board_looks[key].strip():
                entry["look"] = board_looks[key].strip()
            chars[key] = entry
        shots = []
        for s in self.engine._items(st, ctx):
            key, item = s
            if keys is not None and key not in keys:
                continue
            transition = item.get("transition_to_next")
            sh = {
                "id": key,
                "seconds": item.get("seconds") or 5,
                "action": ". ".join(
                    x
                    for x in (
                        item.get("frame"),
                        item.get("motion"),
                        transition and f"Ends ready for: {transition}",
                    )
                    if x
                ),
                "camera": item.get("camera") or "",
                "framing": item.get("framing") or "",
            }
            cast = [k for k in item.get("cast") or [] if k in chars]
            if cast:
                sh["cast"] = cast
            lines = item.get("lines") or []
            if not lines and item.get("dialogue") and cast:
                lines = [{"who": cast[0], "line": item["dialogue"]}]
            lines = [ln for ln in lines if ln.get("who") in chars and ln.get("line")]
            if lines:
                sh["lines"] = lines
            if item.get("continue") and shots:
                sh["continue"] = True
                sh["seconds"] = max(
                    float(sh["seconds"]), MIN_CONTINUE_S
                )  # FreeVideo: continuations are at least 10 seconds
            shots.append(sh)
        rules = board.get("directing_rules") or []
        # one line: an "Avoid:" at the start of a new line would be taken by H3 as a new section
        style = " ".join(
            x
            for x in (
                board.get("visual_style") or (bible.get("style") or {}).get("look", ""),
                board.get("continuity"),
                "Directing rules: " + " ".join(rules) if rules else None,
                board.get("negative_prompt") and "Avoid: " + board["negative_prompt"],
            )
            if x
        )
        return {
            "title": prod["title"],
            "style": style,
            "aspect": settings.get("aspect", "16:9"),
            "quality": settings.get("film_quality", "medium"),
            "characters": chars,
            "shots": shots,
        }

    # ---- shoot / reshoot ----
    def current_film(self, prod_id: str, stage_id: str) -> str | None:
        films = [
            a["meta"].get("film")
            for a in self.store.artifacts(prod_id, stage_id)
            if (a["meta"] or {}).get("film")
        ]
        return films[-1] if films else None

    def start(
        self,
        prod: dict,
        st: Stage,
        idx,
        ctx: dict,
        keys: list[str],
        note: str | None,
        deps_for,
        overrides: dict | None = None,
    ) -> list[dict]:
        """overrides: {quality, takes} for this shoot only (e.g. try one shot at high quality); production settings unchanged."""
        overrides = {k: v for k, v in (overrides or {}).items() if v}
        film_id = self.current_film(prod["id"], st.id)
        made = []
        script = _script_hash(self.screenplay(prod, st, ctx))
        if film_id:
            try:
                if self.fv.film(film_id).get("status") == "cancelled":
                    film_id = None  # a cancelled film is not reshot: start a new one
            except FreeVideoError:
                film_id = None
        if film_id and self._film_script(prod["id"], st.id, film_id) not in (None, script):
            # storyboard / style / character looks changed: FreeVideo's reshoot reuses the old
            # script, so start a new film for these shots
            self.engine.log(f"film {film_id}: screenplay changed; starting a new film for {keys}")
            film_id = None
        if overrides:
            # quality / number of takes are film-wide parameters: start a separate film
            film_id = None
        if film_id:
            try:
                for key in keys:
                    made.append(self._reshoot(prod, st, film_id, key, note, deps_for(key)))
                return made
            # the film has ended or was cancelled: start a new one for these shots
            except FreeVideoError as e:
                self.engine.log(f"film {film_id}: reshoot failed ({e}); starting a new film")
                made = []
        sp = self.screenplay(prod, st, ctx, keys)
        takes = int(overrides.get("takes") or prod["settings"].get("video_candidates") or 1)
        if overrides.get("quality"):
            sp["quality"] = overrides["quality"]
        ref = f"aco-{prod['id']}-{st.id}-{int(time.time())}"
        # validate first (no submit): mistakes surface here
        plan, fixed = self._validated_plan(sp, takes)
        self._make_room()
        try:
            film = self.fv.start_film(sp, takes=takes, ref=ref)
            self._started(film["id"])
        except FreeVideoError as e:
            if e.code != "needs_confirmation":
                raise
            film = (
                # FreeVideo is set to require your approval in its page before shooting: park it and
                # resend unchanged (same ref) once approved
                None
            )
        pending = None if film else {"screenplay": sp, "takes": takes, "ref": ref}
        self.engine.log(
            (
                f"film {film['id']} started"
                if film
                else "film start waits for your approval in FreeVideo"
            )
            + f" for {prod['id']}: {len(sp['shots'])} shot(s) × {takes} take(s)"
            + (f"; durations adjusted for FreeVideo: {fixed}" if fixed else "")
        )
        if pending:
            self.notify_text(prod, "film_needs_approval")
        for sh in sp["shots"]:
            preview = next(
                (p for p in plan.get("preview", {}).get("shots", []) if p["id"] == sh["id"]), {}
            )
            made.append(
                self.store.add_artifact(
                    prod["id"],
                    st.id,
                    sh["id"],
                    "running",
                    deps=deps_for(sh["id"]),
                    note=None if film else "waiting for your approval in the FreeVideo page",
                    meta={
                        "film": film["id"] if film else None,
                        "script": script,
                        "pending_start": pending if sh is sp["shots"][0] else None,
                        "pending_ref": None if film else ref,
                        "shot": sh["id"],
                        "imported": [],
                        "prompt": preview.get("prompt"),
                        "cast_reference": preview.get("cast_reference"),
                        "cast_missing": preview.get("cast_missing"),
                    },
                )
            )
        return made

    def _film_script(self, prod_id: str, stage_id: str, film_id: str) -> str | None:
        for a in reversed(self.store.artifacts(prod_id, stage_id)):
            if (a["meta"] or {}).get("film") == film_id and a["meta"].get("script"):
                return a["meta"]["script"]
        return None  # films started by old versions recorded no script: reshoot as before

    def _validated_plan(self, sp: dict, takes: int) -> tuple[dict, dict]:
        """Plan validation; when FreeVideo gives a clear duration suggestion (e.g. continuations must be 10–11 s) apply it once and validate again.
        Returns (plan, changed shots {id: (old duration, new duration)}). Other errors are raised unchanged for you to see."""
        fixed: dict = {}
        for _ in range(2):
            try:
                return self.fv.plan_film(sp, takes=takes), fixed
            except FreeVideoError as e:
                hints = _FIX.findall(str(e))
                if not hints:
                    raise
                for idx, sec in hints:
                    shot = sp["shots"][int(idx)]
                    fixed[shot["id"]] = (shot["seconds"], float(sec))
                    shot["seconds"] = float(sec)
        return self.fv.plan_film(sp, takes=takes), fixed

    def _reshoot(self, prod, st, film_id, key, note, deps) -> dict:
        takes = int(prod["settings"].get("video_candidates") or 1)
        self._make_room()
        self.fv.reshoot(film_id, key, note=note, takes=takes)
        self._started(film_id)
        imported = sorted(
            {
                job
                for a in self.store.artifacts(prod["id"], st.id)
                if a["key"] == key and (a["meta"] or {}).get("film") == film_id
                for job in (a["meta"] or {}).get("imported", [])
            }
        )  # takes imported before are not imported again
        return self.store.add_artifact(
            prod["id"],
            st.id,
            key,
            "running",
            deps=deps,
            note=note,
            meta={"film": film_id, "shot": key, "imported": imported},
        )

    def intercept_round(self, rnd: dict) -> bool:
        """When you redo / reject with a note / refine a round of takes in review, Studio derives a new freevideo-take
        draft round as usual: here it is replaced by a FreeVideo reshoot (the note passed through as-is) and the draft dropped. Returns whether we took over."""
        if rnd.get("template") != TAKE_TEMPLATE or not rnd.get("parent_round"):
            return False
        parent = self.store.artifact_by_round(rnd["parent_round"])
        if parent is None or not (parent["meta"] or {}).get("film"):
            return False
        note = rnd.get("request")
        for t in self.store.round_tasks(rnd["id"]):
            if t["state"] == "draft":
                self.store.transition(
                    t["id"], "failed", "replaced by FreeVideo reshoot", error="reshoot"
                )
        self.store.update_round(
            rnd["id"], state="closed", decision={"action": "cancel", "note": "FreeVideo reshoot"}
        )
        prod = self.store.get_production(parent["production_id"])
        st = self.engine._wf(prod).stage(parent["stage"])
        self._reshoot(prod, st, parent["meta"]["film"], parent["key"], note, parent["deps"])
        return True

    def on_chosen(self, art: dict, rnd: dict) -> None:
        meta = art["meta"] or {}
        if not meta.get("film"):
            return
        task = self.store.get((rnd.get("decision") or {}).get("task_id") or "")
        if task is None:
            return
        try:
            self.fv.choose(meta["film"], meta["shot"], int(task["params"]["take"]))
        except FreeVideoError as e:
            self.engine.log(
                f"film {meta['film']}: choose {meta['shot']} take {task['params']['take']} failed: {e}"
            )

    def cancel(self, prod_id: str) -> None:
        for film_id in {
            (a["meta"] or {}).get("film")
            for a in self.store.artifacts(prod_id)
            if (a["meta"] or {}).get("film")
        }:
            try:
                self.fv.cancel_film(film_id)
            except FreeVideoError as e:
                self.engine.log(f"film {film_id}: cancel failed: {e}")

    # ---- polling: finished takes are imported as a round for review ----
    def tick(self) -> int:
        imported = 0
        shooting: set[str] = set()
        for prod in self.store.productions():
            # paused means no progress, but FreeVideo may still be shooting: still counts as holding
            # memory
            if prod["state"] == "paused":
                shooting |= self._shooting(prod)
                continue
            if prod["state"] != "active":
                continue
            imported += self._tick_production(prod, shooting)
        # update only after reading a full pass; on errors midway (FreeVideo unreachable) keep the
        # previous conclusion
        if self.resources is not None:
            self.resources.films_shooting(shooting)
        return imported

    def _shooting(self, prod: dict) -> set[str]:
        out = set()
        for film_id in {
            (a["meta"] or {}).get("film")
            for a in self.store.artifacts(prod["id"], None, "running")
            if (a["meta"] or {}).get("film") and not a["round_id"]
        }:
            try:
                film = self.fv.film(film_id)
            except Exception:  # noqa: BLE001 — unreadable: conservatively assume still shooting
                out.add(film_id)
                continue
            if film_active(film):
                out.add(film_id)
        return out

    def _tick_production(self, prod: dict, shooting: set[str]) -> int:
        imported = 0
        self._retry_pending_start(prod)
        waiting: dict[str, dict[str, dict]] = {}
        for a in self.store.artifacts(prod["id"], None, "running"):
            meta = a["meta"] or {}
            if meta.get("film") and not a["round_id"]:
                waiting.setdefault(meta["film"], {})[meta["shot"]] = a
        for film_id, arts in waiting.items():
            try:
                film = self.fv.film(film_id)
            except FreeVideoError as e:
                self.engine.log(f"film {film_id}: read failed: {e}")
                shooting.add(film_id)  # unreadable: conservatively assume still shooting
                continue
            if film_active(film):
                shooting.add(film_id)
            for shot in film.get("shots") or []:
                art = arts.get(shot["id"])
                if art is not None:
                    imported += self._import(prod, art, film, shot)
            # film cancelled: unfinished shots will get no takes, mark them failed so you can rerun
            if film.get("status") == "cancelled":
                for art in arts.values():
                    cur = self.store.get_artifact(art["id"])
                    if cur["status"] == "running" and not cur["round_id"]:
                        self.store.update_artifact(
                            art["id"], status="failed", note=f"film {film_id} was cancelled"
                        )
        return imported

    def _retry_pending_start(self, prod: dict) -> None:
        """The shoot is waiting for your approval in the FreeVideo page: resend the same request (same ref, idempotent); get the film ID once approved."""
        arts = [
            a
            for a in self.store.artifacts(prod["id"], None, "running")
            if (a["meta"] or {}).get("pending_ref")
        ]
        head = next((a for a in arts if (a["meta"] or {}).get("pending_start")), None)
        if head is None:
            return
        req = head["meta"]["pending_start"]
        now = time.monotonic()
        if now - self._last_retry.get(req["ref"], 0) < RETRY_S:
            # do not resend every pass (each one logs a "needs confirmation" in the FreeVideo chat)
            return
        self._last_retry[req["ref"]] = now
        try:
            self._make_room()
            film = self.fv.start_film(req["screenplay"], takes=req["takes"], ref=req["ref"])
            self._started(film["id"])
        except FreeVideoError as e:
            # including your refusal (declined_by_user): stop resending
            if e.code != "needs_confirmation":
                self.engine.log(f"film start for {prod['id']} failed: {e}")
                for a in arts:
                    self.store.update_artifact(a["id"], status="failed", note=str(e)[:500])
            return
        for a in arts:
            if a["meta"].get("pending_ref") == req["ref"]:
                meta = {**a["meta"], "film": film["id"], "pending_start": None, "pending_ref": None}
                self.store.update_artifact(a["id"], meta=meta, note=None)
        self.engine.log(f"film {film['id']} started for {prod['id']} after your approval")

    def _import(self, prod: dict, art: dict, film: dict, shot: dict) -> int:
        done_before = set((art["meta"] or {}).get("imported") or [])
        takes = [
            t
            for t in shot.get("takes") or []
            if not t.get("stale") and t.get("job") not in done_before
        ]
        if not takes:
            return 0
        jobs = {}
        if any(t.get("state") in ("queued", "running", "waiting") for t in takes):
            # this shot still has takes shooting: review them together once all are in (no need to
            # query one by one)
            return 0
        for t in takes:
            if not t.get("task") and t.get("state") == "failed":
                # failed before a task was created (e.g. continuation_unavailable): no job to query,
                # the error is in the take
                err = t.get("error") or {}
                jobs[t["take"]] = {"status": "failed", "error": err.get("error_zh") or err.get("message")}
                continue
            # a take's job is the idempotency key; the real task ID is in the task field
            job = self.fv.call("GET", f"/jobs/{t.get('task') or t['job']}").get("job") or {}
            if job.get("status") not in TERMINAL_JOB:
                return 0
            jobs[t["take"]] = job
        st = self.engine._wf(prod).stage(art["stage"])
        rnd = self.store.create_round(
            project_id=prod["project_id"],
            template=TAKE_TEMPLATE,
            tier="take",
            title=f"{prod['title']} · {st.title or st.id} · {shot['id']}",
            base_params={"film": film["id"], "shot": shot["id"]},
            n=len(takes),
            origin="production",
            request=f"production:{prod['id']}:{st.id}:{shot['id']}",
        )
        tmpl = self.engine.studio.orch.registry.template(TAKE_TEMPLATE)
        for i, t in enumerate(takes, 1):
            job = jobs[t["take"]]
            params = {
                "film": film["id"],
                "shot": shot["id"],
                "take": t["take"],
                "prompt": ((art["meta"] or {}).get("prompt") or "")[:4000],
            }
            outputs, state, note = [], "failed", f"FreeVideo take {t['take']}: {job.get('status')}"
            if job.get("error"):
                note += f" ({job['error']})"
            video = ((job.get("result") or {}).get("video") or {}).get("url")
            if job.get("status") == "succeeded" and video:
                state, note = "review", f"FreeVideo take {t['take']} (job {t['job']})"
                outputs = [
                    {"file": f"take{t['take']}.mp4", "kind": "video", "source": {"job": t["job"]}}
                ]
            task = self.store.create(
                template=TAKE_TEMPLATE,
                template_sha=tmpl.sha,
                kind="video",
                tier="take",
                params=params,
                params_hash=f"fv-{t['job']}",
                title=f"{rnd['title']} #{i}",
                project_id=prod["project_id"],
                round_id=rnd["id"],
                idx=i,
                priority=1,
                state=state,
                outputs=outputs,
                note=note,
            )
            if outputs:
                try:
                    self.fv.download(
                        job["result"]["video"]["url"],
                        self.store.task_dir(task["id"]) / outputs[0]["file"],
                    )
                    self.store.update(
                        task["id"],
                        metrics={
                            "freevideo": {
                                k: job.get(k) for k in ("id", "seconds", "seed") if k in job
                            }
                        },
                    )
                # download failed: mark this one failed, the others continue
                except Exception as e:  # noqa: BLE001
                    self.store.update(
                        task["id"], state="failed", outputs=[], error=f"download failed: {e}"
                    )
            self.store.write_manifest(task["id"])
        rnd = self.store.update_round(rnd["id"], state="review")
        self.store.update_artifact(
            art["id"],
            round_id=rnd["id"],
            meta={**art["meta"], "imported": sorted(done_before | {t["job"] for t in takes})},
        )
        self.engine.log(
            f"film {film['id']}: imported {len(takes)} take(s) of {shot['id']} for review"
        )
        self.notify_round(rnd)
        return 1


def film_summary(film: dict) -> str:
    p = film.get("progress") or {}
    return json.dumps(
        {k: p.get(k) for k in ("takes", "shots", "elapsed_seconds", "remaining_seconds")}
    )
