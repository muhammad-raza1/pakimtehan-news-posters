# Where we are (keep under ~80 lines; replace stale lines, don't append history)

Last updated: 2026-11-26 (Worker keeps unapproved posters 10 h only; all old posters wiped; deployed, hourly cron live)

## How posters are made now
- GitHub Action `.github/workflows/free-ai-posters.yml` runs the pipeline with free AI
  (`ai_editor.py`: Gemini 3.6 Flash -> 3.5 Flash-Lite -> NVIDIA NIM -> OpenRouter -> Cloudflare).
- 15 posters per run (`config/sources.yaml` -> `posters_per_run`), JPEG q90 4:4:4 (~235 KB each).
- Output: stored in the Cloudflare Worker `pakimtehan-posters` (D1 database `pakimtehan-posters`)
  with all values + the photo, id `<date>_<HHMM batch>-<nn>`. GitHub no longer stores posters.

## Worker (`cloudflare/worker/`, https://pakimtehan-posters.<subdomain>.workers.dev)
- **PAUSED (13 Nov): Worker cron removed (`crons = []`) until the client meeting.** Crons take
  up to 15 min to propagate: set test times 20+ min ahead. GitHub's own cron was removed (never fired).
- Storage API (Bearer STORE_TOKEN): list/get/file/edit/render/delete posters, state (seen links, last_run).
- Edits: dashboard -> server -> Worker `POST /posters/<id>/edit` -> starts `poster-edit.yml` ->
  `edit.py` re-renders with the admin's exact text/photo -> rev+1 -> server sync shows it pending again.
- Secrets: GITHUB_TOKEN (dispatch), STORE_TOKEN. Same STORE_TOKEN in GitHub secret
  POSTERS_STORE_TOKEN and in `~/.config/pakimtehan-posters/store.env` (local server, mode 600).
- Old worker `pakimtehan-posters-trigger` deleted.

## Live News + local test setup (17 Nov, not committed/deployed yet)
- Each poster is also a Live News story: AI writes `category`, `breaking`, `key_points`, `body`; `sources` come from
  the real article links (`story.py`). Two new no-photo designs: `typographic` (blue) and `impact` (light). blue/navy/white palette only.
- Server (`pi_user_server`) has NO poster table any more: pending = Worker/D1 (read live); approve writes
  `<News Posters dir>/<date>/<id>.jpg + .json` and deletes it from D1; reject deletes from D1; take down moves
  to `_taken-down/`. Public API `/api/v3/live-news[/:slug[/related]]`; admin batch `POST /admin/news-posters/batch`.
  Worker has new `POST /posters/:id/story` (story-only edits, no re-render) and `DEV_SKIP_DISPATCH`.
- Dashboard: compact table, tabbed edit form. Save only SAVES (Worker `edit` with apply:false); "Update poster image" (`/apply`) starts the re-render; approve is blocked while changes are unrendered.
- Website branch `agent/2026-11-17-live-news`: `src/lib/liveNews.js` now calls the API (sample = VITE_LIVE_NEWS_SAMPLE=true).
- SEO: `pi_user_server/src/services/newsSeo.service.js` writes static story/day/front pages + sitemaps into the website web root
  (`NEWS_SEO_WEBROOT`) on every approve/edit/take-down; website serves them via `_pages/` rules. Stories open as full pages (popup removed).
- POSTER LIBRARY (17 Nov, client brief: many designs, chosen by the news, not random): `templates/catalog.yaml` lists 34 designs
  (layout + colour theme, grouped in families: breaking, politics, economy, world, security, science-tech, climate, education,
  society, general). The AI tags each story `tone`, `stat_value/stat_label` (a figure) and `quote/speaker` (both verified against the
  source text); `select_template.py` scores every design (category, tone, photo, figure, quote, headline length, recent use) and
  picks among the best fits. Designs used recently are remembered in the Worker state `recent_templates`; the catalog is published to
  state `catalog` for the dashboard editor ("Best fit (automatic)" or any design). PALETTE (client rule, 17 Nov): ONLY the website's
  blues, navy and white; no yellow, red, black, green, violet, orange or cream. `tests/` enforces it on every colour in templates/.
  Review every design: `python render_library.py` (contact sheets in out/library/). Tests: `python -m unittest discover -s tests`.
