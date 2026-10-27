"""制片的 film 阶段：镜头视频交给 FreeVideo 多镜头电影拍（角色卡做一致性、上一镜续拍做连贯）。

流程（审核仍在我们这边）：
1. 角色卡（tool: freevideo_cards）：每个人物一张 FreeVideo 角色卡——优先用设置里指定的卡，
   其次按名字复用卡库里已有的卡，否则用已通过的定妆图（和你提供的参考图）新建。
2. 开拍：分镜 → 剧本（角色指向卡，台词带说话人，continue 表示接上一镜）→ FreeVideo plan 校验 → start。
   每个镜头一个产物（status running，meta 里记 film / shot）。
3. 轮询：某镜头的 take 都拍完 → 下载到任务目录，导入为一轮待审阅的候选（模板 freevideo-take）。
4. 你在审阅里选中 → FreeVideo choose（之后的续拍镜头从这条的末帧接着拍）。
   重做 / 带意见打回 → FreeVideo reshoot（意见原样交给它）；不带意见的打回同样 reshoot。
5. 暂停时不推进；“立即停止”会取消还没拍完的 take（拍好的保留）。
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from .freevideo import FreeVideoError

if TYPE_CHECKING:  # pragma: no cover
    from .production import Production, Stage

TERMINAL_JOB = ("succeeded", "failed", "cancelled", "interrupted")
TAKE_TEMPLATE = "freevideo-take"


class FilmDriver:
    def __init__(self, engine: Production, client):
        self.engine, self.fv = engine, client
        self.store = engine.store
        self.notify_round = lambda rnd: None  # daemon 注入：导入的一轮推送到控制台 / Telegram

    # ---- 角色卡 ----
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
        sheets = inputs.get("characters") or {}  # {key: 已通过的定妆产物 data}
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
                continue  # 没有已通过的定妆图：这个人物在镜头里只靠文字（计划里会显示 cast_missing）
            card = self.fv.create_card(
                c.get("name") or key, c.get("look") or c.get("name") or key, pics[:6]
            )
            out["characters"][key] = card["id"]
            out["created"].append(key)
        return out

    # ---- 剧本 ----
    def screenplay(self, prod: dict, st: Stage, ctx: dict, keys: list[str] | None = None) -> dict:
        settings = prod["settings"]
        lang = (settings.get("language") or "English").strip() or "English"
        cast_map = (ctx.get(st.cast) or {}).get("characters", {}) if st.cast else {}
        bible = ctx.get("bible") or {}
        looks = {c["id"]: c for c in bible.get("characters", [])}
        chars = {}
        for key, card in cast_map.items():
            entry = {"card": card, "language": lang}
            if looks.get(key, {}).get("voice"):
                entry["voice"] = looks[key]["voice"]
            chars[key] = entry
        shots = []
        for s in self.engine._items(st, ctx):
            key, item = s
            if keys is not None and key not in keys:
                continue
            sh = {
                "id": key,
                "seconds": item.get("seconds") or 5,
                "action": ". ".join(x for x in (item.get("frame"), item.get("motion")) if x),
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
            shots.append(sh)
        return {
            "title": prod["title"],
            "style": (bible.get("style") or {}).get("look", ""),
            "aspect": settings.get("aspect", "16:9"),
            "quality": settings.get("film_quality", "medium"),
            "characters": chars,
            "shots": shots,
        }

    # ---- 开拍 / 重拍 ----
    def current_film(self, prod_id: str, stage_id: str) -> str | None:
        films = [
            a["meta"].get("film")
            for a in self.store.artifacts(prod_id, stage_id)
            if (a["meta"] or {}).get("film")
        ]
        return films[-1] if films else None

    def start(
        self, prod: dict, st: Stage, idx, ctx: dict, keys: list[str], note: str | None, deps_for
    ) -> list[dict]:
        film_id = self.current_film(prod["id"], st.id)
        made = []
        if film_id:
            try:
                for key in keys:
                    made.append(self._reshoot(prod, st, film_id, key, note, deps_for(key)))
                return made
            except FreeVideoError as e:  # 电影已结束或已取消：为这些镜头开一部新的
                self.engine.log(f"film {film_id}: reshoot failed ({e}); starting a new film")
                made = []
        sp = self.screenplay(prod, st, ctx, keys if film_id else None)
        takes = int(prod["settings"].get("video_candidates") or 1)
        ref = f"aco-{prod['id']}-{st.id}-{int(time.time())}"
        plan = self.fv.plan_film(sp, takes=takes)  # 先校验（不提交）：写错的地方在这里就报出来
        film = self.fv.start_film(sp, takes=takes, ref=ref)
        self.engine.log(
            f"film {film['id']} started for {prod['id']}: {len(sp['shots'])} shot(s) × {takes} take(s)"
        )
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
                    meta={
                        "film": film["id"],
                        "shot": sh["id"],
                        "imported": [],
                        "prompt": preview.get("prompt"),
                        "cast_reference": preview.get("cast_reference"),
                        "cast_missing": preview.get("cast_missing"),
                    },
                )
            )
        return made

    def _reshoot(self, prod, st, film_id, key, note, deps) -> dict:
        takes = int(prod["settings"].get("video_candidates") or 1)
        self.fv.reshoot(film_id, key, note=note, takes=takes)
        imported = sorted({
            job
            for a in self.store.artifacts(prod["id"], st.id)
            if a["key"] == key and (a["meta"] or {}).get("film") == film_id
            for job in (a["meta"] or {}).get("imported", [])
        })  # 之前导入过的 take 不再导入
        return self.store.add_artifact(
            prod["id"], st.id, key, "running", deps=deps, note=note,
            meta={"film": film_id, "shot": key, "imported": imported},
        )

    def intercept_round(self, rnd: dict) -> bool:
        """在审阅里对一轮 take 点重做 / 带意见打回 / 微调时，Studio 会照常派生一轮新的 freevideo-take
        草稿：这里把它换成 FreeVideo reshoot（意见原样交给 FreeVideo），草稿作废。返回是否接管。"""
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

    # ---- 轮询：拍完的 take 导入为待审阅的一轮 ----
    def tick(self) -> int:
        imported = 0
        for prod in self.store.productions():
            if prod["state"] != "active":
                continue
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
                    continue
                for shot in film.get("shots") or []:
                    art = arts.get(shot["id"])
                    if art is not None:
                        imported += self._import(prod, art, film, shot)
        return imported

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
        for t in takes:
            job = self.fv.call("GET", f"/jobs/{t['job']}").get("job") or {}
            if job.get("status") not in TERMINAL_JOB:
                return 0  # 这一镜还有 take 在拍：等齐了一起审
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
                except Exception as e:  # noqa: BLE001 — 下载失败：这条标失败，别的照常
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
