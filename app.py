import csv
import io
import json
import queue
import re
import threading
import time
import uuid
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
from flask import Flask, Response, render_template, request, jsonify

app = Flask(__name__)
requests.packages.urllib3.disable_warnings()

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}

# In-memory job store: job_id -> {status, queue, csv, error}
JOBS = {}


# ---------------------------------------------------------------------------
# SCRAPER — find and extract sponsor names from a conference site
# ---------------------------------------------------------------------------

def find_sponsor_page(homepage_url: str) -> list[str]:
    """
    Given a homepage URL, return candidate sponsor page URLs.
    Looks for nav links containing sponsor/partner/exhibitor keywords.
    """
    try:
        resp = requests.get(homepage_url, headers=HEADERS, timeout=12, verify=False)
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception:
        return []

    keywords = ["sponsor", "partner", "exhibitor", "supporter"]
    candidates = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True).lower()
        full = urljoin(homepage_url, href)
        if any(k in href.lower() or k in text for k in keywords) and full not in seen:
            seen.add(full)
            candidates.append(full)
    return candidates


def try_js_api(page_url: str, page_html: str) -> list[str] | None:
    """
    Look for an embedded JS API endpoint (like ai4.io's Cloudflare worker).
    Returns list of sponsor names if found, else None.
    """
    soup = BeautifulSoup(page_html, "html.parser")
    for script in soup.find_all("script"):
        content = script.string or ""
        # Look for worker/API URL patterns
        m = re.search(r'["\']([https]+://[^\s"\']+workers\.dev[^\s"\']*)["\']', content)
        if not m:
            m = re.search(r'WORKER_URL\s*=\s*["\']([^"\']+)["\']', content)
        if not m:
            m = re.search(r'getAttribute\("data-worker-url"\)[^;]+\|\|\s*["\']([^"\']+)["\']', content)
        if m:
            api_base = m.group(1).rstrip("/")
            for path in ["/sponsors", "/exhibitors", "/api/sponsors"]:
                try:
                    r = requests.get(api_base + path, headers=HEADERS, timeout=10)
                    if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                        data = r.json()
                        sponsors = data.get("sponsors", data.get("exhibitors", []))
                        if sponsors:
                            names = []
                            for s in sponsors:
                                name = s.get("name", "").strip()
                                cf = (s.get("customFields") or {}).get("Include in public list", {})
                                if name and cf.get("value") != "no":
                                    names.append(name)
                            return sorted(set(names))
                except Exception:
                    continue
    return None


def scrape_sponsors_from_html(page_html: str) -> list[str]:
    """
    Fallback: extract sponsor names from HTML using heading + image alt / link text heuristics.
    """
    soup = BeautifulSoup(page_html, "html.parser")
    names = set()
    keywords = ["sponsor", "partner", "exhibitor", "supporter"]

    # Strategy 1: elements near sponsor headings
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5"]):
        if any(k in tag.get_text(strip=True).lower() for k in keywords):
            for sibling in tag.find_next_siblings():
                if sibling.name in ["h1", "h2", "h3"] and not any(k in sibling.get_text(strip=True).lower() for k in keywords):
                    break
                for img in sibling.find_all("img"):
                    alt = img.get("alt", "").strip()
                    if alt and len(alt) > 2 and alt.lower() not in keywords:
                        names.add(alt)
                for a in sibling.find_all("a"):
                    text = a.get_text(strip=True)
                    if text and len(text) > 2 and not any(k in text.lower() for k in ["click", "view", "more", "apply", "become"]):
                        names.add(text)

    # Strategy 2: elements with sponsor-related class/id
    if not names:
        for el in soup.find_all(True):
            cls = " ".join(el.get("class", []))
            eid = el.get("id", "")
            if any(k in (cls + eid).lower() for k in keywords):
                for img in el.find_all("img"):
                    alt = img.get("alt", "").strip()
                    if alt and len(alt) > 2:
                        names.add(alt)
                for a in el.find_all("a"):
                    text = a.get_text(strip=True)
                    if text and 2 < len(text) < 80:
                        names.add(text)

    return sorted(names)


def get_sponsors(homepage_url: str) -> list[str]:
    """Main entry point: return deduplicated sponsor names from a conference site."""
    candidate_pages = find_sponsor_page(homepage_url)
    if not candidate_pages:
        candidate_pages = [homepage_url]

    for page_url in candidate_pages[:5]:
        try:
            resp = requests.get(page_url, headers=HEADERS, timeout=12, verify=False)
            html = resp.text

            # Try JS API first (highest quality)
            names = try_js_api(page_url, html)
            if names and len(names) > 3:
                return names

            # Fallback to HTML parsing
            names = scrape_sponsors_from_html(html)
            if names and len(names) > 3:
                return names
        except Exception:
            continue

    return []


