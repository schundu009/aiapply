import json
import re
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from .models import ATSPlatform, JobDescription


class JobFetcher:
    ATS_PATTERNS = {
        ATSPlatform.GREENHOUSE: [r"greenhouse\.io", r"boards\.greenhouse\.io", r"job-boards\.greenhouse\.io"],
        ATSPlatform.LEVER: [r"lever\.co", r"jobs\.lever\.co"],
        ATSPlatform.WORKDAY: [r"myworkdayjobs\.com", r"\.wd\d+\.myworkdayjobs\.com", r"workday\.com"],
        ATSPlatform.ASHBY: [r"ashbyhq\.com", r"jobs\.ashbyhq\.com"],
        ATSPlatform.EIGHTFOLD: [r"eightfold\.ai", r"\.eightfold\.ai"],
        ATSPlatform.ICIMS: [r"icims\.com", r"jobs\.icims\.com", r"careers-.*\.icims\.com"],
        ATSPlatform.TALEO: [r"taleo\.net", r"taleo\.com", r"oracle.*taleo"],
        ATSPlatform.SMARTRECRUITERS: [r"smartrecruiters\.com", r"jobs\.smartrecruiters\.com"],
        ATSPlatform.JOBVITE: [r"jobvite\.com", r"jobs\.jobvite\.com"],
        ATSPlatform.BAMBOOHR: [r"bamboohr\.com", r".*\.bamboohr\.com/careers"],
        ATSPlatform.BREEZYHR: [r"breezy\.hr", r".*\.breezy\.hr"],
        ATSPlatform.JAZZ: [r"jazz\.co", r".*\.jazz\.co", r"applytojob\.com"],
    }

    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            },
        )

    async def close(self):
        await self.client.aclose()

    def detect_ats(self, url: str) -> ATSPlatform:
        for platform, patterns in self.ATS_PATTERNS.items():
            for p in patterns:
                if re.search(p, url, re.I):
                    return platform
        return ATSPlatform.UNKNOWN

    # ──────────────────────────────────────────────
    # JSON-LD extraction (works on most career sites)
    # ──────────────────────────────────────────────

    def _extract_json_ld(self, html: str) -> dict | None:
        """Extract Schema.org JobPosting from JSON-LD scripts."""
        soup = BeautifulSoup(html, "html.parser")
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                # Could be a single object or an array
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("@type") == "JobPosting":
                            return item
                elif isinstance(data, dict):
                    if data.get("@type") == "JobPosting":
                        return data
                    # Check @graph
                    for item in data.get("@graph", []):
                        if isinstance(item, dict) and item.get("@type") == "JobPosting":
                            return item
            except (json.JSONDecodeError, TypeError):
                continue
        return None

    def _parse_json_ld(self, ld: dict) -> dict:
        """Convert JSON-LD JobPosting into flat metadata dict."""
        meta = {}

        meta["title"] = ld.get("title") or ld.get("identifier", {}).get("name", "")

        # Company
        org = ld.get("hiringOrganization") or {}
        company = org.get("name", "") if isinstance(org, dict) else str(org)
        # Clean company names like "2100 NVIDIA USA" → "NVIDIA"
        company = re.sub(r'^\d+\s+', '', company).strip()
        company = re.sub(r'\s+(USA|Inc|Corp|LLC|Ltd|Co|Company)\.?$', '', company, flags=re.I).strip()
        meta["company"] = company

        # Location
        loc = ld.get("jobLocation")
        if isinstance(loc, dict):
            addr = loc.get("address", {})
            if isinstance(addr, dict):
                locality = addr.get("addressLocality", "")
                region = addr.get("addressRegion", "")
                country = addr.get("addressCountry", "")
                if locality:
                    meta["location"] = locality
                elif region:
                    meta["location"] = f"{region}, {country}" if country else region
        elif isinstance(loc, list) and loc:
            first = loc[0]
            if isinstance(first, dict):
                addr = first.get("address", {})
                meta["location"] = addr.get("addressLocality", "")

        # Dates
        meta["date_posted"] = ld.get("datePosted", "")
        meta["valid_through"] = ld.get("validThrough", "")

        # Employment type
        emp = ld.get("employmentType", "")
        if isinstance(emp, list):
            emp = emp[0] if emp else ""
        meta["employment_type"] = emp.replace("_", "-").title() if emp else ""

        # Salary
        salary = ld.get("baseSalary") or ld.get("estimatedSalary")
        if isinstance(salary, dict):
            value = salary.get("value", {})
            if isinstance(value, dict):
                lo = value.get("minValue", "")
                hi = value.get("maxValue", "")
                currency = salary.get("currency", "USD")
                if lo and hi:
                    meta["salary_range"] = f"${int(lo):,} - ${int(hi):,} {currency}"
                elif lo:
                    meta["salary_range"] = f"${int(lo):,}+ {currency}"

        # Description
        desc = ld.get("description", "")
        if desc:
            # Strip HTML tags from description
            desc_soup = BeautifulSoup(desc, "html.parser")
            meta["description_text"] = desc_soup.get_text(separator="\n", strip=True)
        else:
            meta["description_text"] = ""

        # Skills / qualifications
        meta["skills"] = ld.get("skills", "")
        meta["qualifications"] = ld.get("qualifications", "")
        meta["experience_requirements"] = ld.get("experienceRequirements", "")

        return meta

    # ──────────────────────────────────────────────
    # Workday API (direct JSON API)
    # ──────────────────────────────────────────────

    async def _fetch_workday_api(self, url: str) -> dict | None:
        """Fetch job data from Workday's public JSON API."""
        # URL: https://company.wd5.myworkdayjobs.com/SiteName/job/Location/Title_ID
        # API: https://company.wd5.myworkdayjobs.com/wday/cxs/company/SiteName/job/Location/Title_ID
        parsed = urlparse(url)
        host = parsed.hostname or ""
        path = parsed.path.rstrip("/")

        # Extract company slug from hostname: nvidia.wd5.myworkdayjobs.com → nvidia
        m = re.match(r'^([\w-]+)\.wd\d+\.myworkdayjobs\.com$', host)
        if not m:
            return None
        company_slug = m.group(1)

        # Build API path: insert /wday/cxs/{company}/ before the site path
        # /SiteName/job/... → /wday/cxs/company/SiteName/job/...
        api_path = f"/wday/cxs/{company_slug}{path}"

        # Strip query params
        api_url = f"https://{host}{api_path}"

        try:
            resp = await self.client.get(api_url, headers={"Accept": "application/json"})
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return None

    def _parse_workday_api(self, data: dict) -> dict:
        """Extract structured data from Workday API response."""
        meta = {}
        jd = data.get("jobPostingInfo", {})

        meta["title"] = jd.get("title", "")
        meta["location"] = jd.get("location", "")
        meta["date_posted"] = jd.get("postedOn", "")
        meta["time_type"] = jd.get("timeType", "")
        meta["job_req_id"] = jd.get("jobReqId", "")

        # Additional locations
        addl = jd.get("additionalLocations", [])
        if addl:
            meta["additional_locations"] = addl

        # Job description (HTML)
        desc_html = jd.get("jobDescription", "")
        if desc_html:
            soup = BeautifulSoup(desc_html, "html.parser")
            meta["description_text"] = soup.get_text(separator="\n", strip=True)
            meta["description_html"] = desc_html

        # Company from hiringOrganization
        org = data.get("hiringOrganization", {})
        if org.get("name"):
            company = org["name"]
            company = re.sub(r'^\d+\s+', '', company).strip()
            company = re.sub(r'\s+(USA|Inc|Corp|LLC|Ltd|Co|Company)\.?$', '', company, flags=re.I).strip()
            meta["company"] = company

        return meta

    # ──────────────────────────────────────────────
    # Playwright fallback (for JS-rendered pages)
    # ──────────────────────────────────────────────

    async def _fetch_with_playwright(self, url: str) -> str:
        """Render page with headless browser and return HTML."""
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(url, wait_until="networkidle", timeout=20000)
                html = await page.content()
                await browser.close()
                return html
        except Exception:
            return ""

    # ──────────────────────────────────────────────
    # Company / title extraction (fallback)
    # ──────────────────────────────────────────────

    def _extract_company_name(self, soup: BeautifulSoup, url: str) -> str:
        og_site = soup.find("meta", property="og:site_name")
        if og_site and og_site.get("content", "").strip():
            return og_site["content"].strip()

        if "greenhouse.io" in url:
            parts = url.split("/")
            if len(parts) > 3:
                return parts[3].replace("-", " ").title()
        if "lever.co" in url:
            parts = url.split("/")
            if len(parts) > 3:
                return parts[3].replace("-", " ").title()

        for meta_name in ["author", "application-name"]:
            meta = soup.find("meta", attrs={"name": meta_name})
            if meta and meta.get("content", "").strip():
                return meta["content"].strip()

        title_tag = soup.find("title")
        if title_tag:
            title_text = title_tag.get_text(strip=True)
            for sep in [" - ", " | ", " at ", " @ "]:
                if sep in title_text:
                    parts = title_text.split(sep)
                    if len(parts) >= 2:
                        if sep in (" at ", " @ "):
                            return parts[-1].strip()
                        return parts[-1].strip()

        domain = urlparse(url).hostname or ""
        domain = re.sub(r'^(www\.|jobs\.|careers\.)', '', domain)
        domain = re.sub(r'\.(com|io|co|org|net|jobs).*$', '', domain)
        if domain and domain not in ("greenhouse", "lever", "workday", "ashbyhq"):
            return domain.replace("-", " ").replace(".", " ").title()

        return "Unknown"

    def _extract_job_title(self, soup: BeautifulSoup) -> str:
        h1 = soup.select_one("h1")
        if h1:
            text = h1.get_text(strip=True)
            if text and len(text) < 200:
                return text
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content", "").strip():
            return og_title["content"].strip()
        title_tag = soup.find("title")
        if title_tag:
            title_text = title_tag.get_text(strip=True)
            for sep in [" - ", " | ", " at ", " @ "]:
                if sep in title_text:
                    return title_text.split(sep)[0].strip()
            return title_text
        for selector in [".job-title", ".posting-headline h2", "[data-qa='job-title']",
                         ".job-header h2", ".position-title"]:
            el = soup.select_one(selector)
            if el:
                return el.get_text(strip=True)
        return "Unknown"

    # ──────────────────────────────────────────────
    # Main fetch method
    # ──────────────────────────────────────────────

    async def fetch_job(self, url: str) -> JobDescription:
        platform = self.detect_ats(url)

        # Try httpx first, fall back to Playwright on 403/bot detection
        html = ""
        use_playwright = False

        try:
            resp = await self.client.get(url)
            resp.raise_for_status()
            html = resp.text
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (403, 401, 429):
                # Bot detection - use Playwright instead
                print(f"[JobFetcher] Got {e.response.status_code}, falling back to Playwright for {url}")
                use_playwright = True
            else:
                raise

        if use_playwright or not html:
            pw_html = await self._fetch_with_playwright(url)
            if pw_html:
                html = pw_html
            else:
                raise Exception(f"Failed to fetch job page: {url}")

        # ── Strategy 1: JSON-LD (available without JS rendering) ──
        json_ld = self._extract_json_ld(html)
        ld_meta = self._parse_json_ld(json_ld) if json_ld else {}

        # ── Strategy 2: Workday API (for myworkdayjobs.com sites) ──
        wd_meta = {}
        if platform == ATSPlatform.WORKDAY:
            wd_data = await self._fetch_workday_api(url)
            if wd_data:
                wd_meta = self._parse_workday_api(wd_data)

        # ── Strategy 3: HTML text extraction ──
        soup = BeautifulSoup(html, "lxml")
        for el in soup(["script", "style", "nav", "footer"]):
            el.decompose()
        html_text = soup.get_text(separator="\n", strip=True)

        # ── Strategy 4: Playwright fallback if all above yield thin content ──
        best_description = (
            wd_meta.get("description_text", "")
            or ld_meta.get("description_text", "")
            or html_text
        )

        if len(best_description) < 200:
            pw_html = await self._fetch_with_playwright(url)
            if pw_html:
                pw_soup = BeautifulSoup(pw_html, "lxml")
                for el in pw_soup(["script", "style", "nav", "footer"]):
                    el.decompose()
                pw_text = pw_soup.get_text(separator="\n", strip=True)
                if len(pw_text) > len(best_description):
                    html_text = pw_text
                    best_description = pw_text
                    soup = pw_soup  # use rendered soup for title/company extraction

        # ── Merge all sources (prefer specific APIs over HTML scraping) ──
        title = (
            wd_meta.get("title")
            or ld_meta.get("title")
            or self._extract_job_title(soup)
        )
        company = (
            wd_meta.get("company")
            or ld_meta.get("company")
            or self._extract_company_name(soup, url)
        )
        location = (
            wd_meta.get("location")
            or ld_meta.get("location")
            or ""
        )

        # Build comprehensive raw_text for AI analysis
        raw_parts = []
        raw_parts.append(f"Job Title: {title}")
        raw_parts.append(f"Company: {company}")
        if location:
            raw_parts.append(f"Location: {location}")
        if ld_meta.get("date_posted"):
            raw_parts.append(f"Date Posted: {ld_meta['date_posted']}")
        if ld_meta.get("employment_type"):
            raw_parts.append(f"Employment Type: {ld_meta['employment_type']}")
        if ld_meta.get("salary_range"):
            raw_parts.append(f"Salary Range: {ld_meta['salary_range']}")
        addl_locs = wd_meta.get("additional_locations", [])
        if addl_locs:
            raw_parts.append(f"Additional Locations: {', '.join(addl_locs)}")
        raw_parts.append("")
        raw_parts.append("--- JOB DESCRIPTION ---")
        raw_parts.append(best_description)

        # Add HTML body text if it has additional content
        if html_text and html_text != best_description and len(html_text) > 100:
            raw_parts.append("")
            raw_parts.append("--- ADDITIONAL PAGE CONTENT ---")
            raw_parts.append(html_text)

        raw_text = "\n".join(raw_parts)

        # Extract technologies
        tech_pattern = (
            r"\b("
            r"AWS|GCP|Azure|Kubernetes|Docker|Terraform|Ansible|Python|Go|Linux|"
            r"Java|JavaScript|TypeScript|React|Node\.?js|Rust|C\+\+|Ruby|Scala|"
            r"PostgreSQL|MySQL|MongoDB|Redis|Kafka|RabbitMQ|Elasticsearch|"
            r"Jenkins|GitHub Actions|GitLab CI|CircleCI|ArgoCD|FluxCD|"
            r"Prometheus|Grafana|Datadog|Splunk|CloudWatch|"
            r"Helm|Pulumi|CloudFormation|CDK|Vagrant|"
            r"EKS|ECS|Lambda|S3|RDS|DynamoDB|Aurora|SQS|SNS|"
            r"Istio|Envoy|Nginx|HAProxy|"
            r"Spark|Airflow|dbt|Snowflake|BigQuery|Redshift|"
            r"CUDA|TensorRT|PyTorch|TensorFlow|Slurm|NCCL|"
            r"gRPC|REST|GraphQL|WebSockets|HTML5|CSS3|Bootstrap|jQuery|"
            r"Bash|Shell|Git|JIRA|Confluence"
            r")\b"
        )
        techs = re.findall(tech_pattern, raw_text, re.I)
        keywords = list(set(t.strip() for t in techs))

        # Build the job description
        job = JobDescription(
            url=url,
            ats_platform=platform,
            company_name=company,
            job_title=title,
            raw_text=raw_text[:20000],
            keywords=keywords,
            technologies=keywords,
        )

        # Pre-populate fields from structured data
        if location:
            job.location = location
        if ld_meta.get("salary_range"):
            job.salary_range = ld_meta["salary_range"]
        if ld_meta.get("date_posted"):
            job.posting_date = ld_meta["date_posted"]
        if ld_meta.get("employment_type"):
            job.job_type = ld_meta["employment_type"]

        return job
