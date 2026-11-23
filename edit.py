"""Apply the edits admins requested in the dashboard, then store the re-rendered posters.

    python edit.py

Run by the "Poster edits" GitHub Action, which the Worker starts as soon as an admin sends changes.
No AI is involved: the admin's text and photo are used exactly as sent. For every poster the Worker
has in state "editing", this:
  1. takes the stored values the poster was rendered from and applies the admin's changes,
  2. uses the old photo, the admin's new photo, or no photo, as requested,
  3. re-renders it and stores it as a new revision (the dashboard shows it as pending again).
If anything goes wrong the poster keeps its previous version and the dashboard shows the error.
"""
import tempfile
from pathlib import Path

from yaml import safe_load

import store
from build import ROOT, save_photo, validate
from render import NEEDS_PHOTO, NO_PHOTO_ONLY, TEMPLATES, render
from select_template import choose
from story import normalize_story
from tpf import tpf_render

TEXT_FIELDS = {"kicker": 60, "headline": 140, "highlight": 80, "summary": 400, "why": 250,
               "source": 120, "credit": 160}


def apply_changes(data, changes):
    designs = tpf_render.DESIGNS if data.get("brand") == "tpf" else TEMPLATES
    for key, limit in TEXT_FIELDS.items():
        if key in changes and changes[key] is not None:
            data[key] = str(changes[key]).strip()[:limit]
    if isinstance(changes.get("syllabus"), list):
        data["syllabus"] = [str(s).strip() for s in changes["syllabus"] if str(s).strip()][:3]
    if "key_points" in changes or "body" in changes or "category" in changes or "breaking" in changes:
        for k in ("category", "breaking", "key_points", "body"):
            if k in changes:
                data[k] = changes[k]
        normalize_story(data)
    if changes.get("template") in designs or changes.get("template") == "auto":
        data["template"] = changes["template"]
    if not data.get("headline"):
        raise ValueError("the headline cannot be empty")


def process(poster, topics, tmp: Path):
    pid, data, edit = poster["id"], dict(poster["data"]), poster["edit"] or {}
    apply_changes(data, edit.get("changes") or {})

    mode = edit.get("photo", "keep")
    photo = None
    if mode == "replace":
        raw = store.get_file(pid, "edit_photo", tmp / f"{pid}-upload")
        if not raw:
            raise ValueError("the new photo was not found")
        photo = tmp / f"{pid}-photo.jpg"
        save_photo(raw, photo)
        data["image_source"] = ""
        if "credit" not in (edit.get("changes") or {}):
            data["credit"] = ""
    elif mode == "keep":
        photo = store.get_file(pid, "photo", tmp / f"{pid}-photo.jpg")
    else:  # remove
        data["credit"], data["image_source"] = "", ""

    tpf = data.get("brand") == "tpf"   # The Prime Feed poster: its own designs and renderer
    if tpf:
        if data.get("template") == "auto":
            data["template"] = tpf_render.choose(data, bool(photo))
    else:
        if data.get("template") == "auto":
            data["template"] = choose(data, bool(photo))   # the admin asked for the best fit
        if not photo and data.get("template") in NEEDS_PHOTO:
            data["template"] = choose(data, False)  # the design needs a photo: take the best fit without one
        if photo and data.get("template") in NO_PHOTO_ONLY and "template" not in (edit.get("changes") or {}):
            data["template"] = choose(data, True)   # a new photo should be visible unless the admin chose otherwise

    validate(data, topics)                 # same checks as the daily run (highlight must be in headline, ...)
    data["image"] = str(photo) if photo else None
    out = tmp / pid
    result = (tpf_render.render([data], out, out) if tpf else render([data], out, out))[0]
    data.pop("image", None)
    data["template"] = result["template"]
    data["file"] = Path(result["file"]).name
    data["regions"] = result.get("regions", {})
    data["photo_file"] = f"{pid}-photo.jpg" if photo else None
    data["edited_by"], data["edited_at"] = edit.get("by"), edit.get("at")

    store.put_render(pid, data, result["file"],
                     photo_path=photo if mode == "replace" else None,
                     keep_photo=(mode == "keep" and bool(photo)))
    print(f"  {pid}: re-rendered as {data['template']} ({mode} photo)")


def main():
    topics = safe_load(open(ROOT / "config" / "sources.yaml", encoding="utf-8"))["topics"]
    todo = store.edits()
    print(f"{len(todo)} poster(s) waiting for edits")
    failed = 0
    with tempfile.TemporaryDirectory() as d:
        for poster in todo:
            try:
                process(poster, topics, Path(d))
            except Exception as e:  # noqa: BLE001 - keep the previous version, report why
                failed += 1
                print(f"  {poster['id']}: FAILED {e}")
                store.edit_failed(poster["id"], f"Could not apply the edit: {e}")
    if failed:
        raise SystemExit(f"{failed} edit(s) failed; the dashboard shows the reason")


if __name__ == "__main__":
    main()
