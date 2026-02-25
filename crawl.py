#!/usr/bin/env python3
"""
Crawler for www.makumba.org
Fetches the website and turns it into a static site for GitHub Pages deployment.
Preserves all existing links.
"""

import os
import re
import sys
import time
import hashlib
import urllib.parse
from pathlib import Path
from collections import deque

import requests
from bs4 import BeautifulSoup

# Configuration
BASE_URL = "http://www.makumba.org"
OUTPUT_DIR = Path(__file__).parent
DELAY = 0.3  # seconds between requests (be polite)
MAX_PAGES = 500  # safety limit for wiki pages
MAX_API_PAGES = 2000  # safety limit for javadoc pages
REQUEST_TIMEOUT = 30

# Track what we've processed
visited_urls = set()
downloaded_assets = set()
failed_urls = set()

session = requests.Session()
session.headers.update({
    "User-Agent": "MakumbaStaticCrawler/1.0 (archiving makumba.org for GitHub Pages)"
})


def normalize_url(url):
    """Normalize a URL for deduplication."""
    parsed = urllib.parse.urlparse(url)
    # Remove fragment
    return urllib.parse.urlunparse(parsed._replace(fragment=""))


def is_internal(url):
    """Check if a URL belongs to www.makumba.org."""
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme and not parsed.netloc:
        return True  # relative URL
    return parsed.netloc in ("www.makumba.org", "makumba.org")


def url_to_filepath(url, content_type=None):
    """Convert a URL to a local file path."""
    parsed = urllib.parse.urlparse(url)
    path = parsed.path

    if not path or path == "/":
        path = "/index.html"

    # Decode percent-encoded characters for filesystem
    path = urllib.parse.unquote(path)

    # For page URLs like /page/Something, save as /page/Something/index.html
    # unless it already has an extension
    ext = os.path.splitext(path)[1]
    if not ext:
        # Check content type to decide extension
        if content_type and "text/html" in content_type:
            path = path.rstrip("/") + "/index.html"
        elif content_type and "text/css" in content_type:
            path = path + ".css"
        elif content_type and "javascript" in content_type:
            path = path + ".js"
        else:
            path = path.rstrip("/") + "/index.html"

    # Handle query strings by encoding them into the filename
    if parsed.query:
        # Create a short hash of the query to keep filenames manageable
        query_hash = hashlib.md5(parsed.query.encode()).hexdigest()[:8]
        base, ext = os.path.splitext(path)
        path = f"{base}_q{query_hash}{ext}"

    # Remove leading slash for relative path
    path = path.lstrip("/")

    return OUTPUT_DIR / path


def rewrite_url(url, current_page_url):
    """Rewrite a URL for the static site."""
    if not url or url.startswith("#") or url.startswith("mailto:") or url.startswith("javascript:"):
        return url

    # Resolve relative URLs
    absolute = urllib.parse.urljoin(current_page_url, url)
    parsed = urllib.parse.urlparse(absolute)

    if not is_internal(absolute):
        return url  # keep external URLs as-is

    path = parsed.path
    if not path or path == "/":
        path = "/"

    # Decode for cleanliness
    path = urllib.parse.unquote(path)

    # For paths without extensions, they'll be directories with index.html
    ext = os.path.splitext(path)[1]

    # Handle query strings
    query_part = ""
    if parsed.query:
        query_hash = hashlib.md5(parsed.query.encode()).hexdigest()[:8]
        if not ext:
            # Will become a directory - need to adjust
            path = path.rstrip("/")
            base_name = os.path.basename(path)
            parent = os.path.dirname(path)
            path = f"{parent}/{base_name}_q{query_hash}"
        else:
            base, ext_part = os.path.splitext(path)
            path = f"{base}_q{query_hash}{ext_part}"

    # Reconstruct with fragment
    fragment = f"#{parsed.fragment}" if parsed.fragment else ""

    return f"{path}{fragment}"


