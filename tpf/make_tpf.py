"""The Prime Feed (TPF): render Instagram posters (1080x1350, 4:5) from the SAME stories the Pak Imtehan run wrote.

LOCAL ONLY: reads posters from the Cloudflare Worker (read-only) and never writes back, never uploads, never posts.

    python tpf/make_tpf.py                    # every poster currently in the Worker -> tpf/out/<date>/
    python tpf/make_tpf.py --limit 6          # only the first 6
    python tpf/make_tpf.py --json file.json   # render posters from a local JSON list instead (images = local paths)

Reuses the story text, photo, figure and quote already checked by the fact-check gate. It does not call any AI.
Designs (tpf/templates): breaking, cover, type, light, quote, stat. Black, white and silver only, from the TPF logo.
"""
import argparse
import base64
import io
import json
import os
import sys
import urllib.request
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from jinja2 import Environment, FileSystemLoader, select_autoescape  # noqa: E402
from PIL import Image  # noqa: E402
from render import JPEG_OPTS, headline_html, launch_chromium, to_uri  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

DESIGNS = ["tpf-breaking", "tpf-cover", "tpf-type", "tpf-light", "tpf-quote", "tpf-stat"]
env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html"]))
env.filters["head"] = lambda items, n: list(items or [])[:n]


def choose(p, has_photo, used):
    """The design for a story: a quote or a figure gets its own design, else rotate so the feed never repeats a look."""
    if (p.get("quote") or "").strip():
        return "tpf-quote"
    if (p.get("stat") or {}).get("value"):
        return "tpf-stat"
    if has_photo:
        pool = ["tpf-breaking", "tpf-cover", "tpf-light"]
        if p.get("breaking"):
            return "tpf-breaking"
    else:
        pool = ["tpf-type", "tpf-light"]
    return min(pool, key=lambda t: (used[t], pool.index(t)))


def load_store():
    cfg = {}
    for line in (Path.home() / ".config/pakimtehan-posters/store.env").read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg["NEWS_POSTERS_STORE_URL"].rstrip("/"), cfg["NEWS_POSTERS_STORE_TOKEN"]


def get(url, token):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": "tpf-local-render"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def from_worker(work: Path, limit):
    url, token = load_store()
    posters = get(f"{url}/posters", token)["posters"]
    posters.sort(key=lambda x: x["id"])
    out = []
    for item in posters[:limit]:
        p = dict(item["data"])
        p["id"] = item["id"]
        try:
            b64 = get(f"{url}/posters/{item['id']}/file/photo", token).get("b64")
            if b64:
                img = work / f"{item['id']}.jpg"
                img.write_bytes(base64.b64decode(b64))
                p["image"] = str(img)
        except Exception:
            p["image"] = None
        out.append(p)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--json", help="render a local JSON list instead of reading the Worker")
    args = ap.parse_args()

    work = HERE / "work"
    work.mkdir(exist_ok=True)
    posters = json.loads(Path(args.json).read_text(encoding="utf-8"))[: args.limit] if args.json else from_worker(work, args.limit)
    if args.json:  # photo paths in a JSON file are relative to that file
        for p in posters:
            if p.get("image"):
                p["image"] = str((Path(args.json).resolve().parent / p["image"]).resolve())
    if not posters:
        sys.exit("no posters to render")
    date = Path(args.json).stem if args.json else (posters[0].get("id") or "")[:10]
    out_dir = HERE / "out" / date
    out_dir.mkdir(parents=True, exist_ok=True)

    used = Counter()
    with sync_playwright() as pw:
        browser = launch_chromium(pw)
        page = browser.new_page(viewport={"width": 1080, "height": 1350}, device_scale_factor=1)
        for i, p in enumerate(posters, 1):
            has_photo = bool(p.get("image")) and Path(p["image"]).exists()
            t = p["template"] if p.get("template") in DESIGNS else choose(p, has_photo, used)
            used[t] += 1
            data = dict(p, headline_html=headline_html(p), image=to_uri(p["image"], work) if has_photo else None)
            html = env.get_template(f"{t}.html").render(p=data, root=ROOT.as_uri())
            hp = out_dir / f"{i:02d}-{t}.html"
            hp.write_text(html, encoding="utf-8")
            page.goto(hp.as_uri(), wait_until="networkidle")
            page.wait_for_selector("body[data-ready='1']", timeout=15000)
            img = out_dir / f"{i:02d}-{t}.jpg"
            Image.open(io.BytesIO(page.screenshot(type="png"))).convert("RGB").save(img, "JPEG", **JPEG_OPTS)
            hp.unlink()
            print(f"{img.name}  <- {p.get('headline', '')[:70]}")
        browser.close()
    print(f"\n{len(posters)} posters in {out_dir}")


if __name__ == "__main__":
    main()
