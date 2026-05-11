#!/usr/bin/env python3
"""scrape_jobs.py - pull a company's job board and emit normalized markdown
for downstream fit analysis.

{chazr note: this file was authored and is maintained by Claude. I disavow any responsibility for its contents.}

Two-phase design:
  Phase 1 (index)  - cheap fetches pull every listing's title + URL + location.
  Phase 2 (detail) - only shortlisted roles get their full prose fetched.

Supported ATSes:
  greenhouse-api   https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=true
                   Single call with full prose. Preferred when reachable.
  greenhouse-html  https://job-boards.greenhouse.io/{company} (paginated HTML fallback).
                   Used when API is blocked by network egress policy.
  lever            https://api.lever.co/v0/postings/{company}?mode=json
  google-urls      www.google.com/about/careers/applications/jobs/results/... (one URL
                   per line in --urls-file). Google has no public board API; the HTML is
                   SSR'd with the job prose inline, so we fetch each URL and extract
                   Title/Location/Salary/About/Responsibilities/Min/Pref qualifications.

Usage:
  python scripts/scrape_jobs.py \\
         --company anthropic --ats greenhouse \\
         --location seattle --keywords senior,staff \\
         --out-dir scraped/anthropic_2026-04-21

  python scripts/scrape_jobs.py \\
         --company google --ats google-urls \\
         --urls-file jobs_google_sr_staff.txt \\
         --out-dir scraped/google_2026-04-23

Outputs in --out-dir:
  raw.json       full normalized dump for diffing
  index.md       every listing grouped by department
  shortlist.md   listings matching --location and --keywords
  full.md        full prose for shortlisted listings only
"""

import argparse, html as htmllib, json, pathlib, re, sys, time
import urllib.error, urllib.request

UA = {"User-Agent": "job-hunt-scraper/1.0"}


def _get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _get_json(url, timeout=30):
    return json.loads(_get(url, timeout).decode("utf-8"))


def _get_text(url, timeout=30):
    return _get(url, timeout).decode("utf-8", errors="replace")


