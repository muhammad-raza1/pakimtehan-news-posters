"""Render The Prime Feed (TPF) posters (1080x1350) from stories the Pak Imtehan run already wrote and fact-checked.

Same input and same return shape as render.render(), so build.py, edit.py and make_tpf.py share one renderer.
Black, white and silver only (from the TPF logo). No AI is called here.
"""
import io
from collections import Counter
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image
from playwright.sync_api import sync_playwright

from render import JPEG_OPTS, headline_html, launch_chromium, to_uri

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DESIGNS = ["tpf-breaking", "tpf-cover", "tpf-type", "tpf-light", "tpf-quote", "tpf-stat"]
NEEDS_PHOTO = {"tpf-breaking", "tpf-cover"}
env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html"]))
env.filters["head"] = lambda items, n: list(items or [])[:n]

REGIONS_JS = """() => {
  const out = {};
  document.querySelectorAll('[data-field]').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return;
    const k = el.dataset.field, o = out[k];
    out[k] = o ? [Math.min(o[0], r.left), Math.min(o[1], r.top), Math.max(o[2], r.right), Math.max(o[3], r.bottom)] : [r.left, r.top, r.right, r.bottom];
  });
  return Object.fromEntries(Object.entries(out).map(([k, v]) => [k, [Math.round(Math.max(0, v[0])), Math.round(Math.max(0, v[1])), Math.round(Math.min(1080, v[2]) - Math.max(0, v[0])), Math.round(Math.min(1350, v[3]) - Math.max(0, v[1]))]]));
}"""


def choose(p, has_photo, used=None):
    """A quote or a figure gets its own design, else rotate so the feed never repeats a look."""
    used = used if used is not None else Counter()
    if (p.get("quote") or "").strip():
        return "tpf-quote"
    if (p.get("stat") or {}).get("value"):
        return "tpf-stat"
    if has_photo:
        if p.get("breaking"):
            return "tpf-breaking"
        pool = ["tpf-breaking", "tpf-cover", "tpf-light"]
    else:
        pool = ["tpf-type", "tpf-light"]
    return min(pool, key=lambda t: (used[t], pool.index(t)))


def pick_template(p, used=None):
    t = p.get("template")
    has_photo = bool(p.get("image"))
    if t not in DESIGNS or (t in NEEDS_PHOTO and not has_photo):
        return choose(p, has_photo, used)
    return t


def render(posters, out_dir: Path, base: Path, names=None):
    """Render each poster to out_dir/<name>-<design>.jpg. `names` gives each file's prefix (default 01, 02, ...)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    used, results = Counter(), []
    with sync_playwright() as pw:
        browser = launch_chromium(pw)
        page = browser.new_page(viewport={"width": 1080, "height": 1350}, device_scale_factor=1)
        for i, p in enumerate(posters):
            t = pick_template(p, used)
            used[t] += 1
            name = names[i] if names else f"{i + 1:02d}"
            data = dict(p, headline_html=headline_html(p), image=to_uri(p.get("image"), base))
            hp = out_dir / f"{name}-{t}.html"
            hp.write_text(env.get_template(f"{t}.html").render(p=data, root=ROOT.as_uri()), encoding="utf-8")
            page.goto(hp.as_uri(), wait_until="networkidle")
            page.wait_for_selector("body[data-ready='1']", timeout=15000)
            regions = page.evaluate(REGIONS_JS)
            img = out_dir / f"{name}-{t}.jpg"
            Image.open(io.BytesIO(page.screenshot(type="png"))).convert("RGB").save(img, "JPEG", **JPEG_OPTS)
            hp.unlink()
            results.append({"file": str(img), "template": t, "regions": regions, **{k: p.get(k) for k in ("headline", "source")}})
            print(f"rendered {img.name}")
        browser.close()
    return results
