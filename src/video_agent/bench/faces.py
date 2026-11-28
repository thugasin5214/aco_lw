"""Identity consistency with insightface (ArcFace embeddings, `buffalo_l`, CPU).

For each delivered episode: sample frames, take the largest face in each, and compare it with the
character's reference (the case's reference image, else the project's first casting image). Cosine
similarity of ArcFace embeddings: the same person is usually ≥ ~0.45, different people ≤ ~0.3
(a rule of thumb, not calibrated on this setup). Also the spread between frames (does the face stay
one person across shots).

Needs `uv sync --extra bench` (insightface, onnxruntime, opencv); the model (~280 MB) is downloaded
on first use into ~/.insightface.
"""

from __future__ import annotations

from pathlib import Path

SAME = 0.45  # rule of thumb for "same person" with buffalo_l
_app = None


def _analyzer():
    global _app
    if _app is None:
        from insightface.app import FaceAnalysis

        _app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
        _app.prepare(ctx_id=-1, det_size=(640, 640))
    return _app


def embedding(image: Path):
    import cv2
    import numpy as np

    img = cv2.imread(str(image))
    if img is None:
        return None
    faces = _analyzer().get(img)
    if not faces:
        return None
    f = max(faces, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))
    v = f.normed_embedding
    return np.asarray(v, dtype="float32")


def face_quality(image: Path) -> dict | None:
    """Objective look of the main face: detector confidence, face height in pixels, sharpness of
    the face crop (variance of the Laplacian; higher is sharper)."""
    import cv2

    img = cv2.imread(str(image))
    if img is None:
        return None
    faces = _analyzer().get(img)
    if not faces:
        return None
    f = max(faces, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))
    x0, y0, x1, y1 = (max(0, int(v)) for v in f.bbox)
    crop = cv2.cvtColor(img[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    sharp = float(cv2.Laplacian(crop, cv2.CV_64F).var()) if crop.size else 0.0
    return {"det": round(float(f.det_score), 3), "face_px": y1 - y0, "sharpness": round(sharp, 1)}


def score(reference: Path | None, frames: list[Path]) -> dict:
    import numpy as np

    embs = [(f, embedding(f)) for f in frames]
    found = [(f, e) for f, e in embs if e is not None]
    out: dict = {"frames": len(frames), "with_face": len(found)}
    if not found:
        return out
    q = [x for x in (face_quality(f) for f, _ in found) if x]
    if q:
        out["face_quality"] = {
            k: round(float(np.median([x[k] for x in q])), 3)
            for k in ("det", "face_px", "sharpness")
        }
    if reference is not None:
        ref = embedding(reference)
        if ref is None:
            out["reference_error"] = "no face in the reference image"
        else:
            sims = [float(np.dot(ref, e)) for _, e in found]
            out["to_reference"] = {
                "mean": round(float(np.mean(sims)), 3),
                "min": round(float(np.min(sims)), 3),
                "share_same": round(sum(s >= SAME for s in sims) / len(sims), 3),
            }
    if len(found) > 1:
        m = np.stack([e for _, e in found])
        sim = m @ m.T
        iu = np.triu_indices(len(found), 1)
        out["between_frames"] = {
            "mean": round(float(sim[iu].mean()), 3),
            "min": round(float(sim[iu].min()), 3),
        }
    return out