def strip_html(s):
    if not s:
        return ""
    s = re.sub(r"<\s*/?\s*(p|div|h[1-6])\s*>", "\n\n", s, flags=re.I)
    s = re.sub(r"<\s*br\s*/?\s*>", "\n", s, flags=re.I)
    s = re.sub(r"<\s*li[^>]*>", "\n- ", s, flags=re.I)
    s = re.sub(r"<\s*/\s*li\s*>", "", s, flags=re.I)
    s = re.sub(r"<\s*(ul|ol)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<\s*/\s*(ul|ol)\s*>", "\n", s, flags=re.I)
    s = re.sub(r"<\s*(strong|b)\s*>", "**", s, flags=re.I)
    s = re.sub(r"<\s*/\s*(strong|b)\s*>", "**", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = htmllib.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n[ \t]+", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# ---- Greenhouse API ------------------------------------------------------

def fetch_greenhouse_api(company):
    url = f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=true"
    raw = _get_json(url)
    out = []
    for job in raw.get("jobs", []):
        locs = [o["name"] for o in (job.get("offices") or []) if o.get("name")]
        if not locs and job.get("location", {}).get("name"):
            locs = [job["location"]["name"]]
        dept = ", ".join(d.get("name", "") for d in (job.get("departments") or []) if d.get("name"))
        out.append({
            "id": str(job.get("id", "")),
            "title": (job.get("title") or "").strip(),
            "url": job.get("absolute_url", ""),
            "locations": locs,
            "location_str": " | ".join(locs),
            "department": dept,
            "updated_at": job.get("updated_at", ""),
            "content_text": strip_html(job.get("content", "")),
            "_source": "greenhouse-api",
        })
    return out


# ---- Greenhouse HTML fallback --------------------------------------------

_JOB_BLOCK_RE = re.compile(
    r'<a[^>]+href="(?P<url>https://job-boards\.greenhouse\.io/[^"/]+/jobs/(?P<id>\d+))"[^>]*>'
    r'\s*<p[^>]*class="body body--medium"[^>]*>(?P<title>.*?)</p>'
    r'\s*<p[^>]*class="body body__secondary body--metadata"[^>]*>(?P<location>.*?)</p>',
    re.DOTALL,
)
_DEPT_SECTION_RE = re.compile(
    r'<h3[^>]*class="section-header font-primary"[^>]*>(?P<dept>[^<]+)</h3>'
    r'(?P<body>.*?)(?=<h3[^>]*class="section-header font-primary"|</main>)',
    re.DOTALL,
)
_REMIX_CONTEXT_RE = re.compile(
    r'window\.__remixContext\s*=\s*(?P<json>\{.*?\});</script>',
    re.DOTALL,
)
_PAGE_COUNT_RE = re.compile(r'aria-label="Go to page (\d+)"')


def _clean_title(fragment):
    cleaned = re.sub(r'<span class="tag-container".*?</span></span></span>', "", fragment, flags=re.DOTALL)
    return strip_html(cleaned).strip()


def fetch_greenhouse_html_index(company, max_pages=20):
    base = f"https://job-boards.greenhouse.io/{company}"
    first = _get_text(base)
    page_nums = [int(m) for m in _PAGE_COUNT_RE.findall(first)]
    last_page = max(page_nums) if page_nums else 1

    listings = []
    seen = set()

    def harvest(html_text):
        for sec in _DEPT_SECTION_RE.finditer(html_text):
            dept = htmllib.unescape(sec.group("dept")).strip()
            body = sec.group("body")
            for m in _JOB_BLOCK_RE.finditer(body):
                jid = m.group("id")
                if jid in seen:
                    continue
                seen.add(jid)
                loc_str = strip_html(m.group("location"))
                listings.append({
                    "id": jid,
                    "title": _clean_title(m.group("title")),
                    "url": m.group("url"),
                    "location_str": loc_str,
                    "locations": [s.strip() for s in re.split(r"[|;]", loc_str) if s.strip()],
                    "department": dept,
                    "updated_at": "",
                    "content_text": "",
                    "_source": "greenhouse-html",
                })

    harvest(first)
    for page in range(2, min(last_page, max_pages) + 1):
        harvest(_get_text(f"{base}?page={page}"))
    return listings


def fetch_greenhouse_html_detail(listing):
    html_text = _get_text(listing["url"])
    m = _REMIX_CONTEXT_RE.search(html_text)
    if not m:
        listing["content_text"] = "(__remixContext not found)"
        return listing
    try:
        ctx = json.loads(m.group("json"))
    except json.JSONDecodeError:
        listing["content_text"] = "(__remixContext JSON parse failed)"
        return listing
    loader = ctx.get("state", {}).get("loaderData", {}) or ctx.get("loaderData", {})
    job_post = None
    for v in loader.values():
        if isinstance(v, dict) and v.get("jobPost"):
            job_post = v["jobPost"]
            break
    if not job_post:
        listing["content_text"] = "(jobPost missing from loaderData)"
        return listing
    listing["content_text"] = strip_html(job_post.get("content", ""))
    listing["updated_at"] = job_post.get("updated_at", listing.get("updated_at", ""))
    pay = job_post.get("pay_ranges") or job_post.get("pay_input_ranges") or []
    if pay:
        pr = pay[0]
        lo = pr.get("min_cents", 0) // 100
        hi = pr.get("max_cents", 0) // 100
        cc = pr.get("currency_code", "USD")
        listing["pay_range"] = f"${lo:,} - ${hi:,} {cc}"
    return listing


# ---- Google (careers site, URL-list mode) --------------------------------
#
# Google has no public board API and its careers site is SSR'd with the job
# prose directly in the HTML. We accept a list of listing URLs and extract:
#   Title, Location(s), Salary, About the job, Responsibilities,
#   Minimum qualifications, Preferred qualifications.

_GOOGLE_TITLE_RE = re.compile(r"<title>([^<]+?)\s*[\u2014\-]\s*Google Careers</title>", re.I)
_GOOGLE_MULTI_LOC_RE = re.compile(
    r"preferred working location from the following:\s*<b>([^<]+)</b>", re.I)
# ds:0 structured-data callback contains a canonical location tuple for each
# listing (even single-location roles that have no "preferred working location"
# banner). Shape: ... [["City, ST, USA",["City, ST, USA"],"City",null,"ST","US"]] ...
_GOOGLE_DS0_LOC_RE = re.compile(
    r"AF_initDataCallback\(\{key:\s*'ds:0'.*?\[\[\"([^\"]+,\s*[A-Z]{2}(?:,\s*USA)?)\",\[",
    re.DOTALL)
_GOOGLE_ID_RE = re.compile(r"/jobs/results/(\d+)")
_GOOGLE_SALARY_RE = re.compile(
    r"US base salary range for this full-time position is\s*([^<+.]+)", re.I)
# Extract sections keyed on their <h3> header. Greedy until the next <h3> or
# a structural boundary. We keep it section-at-a-time for resilience.
_GOOGLE_SECTION_HEADERS = [
    ("about", r"About the job"),
    ("responsibilities", r"Responsibilities"),
    ("minimum", r"Minimum qualifications"),
    ("preferred", r"Preferred qualifications"),
]


def _google_section(html_text, header_re):
    pat = (r"<h3[^>]*>\s*" + header_re + r"[^<]*</h3>(.*?)"
           r"(?=<h3|<div[^>]+class=\"[A-Za-z0-9_\-]*BDNOWe|<div[^>]+class=\"[A-Za-z0-9_\-]*aG5W3|</main)")
    m = re.search(pat, html_text, re.DOTALL | re.I)
    return strip_html(m.group(1)) if m else ""


def parse_google_listing(html_text, url):
    title_m = _GOOGLE_TITLE_RE.search(html_text)
    title = htmllib.unescape(title_m.group(1)).strip() if title_m else url.rsplit("/", 1)[-1]

    loc_m = _GOOGLE_MULTI_LOC_RE.search(html_text)
    if loc_m:
        loc_str = htmllib.unescape(loc_m.group(1)).strip()
    else:
        ds0 = _GOOGLE_DS0_LOC_RE.search(html_text)
        loc_str = ds0.group(1).strip() if ds0 else ""

    sal_m = _GOOGLE_SALARY_RE.search(html_text)
    salary = sal_m.group(1).strip() if sal_m else ""

    sections = {key: _google_section(html_text, hdr) for key, hdr in _GOOGLE_SECTION_HEADERS}
    content_parts = []
    if sections["about"]:
        content_parts.append("**About the job**\n\n" + sections["about"])
    if sections["responsibilities"]:
        content_parts.append("**Responsibilities**\n\n" + sections["responsibilities"])
    if sections["minimum"]:
        content_parts.append("**Minimum qualifications**\n\n" + sections["minimum"])
    if sections["preferred"]:
        content_parts.append("**Preferred qualifications**\n\n" + sections["preferred"])

    id_m = _GOOGLE_ID_RE.search(url)
    job_id = id_m.group(1) if id_m else ""

    return {
        "id": job_id,
        "title": title,
        "url": url,
        "locations": [s.strip() for s in re.split(r"[;|]", loc_str) if s.strip()],
        "location_str": loc_str,
        "department": "Google",
        "updated_at": "",
        "content_text": "\n\n".join(content_parts).strip(),
        "pay_range": salary,
        "_source": "google-urls",
    }


def fetch_google_urls(urls, sleep_s=1.0):
    out = []
    for i, url in enumerate(urls, 1):
        try:
            html_text = _get_text(url)
            listing = parse_google_listing(html_text, url)
            out.append(listing)
            print(f"  [{i}/{len(urls)}] {listing['title'][:70]}", file=sys.stderr)
        except Exception as e:
            out.append({
                "id": "", "title": url.rsplit("/", 1)[-1], "url": url,
                "locations": [], "location_str": "", "department": "Google",
                "updated_at": "", "pay_range": "",
                "content_text": f"(fetch failed: {type(e).__name__}: {e})",
                "_source": "google-urls",
            })
            print(f"  [{i}/{len(urls)}] FAIL {url}: {e}", file=sys.stderr)
        if i < len(urls) and sleep_s:
            time.sleep(sleep_s)
    return out


# ---- Microsoft (Eightfold-powered, URL-list mode) ------------------------
#
# apply.careers.microsoft.com is an Eightfold AI front-end. There is no public
# board API reachable from this sandbox (jobs.careers.microsoft.com is not
# whitelisted, and apply.careers.microsoft.com/api/* returns the SPA 404 HTML).
#
# The listing page is server-rendered with one useful blob: a JSON-LD
# JobPosting <script type="application/ld+json"> containing title, full
# description (responsibilities + required quals concatenated, no section
# headers), jobLocation.address, datePosted, validThrough, employmentType.
#
# Salary band and "Other / Preferred" qualifications are NOT in the SSR'd
# HTML. They render client-side. Treat the description as best-effort prose.

_MSFT_LDJSON_RE = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
    re.DOTALL,
)
_MSFT_ID_RE = re.compile(r"/careers/job/(\d+)")


