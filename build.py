"""Pak Imtehan daily news posters — the non-AI steps. Claude does the editorial work in between.

    python build.py fetch     # RSS from the 27 channels -> work/headlines.txt   (Claude reads this)
    #   Claude writes work/posts.json  (see ROUTINE.md for the format)
    python build.py photos    # free CC photos -> work/photos/, contact sheets for Claude to look at
    #   Claude sets "photo": "a" | "b" | "c" | null on each post
    python build.py render    # random templates -> posters/<date>/*.jpg + posters.json + README.md
"""
import datetime as dt
import json
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw

import factcheck
from fetch_news import fetch_all
from images import candidates
from render import render
from select_template import RECENT_KEEP, choose
from story import normalize_story

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "work"
POSTS = WORK / "posts.json"
SEEN = ROOT / "state" / "seen.json"
RECENT = ROOT / "state" / "recent_templates.json"
PKT = dt.timezone(dt.timedelta(hours=5))


def today():
    return dt.datetime.now(PKT)


def cmd_fetch():
    WORK.mkdir(exist_ok=True)
    items, cfg = fetch_all()
    seen = set(json.loads(SEEN.read_text())) if SEEN.exists() else set()
    items = [it for it in items if it["link"] not in seen]
    recent = factcheck.load_recent()  # stories that already have a poster: a new outlet's version is not a new story
    fresh = [it for it in items if not factcheck.repeat_of(it["title"], recent)]
    print(f"  {len(items) - len(fresh)} headline(s) repeat a story that already has a poster")
    items = fresh
    (WORK / "headlines.json").write_text(json.dumps(items, ensure_ascii=False, indent=1))
    lines = [f"{i} | {it['source']} | {it['title']} | {it['summary'][:220]}" for i, it in enumerate(items)]
    cap = cfg["settings"].get("posters_per_run", cfg["settings"].get("posters_per_day", 15))
    want = min(cap, max(3, len(items) // 3))  # about one poster per three unseen headlines (outlets repeat stories)
    (WORK / "headlines.txt").write_text(
        f"TOPICS: {', '.join(cfg['topics'])}\nPOSTERS WANTED: {want}\n\n"
        "id | source | headline | teaser\n" + "\n".join(lines) + "\n")
    print(f"{len(items)} new headlines -> work/headlines.txt")


def cmd_photos():
    posts = json.loads(POSTS.read_text())
    pdir = WORK / "photos"
    pdir.mkdir(parents=True, exist_ok=True)
    for i, p in enumerate(posts, 1):
        cands = candidates(p.get("image_query", ""), pdir, f"p{i}") if p.get("image_query") else []
        p["candidates"] = cands
        if cands:  # one labelled contact sheet per post, so Claude can judge all options in one look
            thumbs = [Image.open(c["image"]).convert("RGB") for c in cands]
            for t in thumbs:
                t.thumbnail((600, 400))
            sheet = Image.new("RGB", (610 * len(thumbs), 450), "white")
            d = ImageDraw.Draw(sheet)
            for j, t in enumerate(thumbs):
                sheet.paste(t, (610 * j, 40))
                d.text((610 * j + 8, 8), f"{chr(97 + j)}: {cands[j]['title'][:60]}", fill="black")
            sheet.save(pdir / f"sheet-{i}.jpg", quality=80)
        print(f"post {i}: {len(cands)} photo candidates for {p.get('image_query')!r}"
              + (f" -> work/photos/sheet-{i}.jpg" if cands else ""))
    POSTS.write_text(json.dumps(posts, ensure_ascii=False, indent=1))


def save_photo(src, dest, max_width=1600):
    """Store a photo as a JPEG of at most 1600 px wide (keeps each stored file well under 1 MB)."""
    with Image.open(src) as im:
        im = im.convert("RGB")
        if im.width > max_width:
            im = im.resize((max_width, round(im.height * max_width / im.width)), Image.LANCZOS)
        im.save(dest, "JPEG", quality=85, optimize=True, progressive=True)


def validate(p, topics):
    p["headline"] = p["headline"].strip()
    if p.get("highlight") and p["highlight"] not in p["headline"]:
        p["highlight"] = ""
    p["syllabus"] = [s for s in p.get("syllabus", []) if s in topics][:3] or ["Current Affairs"]
    outlets = [s.strip() for s in p.get("source", "").split(",") if s.strip()]
    p["source"] = ", ".join(outlets[:2]) + (f" +{len(outlets) - 2}" if len(outlets) > 2 else "")
    for k, n in (("headline", 100), ("summary", 260), ("why", 150)):
        if len(p.get(k) or "") > n:
            print(f"  warning: {k} is {len(p[k])} chars (max {n}) — may be shrunk to fit")


def assign_templates(posts, rng, recent=()):
    """Pick each poster's design from what the story is (category, tone, photo, figure, quote), never at random.
    Within a batch no design repeats; across batches recently used designs are avoided (see select_template.py)."""
    used = []
    for p in posts:
        p["template"] = choose(p, bool(p.get("image")), in_batch=used, recent=recent, rng=rng)
        used.append(p["template"])
    return used


def cmd_render():
    from yaml import safe_load
    cfg = safe_load(open(ROOT / "config" / "sources.yaml", encoding="utf-8"))
    posts = json.loads(POSTS.read_text())
    heads = json.loads((WORK / "headlines.json").read_text())
    now = today()
    out = ROOT / "posters" / now.strftime("%Y-%m-%d")
    out.mkdir(parents=True, exist_ok=True)

    for p in posts:
        validate(p, cfg["topics"])
        choice, cands = p.get("photo"), p.pop("candidates", [])
        pick = next((c for j, c in enumerate(cands) if chr(97 + j) == choice), None)
        p.update({"image": pick["image"], "credit": pick["credit"], "image_source": pick["image_source"]} if pick
                 else {"image": None, "credit": ""})
        picked = [heads[i] for i in p.get("story_ids", []) if 0 <= i < len(heads)]
        p["links"] = [h["link"] for h in picked]
        normalize_story(p, sources=[{"label": h["source"][:60], "url": h["link"]} for h in picked
                                    if str(h["link"]).startswith(("http://", "https://"))])
        p["date"] = now.strftime("%-d %b %Y")
        p["edition"] = "Daily Current Affairs"

    recent = json.loads(RECENT.read_text()) if RECENT.exists() else []
    used = assign_templates(posts, random.Random(), recent)
    RECENT.parent.mkdir(exist_ok=True)
    RECENT.write_text(json.dumps((recent + used)[-RECENT_KEEP:]))
    # The Prime Feed (TPF): the same stories again in the TPF designs, stored as their own posters (brand "tpf").
    tpf_posts = [dict(p, brand="tpf", template=None) for p in posts]
    for p in posts:
        p["brand"] = "pakimtehan"
    for r, p in zip(render(posts, out, out), posts):
        p["file"] = Path(r["file"]).name
        p["regions"] = r.get("regions", {})
        if p.get("image"):  # keep the photo, so an admin's text edit can re-render the same poster
            p["photo_file"] = f"{Path(r['file']).stem}-photo.jpg"
            save_photo(p["image"], out / p["photo_file"])
        p.pop("image", None)

    from tpf.tpf_render import render as render_tpf
    names = [f"T{i:02d}" for i in range(1, len(tpf_posts) + 1)]
    for r, p in zip(render_tpf(tpf_posts, out, out, names=names), tpf_posts):
        p["file"] = Path(r["file"]).name
        p["template"] = r["template"]
        p["regions"] = r.get("regions", {})
        if p.get("image"):
            p["photo_file"] = f"{Path(r['file']).stem}-photo.jpg"
            save_photo(p["image"], out / p["photo_file"])
        p.pop("image", None)

    (out / "posters.json").write_text(json.dumps(posts + tpf_posts, ensure_ascii=False, indent=1))
    md = [f"# Pak Imtehan posters — {now.strftime('%A %-d %B %Y')}\n",
          "Review and approve these in the Pak Imtehan dashboard → News Posters.\n"]
    for p in posts:
        md += [f"## {p['headline']}", f"![{p['template']}]({p['file']})",
               f"*{p['summary']}*  ", f"Template: `{p['template']}` · Source: {p['source']}  ",
               "Links: " + " · ".join(f"[{j + 1}]({l})" for j, l in enumerate(p["links"])) + "\n"]
    (out / "README.md").write_text("\n".join(md))

    factcheck.remember(posts)
    seen = json.loads(SEEN.read_text()) if SEEN.exists() else []  # oldest first
    new = [l for p in posts for l in p["links"] if l not in set(seen)]
    SEEN.parent.mkdir(exist_ok=True)
    SEEN.write_text(json.dumps((seen + list(dict.fromkeys(new)))[-3000:], indent=0))
    print(f"done -> {out.relative_to(ROOT)}")


if __name__ == "__main__":
    {"fetch": cmd_fetch, "photos": cmd_photos, "render": cmd_render}[sys.argv[1]]()
