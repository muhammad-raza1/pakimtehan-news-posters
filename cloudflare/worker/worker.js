// pakimtehan-posters: the posters' home until an admin decides on them.
//
//  * Cron: starts the "Daily posters (free AI)" GitHub Action (GitHub's own scheduler never fired).
//  * Storage API (D1): the pipeline uploads each day's posters here with every value used to render
//    them. The Pak Imtehan server lists them for the dashboard, copies a poster to its own disk when
//    an admin approves it, and deletes it here on approve or reject.
//  * Edits: the server sends the admin's changes; the Worker stores them and starts the
//    "Poster edits" GitHub Action, which re-renders and saves a new revision.
//
// Secrets: GITHUB_TOKEN (Actions: read/write on the repo), STORE_TOKEN (shared with the pipeline and
// the server; every API call must send "Authorization: Bearer <STORE_TOKEN>").
// Files travel as base64 strings so the Worker never converts bytes (keeps CPU time tiny).

const REPO = "muhammad-raza1/pakimtehan-news-posters";
const DAILY_WORKFLOW = "free-ai-posters.yml";
const EDIT_WORKFLOW = "poster-edit.yml";
const ID = /^\d{4}-\d{2}-\d{2}_[\w-]{1,40}$/;
const KINDS = new Set(["image", "photo", "edit_photo"]);
const MAX_B64 = 1_900_000; // D1 caps a row at 2 MB