def parse_microsoft_listing(html_text, url):
    posting = None
    for m in _MSFT_LDJSON_RE.finditer(html_text):
        try:
            data = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            posting = data
            break

    id_m = _MSFT_ID_RE.search(url)
    job_id = id_m.group(1) if id_m else ""

    if not posting:
        return {
            "id": job_id, "title": "", "url": url,
            "locations": [], "location_str": "", "department": "Microsoft",
            "updated_at": "", "valid_through": "", "pay_range": "",
            "content_text": "(JobPosting JSON-LD not found in HTML)",
            "_source": "microsoft-urls",
        }

    title = (posting.get("title") or "").strip()

    # jobLocation can be a dict or a list of dicts.
    locs = posting.get("jobLocation") or []
    if isinstance(locs, dict):
        locs = [locs]
    loc_strs = []
    for loc in locs:
        addr = (loc or {}).get("address") or {}
        city = addr.get("addressLocality", "")
        # addressRegion sometimes "WA,US", sometimes "WA"
        region = (addr.get("addressRegion") or "").split(",")[0].strip()
        country = ""
        c = addr.get("addressCountry")
        if isinstance(c, dict):
            country = c.get("name", "") or ""
        elif isinstance(c, str):
            country = c
        parts = [p for p in [city, region, country] if p]
        if parts:
            loc_strs.append(", ".join(parts))
    loc_str = " | ".join(loc_strs)

    description = posting.get("description") or ""
    # Description is a single concatenated paragraph. Best-effort split on the
    # qualifications transition (almost always begins with a degree clause).
    qual_split_re = re.compile(
        r"(?=(?:Bachelor['\u2019]?s|Master['\u2019]?s|Doctorate|Ph\.?D|"
        r"PhD|Required[/ ]?Qualifications|Qualifications:))",
        re.I,
    )
    parts = qual_split_re.split(description, maxsplit=1)
    if len(parts) == 2 and len(parts[0].strip()) > 50:
        body = (
            "**Responsibilities (concatenated)**\n\n"
            + parts[0].strip()
            + "\n\n**Qualifications (as posted)**\n\n"
            + parts[1].strip()
        )
    else:
        body = description.strip()

    body += (
        "\n\n_Note: salary band and preferred/other qualifications are not "
        "exposed in the server-rendered HTML — the careers page renders them "
        "client-side via Eightfold. Check the listing URL directly to confirm."
    )

    return {
        "id": job_id,
        "title": title,
        "url": url,
        "locations": loc_strs,
        "location_str": loc_str,
        "department": "Microsoft",
        "updated_at": posting.get("datePosted", ""),
        "valid_through": posting.get("validThrough", ""),
        "pay_range": "",  # not available from SSR'd HTML
        "content_text": body.strip(),
        "_source": "microsoft-urls",
    }


def fetch_microsoft_urls(urls, sleep_s=1.0):
    out = []
    for i, url in enumerate(urls, 1):
        try:
            html_text = _get_text(url)
            listing = parse_microsoft_listing(html_text, url)
            out.append(listing)
            print(f"  [{i}/{len(urls)}] {listing['title'][:70]} | {listing['location_str']}", file=sys.stderr)
        except Exception as e:
            out.append({
                "id": "", "title": url.rsplit("/", 1)[-1], "url": url,
                "locations": [], "location_str": "", "department": "Microsoft",
                "updated_at": "", "valid_through": "", "pay_range": "",
                "content_text": f"(fetch failed: {type(e).__name__}: {e})",
                "_source": "microsoft-urls",
            })
            print(f"  [{i}/{len(urls)}] FAIL {url}: {e}", file=sys.stderr)
        if i < len(urls) and sleep_s:
            time.sleep(sleep_s)
    return out


