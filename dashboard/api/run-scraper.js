// Vercel serverless function: triggers the scraper's GitHub Action from the dashboard.
//   POST /api/run-scraper  -> starts the workflow (refuses if one is running or a manual run was very recent)
//   GET  /api/run-scraper  -> latest manually-triggered run, so the page can show progress
//
// Vercel -> Project -> Settings -> Environment Variables:
//   GITHUB_TOKEN  (required) fine-grained PAT, repo Shreshth1006/RevPod only, permission "Actions: Read and write"
//   GH_REPO, GH_WORKFLOW, GH_REF, RUN_COOLDOWN_MIN  (optional overrides)
// The token never reaches the browser.

const REPO = process.env.GH_REPO || "Shreshth1006/RevPod";
const WORKFLOW = process.env.GH_WORKFLOW || "hero-scrape.yml";
const REF = process.env.GH_REF || "main";
const COOLDOWN_MIN = Number(process.env.RUN_COOLDOWN_MIN || 3);

function gh(path, opts = {}) {
  return fetch(`https://api.github.com/repos/${REPO}${path}`, {
    ...opts,
    headers: {
      Authorization: `Bearer ${process.env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "revpod-dashboard",
      ...(opts.headers || {}),
    },
  });
}

async function latestRun(event) {
  const q = event ? `&event=${event}` : "";
  const r = await gh(`/actions/workflows/${WORKFLOW}/runs?per_page=1${q}`);
  if (!r.ok) throw new Error(`GitHub ${r.status}: ${(await r.text()).slice(0, 200)}`);
  const x = (await r.json()).workflow_runs?.[0];
  return x ? { id: x.id, status: x.status, conclusion: x.conclusion, created_at: x.created_at,
               html_url: x.html_url, event: x.event } : null;
}

module.exports = async (req, res) => {
  res.setHeader("Cache-Control", "no-store");
  if (!process.env.GITHUB_TOKEN) {
    return res.status(500).json({ error: "GITHUB_TOKEN is not set in Vercel environment variables" });
  }
  try {
    if (req.method === "GET") {
      return res.status(200).json({ run: await latestRun("workflow_dispatch") });
    }
    if (req.method !== "POST") {
      res.setHeader("Allow", "GET, POST");
      return res.status(405).json({ error: "Use POST to start a run" });
    }

    // Guard rails: the page is public, so don't let repeated clicks pile up runs.
    const last = await latestRun();
    if (last && last.status !== "completed") {
      return res.status(409).json({ error: "A run is already in progress", run: last });
    }
    const lastManual = await latestRun("workflow_dispatch");
    if (lastManual && Date.now() - Date.parse(lastManual.created_at) < COOLDOWN_MIN * 60e3) {
      return res.status(429).json({ error: `Ran manually under ${COOLDOWN_MIN} min ago, try again shortly`, run: lastManual });
    }

    const dispatched_at = new Date().toISOString();
    const r = await gh(`/actions/workflows/${WORKFLOW}/dispatches`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ref: REF }),
    });
    if (r.status !== 204) {
      return res.status(502).json({ error: `GitHub refused (${r.status}): ${(await r.text()).slice(0, 200)}` });
    }
    return res.status(202).json({ ok: true, dispatched_at });
  } catch (e) {
    return res.status(502).json({ error: String(e.message || e) });
  }
};
