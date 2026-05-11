# Job Hunt Workflow

A repeatable, token-efficient process for scraping company job boards, matching roles against a resume, and producing an Apply / Stretch / Pass fit analysis.

_Last validated: 2026-04-23 against Google (12 URL-list → fit analysis). Previously: 2026-04-21 against Anthropic (446 listings → 14 SWE shortlist → fit analysis)._

---

[comment]: # (chazr note: this file is generated and maintained by Claude. I cannot vouch for its quality, only its efficacy. Minor edits to scrub references to me.)
[comment]: # (chazr note: The "candidate fingerprint" file mentioned below is not included in this example data. You will need to generate one.)

## The core insight

Do **not** spawn web-browsing agents to fetch job listings one URL at a time. Greenhouse and Lever both expose their boards as structured HTML/JSON that one Python script can harvest in a single pass. One script call replaces ~14 individual agent runs and keeps the analysis prose out of the main context window until we actually need it.

---

## The toolkit

- **`scripts/scrape_jobs.py`** — two-phase scraper. Phase 1 harvests a compact index (title, URL, location, department) for every listing on a company's public board. Phase 2 fetches full prose only for shortlisted listings.
- **`candidate_fingerprint.md`** — a one-page condensed summary of the current resume (key hooks, strengths, gaps, target levels, comp floor). Used as matching input without loading the whole .docx.
- **`scraped/{company}_{date}/`** — per-run output directory. Contains `raw.json`, `index.md`, `shortlist.md`, `full.md`.

---

## Standard workflow (per company)

### 1. Identify the ATS

Visit the company's careers page and look at the "Apply" URL on any listing:
- `boards.greenhouse.io/{company}` or `job-boards.greenhouse.io/{company}` → Greenhouse (covers Anthropic, OpenAI, Scale, many AI startups)
- `jobs.lever.co/{company}` → Lever
- `google.com/about/careers/applications/jobs/results/...` → Google (see URL-list mode below)
- `{company}.wd5.myworkdayjobs.com/...` → Workday (not currently supported — add a fetcher if needed)
- Custom pages → fall back to targeted scraping (last resort)

### 2. Run the scraper

```bash
python3 scripts/scrape_jobs.py \
  --company anthropic \
  --ats greenhouse-html \
  --location seattle \
  --keywords "software engineer" \
  --out-dir scraped/anthropic_2026-04-21
```

Flags:
- `--ats`: `greenhouse` (auto: try API then HTML), `greenhouse-api`, `greenhouse-html`, `lever`, `google-urls`. In this sandbox, `boards-api.greenhouse.io` is blocked — use `greenhouse-html` explicitly for Greenhouse boards.
- `--location`: case-insensitive substring match against the posted location.
- `--keywords`: comma-separated substrings, ANY-match against the title. Tighter keywords = cleaner shortlists. "senior,staff" over-matches (catches "Senior Manager, Accounting"). Prefer `"software engineer"` or `"senior software engineer,staff software engineer"` for SWE-only shortlists.
- `--urls-file`: path to text file with one URL per line (comments `#...` allowed). Required with `--ats google-urls`.

### Google URL-list mode

Google has no public board API and no cheap board index — the careers search UI paginates through a lot of noise. The realistic workflow is: do the filtering in Google's UI, paste the matching listing URLs into a text file in the Job Hunt root (e.g., `jobs_google_sr_staff.txt`), then run:

```bash
python3 scripts/scrape_jobs.py \
  --company google --ats google-urls \
  --urls-file jobs_google_sr_staff.txt \
  --out-dir scraped/google_2026-04-23
```

The Google fetcher pulls each listing's SSR'd HTML, extracts Title / Location(s) / Salary / About / Responsibilities / Min quals / Pref quals, and writes the same `raw.json` / `index.md` / `shortlist.md` / `full.md` structure as the other ATSes so the downstream fit-analysis pass is identical. Location extraction handles both multi-location listings (the "preferred working location" banner) and single-location listings (falls back to the `AF_initDataCallback({key: 'ds:0'})` structured-data blob).

Rate-limiting: the fetcher sleeps ~1s between requests. For batches >50 URLs, consider raising the delay.

### 3. Review the shortlist

Open `scraped/{company}_{date}/shortlist.md`. Trim any false positives manually before moving to Phase 2 fit analysis. (Phase 2 already ran; re-running with tightened filters only re-fetches listings that changed.)

### 4. Produce the fit analysis

Feed `shortlist.md` + `full.md` + `candidate_fingerprint.md` to a fit-analysis pass. Output structure that worked for Anthropic:

- **TL;DR tier table** — Apply / Stretch / Pass with counts and one-word reason.
- **Apply roles** — per-role paragraph: which specific bullets on the resume map to which specific teams/quals in the listing. Name the sub-teams. Call out salary band explicitly.
- **Stretch roles** — what fits, what's the reach, whether to apply anyway as a signal.
- **Pass roles** — one line each: the specialization gap.
- **Resume tailoring** — light-touch edits, not a rewrite. Flag any factual inconsistencies between Draft-N and reality.
- **Cover-letter hooks** — the narrative that connects the resume domain to the company's mission.
- **Application ordering** — if applying serially, which to send first and why.

### 5. Save the output

Put the analysis at `{company}_fit_analysis.md` in the Job Hunt folder. Keep the scraped/ directory as evidence. The fit analysis, not the scrape, is the durable artifact.

---

## Token-efficiency principles

1. **Index first, then prose.** Load all raw listings as (title, URL, location, dept) rows first. Filter. Only fetch full prose for the 10-20 that survive.
2. **Python + direct endpoints, not agents.** Agents that do "visit this URL and summarize" burn context by reading the page into their own context window and returning a summary. A Python scraper reads the page, extracts the prose, and writes it to disk — the main conversation only pays for the file paths.
3. **Cache + diff across runs.** Re-running next month? Diff `raw.json` against the previous snapshot to see only new or updated listings. (Current script writes fresh each run; add a diff step when you want it.)
4. **Fingerprint, not full resume.** Load `candidate_fingerprint.md` (~1 page) into the fit-analysis pass, not the full 3-page resume. Pull the .docx only when writing tailoring recommendations.
5. **Don't re-fetch what you already scraped.** When iterating on analysis framing, re-read `full.md` from disk rather than re-running the scraper.

---

## Known rough edges

- **Keyword filter over-matches.** "senior" catches "Senior Manager, Accounting." Tighter phrase keywords (`"software engineer"`) solve this. The script matches ANY keyword, not ALL — so `"senior software engineer,staff software engineer"` is broader than ideal; consider adding an ALL-match mode if needed.
- **Greenhouse API is blocked in this sandbox.** HTML fallback works fine. If you ever run this outside the sandbox, `--ats greenhouse-api` is cheaper.
- **Sandbox file-mount sync lag.** When editing the scraper, if bash runs the old version, write via bash heredoc directly to `/sessions/.../mnt/Job Hunt/scripts/scrape_jobs.py` rather than via the Write tool.
- **Workday / custom boards not supported.** Add a fetcher when you hit one. Workday's JSON endpoint is `{host}/wday/cxs/{tenant}/{site}/jobs` (POST).
- **Salary in `raw.json` defaults to $0–$0** when not present in the Greenhouse HTML detail page. Salary bands live in the posting body text; parse from prose if needed.

---

## When to re-validate this workflow

- Quarterly, or whenever a scrape returns zero / garbage results.
- When a new ATS shows up in the target company set (add a fetcher).
- When the fit-analysis template needs adjusting for a different company's hiring culture (e.g., Google / Meta rubric-heavy postings may warrant a different output shape).