# ---- OpenAI (Next.js careers site, URL-list mode) ------------------------
#
# openai.com/careers/* is a Next.js App Router page. The job prose is shipped
# inside the page's React Server Components payload via repeated
# `self.__next_f.push([1, "..."])` calls. The strings concatenate to a single
# large blob containing both the JSX-tree representation (`["$","p",null,...]`)
# AND raw HTML fragments for the long-prose body (one chunk holds the whole
# body as a single HTML string under a `"children":"<p>...<p>...<ul>..."` key).
#
# We extract:
#   Title       from the <title> tag (strip " | OpenAI")
#   Compensation from the structured Compensation block in the merged payload
#   Body HTML   from the chunk that contains "About the" + "<p>" markers
#   Location    from "based in <city...>." sentence in the body, with a
#               URL-slug fallback (last 1-3 trailing tokens after the role).
# Apply links go to jobs.ashbyhq.com/openai/<uuid>/application.

_OPENAI_TITLE_RE = re.compile(r"<title>([^<]+?)\s*\|\s*OpenAI</title>", re.I)
_OPENAI_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,(".*?")\]\)', re.DOTALL)
_OPENAI_COMP_RE = re.compile(
    r'Compensation"\}\]\}\],\[\s*"\$"\s*,\s*"p"\s*,\s*null\s*,\s*\{\s*"children":"([^"]+)"'
)
# The hero block on every careers page renders a `subhead` field of the form
# "<department> - <locations>", where <locations> is a natural-language list
# joined with comma + Oxford "and" (e.g. "San Francisco, Seattle, and London,
# UK"). Department itself can contain " - " (e.g. "Codex - Engineering"), so
# we rsplit on the LAST " - " to separate.
_OPENAI_SUBHEAD_RE = re.compile(r'"subhead":"([^"]+)"')
# Body-prose fallback if the subhead ever disappears.
_OPENAI_BASED_RE = re.compile(r"This role is based in ([^.<]+)", re.I)
_OPENAI_ASHBY_RE = re.compile(r"https://jobs\.ashbyhq\.com/openai/([0-9a-f\-]+)/application")
_OPENAI_SLUG_LOC_HINTS = {
    "san-francisco": "San Francisco, CA",
    "new-york": "New York, NY",
    "washington-dc": "Washington, DC",
    "seattle": "Seattle, WA",
    "london": "London, UK",
    "tokyo": "Tokyo, Japan",
    "singapore": "Singapore",
    "dublin": "Dublin, Ireland",
    "remote-us": "Remote (US)",
    "remote": "Remote",
    "paris": "Paris, France",
}


def _openai_slug_location(url):
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    # Try known multi-word suffixes first (longest match wins)
    for hint, label in sorted(_OPENAI_SLUG_LOC_HINTS.items(), key=lambda kv: -len(kv[0])):
        if slug.endswith("-" + hint) or slug == hint:
            return label
    # Fallback: take the last token, capitalize
    last = slug.rsplit("-", 1)[-1]
    return last.replace("-", " ").title()


def parse_openai_listing(html_text, url):
    title_m = _OPENAI_TITLE_RE.search(html_text)
    title = htmllib.unescape(title_m.group(1)).strip() if title_m else url.rsplit("/", 1)[-1]

    chunks = []
    for m in _OPENAI_PUSH_RE.finditer(html_text):
        try:
            chunks.append(json.loads(m.group(1)))
        except json.JSONDecodeError:
            continue
    merged = "".join(chunks)

    # Compensation
    comp_m = _OPENAI_COMP_RE.search(merged)
    pay = ""
    if comp_m:
        pay = comp_m.group(1).strip()
        # The payload has "$$185K – $385K + Offers Equity" because it's HTML-text
        # within a JSON string and the leading $ is an artifact. Collapse.
        if pay.startswith("$$"):
            pay = pay[1:]
        # Decode common unicode escapes that survived JSON parsing
        pay = pay.replace("\\u2013", "-").replace("–", "-")

    # Body chunk: the one containing "About the" plus "<p>" markup.
    body_html = ""
    for c in chunks:
        if "About the" in c and "<p>" in c:
            body_html = c
            break
    if not body_html:
        # Fallback: any chunk with substantial HTML prose
        for c in chunks:
            if "<p>" in c and len(c) > 1000:
                body_html = c
                break

    # Location + Department: extract from the hero `subhead` field. This is
    # the canonical, structured source the page itself renders. Format:
    # "<department> - <locations>", but with two complications:
    #   1. Department can itself contain " - " (e.g. "Codex - Engineering").
    #   2. Locations occasionally use " - " internally for "Remote - US",
    #      which can appear in the middle ("Security - Remote - US, ...") or
    #      at the end ("Applied AI Engineering - SF, ..., and Remote - US").
    # Heuristic: split on " - ", then find the first segment that starts
    # with a known city/region token. Everything before is department;
    # from that segment onward (re-joined with " - ") is the locations
    # string. This handles all three patterns observed in the 26 sampled
    # OpenAI listings.
    sub_m = _OPENAI_SUBHEAD_RE.search(merged)
    if sub_m:
        subhead = sub_m.group(1).strip()
        parts = subhead.split(" - ")
        if len(parts) == 1:
            department, loc_str = "OpenAI", subhead
        else:
            loc_starts = (
                "San Francisco", "Seattle", "Washington", "New York",
                "Bellevue", "London", "Tokyo", "Singapore", "Dublin",
                "Paris", "Remote",
            )
            loc_idx = None
            for i, p in enumerate(parts):
                if any(p.startswith(t) for t in loc_starts):
                    loc_idx = i
                    break
            if loc_idx is None or loc_idx == 0:
                # Couldn't identify a location start — keep raw and don't
                # invent a department.
                department, loc_str = "OpenAI", subhead
            else:
                department = " - ".join(parts[:loc_idx]).strip()
                loc_str = " - ".join(parts[loc_idx:]).strip()
    else:
        # Fallbacks if the subhead ever disappears: body prose, then slug.
        loc_m = _OPENAI_BASED_RE.search(body_html or "")
        if loc_m:
            loc_str = loc_m.group(1).strip().rstrip(",;:")
        else:
            loc_str = _openai_slug_location(url)
        department = "OpenAI"

    # Apply link
    apply_m = _OPENAI_ASHBY_RE.search(html_text)
    apply_link = apply_m.group(0) if apply_m else ""

    body_md = strip_html(body_html) if body_html else "(body chunk not found in __next_f payload)"
    if apply_link:
        body_md += f"\n\n**Apply:** {apply_link}"

    # ID: the Ashby UUID is a stable per-listing identifier.
    job_id = apply_m.group(1) if apply_m else url.rstrip("/").rsplit("/", 1)[-1]

    return {
        "id": job_id,
        "title": title,
        "url": url,
        # Store the raw natural-language location string. Preserve list shape
        # for schema compatibility with other ATS modes by wrapping in a list.
        "locations": [loc_str],
        "location_str": loc_str,
        "department": department,
        "updated_at": "",
        "pay_range": pay,
        "content_text": body_md.strip(),
        "_source": "openai-urls",
    }