- LOCAL: Worker `wrangler dev --local --port 8787 --compatibility-date 2026-09-28` (secrets in .dev.vars),
  `.venv-local/bin/python seed_local.py` seeds posters, `python edit.py` (env from ~/.config/pakimtehan-posters/local.env)
  stands in for the GitHub edit Action. Server .env has local Worker URL; website/dashboard `.env.local` point at :4000.
- BEFORE PRODUCTION: `wrangler deploy` (new endpoint), set server env STORE_URL/TOKEN/STORAGE_DIR, delete the
  website/dashboard `.env.local`, nothing to run in MySQL (the old `news_posters` table is unused; drop it by hand if it exists).

## Fact-check gate + repeat memory + live editor (17 Nov, client rules; NOT deployed yet)
- `ai_editor.py verify` runs after `write` and BEFORE photos/render: `factcheck.py` reads each story's articles (headline+teaser if
  unreadable), checks every number deterministically, then a second model (different provider than the writer where possible) checks
  every claim; anything unsupported or not checkable is DROPPED (fails closed). Passing posters carry `fact_check` in their data.
  Workflow step "Fact-check posters" gates the rest. Tests: `tests/test_factcheck.py`.
- Repeat stories: `state/recent_stories` (Worker state, 14 days, headline-match `factcheck.same_story`) drops repeats at fetch, in the
  editor prompt ("ALREADY COVERED"), in validate, and in the verifier. Rejected stories also count (everything rendered is remembered).
- Live editor: dashboard `PosterEditModal` draws the poster from the SAME templates with nunjucks (`public/poster-kit/`, generated by
  `python scripts/export_poster_kit.py` — rerun after any template change; templates use `|head(n)` not `[:n]`, parentheses in chained
  inline ifs). Click text -> focuses its field, click photo -> file chooser; story tab shows a live Live News page. Server has new
  `GET /admin/news-posters/:id/photo` and `render`/`factCheck` in `/source`. Dashboard + server changes are uncommitted in their own repos.

- Z.ai (GLM) free backup, added 17 Nov: `zai:glm-4.7-flash` (text) after the two Gemini models, `zai:glm-4.6v-flash` (photos). Only the 3 free
  models are allowed (code refuses others). Secret ZAI_API_KEY (must be set in GitHub). Thinking is OFF for writing (else it returns nothing)
  and ON for fact-check. 429/1305 = "overloaded" (random, retried), NOT quota. No official monthly cap is published; check z.ai/manage-apikey/rate-limits.

- Groq free backup (17 Nov): text `groq:openai/gpt-oss-120b`, vision `groq:qwen/qwen3.8-27b`, secret GROQ_API_KEY (must be set in GitHub). Limits seen in
  headers: 1,000 requests/day, but only ~8,000 tokens/MIN (7,000 input for vision), so the writer sends Groq a short list (4 posters), the fact-check one poster per
  request, and photo calls are paced 13 s apart. Used as a fallback, not the main writer.

## Branches
- `main` only is used now. `daily-posters-test` (old test output) is no longer written; can be deleted.
  Tag `archive/daily-posters` keeps the 12 Nov routine output.

## Open items
- Replace any classic GitHub token with a fine-grained one: this repo only,
  Actions read/write. `cd cloudflare/worker && wrangler secret put GITHUB_TOKEN`, then revoke the old one.
- PAUSED again 17 evening: Worker redeployed with `crons = []` (testing done). Tomorrow: set `crons = ["7 * * * *"]`, `wrangler deploy`, add secrets ZAI_API_KEY + GROQ_API_KEY, rotate the keys pasted in chat.
- Schedule DECIDED (17 Nov): every HOUR (`7 * * * *`), set in wrangler.toml but NOT deployed. Production also needs GitHub variable POSTERS_TEST_MODE=false.
  Per run: up to 15 posters (about one per 3 unseen headlines), stops early under 9 unseen headlines; D1 keeps unreviewed posters 7 days. Repo is public (free Actions minutes).
- Photo-library keys (PEXELS/UNSPLASH/PIXABAY) are not set as repo secrets.
- `docs/Pak-Imtehan-Poster-Automation.pdf` is untracked (not committed).

## Budget notes
- Daily run: ~7 min Actions; edit run: ~2 min (Chromium install). 2,000 free min/month (private repo).
- Gemini per run: ~18 requests, ~31k input tokens.
- D1 free: 5 GB, 100k writes/day. Each poster ~0.6 MB (image + photo as base64); rows deleted on
  approve/reject, so storage stays small.