def should_crawl(url):
    """Determine if we should crawl this URL for more links."""
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.lower()

    # Skip /api/ URLs - handled separately by crawl_api_docs()
    if parsed.path.startswith("/api/") or parsed.path == "/api":
        return False

    # Skip non-page resources
    skip_extensions = {
        ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
        ".pdf", ".zip", ".tar", ".gz", ".woff", ".woff2", ".ttf", ".eot",
        ".xml", ".rdf", ".rss"
    }
    ext = os.path.splitext(path)[1]
    if ext in skip_extensions:
        return False

    # Skip edit/action URLs - we only want view pages
    skip_patterns = [
        "do=Edit", "do=Comment", "do=Diff", "do=PageInfo",
        "do=Delete", "do=Rename", "do=Upload",
        "Login.jsp", "UserPreferences.jsp",
        "JSON-RPC", "rss.jsp", "rss.rdf",
        "SisterSites.jsp"
    ]
    full_url = url if "?" in url else f"{url}?{parsed.query}" if parsed.query else url
    for pattern in skip_patterns:
        if pattern in full_url:
            return False

    return True


def should_download(url):
    """Determine if we should download this asset."""
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.lower()

    # Skip action/dynamic URLs
    skip_patterns = [
        "Login.jsp", "UserPreferences.jsp", "JSON-RPC",
        "rss.jsp", "do=Edit", "do=Comment", "do=Diff",
        "do=PageInfo", "do=Delete", "do=Rename", "do=Upload"
    ]
    for pattern in skip_patterns:
        if pattern in url:
            return False

    return True


def download_asset(url):
    """Download a static asset (CSS, JS, image, etc.)."""
    if url in downloaded_assets:
        return
    downloaded_assets.add(url)

    if not should_download(url):
        return

    filepath = url_to_filepath(url)

    if filepath.exists():
        return

    try:
        print(f"  Asset: {url}")
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()

        filepath.parent.mkdir(parents=True, exist_ok=True)

        # For CSS files, also find and download referenced assets (images, fonts)
        content_type = resp.headers.get("content-type", "")
        if "text/css" in content_type:
            css_text = resp.text
            css_text = process_css(css_text, url)
            filepath.write_text(css_text, encoding="utf-8")
        else:
            filepath.write_bytes(resp.content)

    except Exception as e:
        print(f"  FAILED asset {url}: {e}")
        failed_urls.add(url)


def process_css(css_text, css_url):
    """Find and download assets referenced in CSS (url(...))."""
    url_pattern = re.compile(r'url\(["\']?([^)"\']+)["\']?\)')

    def replace_css_url(match):
        ref = match.group(1)
        if ref.startswith("data:"):
            return match.group(0)
        absolute = urllib.parse.urljoin(css_url, ref)
        if is_internal(absolute):
            download_asset(absolute)
            # Make the path relative or absolute from site root
            parsed = urllib.parse.urlparse(absolute)
            return f'url("{parsed.path}")'
        return match.group(0)

    return url_pattern.sub(replace_css_url, css_text)


def fetch_page(url):
    """Fetch an HTML page and return the response."""
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "text/html" not in content_type:
            return None
        return resp
    except Exception as e:
        print(f"  FAILED page {url}: {e}")
        failed_urls.add(url)
        return None


def process_page(url, resp):
    """Process an HTML page: rewrite links, download assets, extract new URLs."""
    soup = BeautifulSoup(resp.text, "html.parser")
    new_urls = set()

    # Remove edit links, login links, and other dynamic elements
    for elem in soup.select('a[href*="do=Edit"], a[href*="do=Comment"], a[href*="do=Diff"]'):
        elem.decompose()
    for elem in soup.select('a[href*="Login.jsp"], a[href*="UserPreferences.jsp"]'):
        elem.decompose()

    # Process all links
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        absolute = urllib.parse.urljoin(url, href)

        if is_internal(absolute) and should_crawl(absolute):
            normalized = normalize_url(absolute)
            if normalized not in visited_urls:
                new_urls.add(normalized)

        tag["href"] = rewrite_url(href, url)

    # Process CSS links
    for tag in soup.find_all("link", href=True):
        href = tag["href"]
        absolute = urllib.parse.urljoin(url, href)
        if is_internal(absolute):
            download_asset(absolute)
            tag["href"] = rewrite_url(href, url)

    # Process scripts
    for tag in soup.find_all("script", src=True):
        src = tag["src"]
        absolute = urllib.parse.urljoin(url, src)
        if is_internal(absolute):
            download_asset(absolute)
            tag["src"] = rewrite_url(src, url)

    # Process images
    for tag in soup.find_all("img", src=True):
        src = tag["src"]
        absolute = urllib.parse.urljoin(url, src)
        if is_internal(absolute):
            download_asset(absolute)
            tag["src"] = rewrite_url(src, url)

    # Process meta tags with URLs
    for tag in soup.find_all("meta", content=True):
        content = tag.get("content", "")
        if content.startswith("http://www.makumba.org"):
            tag["content"] = content.replace("http://www.makumba.org", "")

    # Process favicon links
    for tag in soup.find_all("link", rel=True):
        if "icon" in " ".join(tag.get("rel", [])):
            href = tag.get("href", "")
            absolute = urllib.parse.urljoin(url, href)
            if is_internal(absolute):
                download_asset(absolute)

    # Save the page
    filepath = url_to_filepath(url, resp.headers.get("content-type", ""))
    filepath.parent.mkdir(parents=True, exist_ok=True)

    html = str(soup)
    filepath.write_text(html, encoding="utf-8")

    return new_urls