def fetch_openai_urls(urls, sleep_s=1.0):
    out = []
    for i, url in enumerate(urls, 1):
        try:
            html_text = _get_text(url)
            listing = parse_openai_listing(html_text, url)
            out.append(listing)
            print(f"  [{i}/{len(urls)}] {listing['title'][:60]} | {listing['location_str']} | {listing['pay_range']}", file=sys.stderr)
        except Exception as e:
            out.append({
                "id": "", "title": url.rsplit("/", 1)[-1], "url": url,
                "locations": [], "location_str": "", "department": "OpenAI",
                "updated_at": "", "pay_range": "",
                "content_text": f"(fetch failed: {type(e).__name__}: {e})",
                "_source": "openai-urls",
            })
            print(f"  [{i}/{len(urls)}] FAIL {url}: {e}", file=sys.stderr)
        if i < len(urls) and sleep_s:
            time.sleep(sleep_s)
    return out


# ---- Snap (careers.snap.com, URL-list mode) ------------------------------
#
# careers.snap.com is a SPA with SSR'd Helmet metadata; the job content itself
# is shipped inline in `window.ASYNC_DATA_CONTROLLER_CACHE = {...}` keyed as
# "job-<id>". The body contains:
#   title, id, role, departments, primary_location,
#   offices: [{location, name}, ...]   (list of city, state pairs)
#   jobDescription                     (HTML string with about / responsibilities
#                                       / qualifications / Compensation block)
#   External_Apply_URL                 (Workday apply URL)
#   timeType                           (e.g. "Full time")
#   lastUpdateDateTime                 (ISO 8601)
# Salary lives in the description prose under "Zone A (CA, WA, NYC)" /
# "Zone B" / "Zone C" headers — we extract Zone A (= WA, where Chaz is) plus
# the full Zone A/B/C trio for transparency.
#
# Apply links go to wd1.myworkdaysite.com/recruiting/snapchat/snap/...

_SNAP_CACHE_RE = re.compile(r'window\.ASYNC_DATA_CONTROLLER_CACHE\s*=\s*')
_SNAP_ID_RE = re.compile(r"[?&]id=([A-Za-z0-9_]+)")
# Salary regex applied to the *stripped* description text (not raw HTML).
# Each Zone heading sits on its own line, then a blank line, then a sentence
# of the form: "The base salary range for this position is $XXX,XXX-$YYY,YYY annually."
_SNAP_ZONE_RE = re.compile(
    r"Zone\s+(?P<zone>[ABC])(?:[^\n]*?):\s*\n+\s*"
    r"The base salary range for this position is\s+"
    r"(?P<band>\$[\d,]+\s*-\s*\$[\d,]+)\s*annually",
    re.I,
)


def _snap_extract_blob(html_text):
    """Brace-match window.ASYNC_DATA_CONTROLLER_CACHE = {...}; → dict."""
    m = _SNAP_CACHE_RE.search(html_text)
    if not m:
        return None
    start = m.end()
    if start >= len(html_text) or html_text[start] != "{":
        return None
    depth, in_str, esc, i = 0, False, False, start
    while i < len(html_text):
        ch = html_text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(html_text[start:i + 1])
                    except json.JSONDecodeError:
                        return None
        i += 1
    return None


def parse_snap_listing(html_text, url):
    id_m = _SNAP_ID_RE.search(url)
    job_id = id_m.group(1) if id_m else ""

    blob = _snap_extract_blob(html_text)
    if not blob:
        return {
            "id": job_id, "title": "", "url": url,
            "locations": [], "location_str": "", "department": "Snap",
            "updated_at": "", "pay_range": "",
            "content_text": "(ASYNC_DATA_CONTROLLER_CACHE not found)",
            "_source": "snap-urls",
        }

    # Cache is keyed "job-<id>". Pick the first job-* entry.
    body = None
    for k, v in blob.items():
        if k.startswith("job-") and isinstance(v, dict) and v.get("data"):
            body = v["data"].get("body")
            break
    if not body:
        return {
            "id": job_id, "title": "", "url": url,
            "locations": [], "location_str": "", "department": "Snap",
            "updated_at": "", "pay_range": "",
            "content_text": "(no job-* entry in cache blob)",
            "_source": "snap-urls",
        }

    title = (body.get("title") or "").strip()
    department = (body.get("departments") or body.get("role") or "Snap").strip()
    primary = (body.get("primary_location") or "").strip()

    offices = body.get("offices") or []
    loc_strs = [o.get("location", "").strip() for o in offices if o.get("location")]
    if not loc_strs and primary:
        loc_strs = [primary]

    desc_html = body.get("jobDescription") or ""
    apply_link = (body.get("External_Apply_URL") or body.get("absolute_url") or "").strip()
    body_md = strip_html(desc_html) if desc_html else "(jobDescription empty)"

    # Pull Zone A/B/C salary bands from the *stripped* prose.
    zones = {}
    for m in _SNAP_ZONE_RE.finditer(body_md):
        zones[m.group("zone").upper()] = m.group("band").strip()
    pay_parts = []
    if "A" in zones:
        pay_parts.append(f"Zone A (CA/WA/NYC): {zones['A']}")
    if "B" in zones:
        pay_parts.append(f"Zone B: {zones['B']}")
    if "C" in zones:
        pay_parts.append(f"Zone C: {zones['C']}")
    pay_range = " | ".join(pay_parts)

    if apply_link:
        body_md += f"\n\n**Apply:** {apply_link}"

    return {
        "id": body.get("id") or job_id,
        "title": title,
        "url": url,
        "locations": loc_strs,
        "location_str": " | ".join(loc_strs),
        "department": department,
        "updated_at": (body.get("lastUpdateDateTime") or body.get("startDate") or "").strip(),
        "pay_range": pay_range,
        "content_text": body_md.strip(),
        "_source": "snap-urls",
    }


