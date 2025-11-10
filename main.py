import requests
import os
import re
from bs4 import BeautifulSoup
import Levenshtein
import argparse
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from functools import lru_cache
from urllib.parse import unquote
import json
import time
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# Global flag for production mode
production_mode = False

# Global HTTP session with retries
session = requests.Session()
retry_strategy = Retry(
    total=3,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "HEAD"],
    backoff_factor=0.5,
)
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("http://", adapter)
session.mount("https://", adapter)
DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

# Default networking knobs (overridable via CLI)
REQUEST_TIMEOUT = 15

# Optional integrations and runtime knobs
UNPAYWALL_EMAIL: str | None = None
SCIHUB_DOMAINS: list[str] | None = None

# Simple global rate limiter (shared among threads)
class RateLimiter:
    def __init__(self, rps: float):
        self.interval = 0.0 if rps <= 0 else 1.0 / float(rps)
        self.lock = threading.Lock()
        self.last = 0.0

    def wait(self):
        if self.interval <= 0:
            return
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.last
            if elapsed < self.interval:
                time.sleep(self.interval - elapsed)
                now = time.monotonic()
            self.last = now

RATE_LIMITER: RateLimiter | None = None

def http_get(url: str, *, timeout: float | None = None, headers=None, **kwargs):
    if RATE_LIMITER is not None:
        RATE_LIMITER.wait()
    return session.get(url, timeout=timeout or REQUEST_TIMEOUT, headers=headers or DEFAULT_HEADERS, **kwargs)

# Persistent download history
HISTORY_PATH = os.path.join(os.getcwd(), "Downloads_pdf", "sample", ".history.json")
_history_lock = threading.Lock()
_history_cache = None

def _ensure_history_loaded():
    global _history_cache
    if _history_cache is None:
        os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
        if os.path.exists(HISTORY_PATH):
            try:
                with open(HISTORY_PATH, 'r', encoding='utf-8') as f:
                    _history_cache = json.load(f)
            except Exception:
                _history_cache = {}
        else:
            _history_cache = {}

def history_get(doi: str):
    _ensure_history_loaded()
    return _history_cache.get(doi)