def crawl_api_docs():
    """Crawl the Javadoc API documentation at /api/.

    The Javadoc is already static HTML, so we do a simple recursive crawl
    downloading all HTML, CSS, JS, and image files under /api/.
    """
    print()
    print("=" * 60)
    print("Crawling Javadoc API docs at /api/")
    print("=" * 60)

    api_visited = set()
    api_queue = deque()
    api_page_count = 0

    # Seed with the main Javadoc entry points
    api_seeds = [
        f"{BASE_URL}/api/",
        f"{BASE_URL}/api/allclasses-frame.html",
        f"{BASE_URL}/api/allclasses-noframe.html",
        f"{BASE_URL}/api/overview-summary.html",
        f"{BASE_URL}/api/overview-tree.html",
        f"{BASE_URL}/api/overview-frame.html",
        f"{BASE_URL}/api/constant-values.html",
        f"{BASE_URL}/api/deprecated-list.html",
        f"{BASE_URL}/api/help-doc.html",
        f"{BASE_URL}/api/index-all.html",
        f"{BASE_URL}/api/serialized-form.html",
    ]

    for url in api_seeds:
        normalized = normalize_url(url)
        if normalized not in api_visited:
            api_queue.append(normalized)
            api_visited.add(normalized)

    while api_queue and api_page_count < MAX_API_PAGES:
        url = api_queue.popleft()
        parsed = urllib.parse.urlparse(url)
        path = parsed.path

        # Only process files under /api/
        if not path.startswith("/api/"):
            continue

        filepath = url_to_filepath(url)
        if filepath.exists():
            api_page_count += 1
            continue

        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except Exception as e:
            print(f"  FAILED: {url}: {e}")
            failed_urls.add(url)
            continue

        content_type = resp.headers.get("content-type", "")
        filepath = url_to_filepath(url, content_type)
        filepath.parent.mkdir(parents=True, exist_ok=True)

        if "text/html" in content_type:
            api_page_count += 1
            print(f"  [api {api_page_count}] {url}")

            soup = BeautifulSoup(resp.text, "html.parser")

            # Extract links from this page
            for tag in soup.find_all(["a", "link"], href=True):
                href = tag["href"]
                if href.startswith("#") or href.startswith("mailto:") or href.startswith("javascript:"):
                    continue
                absolute = urllib.parse.urljoin(url, href)
                if not is_internal(absolute):
                    continue
                abs_parsed = urllib.parse.urlparse(absolute)
                # Only follow links under /api/
                if abs_parsed.path.startswith("/api/"):
                    normalized = normalize_url(absolute)
                    if normalized not in api_visited:
                        api_visited.add(normalized)
                        api_queue.append(normalized)

            # Extract script/img src
            for tag in soup.find_all(["script", "img"], src=True):
                src = tag["src"]
                absolute = urllib.parse.urljoin(url, src)
                if is_internal(absolute):
                    abs_parsed = urllib.parse.urlparse(absolute)
                    if abs_parsed.path.startswith("/api/"):
                        normalized = normalize_url(absolute)
                        if normalized not in api_visited:
                            api_visited.add(normalized)
                            api_queue.append(normalized)

            # For FRAME/IFRAME src
            for tag in soup.find_all(["frame", "iframe"], src=True):
                src = tag["src"]
                absolute = urllib.parse.urljoin(url, src)
                if is_internal(absolute):
                    abs_parsed = urllib.parse.urlparse(absolute)
                    if abs_parsed.path.startswith("/api/"):
                        normalized = normalize_url(absolute)
                        if normalized not in api_visited:
                            api_visited.add(normalized)
                            api_queue.append(normalized)

            filepath.write_text(resp.text, encoding="utf-8")

        elif "text/css" in content_type:
            css_text = process_css(resp.text, url)
            filepath.write_text(css_text, encoding="utf-8")
        else:
            filepath.write_bytes(resp.content)

        time.sleep(DELAY)

    print(f"  API docs pages crawled: {api_page_count}")
    return api_page_count