def fetch_snap_urls(urls, sleep_s=1.0):
    out = []
    for i, url in enumerate(urls, 1):
        try:
            html_text = _get_text(url)
            listing = parse_snap_listing(html_text, url)
            out.append(listing)
            print(f"  [{i}/{len(urls)}] {listing['title'][:60]} | {listing['location_str']} | {listing['pay_range']}", file=sys.stderr)
        except Exception as e:
            out.append({
                "id": "", "title": url.rsplit("/", 1)[-1], "url": url,
                "locations": [], "location_str": "", "department": "Snap",
                "updated_at": "", "pay_range": "",
                "content_text": f"(fetch failed: {type(e).__name__}: {e})",
                "_source": "snap-urls",
            })
            print(f"  [{i}/{len(urls)}] FAIL {url}: {e}", file=sys.stderr)
        if i < len(urls) and sleep_s:
            time.sleep(sleep_s)
    return out


# ---- Stripe (stripe.com/jobs/listing/, URL-list mode) --------------------
#
# Stripe's careers site is fully SSR'd and clean: the listing prose lives
# directly in the HTML in stable, labeled sections. There is no public board
# API we can cheaply hit, so we go URL-list-style like Google / Microsoft.
#
# Page anatomy (verified 2026-04-26 against /jobs/listing/sr-staff-engineer-billing/6372750):
#   - data-page-title="<role title>"  on the root <html> element
#   - <h1>{title}</h1>                 (3rd h1 on the page; first two are nav)
#   - <h2>Who we are</h2>              -> contains <h3>About Stripe</h3>
#                                                  <h3>About the team</h3>
#   - <h2>What you'll do</h2>          -> intro paragraphs +
#                                         <h3>Responsibilities</h3> bullet list
#   - <h2>Who you are</h2>             -> <h3>Minimum requirements</h3>
#                                         <h3>Preferred qualifications</h3>
#   - <h1>In-office expectations</h1>  (boilerplate; skip)
#   - <h1>Pay and benefits</h1>        ->  "The annual US base salary range
#                                          for this role is $X - $Y."
#   - JobDetailCardProperty blocks:
#         <p class="JobDetailCardProperty__title">Office locations</p>
#         <p>South San Francisco HQ, New York, or Seattle</p>
#         (also Team, Job type)
#
# The role ID is the trailing numeric path segment: /listing/{slug}/{id}.

_STRIPE_PAGE_TITLE_RE = re.compile(r'data-page-title="([^"]+)"')
_STRIPE_ID_RE = re.compile(r"/jobs/listing/[^/]+/(\d+)")
_STRIPE_PROPERTY_RE = re.compile(
    r'<p class="JobDetailCardProperty__title">([^<]+)</p>\s*<p>(.*?)</p>',
    re.DOTALL,
)
_STRIPE_SALARY_RE = re.compile(
    r"annual US base salary range for this role is\s*([^<.]+?)\s*\.",
    re.I,
)


def _stripe_section(html_text, header_tag, header_text, end_tag_pattern):
    """Slice from `<{header_tag}>{header_text}</{header_tag}>` to next boundary."""
    pat = (
        rf"<{header_tag}[^>]*>\s*"
        + re.escape(header_text)
        + rf"\s*</{header_tag}>"
        + r"(.*?)(?="
        + end_tag_pattern
        + r")"
    )
    m = re.search(pat, html_text, re.DOTALL | re.I)
    return strip_html(m.group(1)) if m else ""