def history_set(doi: str, record: dict):
    _ensure_history_loaded()
    with _history_lock:
        _history_cache[doi] = record
        try:
            with open(HISTORY_PATH, 'w', encoding='utf-8') as f:
                json.dump(_history_cache, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

def configure_http(retries: int | None = None, backoff: float | None = None, timeout: float | None = None, rps: float | None = None):
    global REQUEST_TIMEOUT, RATE_LIMITER
    if timeout is not None and timeout > 0:
        REQUEST_TIMEOUT = timeout
    if rps is not None:
        RATE_LIMITER = RateLimiter(rps)
    if retries is not None or backoff is not None:
        r = retries if retries is not None else retry_strategy.total
        b = backoff if backoff is not None else retry_strategy.backoff_factor
        new_strategy = Retry(
            total=r,
            status_forcelist=retry_strategy.status_forcelist,
            allowed_methods=retry_strategy.allowed_methods,
            backoff_factor=b,
        )
        new_adapter = HTTPAdapter(max_retries=new_strategy)
        session.mount("http://", new_adapter)
        session.mount("https://", new_adapter)

def get_official_title_from_doi(doi):
    """
    Fetches the official title of an article from the CrossRef API.
    This is considered the ground truth for title comparison.
    """
    try:
        crossref_url = f"https://api.crossref.org/works/{doi}"
        response = http_get(crossref_url, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        title = data.get('message', {}).get('title', [None])[0]
        if title:
            if not production_mode:
                print(f"Fetched official title from CrossRef: '{title}'")
            return title.strip()
    except Exception as e:
        if not production_mode:
            print(f"Error fetching official title from CrossRef API: {e}")
    return None

def verify_title_similarity(official_title, downloaded_title):
    """
    Compares the official title with the downloaded title using Levenshtein distance.
    Returns True if they are similar enough, False otherwise.
    """
    if not official_title or not downloaded_title:
        if not production_mode:
            print("Warning: Cannot verify title similarity because one of the titles is missing.")
        return False # Cannot verify

    similarity = Levenshtein.ratio(official_title.lower(), downloaded_title.lower())
    if not production_mode:
        print(f"Title similarity ratio: {similarity:.2f}")
    if similarity < 0.8:
        if not production_mode:
            print(f"Warning: Potential title mismatch!")
            print(f"  - Official Title:   '{official_title}'")
            print(f"  - Downloaded Title: '{downloaded_title}'")
        return False
    
    if not production_mode:
        print("Titles match. Download is likely correct.")
    return True

def ExtractReferences(response_text):
    """
    Extract references from the response text and convert them to DOI format.
    """
    references = set()
    try:
        soup = BeautifulSoup(response_text, 'html.parser')
        # 1. 链接中的DOI
        for link in soup.find_all('a', href=True):
            href = link['href']
            if 'doi.org/' in href:
                doi = href.split('doi.org/')[-1].split('?')[0].split('#')[0]
                references.add(doi)
        # 2. meta标签中的DOI
        for meta in soup.find_all('meta'):
            if meta.get('name', '').lower() == 'citation_doi' and meta.get('content'):
                references.add(meta['content'])
        # 3. 参考文献区块中的DOI（常见于期刊页面）
        ref_blocks = soup.find_all(['div', 'li', 'span', 'p'], string=re.compile(r'10\.\d{4,9}/[-._;()/:A-Z0-9]+', re.I))
        for block in ref_blocks:
            found = re.findall(r'10\.\d{4,9}/[-._;()/:A-Z0-9]+', block.get_text(), re.I)
            for doi in found:
                references.add(doi)
        # 4. 全文正则匹配DOI（兜底方案）
        found = re.findall(r'10\.\d{4,9}/[-._;()/:A-Z0-9]+', response_text, re.I)
        for doi in found:
            references.add(doi)
        # 规范化与去重
        references = list({normalize_doi(d) for d in references if d})
        if not production_mode:
            print("Extracted References (enhanced):", references)
    except Exception as e:
        if not production_mode:
            print(f"Error extracting references: {e}")
    return references

def normalize_doi(doi: str) -> str:
    """Normalize DOI text for consistency and deduplication."""
    if not doi:
        return doi
    doi = unquote(doi).strip()
    # 去掉前缀如 doi: 或 https://doi.org/
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", doi, flags=re.I)
    # 空白与末尾标点
    doi = doi.strip().strip(' .;')
    return doi

# CrossRef metadata cache
@lru_cache(maxsize=1024)
def get_crossref_metadata(doi: str) -> dict | None:
    try:
        url = f"https://api.crossref.org/works/{doi}"
        resp = http_get(url)
        resp.raise_for_status()
        return resp.json().get('message', {})
    except Exception:
        return None

DEFAULT_YOUNG_KEYWORDS = [
    'student', 'phd', 'doctoral', 'candidate', 'undergraduate', 'master',
    '硕士', '博士', '博后', '研究生', '学生', '博士生', '博士候选人', '本科生'
]

def paper_has_young_author(doi: str, keywords: list[str] | None = None) -> bool:
    """Heuristic: an article is considered to have a 'young author' if any author's affiliation
    contains youth-related keywords (student/PhD/研究生/博士等)."""
    kw = [k.lower() for k in (keywords or DEFAULT_YOUNG_KEYWORDS)]
    meta = get_crossref_metadata(doi)
    if not meta:
        return False
    authors = meta.get('author') or []
    for a in authors:
        affs = a.get('affiliation') or []
        # affiliation is list of {name: str}
        for aff in affs:
            name = (aff.get('name') or '').lower()
            if any(k in name for k in kw):
                return True
    return False

@lru_cache(maxsize=512)
def GetTitleFromDOI(DOI):
    doi_url = f"https://doi.org/{DOI}"
    headers = DEFAULT_HEADERS

    # Try fetching title from DOI website
    try:
        response = http_get(doi_url, headers=headers, timeout=REQUEST_TIMEOUT)
        if not production_mode:
            print("DOI URL Status Code:", response.status_code)
        if response.status_code == 200:
            if not production_mode:
                print("DOI URL Response Text:", response.text[:500])  # Print first 500 characters of the response
            references = ExtractReferences(response.text)  # Extract references
            soup = BeautifulSoup(response.text, 'html.parser')
            title_tag = soup.find('title')
            if not production_mode:
                print("Extracted Title Tag:", title_tag)
            if title_tag and title_tag.text:
                return title_tag.text.strip(), references
    except Exception as e:
        if not production_mode:
            print(f"Error fetching title from DOI: {e}")

    # Fallback: Try CrossRef API
    try:
        crossref_url = f"https://api.crossref.org/works/{DOI}"
        response = http_get(crossref_url, headers=headers, timeout=REQUEST_TIMEOUT)
        if not production_mode:
            print("CrossRef API Status Code:", response.status_code)
        if response.status_code == 200:
            data = response.json()
            title = data.get('message', {}).get('title', [None])[0]
            references = [item.get('DOI') for item in data.get('message', {}).get('reference', []) if item.get('DOI')]
            if title:
                if not production_mode:
                    print("Extracted Title from CrossRef:", title)
                    print("Extracted References from CrossRef:", references)
                return title, references
    except Exception as e:
        if not production_mode:
            print(f"Error fetching title from CrossRef API: {e}")

    # Optional fallback: Try user-provided Sci-Hub mirror pages (no hardcoded domains)
    try:
        if SCIHUB_DOMAINS:
            for mirror in SCIHUB_DOMAINS:
                sci_hub_url = f"{mirror.rstrip('/')}/{DOI}"
                response = http_get(sci_hub_url, headers=headers, timeout=REQUEST_TIMEOUT)
                if not production_mode:
                    print("Sci-Hub mirror Status Code:", response.status_code, sci_hub_url)
                if response.status_code == 200:
                    references = ExtractReferences(response.text)
                    soup = BeautifulSoup(response.text, 'html.parser')
                    title_tag = soup.find('title')
                    if title_tag and title_tag.text:
                        return title_tag.text.strip(), references
    except Exception as e:
        if not production_mode:
            print(f"Error fetching title from configured Sci-Hub mirror(s): {e}")

    return "unknown_title", []

def get_recent_dois_from_crossref(query="physics", num_articles=5):
    """
    Get recent article DOIs from CrossRef API based on a query.
    """
    try:
        # Query CrossRef API for recent works related to the query, sorted by publication date
        url = f"https://api.crossref.org/works?query.bibliographic={query}&sort=published&order=desc&rows={num_articles}"
        response = http_get(url, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()  # Raise an exception for bad status codes
        data = response.json()
        
        dois = [item['DOI'] for item in data['message']['items']]
        if not production_mode:
            print(f"Found {len(dois)} recent DOIs from CrossRef for query '{query}': {dois}")
        return dois
    except Exception as e:
        if not production_mode:
            print(f"Error fetching recent DOIs from CrossRef: {e}")
        return []

def get_recent_dois_from_arxiv(category="physics", num_articles=5):
    """
    Get recent article DOIs from arXiv.
    Note: arXiv papers might not have a DOI immediately. This function gets arXiv IDs.
    The rest of the script can try to find a DOI or download directly if possible.
    """
    try:
        # Scrape the 'new' page for the given category
        url = f"https://arxiv.org/list/{category}/new"
        headers = DEFAULT_HEADERS
        response = http_get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Find links to the abstracts
        arxiv_ids = []
        for dt in soup.find_all('dt'):
            span = dt.find('span', class_='list-identifier')
            if span:
                arxiv_id_tag = span.find('a', title='Abstract')
                if arxiv_id_tag:
                    arxiv_ids.append(arxiv_id_tag.text.replace('arXiv:', ''))
            if len(arxiv_ids) >= num_articles:
                break
        
        if not production_mode:
            print(f"Found {len(arxiv_ids)} recent arXiv IDs from category '{category}': {arxiv_ids}")
        return arxiv_ids
    except Exception as e:
        if not production_mode:
            print(f"Error fetching recent DOIs from arXiv: {e}")
        return []

def get_recent_dois_from_pubmed(query="cancer", num_articles=5):
    """
    Get recent article DOIs from PubMed.
    """
    try:
        # Step 1: Search for PubMed IDs (PMIDs)
        search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        search_params = {
            'db': 'pubmed',
            'term': query,
            'sort': 'pub_date',
            'retmax': num_articles,
            'retmode': 'json'
        }
        headers = DEFAULT_HEADERS
        response = http_get(search_url, params=search_params, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        search_data = response.json()
        pmids = search_data.get('esearchresult', {}).get('idlist', [])
        
        if not pmids:
            if not production_mode:
                print(f"No articles found on PubMed for query '{query}'.")
            return []

        # Step 2: Fetch details for the PMIDs to get the DOIs
        summary_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
        summary_params = {
            'db': 'pubmed',
            'id': ','.join(pmids),
            'retmode': 'json'
        }
        response = http_get(summary_url, params=summary_params, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        summary_data = response.json()
        
        dois = []
        results = summary_data.get('result', {})
        for pmid in pmids:
            article_info = results.get(pmid, {})
            article_ids = article_info.get('articleids', [])
            for article_id in article_ids:
                if article_id.get('idtype') == 'doi':
                    dois.append(article_id.get('value'))
                    break # Found DOI, move to next PMID
        
        if not production_mode:
            print(f"Found {len(dois)} recent DOIs from PubMed for query '{query}': {dois}")
        return dois
    except Exception as e:
        if not production_mode:
            print(f"Error fetching recent DOIs from PubMed: {e}")
        return []

def get_recent_dois_from_semantic_scholar(query="quantum computing", num_articles=5):
    """
    Get recent article DOIs from Semantic Scholar.
    Note: The API's default sort is by relevance, which often includes recent papers.
    """
    try:
        url = "https://api.semanticscholar.org/graph/v1/paper/search"
        params = {
            'query': query,
            'limit': num_articles,
            'fields': 'externalIds'
        }
        headers = DEFAULT_HEADERS
        response = http_get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        
        dois = []
        papers = data.get('data', [])
        for paper in papers:
            external_ids = paper.get('externalIds', {})
            if 'DOI' in external_ids:
                dois.append(external_ids['DOI'])
        
        if not production_mode:
            print(f"Found {len(dois)} DOIs from Semantic Scholar for query '{query}': {dois}")
        return dois
    except Exception as e:
        if not production_mode:
            print(f"Error fetching DOIs from Semantic Scholar: {e}")
        return []

def get_citing_dois_from_semantic_scholar(doi: str, limit: int = 20, offset: int = 0) -> list[str]:
    """Fetch DOIs of works that cite the given DOI via Semantic Scholar Graph API.
    Returns a list of DOIs (may contain None/empty filtered out).

    Endpoint: /graph/v1/paper/DOI:{doi}/citations?fields=externalIds
    """
    try:
        base = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{requests.utils.quote(doi)}"
        url = f"{base}/citations"
        params = {
            'fields': 'externalIds',
            'limit': max(1, min(200, int(limit))),
            'offset': max(0, int(offset)),
        }
        headers = DEFAULT_HEADERS
        response = http_get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        dois: list[str] = []
        for item in data.get('data', []) or []:
            citing = (item or {}).get('citingPaper') or {}
            ext = citing.get('externalIds') or {}
            d = ext.get('DOI')
            if d:
                dois.append(d)
        if not production_mode:
            print(f"Found {len(dois)} citing DOIs from Semantic Scholar for '{doi}': {dois}")
        return dois
    except Exception as e:
        if not production_mode:
            print(f"Error fetching citing DOIs from Semantic Scholar for {doi}: {e}")
        return []

def get_pdf_from_publisher(doi):
    """
    Tries to find the direct PDF download link from the publisher's website.
    This method is most effective when run from an academic network (e.g., via University VPN).
    """
    try:
        landing_url = f"https://doi.org/{doi}"
        if not production_mode:
            print(f"Attempting to find PDF via publisher page: {landing_url}")
        
        headers = DEFAULT_HEADERS
        response = http_get(landing_url, headers=headers, allow_redirects=True, timeout=20)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # --- Heuristics to find the PDF link ---
        # This may need to be expanded for different publishers
        for link in soup.find_all('a', href=True):
            href = link['href']
            # Rule 1: Direct link to a .pdf file
            if href.lower().endswith('.pdf'):
                if not production_mode:
                    print(f"Found potential PDF link (ends with .pdf): {href}")
                # Handle relative URLs
                return requests.compat.urljoin(response.url, href)
            
            # Rule 2: Link contains '/pdf/' which is common (e.g., Wiley, Springer)
            if '/pdf/' in href or '/content/pdf/' in href:
                if not production_mode:
                    print(f"Found potential PDF link (contains /pdf/): {href}")
                return requests.compat.urljoin(response.url, href)

            # Rule 3: Link text suggests it's a PDF download
            if 'pdf' in link.text.lower() or 'full text' in link.text.lower():
                if not production_mode:
                    print(f"Found potential PDF link (text match): {href}")
                return requests.compat.urljoin(response.url, href)

        if not production_mode:
            print("Could not find a direct PDF link on the publisher page.")
        return None

    except Exception as e:
        if not production_mode:
            print(f"Error fetching from publisher page: {e}")
        return None

# function: Download English-paper by DOI
def GetDownloadUrl(doi, scihub_domains: list[str] | None = None):
    """Resolve a PDF URL via user-supplied Sci-Hub style mirrors.

    为减少仓库中出现潜在敏感或存在合规风险的硬编码镜像，本函数不再提供任何默认域名。
    如果调用方未提供 scihub_domains / 未在全局配置 SCIHUB_DOMAINS，则直接抛出 ConnectionError。
    传入的域名列表将按顺序尝试，遇到可解析页面再利用 parse_scihub_pdf_url 提取 PDF。
    """
    base_urls = scihub_domains or SCIHUB_DOMAINS or []
    if not base_urls:
        raise ConnectionError("No Sci-Hub mirrors supplied. Provide --scihub-domains to enable this fallback.")
    for base_url in base_urls:
        url = f"{base_url}/{doi}"
        try:
            if not production_mode:
                print(f"Trying URL: {url}")
            r = http_get(url, timeout=10, headers=DEFAULT_HEADERS)
            if r.status_code == 200 and r.text:
                # Try to parse direct PDF URL from Sci-Hub page
                pdf = parse_scihub_pdf_url(r.text, r.url)
                if pdf:
                    return pdf
                return url
            else:
                if not production_mode:
                    print(f"Failed with status code: {r.status_code}")
        except requests.exceptions.RequestException as e:
            if not production_mode:
                print(f"Error accessing {url}: {e}")
    raise ConnectionError("All Sci-Hub domains failed.")

def parse_scihub_pdf_url(html: str, base_url: str) -> str | None:
    """Parse a likely PDF url from Sci-Hub HTML.
    Heuristics:
      - <iframe src="...pdf"> or <embed src="...pdf">
      - citation pdf links in <a> tags
    """
    try:
        soup = BeautifulSoup(html, 'html.parser')
        # iframe/pdf
        for tag in soup.find_all(['iframe', 'embed'], src=True):
            src = tag.get('src')
            if not src:
                continue
            if src.lower().endswith('.pdf') or 'pdf' in src.lower():
                return requests.compat.urljoin(base_url, src)
        # a href pdf
        for a in soup.find_all('a', href=True):
            href = a['href']
            if href.lower().endswith('.pdf') or 'pdf' in href.lower():
                return requests.compat.urljoin(base_url, href)
    except Exception:
        return None
    return None

def get_pdf_from_unpaywall(doi: str, email: str | None) -> str | None:
    """Try obtain OA PDF via Unpaywall API using a contact email.
    Returns direct PDF URL if available, else None.
    """
    if not email:
        return None
    try:
        api = f"https://api.unpaywall.org/v2/{requests.utils.quote(doi)}"
        params = {"email": email}
        r = http_get(api, params=params, headers={"Accept": "application/json", **DEFAULT_HEADERS}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        # Prefer best_oa_location.url_for_pdf, fallback to any oa_locations
        def pick(loc):
            if not isinstance(loc, dict):
                return None
            return loc.get("url_for_pdf") or loc.get("url")
        pdf = pick(data.get("best_oa_location"))
        if pdf:
            if not production_mode:
                print(f"Unpaywall best OA PDF: {pdf}")
            return pdf
        for loc in data.get("oa_locations", []) or []:
            pdf = pick(loc)
            if pdf:
                if not production_mode:
                    print(f"Unpaywall OA PDF: {pdf}")
                return pdf
    except Exception as e:
        if not production_mode:
            print(f"Unpaywall lookup failed: {e}")
    return None

def download_cited_papers_concurrent(target_doi: str, rows: int = 10, max_workers: int = 4):
    """Download papers that cite the target DOI, concurrently, into folder 'cited'."""
    target_doi = normalize_doi(target_doi)
    citing = get_citing_dois_from_semantic_scholar(target_doi, limit=rows)
    if not citing:
        print(f"No citing DOIs found for: {target_doi}")
        return
    subdir = "cited"
    if production_mode:
        print(f"Scheduling {len(citing)} citing downloads -> folder '{subdir}' with {max_workers} workers")
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(download_and_process_doi, d, subdir): d for d in citing}
        for fut in as_completed(future_map):
            d = future_map[fut]
            try:
                fut.result()
            except Exception as e:
                if not production_mode:
                    print(f"Error downloading citing DOI {d}: {e}")

def DownloadFileByUrl(DownloadUrl, FileTitle, subdirectory="main"):
    """
    Downloads a file from a URL and saves it.
    Returns the full path to the saved file, or None if download fails.
    """
    if not DownloadUrl or 'http' not in DownloadUrl:
        if not production_mode:
            print("Invalid download URL provided.")
        return None

    # Sanitize the file name to remove illegal characters
    sane_title = re.sub(r'[\\/:*?"<>|#&]', '_', FileTitle)
    # Prevent extremely long filenames
    sane_title = (sane_title[:150] + '..') if len(sane_title) > 150 else sane_title
    file_name = f"{sane_title}.pdf"

    # Ensure the save path exists
    save_path = os.path.join(os.getcwd(), "Downloads_pdf", "sample", subdirectory)
    os.makedirs(save_path, exist_ok=True)
    full_path = os.path.join(save_path, file_name)

    try:
        if not production_mode:
            print(f"Downloading from: {DownloadUrl}")
        r = http_get(DownloadUrl, timeout=60, headers=DEFAULT_HEADERS) # Increased timeout for large files
        r.raise_for_status()

        # Crucial check: Ensure we are actually downloading a PDF
        content_type = (r.headers.get('Content-Type') or '').lower()
        is_pdf = ('pdf' in content_type) or (r.content[:4] == b'%PDF')
        if not is_pdf:
            if not production_mode:
                print(f"Error: The content at the URL is not a PDF. Content-Type: {r.headers.get('Content-Type')}")
                print("Skipping download.")
            return None

        with open(full_path, "wb") as code:
            code.write(r.content)
        
        if production_mode:
            print(f"Downloaded: {file_name}")
        else:
            print(f"File '{file_name}' has been downloaded successfully to '{save_path}'.")
        return full_path
    except requests.exceptions.RequestException as e:
        if not production_mode:
            print(f"Failed to download file: {e}")
        return None

def download_and_process_doi(doi, subdirectory="main"):
    """Helper function to get title, download, verify, and process one DOI."""
    if not production_mode:
        print(f"\n--- Processing DOI: {doi} ---")
    
    # 1. Get the official title from CrossRef as the ground truth
    official_title = get_official_title_from_doi(doi)
    if not official_title:
        if not production_mode:
            print("Could not retrieve official title. Aborting processing for this DOI.")
        return [] # Return empty list of references

    # 2. Get the title from the source page (this will be used for verification)
    page_title, references = GetTitleFromDOI(doi)
    
    # Use the official title for the filename for consistency
    if not production_mode:
        print(f"Using official title for filename: '{official_title}'")
    
    # Fast path: history check
    hist = history_get(doi)
    if hist and os.path.exists(hist.get('path', '')):
        # 文件已存在，直接返回引用（若历史中有）
        if production_mode:
            print(f"Cache hit: {hist.get('path')}")
        # 尽量返回历史记录中的references，否则用当前解析
        return hist.get('references', []) or references or []

    # 3. Attempt to get a download URL
    paperDownloadUrl = None
    
    # Strategy A: Prioritize direct download from publisher
    if not production_mode:
        print("\nStep 1: Trying to download directly from publisher (requires VPN for off-campus).")
    paperDownloadUrl = get_pdf_from_publisher(doi)
    
    # Strategy B: Try Unpaywall (OA) if configured
    if not paperDownloadUrl:
        if not production_mode:
            print("Step 2: Trying Unpaywall for Open Access PDF...")
        paperDownloadUrl = get_pdf_from_unpaywall(doi, UNPAYWALL_EMAIL)

    # Strategy C: Fallback to Sci-Hub if above fails
    if not paperDownloadUrl:
        if not production_mode:
            print("Step 3: Falling back to Sci-Hub (may require accessible mirror)...")
        try:
            paperDownloadUrl = GetDownloadUrl(doi, scihub_domains=SCIHUB_DOMAINS)
        except ConnectionError as e:
            if not production_mode:
                print(f"Could not get a download URL from any source for {doi}: {e}")
                print("Proceeding with references only (no file).")
            # 即便下载失败，也返回已解析的引用，允许继续递归
            return references or []

    # 4. Download the file
    downloaded_file_path = DownloadFileByUrl(paperDownloadUrl, official_title, subdirectory)
    
    # 5. Verify and Cleanup
    if downloaded_file_path:
        # Compare the official title with the title scraped from the download page
        is_verified = verify_title_similarity(official_title, page_title)
        
        if not is_verified:
            if not production_mode:
                print(f"Verification failed. Deleting downloaded file: {downloaded_file_path}")
            try:
                os.remove(downloaded_file_path)
                if not production_mode:
                    print("File deleted successfully.")
            except OSError as e:
                if not production_mode:
                    print(f"Error deleting file: {e}")
            # 标题校验失败也继续返回引用，允许递归到下一层
            return references or []
        else:
            if not production_mode:
                print("Verification successful. File has been kept.")
            # 记录历史
            history_set(doi, {
                'title': official_title,
                'path': downloaded_file_path,
                'subdir': subdirectory,
                'references': references,
                'ts': datetime.utcnow().isoformat() + 'Z'
            })
            return references
    
    # 下载失败也返回引用，让递归继续
    return references or []

def download_with_references(initial_doi, depth=1):
    """
    Downloads a paper and recursively downloads its references up to a specified depth.
    """
    from collections import deque
    dois_to_process = deque([(initial_doi, 0, "main")])
    processed_dois = set()
    total_downloads = 0

    while dois_to_process:
        doi, current_depth, subdirectory = dois_to_process.popleft()

        if doi in processed_dois:
            if not production_mode:
                print(f"Skipping already processed DOI: {doi}")
            continue

        if current_depth > depth:
            if not production_mode:
                print(f"Reached max depth ({depth}). Skipping DOI: {doi}")
            continue

        processed_dois.add(doi)

        if production_mode:
            total_downloads += 1
            print(f"[{total_downloads}/{len(processed_dois) + len(dois_to_process)}] Processing DOI: {doi} at depth {current_depth}")

        references = download_and_process_doi(doi, subdirectory)
        if not references or not isinstance(references, list):
            continue
        # 去重，只添加未处理过的DOI
        new_refs = set(references) - processed_dois
        if new_refs and current_depth < depth:
            if not production_mode:
                print(f"Queueing {len(new_refs)} references for download...")
            for ref_doi in new_refs:
                dois_to_process.append((ref_doi, current_depth + 1, f"ref{current_depth + 1}"))

    print(f"\n--- Iterative download complete. Total unique articles processed: {len(processed_dois)} ---")


def download_with_references_concurrent(initial_doi: str, depth: int = 1, max_workers: int = 4,
                                        young_filter: bool = False, young_depth: int = 2,
                                        young_keywords: list[str] | None = None):
    """
    Concurrent BFS download: per depth level, download all DOIs concurrently, then expand to next level.
    """
    initial_doi = normalize_doi(initial_doi)
    processed_dois = set()
    current_level = {initial_doi}

    for d in range(0, max(0, depth) + 1):
        if not current_level:
            break
        subdir = "main" if d == 0 else f"ref{d}"
        # 剔除已处理
        batch = [doi for doi in current_level if doi not in processed_dois]
        if not batch:
            current_level = set()
            continue

        if production_mode:
            print(f"Depth {d}: scheduling {len(batch)} downloads -> folder '{subdir}' with {max_workers} workers")

        next_level = set()
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {executor.submit(download_and_process_doi, doi, subdir): doi for doi in batch}
            for fut in as_completed(future_map):
                doi = future_map[fut]
                try:
                    refs = fut.result() or []
                    processed_dois.add(doi)
                    # 规范化引用DOI并去重
                    for r in refs:
                        r_norm = normalize_doi(r)
                        if not r_norm or r_norm in processed_dois:
                            continue
                        # 若开启年轻作者筛选，且下一层正好是目标层，则过滤
                        if young_filter and (d + 1) == young_depth:
                            try:
                                if not paper_has_young_author(r_norm, young_keywords):
                                    if not production_mode:
                                        print(f"Filtered (no young author): {r_norm}")
                                    continue
                            except Exception as e:
                                if not production_mode:
                                    print(f"Young-author check error for {r_norm}: {e}")
                                continue
                        next_level.add(r_norm)
                except Exception as e:
                    if not production_mode:
                        print(f"Error processing DOI {doi}: {e}")
                    processed_dois.add(doi)

        current_level = next_level

    print(f"\n--- Concurrent iterative download complete. Total unique articles processed: {len(processed_dois)} ---")


# --- Main Execution ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download scientific articles and their references.")
    parser.add_argument('--prod', action='store_true', help='Enable production mode for simplified output.')
    parser.add_argument('--doi', type=str, help='DOI(s) to download; comma or space separated.')
    parser.add_argument('--depth', type=int, default=1, help='Depth for recursive reference download (>=0).')
    parser.add_argument('--workers', type=int, default=4, help='Max concurrent downloads per level.')
    parser.add_argument('--young', action='store_true', help='Enable filtering for young authors at a target depth (default depth=2).')
    parser.add_argument('--young-depth', type=int, default=2, help='Depth at which to apply young-author filtering (default 2).')
    parser.add_argument('--young-keywords', type=str, default=None, help='Comma-separated keywords to detect young authors in affiliations.')
    parser.add_argument('--rps', type=float, default=0.0, help='Global rate limit (requests per second). 0 = unlimited.')
    parser.add_argument('--retries', type=int, default=None, help='Total HTTP retries (override default).')
    parser.add_argument('--backoff', type=float, default=None, help='HTTP retry backoff factor (override default).')
    parser.add_argument('--timeout', type=float, default=None, help='HTTP request timeout in seconds (override default).')
    parser.add_argument('--unpaywall-email', type=str, default=None, help='Optional email for Unpaywall API to find OA PDFs.')
    parser.add_argument('--scihub-domains', type=str, default=None, help='Comma-separated Sci-Hub base URLs to try in order.')
    parser.add_argument('--cited', action='store_true', help='Also fetch and download articles that cite the given DOI(s).')
    parser.add_argument('--cited-rows', type=int, default=10, help='Max number of citing articles to retrieve per DOI.')
    args = parser.parse_args()

    if args.prod:
        production_mode = True
        print("--- Running in Production Mode ---")

    # Configure HTTP knobs from CLI
    configure_http(retries=args.retries, backoff=args.backoff, timeout=args.timeout, rps=args.rps)

    # Optional integrations
    UNPAYWALL_EMAIL = args.unpaywall_email or None
    if args.scihub_domains:
        SCIHUB_DOMAINS = [u.strip() for u in args.scihub_domains.split(',') if u.strip()]
    else:
        SCIHUB_DOMAINS = None

    # If user provides --doi, run concurrent recursive downloader for those DOIs
    if args.doi:
        # 支持逗号或空白分隔的多DOI输入
        raw = [p.strip() for p in re.split(r'[\s,]+', args.doi) if p.strip()]
        dois = [normalize_doi(d) for d in raw]
        print(f"\n=== Running for {len(dois)} DOI(s) with depth={args.depth}, workers={args.workers} ===")
        yk = [s.strip() for s in (args.young_keywords.split(',') if args.young_keywords else []) if s.strip()] or None
        for i, d in enumerate(dois, 1):
            print(f"\n--- Target {i}/{len(dois)}: {d} ---")
            download_with_references_concurrent(d, depth=args.depth, max_workers=args.workers,
                                                young_filter=args.young, young_depth=args.young_depth,
                                                young_keywords=yk)
            if args.cited:
                print(f"\n--- Cited-by for target {i}: {d} ---")
                download_cited_papers_concurrent(d, rows=args.cited_rows, max_workers=args.workers)
        print("\n--- All requested DOI tasks completed. ---")
    else:
        # Default behavior: Production mode + download one DOI with depth=2 and young-author filter at depth 2
        production_mode = True
        default_doi = '10.1126/science.177.4047.393'
        print("\n=== Default: Download main + level-1 & level-2 references (young-author filter at level 2) ===")
        download_with_references_concurrent(default_doi, depth=2, max_workers=args.workers,
                                            young_filter=True, young_depth=2)
        print("\n--- Task completed. ---")