def create_root_redirect():
    """Create index.html at root that matches the Main page."""
    # The root / should serve the same content as /page/Main
    main_page = OUTPUT_DIR / "page" / "Main" / "index.html"
    root_index = OUTPUT_DIR / "index.html"

    if main_page.exists() and not root_index.exists():
        # Copy content or create a redirect
        import shutil
        shutil.copy2(main_page, root_index)
        print("Created root index.html from Main page")


def create_nojekyll():
    """Create .nojekyll file to disable Jekyll processing on GitHub Pages."""
    nojekyll = OUTPUT_DIR / ".nojekyll"
    nojekyll.touch()
    print("Created .nojekyll file")


def crawl():
    """Main crawl loop."""
    print(f"Starting crawl of {BASE_URL}")
    print(f"Output directory: {OUTPUT_DIR}")
    print()

    # Clean previously crawled content (preserve repo files like crawl.py, .git, .venv, etc.)
    crawled_dirs = ["api", "api-developer", "attach", "images", "page", "scripts", "templates"]
    crawled_files = ["index.html", "rss.rdf", ".nojekyll"]
    import shutil
    for d in crawled_dirs:
        p = OUTPUT_DIR / d
        if p.exists():
            shutil.rmtree(p)
    for f in crawled_files:
        p = OUTPUT_DIR / f
        if p.exists():
            p.unlink()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # BFS crawl
    queue = deque()

    # Seed URLs
    seed_urls = [
        f"{BASE_URL}/",
        f"{BASE_URL}/page/Main",
        f"{BASE_URL}/page/QuickStart",
        f"{BASE_URL}/page/Configuration",
        f"{BASE_URL}/page/Usage",
        f"{BASE_URL}/page/Documentation",
        f"{BASE_URL}/page/Showcase",
        f"{BASE_URL}/page/Download",
        f"{BASE_URL}/page/Blog",
        f"{BASE_URL}/page/MakumbaManifesto",
        f"{BASE_URL}/page/GettingHelp",
        f"{BASE_URL}/page/Supporters",
        f"{BASE_URL}/page/About",
    ]

    for url in seed_urls:
        normalized = normalize_url(url)
        if normalized not in visited_urls:
            queue.append(normalized)
            visited_urls.add(normalized)

    page_count = 0

    while queue and page_count < MAX_PAGES:
        url = queue.popleft()

        print(f"[{page_count + 1}] Crawling: {url}")

        resp = fetch_page(url)
        if resp is None:
            continue

        new_urls = process_page(url, resp)
        page_count += 1

        for new_url in new_urls:
            if new_url not in visited_urls:
                visited_urls.add(new_url)
                queue.append(new_url)

        time.sleep(DELAY)

    # Crawl Javadoc API docs
    api_count = crawl_api_docs()

    # Post-processing
    create_root_redirect()
    create_nojekyll()

    print()
    print("=" * 60)
    print(f"Crawl complete!")
    print(f"  Wiki pages crawled: {page_count}")
    print(f"  API doc pages crawled: {api_count}")
    print(f"  Assets downloaded: {len(downloaded_assets)}")
    print(f"  Failed URLs: {len(failed_urls)}")
    if failed_urls:
        print("  Failed:")
        for url in sorted(failed_urls):
            print(f"    - {url}")
    print(f"  Output: {OUTPUT_DIR}")
    print()
    print("To deploy to GitHub Pages:")
    print("  1. Commit and push to main branch")
    print("  2. Enable GitHub Pages in repository settings (source: main branch, root)")


if __name__ == "__main__":
    crawl()