def parse_stripe_listing(html_text, url):
    title_m = _STRIPE_PAGE_TITLE_RE.search(html_text)
    title = htmllib.unescape(title_m.group(1)).strip() if title_m else ""

    body = re.sub(r"<script[^>]*>.*?</script>", "", html_text, flags=re.S)
    body = re.sub(r"<style[^>]*>.*?</style>", "", body, flags=re.S)

    props = {}
    for m in _STRIPE_PROPERTY_RE.finditer(body):
        label = strip_html(m.group(1)).strip()
        value = strip_html(m.group(2)).strip()
        value = re.sub(r"\s+", " ", value)
        props[label] = value

    loc_str = props.get("Office locations", "")
    team = props.get("Team", "")
    job_type = props.get("Job type", "")

    sal_m = _STRIPE_SALARY_RE.search(body)
    salary = sal_m.group(1).strip() if sal_m else ""

    section_end = r"<h2|<h1[^>]*>\s*(?:In-office|Pay and benefits|Please find)"
    about_stripe = _stripe_section(body, "h3", "About Stripe", section_end + r"|<h3")
    about_team = _stripe_section(body, "h3", "About the team", section_end + r"|<h3")
    # Some listings put Responsibilities at h2 instead of h3 (e.g. Stripe Scale).
    responsibilities = (
        _stripe_section(body, "h3", "Responsibilities", section_end + r"|<h3")
        or _stripe_section(body, "h2", "Responsibilities", section_end + r"|<h3")
    )
    min_quals = _stripe_section(body, "h3", "Minimum requirements", section_end + r"|<h3")
    pref_quals = _stripe_section(body, "h3", "Preferred qualifications", section_end + r"|<h3")

    intro_m = re.search(
        r"<h2[^>]*>\s*What you[’']ll do\s*</h2>(.*?)(?=<h[23][^>]*>\s*Responsibilities)",
        body,
        re.DOTALL | re.I,
    )
    intro = strip_html(intro_m.group(1)) if intro_m else ""

    parts = []
    if about_stripe:
        parts.append("**About Stripe**\n\n" + about_stripe)
    if about_team:
        parts.append("**About the team**\n\n" + about_team)
    if intro:
        parts.append("**What you'll do**\n\n" + intro)
    if responsibilities:
        parts.append("**Responsibilities**\n\n" + responsibilities)
    if min_quals:
        parts.append("**Minimum requirements**\n\n" + min_quals)
    if pref_quals:
        parts.append("**Preferred qualifications**\n\n" + pref_quals)

    id_m = _STRIPE_ID_RE.search(url)
    job_id = id_m.group(1) if id_m else ""

    return {
        "id": job_id,
        "title": title,
        "url": url,
        "locations": [s.strip() for s in re.split(r"[,;|]| or ", loc_str) if s.strip()],
        "location_str": loc_str,
        "department": team or "Stripe",
        "updated_at": "",
        "pay_range": salary,
        "job_type": job_type,
        "content_text": "\n\n".join(parts).strip(),
        "_source": "stripe-urls",
    }


def fetch_stripe_urls(urls, sleep_s=1.0):
    out = []
    for i, url in enumerate(urls, 1):
        try:
            html_text = _get_text(url)
            listing = parse_stripe_listing(html_text, url)
            out.append(listing)
            print(
                f"  [{i}/{len(urls)}] {listing['title'][:70]} | {listing['location_str']}",
                file=sys.stderr,
            )
        except Exception as e:
            out.append({
                "id": "", "title": url.rsplit("/", 1)[-1], "url": url,
                "locations": [], "location_str": "", "department": "Stripe",
                "updated_at": "", "pay_range": "", "job_type": "",
                "content_text": f"(fetch failed: {type(e).__name__}: {e})",
                "_source": "stripe-urls",
            })
            print(f"  [{i}/{len(urls)}] FAIL {url}: {e}", file=sys.stderr)
        if i < len(urls) and sleep_s:
            time.sleep(sleep_s)
    return out


# ---- Lever ---------------------------------------------------------------

def fetch_lever(company):
    raw = _get_json(f"https://api.lever.co/v0/postings/{company}?mode=json")
    out = []
    for job in raw:
        cats = job.get("categories") or {}
        loc = cats.get("location", "") or ""
        body = [strip_html(job.get("descriptionHtml") or job.get("description") or "")]
        for lst in (job.get("lists") or []):
            if lst.get("text"):
                body.append(f"\n**{lst['text'].strip()}**")
            body.append(strip_html(lst.get("content") or ""))
        if job.get("additional"):
            body.append(strip_html(job.get("additional") or ""))
        out.append({
            "id": job.get("id", ""),
            "title": (job.get("text") or "").strip(),
            "url": job.get("hostedUrl", ""),
            "locations": [loc] if loc else [],
            "location_str": loc,
            "department": cats.get("team", "") or cats.get("department", "") or "",
            "updated_at": job.get("createdAt", ""),
            "content_text": "\n\n".join(p for p in body if p).strip(),
            "_source": "lever",
        })
    return out


# ---- Filter / write ------------------------------------------------------

def matches(listing, location, keywords):
    if location and location.lower() not in listing["location_str"].lower():
        return False
    if keywords:
        t = listing["title"].lower()
        if not any(k.lower() in t for k in keywords):
            return False
    return True


def _mde(s):
    return s.replace("|", "\\|")