# ---------------------------------------------------------------------------
# ENRICHER — look up company info via DuckDuckGo + website meta tags
# ---------------------------------------------------------------------------

def ddg(query: str, n: int = 5) -> list[dict]:
    try:
        with DDGS() as d:
            return list(d.text(query, max_results=n))
    except Exception:
        time.sleep(2)
        return []


def fetch_meta_description(url: str) -> str:
    try:
        r = requests.get(url, headers=HEADERS, timeout=8, verify=False, allow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
        desc = (
            (soup.find("meta", property="og:description") or {}).get("content")
            or (soup.find("meta", attrs={"name": "description"}) or {}).get("content")
            or ""
        )
        return desc.strip()[:400]
    except Exception:
        return ""


def first_sentence(text: str) -> str:
    if not text:
        return "Not Found"
    text = re.sub(r'\s+', ' ', text).strip()
    m = re.match(r'^(.+?[.!?])(?:\s|$)', text)
    return m.group(1) if m else text[:200]


def parse_employee_number(size_str: str) -> int:
    """Convert employee size string to a number for sorting."""
    if not size_str or size_str == "Not Found":
        return -1
    nums = re.findall(r'[\d,]+', size_str.replace(",", ""))
    nums = [int(n) for n in nums if n.isdigit()]
    return max(nums) if nums else -1


def enrich(name: str) -> dict:
    result = {
        "company_name": name,
        "website": "Not Found",
        "industry": "Not Found",
        "summary": "Not Found",
        "hq_city": "Not Found",
        "hq_country": "Not Found",
        "employee_size": "Not Found",
    }

    # General search
    results = ddg(f"{name} company", 5)
    blob = " | ".join(r.get("title", "") + " " + r.get("body", "") for r in results)

    # Website
    skip_domains = ("linkedin", "facebook", "twitter", "crunchbase", "bloomberg",
                    "wikipedia", "glassdoor", "indeed", "youtube", "instagram", "yelp")
    for r in results:
        href = r.get("href", "")
        if href and not any(s in href for s in skip_domains):
            result["website"] = href.split("?")[0]
            break

    # Summary from meta description
    if result["website"] != "Not Found":
        meta = fetch_meta_description(result["website"])
        if meta and len(meta) > 30:
            result["summary"] = first_sentence(meta)

    if result["summary"] == "Not Found":
        for r in results:
            body = r.get("body", "").strip()
            if body and len(body) > 40:
                result["summary"] = first_sentence(body)
                break

    # Industry
    industry_results = ddg(f"{name} company industry sector what does it do", 4)
    industry_blob = blob + " | ".join(r.get("body", "") for r in industry_results)
    for pat in [
        r'(?:is an?|is the)\s+([\w\s&/-]{3,40}?)\s+(?:company|provider|platform|firm|leader|software)',
        r'(?:leading|top|global)\s+([\w\s&/-]{3,40}?)\s+(?:company|provider|platform)',
        r'specializes?\s+in\s+([\w\s&,/-]{3,50}?)(?:\.|,)',
        r'(artificial intelligence|machine learning|cloud computing|cybersecurity|data analytics|'
        r'fintech|healthcare IT|e-commerce|SaaS|enterprise software|robotics|networking|'
        r'semiconductor|consulting|financial services|telecommunications|logistics|'
        r'human resources|marketing technology|education technology)',
        r'Industry:\s*([\w\s&,/-]+?)(?:\||\.|\n)',
    ]:
        m = re.search(pat, industry_blob, re.IGNORECASE)
        if m:
            result["industry"] = m.group(1).strip(" .,|")[:60]
            break

    # HQ
    hq_results = ddg(f"{name} headquarters location city country", 4)
    hq_blob = blob + " | ".join(r.get("body", "") for r in hq_results)
    for pat in [
        r'headquartered?\s+in\s+([A-Z][a-z]+(?:[\s][A-Z][a-z]+)*),\s*([A-Z][a-z]+(?:[\s][A-Z][a-z]+)*|[A-Z]{2})',
        r'based\s+in\s+([A-Z][a-z]+(?:[\s][A-Z][a-z]+)*),\s*([A-Z][a-z]+(?:[\s][A-Z][a-z]+)*)',
        r'([A-Z][a-z]+(?:[\s][A-Z][a-z]+)*),\s*(United States|USA|UK|United Kingdom|Canada|Germany|France|India|Israel|Australia|Singapore|Japan|China|Netherlands|Sweden|Finland)',
    ]:
        m = re.search(pat, hq_blob)
        if m:
            result["hq_city"] = m.group(1).strip()
            result["hq_country"] = m.group(2).strip()
            break

    # Employees
    emp_results = ddg(f"{name} number of employees company size", 4)
    emp_blob = blob + " | ".join(r.get("body", "") for r in emp_results)
    for pat in [
        r'(\d[\d,]+)\s*[-–to]+\s*(\d[\d,]+)\s*employees',
        r'(\d[\d,]+)\+?\s*employees',
        r'(1-10|11-50|51-200|201-500|501-1,000|1,001-5,000|5,001-10,000|10,000\+)\s*employees',
        r'(?:over|more than|approximately|about)\s+(\d[\d,]+)\s*(?:employees|people)',
        r'(\d[\d,]+)\+?\s*(?:people|workers|staff)\s+(?:worldwide|globally)',
    ]:
        m = re.search(pat, emp_blob, re.IGNORECASE)
        if m:
            if m.lastindex and m.lastindex >= 2:
                try:
                    result["employee_size"] = f"{m.group(1)}-{m.group(2)} employees"
                except IndexError:
                    result["employee_size"] = m.group(1) + " employees"
            else:
                val = m.group(1).strip()
                result["employee_size"] = val if "employees" in val.lower() else val + " employees"
            break

    return result


# ---------------------------------------------------------------------------
# PIPELINE — runs in background thread, pushes progress via queue
# ---------------------------------------------------------------------------

def run_pipeline(job_id: str, homepage_url: str):
    q = JOBS[job_id]["queue"]

    def push(event: str, data: dict):
        q.put(f"event: {event}\ndata: {json.dumps(data)}\n\n")

    try:
        push("status", {"message": f"Finding sponsors page on {homepage_url}..."})
        names = get_sponsors(homepage_url)

        if not names:
            push("error", {"message": "Could not find any sponsor names on this site. Try pasting the direct sponsors page URL."})
            JOBS[job_id]["status"] = "error"
            return

        push("status", {"message": f"Found {len(names)} sponsors. Starting enrichment..."})
        push("total", {"total": len(names)})

        rows = []
        for i, name in enumerate(names, 1):
            push("progress", {"current": i, "total": len(names), "company": name})
            try:
                data = enrich(name)
            except Exception as e:
                data = {
                    "company_name": name, "website": "Not Found", "industry": "Not Found",
                    "summary": "Not Found", "hq_city": "Not Found",
                    "hq_country": "Not Found", "employee_size": "Not Found",
                }
            rows.append(data)
            time.sleep(0.6)

        # Sort by employee size descending
        rows.sort(key=lambda r: parse_employee_number(r["employee_size"]), reverse=True)

        # Build CSV
        fieldnames = ["company_name", "website", "industry", "summary", "hq_city", "hq_country", "employee_size"]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        JOBS[job_id]["csv"] = buf.getvalue()
        JOBS[job_id]["status"] = "done"

        push("done", {"total": len(rows)})

    except Exception as e:
        push("error", {"message": str(e)})
        JOBS[job_id]["status"] = "error"


# ---------------------------------------------------------------------------
# FLASK ROUTES
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/run", methods=["POST"])
def run():
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if not url.startswith("http"):
        url = "https://" + url

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"status": "running", "queue": queue.Queue(), "csv": None, "error": None}
    threading.Thread(target=run_pipeline, args=(job_id, url), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/stream/<job_id>")
def stream(job_id):
    if job_id not in JOBS:
        return jsonify({"error": "Job not found"}), 404

    def generate():
        q = JOBS[job_id]["queue"]
        while True:
            try:
                msg = q.get(timeout=30)
                yield msg
                if "event: done" in msg or "event: error" in msg:
                    break
            except queue.Empty:
                yield "event: ping\ndata: {}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/download/<job_id>")
def download(job_id):
    job = JOBS.get(job_id)
    if not job or not job.get("csv"):
        return jsonify({"error": "Not ready"}), 404

    buf = io.BytesIO(job["csv"].encode("utf-8"))
    buf.seek(0)
    from flask import send_file
    return send_file(buf, mimetype="text/csv", as_attachment=True,
                     download_name="sponsors_enriched.csv")


if __name__ == "__main__":
    app.run(debug=True)