async function dispatch(env, workflow) {
  // Local testing (wrangler dev): no GitHub call; run `python edit.py` by hand against the local Worker.
  if (env.DEV_SKIP_DISPATCH === "1") { console.log(`dispatch ${workflow} skipped (DEV_SKIP_DISPATCH)`); return; }
  const r = await fetch(`https://api.github.com/repos/${REPO}/actions/workflows/${workflow}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "pakimtehan-posters-worker",
    },
    body: JSON.stringify({ ref: "main" }),
  });
  const text = await r.text();
  console.log(`dispatch ${workflow} -> HTTP ${r.status} ${text.slice(0, 200)}`);
  if (r.status !== 204) throw new Error(`GitHub dispatch of ${workflow} failed: HTTP ${r.status}`);
}

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const fail = (status, message) => json({ ok: false, error: message }, status);
const now = () => new Date().toISOString();

function authorised(req, env) {
  const got = req.headers.get("authorization") || "";
  const want = `Bearer ${env.STORE_TOKEN || ""}`;
  if (!env.STORE_TOKEN || got.length !== want.length) return false;
  let diff = 0; // constant-time compare
  for (let i = 0; i < want.length; i++) diff |= got.charCodeAt(i) ^ want.charCodeAt(i);
  return diff === 0;
}

async function putFile(env, id, kind, b64) {
  if (typeof b64 !== "string" || !b64 || b64.length > MAX_B64) throw new Error(`${kind} missing or over 1.9 MB`);
  await env.DB.prepare("INSERT OR REPLACE INTO files (poster_id, kind, b64) VALUES (?, ?, ?)").bind(id, kind, b64).run();
}

// Safety net so D1 never fills up: posters nobody approved or rejected within RETAIN_DAYS are deleted
// together with their files, and files whose poster no longer exists are removed.
const RETAIN_DAYS = 7;
async function cleanup(env) {
  const cutoff = new Date(Date.now() - RETAIN_DAYS * 86400000).toISOString().slice(0, 10);
  const old = await env.DB.prepare("DELETE FROM files WHERE poster_id IN (SELECT id FROM posters WHERE date < ?)").bind(cutoff).run();
  const posters = await env.DB.prepare("DELETE FROM posters WHERE date < ?").bind(cutoff).run();
  const orphans = await env.DB.prepare("DELETE FROM files WHERE poster_id NOT IN (SELECT id FROM posters)").run();
  const n = (posters.meta?.changes || 0) + (orphans.meta?.changes || 0);
  if (n) console.log(`cleanup: removed ${posters.meta?.changes || 0} old poster(s), ${orphans.meta?.changes || 0} orphan file(s)`);
  return { oldPosters: posters.meta?.changes || 0, oldFiles: old.meta?.changes || 0, orphanFiles: orphans.meta?.changes || 0 };
}

async function startEdit(env, id) {
  await env.DB.prepare("UPDATE posters SET state = 'editing', error = NULL, updated_at = ? WHERE id = ?").bind(now(), id).run();
  try {
    await dispatch(env, EDIT_WORKFLOW);
  } catch (e) {
    await env.DB.prepare("UPDATE posters SET state = 'ready', error = ?, updated_at = ? WHERE id = ?")
      .bind(`Could not start the edit job: ${e.message}`, now(), id).run();
    return fail(502, e.message);
  }
  return json({ ok: true, state: "editing" });
}

const rowOut = (r) => ({
  id: r.id, date: r.date, rev: r.rev, state: r.state, error: r.error,
  data: JSON.parse(r.data), edit: r.edit ? JSON.parse(r.edit) : null,
  created_at: r.created_at, updated_at: r.updated_at,
});

async function route(req, env, ctx) {
  const url = new URL(req.url);
  const parts = url.pathname.split("/").filter(Boolean);
  const m = req.method;
  const body = async () => { try { return await req.json(); } catch { return {}; } };

  // GET /posters?since=YYYY-MM-DD   (list without files)
  if (m === "GET" && parts.length === 1 && parts[0] === "posters") {
    const since = url.searchParams.get("since") || "0000-00-00";
    const { results } = await env.DB.prepare(
      "SELECT * FROM posters WHERE date >= ? ORDER BY date DESC, id ASC").bind(since).all();
    return json({ ok: true, posters: results.map(rowOut) });
  }
  // GET /usage   how much is stored (to watch the D1 limit)
  if (m === "GET" && parts.length === 1 && parts[0] === "usage") {
    const p = await env.DB.prepare("SELECT COUNT(*) n FROM posters").first();
    const f = await env.DB.prepare("SELECT COUNT(*) n, COALESCE(SUM(LENGTH(b64)),0) bytes FROM files").first();
    return json({ ok: true, posters: p.n, files: f.n, megabytes: +(f.bytes / 1048576).toFixed(2), limit_mb: 5120 });
  }
  // POST /cleanup   run the retention cleanup now
  if (m === "POST" && parts.length === 1 && parts[0] === "cleanup") return json({ ok: true, ...(await cleanup(env)) });
  // GET /edits   (posters waiting to be re-rendered)
  if (m === "GET" && parts.length === 1 && parts[0] === "edits") {
    const { results } = await env.DB.prepare("SELECT * FROM posters WHERE state = 'editing' ORDER BY updated_at").all();
    return json({ ok: true, posters: results.map(rowOut) });
  }
  // GET/PUT /state/<key>
  if (parts[0] === "state" && parts.length === 2) {
    if (m === "GET") {
      const r = await env.DB.prepare("SELECT v FROM state WHERE k = ?").bind(parts[1]).first();
      return json({ ok: true, value: r ? JSON.parse(r.v) : null });
    }
    if (m === "PUT") {
      const b = await body();
      await env.DB.prepare("INSERT OR REPLACE INTO state (k, v) VALUES (?, ?)").bind(parts[1], JSON.stringify(b.value ?? null)).run();
      return json({ ok: true });
    }
  }

  if (parts[0] !== "posters" || !parts[1]) return fail(404, "not found");
  const id = parts[1];
  if (!ID.test(id)) return fail(400, "bad poster id");
  const action = parts[2] || "";

  // POST /posters/<id>   {date, data, image, photo?}   new poster from the daily run
  if (m === "POST" && !action) {
    const b = await body();
    if (!/^\d{4}-\d{2}-\d{2}$/.test(b.date || "") || typeof b.data !== "object") return fail(400, "date and data required");
    await putFile(env, id, "image", b.image);
    if (b.photo) await putFile(env, id, "photo", b.photo);
    const t = now();
    ctx.waitUntil(cleanup(env).catch((e) => console.log(`cleanup failed: ${e.message}`)));
    await env.DB.prepare(
      "INSERT OR REPLACE INTO posters (id, date, rev, state, data, edit, error, created_at, updated_at) VALUES (?, ?, 1, 'ready', ?, NULL, NULL, ?, ?)"
    ).bind(id, b.date, JSON.stringify(b.data), t, t).run();
    return json({ ok: true, id, rev: 1 });
  }

  const row = await env.DB.prepare("SELECT * FROM posters WHERE id = ?").bind(id).first();

  // DELETE /posters/<id>   (approved and copied by the server, or rejected)
  if (m === "DELETE" && !action) {
    await env.DB.batch([
      env.DB.prepare("DELETE FROM files WHERE poster_id = ?").bind(id),
      env.DB.prepare("DELETE FROM posters WHERE id = ?").bind(id),
    ]);
    return json({ ok: true, deleted: Boolean(row) });
  }
  if (!row) return fail(404, "poster not found");

  // GET /posters/<id>   (one poster with all its values)
  if (m === "GET" && !action) return json({ ok: true, poster: rowOut(row) });

  // GET /posters/<id>/file/<kind>   -> {b64}
  if (m === "GET" && action === "file" && KINDS.has(parts[3])) {
    const f = await env.DB.prepare("SELECT b64 FROM files WHERE poster_id = ? AND kind = ?").bind(id, parts[3]).first();
    return f ? json({ ok: true, b64: f.b64 }) : fail(404, "no such file");
  }

  // PUT /posters/<id>/edit-photo   {b64}   admin's replacement photo, before POST .../edit
  if (m === "PUT" && action === "edit-photo") {
    const b = await body();
    await putFile(env, id, "edit_photo", b.b64);
    return json({ ok: true });
  }

  // POST /posters/<id>/story   {fields:{category,breaking,key_points,body,sources,slug}}
  // Story-only changes: they do not appear on the poster image, so no re-render is needed.
  if (m === "POST" && action === "story") {
    if (row.state === "editing") return fail(409, "an edit is already being applied");
    const b = await body();
    if (typeof b.fields !== "object" || b.fields === null) return fail(400, "fields required");
    const data = JSON.parse(row.data);
    for (const k of ["category", "breaking", "key_points", "body", "sources"]) if (k in b.fields) data[k] = b.fields[k];
    await env.DB.prepare("UPDATE posters SET data = ?, updated_at = ? WHERE id = ?").bind(JSON.stringify(data), now(), id).run();
    return json({ ok: true });
  }

  // POST /posters/<id>/edit   {changes:{...}, photo:"keep"|"replace"|"remove", note?, apply?}
  // apply:false  = only SAVE the changes (merged with earlier saved ones); nothing is re-rendered yet.
  // otherwise    = save and start the re-render (same as POST .../apply).
  if (m === "POST" && action === "edit") {
    if (row.state === "editing") return fail(409, "an edit is already being applied");
    const b = await body();
    if (typeof b.changes !== "object" || !["keep", "replace", "remove"].includes(b.photo || "keep")) return fail(400, "bad edit");
    if (b.photo === "replace") {
      const f = await env.DB.prepare("SELECT 1 FROM files WHERE poster_id = ? AND kind = 'edit_photo'").bind(id).first();
      if (!f) return fail(400, "upload the new photo first");
    }
    const old = row.edit ? JSON.parse(row.edit) : null;
    const edit = {
      changes: { ...(old?.changes || {}), ...b.changes },
      photo: (b.photo && b.photo !== "keep") ? b.photo : (old?.photo || "keep"),
      note: (b.note || old?.note || "").slice(0, 500), by: (b.by || old?.by || "").slice(0, 120), at: now(),
    };
    await env.DB.prepare("UPDATE posters SET edit = ?, error = NULL, updated_at = ? WHERE id = ?")
      .bind(JSON.stringify(edit), now(), id).run();
    if (b.apply === false) return json({ ok: true, state: "ready", saved: true });
    return startEdit(env, id);
  }

  // POST /posters/<id>/apply   re-render the poster with the saved changes
  if (m === "POST" && action === "apply") {
    if (row.state === "editing") return fail(409, "an edit is already being applied");
    if (!row.edit) return fail(400, "no saved changes to apply");
    return startEdit(env, id);
  }

  // POST /posters/<id>/discard   drop the saved (not yet rendered) changes
  if (m === "POST" && action === "discard") {
    if (row.state === "editing") return fail(409, "an edit is already being applied");
    await env.DB.batch([
      env.DB.prepare("DELETE FROM files WHERE poster_id = ? AND kind = 'edit_photo'").bind(id),
      env.DB.prepare("UPDATE posters SET edit = NULL, error = NULL, updated_at = ? WHERE id = ?").bind(now(), id),
    ]);
    return json({ ok: true });
  }

  // PUT /posters/<id>/render   {data, image, photo?, keep_photo?}   result of an edit, from the Action
  if (m === "PUT" && action === "render") {
    const b = await body();
    if (typeof b.data !== "object") return fail(400, "data required");
    await putFile(env, id, "image", b.image);
    const ops = [];
    if (b.photo) await putFile(env, id, "photo", b.photo);
    else if (!b.keep_photo) ops.push(env.DB.prepare("DELETE FROM files WHERE poster_id = ? AND kind = 'photo'").bind(id));
    ops.push(env.DB.prepare("DELETE FROM files WHERE poster_id = ? AND kind = 'edit_photo'").bind(id));
    ops.push(env.DB.prepare(
      "UPDATE posters SET data = ?, rev = rev + 1, state = 'ready', edit = NULL, error = NULL, updated_at = ? WHERE id = ?"
    ).bind(JSON.stringify(b.data), now(), id));
    await env.DB.batch(ops);
    return json({ ok: true, rev: row.rev + 1 });
  }

  // POST /posters/<id>/edit-failed   {error}   the Action could not apply the edit
  if (m === "POST" && action === "edit-failed") {
    const b = await body();
    await env.DB.batch([
      env.DB.prepare("DELETE FROM files WHERE poster_id = ? AND kind = 'edit_photo'").bind(id),
      env.DB.prepare("UPDATE posters SET state = 'ready', edit = NULL, error = ?, updated_at = ? WHERE id = ?")
        .bind(String(b.error || "edit failed").slice(0, 500), now(), id),
    ]);
    return json({ ok: true });
  }

  return fail(404, "not found");
}

export default {
  async scheduled(event, env) {
    console.log(`cron "${event.cron}" fired at ${new Date(event.scheduledTime).toISOString()}`);
    await cleanup(env);
    await dispatch(env, DAILY_WORKFLOW);
  },
  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    if (url.pathname === "/" || url.pathname === "/health") return new Response("pakimtehan-posters: alive\n");
    if (!authorised(req, env)) return fail(401, "unauthorised");
    try {
      return await route(req, env, ctx);
    } catch (e) {
      console.log(`error ${req.method} ${url.pathname}: ${e.message}`);
      return fail(500, e.message);
    }
  },
};