def write_index(listings, path, company):
    by_dept = {}
    for l in listings:
        by_dept.setdefault(l["department"] or "Uncategorized", []).append(l)
    lines = [f"# {company} - all listings ({len(listings)})"]
    for dept in sorted(by_dept):
        lines.append(f"\n## {dept} ({len(by_dept[dept])})")
        lines.append("")
        lines.append("| # | Role | Location | URL |")
        lines.append("|---|------|----------|-----|")
        for i, l in enumerate(by_dept[dept], 1):
            lines.append(f"| {i} | {_mde(l['title'])} | {_mde(l['location_str'])} | {l['url']} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_shortlist(listings, path, location, keywords):
    lines = [
        "# Shortlist",
        "",
        f"**Filters:** location={location or '(any)'}, keywords={','.join(keywords) or '(any)'}",
        f"**Matches:** {len(listings)}",
        "",
        "| # | Role | Location | Department | URL |",
        "|---|------|----------|------------|-----|",
    ]
    for i, l in enumerate(listings, 1):
        lines.append(
            f"| {i} | {_mde(l['title'])} | {_mde(l['location_str'])} "
            f"| {_mde(l['department'])} | {l['url']} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_full(listings, path):
    lines = [f"# Full listings ({len(listings)})"]
    for i, l in enumerate(listings, 1):
        lines.append(f"\n## {i}. {l['title']}\n")
        lines.append(f"**Location:** {l['location_str']}  ")
        lines.append(f"**Department:** {l['department']}  ")
        lines.append(f"**URL:** {l['url']}  ")
        lines.append(f"**ID:** {l['id']}  ")
        if l.get("pay_range"):
            lines.append(f"**Pay:** {l['pay_range']}  ")
        if l.get("valid_through"):
            lines.append(f"**Valid through:** {l['valid_through']}  ")
        if l.get("updated_at"):
            lines.append(f"**Posted/updated:** {l['updated_at']}  ")
        lines.append("")
        lines.append(l["content_text"])
        lines.append("\n---")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def fetch_all(company, ats, urls_file=None):
    if ats == "greenhouse":
        try:
            return fetch_greenhouse_api(company), "done"
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            print(f"  boards-api unavailable ({e}); falling back to HTML", file=sys.stderr)
            return fetch_greenhouse_html_index(company), "needed"
    if ats == "greenhouse-api":
        return fetch_greenhouse_api(company), "done"
    if ats == "greenhouse-html":
        return fetch_greenhouse_html_index(company), "needed"
    if ats == "lever":
        return fetch_lever(company), "done"
    if ats == "google-urls":
        if not urls_file:
            raise SystemExit("--ats google-urls requires --urls-file")
        urls = [ln.strip() for ln in pathlib.Path(urls_file).read_text().splitlines()
                if ln.strip() and not ln.strip().startswith("#")]
        if not urls:
            raise SystemExit(f"no URLs found in {urls_file!r}")
        return fetch_google_urls(urls), "done"
    if ats == "microsoft-urls":
        if not urls_file:
            raise SystemExit("--ats microsoft-urls requires --urls-file")
        urls = [ln.strip() for ln in pathlib.Path(urls_file).read_text().splitlines()
                if ln.strip() and not ln.strip().startswith("#")]
        if not urls:
            raise SystemExit(f"no URLs found in {urls_file!r}")
        return fetch_microsoft_urls(urls), "done"
    if ats == "openai-urls":
        if not urls_file:
            raise SystemExit("--ats openai-urls requires --urls-file")
        urls = [ln.strip() for ln in pathlib.Path(urls_file).read_text().splitlines()
                if ln.strip() and not ln.strip().startswith("#")]
        if not urls:
            raise SystemExit(f"no URLs found in {urls_file!r}")
        return fetch_openai_urls(urls), "done"
    if ats == "snap-urls":
        if not urls_file:
            raise SystemExit("--ats snap-urls requires --urls-file")
        urls = [ln.strip() for ln in pathlib.Path(urls_file).read_text().splitlines()
                if ln.strip() and not ln.strip().startswith("#")]
        if not urls:
            raise SystemExit(f"no URLs found in {urls_file!r}")
        return fetch_snap_urls(urls), "done"
    if ats == "stripe-urls":
        if not urls_file:
            raise SystemExit("--ats stripe-urls requires --urls-file")
        urls = [ln.strip() for ln in pathlib.Path(urls_file).read_text().splitlines()
                if ln.strip() and not ln.strip().startswith("#")]
        if not urls:
            raise SystemExit(f"no URLs found in {urls_file!r}")
        return fetch_stripe_urls(urls), "done"
    raise SystemExit(f"unknown --ats {ats!r}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--company", required=True)
    p.add_argument("--ats", default="greenhouse",
                   choices=["greenhouse", "greenhouse-api", "greenhouse-html", "lever",
                            "google-urls", "microsoft-urls", "openai-urls", "snap-urls",
                            "stripe-urls"])
    p.add_argument("--location", default="")
    p.add_argument("--keywords", default="")
    p.add_argument("--urls-file", default="",
                   help="Path to text file with one listing URL per line (for --ats google-urls, microsoft-urls, openai-urls, snap-urls, or stripe-urls).")
    p.add_argument("--out-dir", required=True)
    args = p.parse_args(argv)

    out = pathlib.Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    print(f"Phase 1: fetching {args.company!r} via {args.ats} ...", file=sys.stderr)
    listings, phase2 = fetch_all(args.company, args.ats, urls_file=args.urls_file)
    print(f"  indexed {len(listings)} listings", file=sys.stderr)

    kw = [k.strip() for k in args.keywords.split(",") if k.strip()]
    shortlist = [l for l in listings if matches(l, args.location, kw)]
    print(f"  shortlist: {len(shortlist)} matches (location={args.location!r}, keywords={kw})", file=sys.stderr)

    if phase2 == "needed" and shortlist:
        print(f"Phase 2: fetching detail for {len(shortlist)} shortlisted listings ...", file=sys.stderr)
        for i, l in enumerate(shortlist, 1):
            try:
                fetch_greenhouse_html_detail(l)
                print(f"  [{i}/{len(shortlist)}] {l['title'][:60]}", file=sys.stderr)
            except Exception as e:
                l["content_text"] = f"(fetch failed: {type(e).__name__}: {e})"
                print(f"  [{i}/{len(shortlist)}] FAIL {l['title'][:60]}: {e}", file=sys.stderr)

    (out / "raw.json").write_text(json.dumps(listings, indent=2, default=str), encoding="utf-8")
    write_index(listings, out / "index.md", args.company)
    write_shortlist(shortlist, out / "shortlist.md", args.location, kw)
    write_full(shortlist, out / "full.md")

    print("\nWrote:")
    print(f"  {out}/raw.json      ({len(listings)} listings)")
    print(f"  {out}/index.md      (all {len(listings)} listings)")
    print(f"  {out}/shortlist.md  ({len(shortlist)} matches)")
    print(f"  {out}/full.md       (full prose for shortlist)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
