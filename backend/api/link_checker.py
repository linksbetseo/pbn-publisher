import asyncio
import logging
import time
import re
from typing import Optional, List
from datetime import datetime, timezone

import csv
import io

import aiosqlite
import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from config import DB_PATH

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/link-checker", tags=["link-checker"])

# ── concurrency control ──────────────────────────────────────────────
_CRAWL_SEM = asyncio.Semaphore(10)  # max 10 concurrent HTTP fetches
_scan_progress: dict[str, dict] = {}  # scan_id -> progress info


# ── DB table ─────────────────────────────────────────────────────────
async def ensure_tables():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS link_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id TEXT NOT NULL,
                domain_id INTEGER NOT NULL,
                domain TEXT NOT NULL,
                target_domain TEXT NOT NULL,
                page_url TEXT NOT NULL,
                link_found INTEGER DEFAULT 0,
                link_location TEXT DEFAULT '',
                link_url TEXT DEFAULT '',
                anchor_text TEXT DEFAULT '',
                article_excerpt TEXT DEFAULT '',
                checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_lc_scan ON link_checks(scan_id);
            CREATE INDEX IF NOT EXISTS idx_lc_domain ON link_checks(domain_id, scan_id);
        """)
        await db.commit()


_tables_ok = False


async def _ensure():
    global _tables_ok
    if not _tables_ok:
        await ensure_tables()
        _tables_ok = True


# ── HTML parsing helpers ─────────────────────────────────────────────
def _classify_link_position(html: str, link_start: int) -> str:
    """Determine where in the page the link appears: menu, footer, sidebar, article."""
    # Look at surrounding HTML context (up to 3000 chars before the link)
    before = html[max(0, link_start - 3000):link_start].lower()

    # Check for footer context
    footer_tags = ['<footer', 'id="footer"', 'class="footer"', 'id="colophon"',
                   'class="site-footer"', 'class="widget-area"', 'class="footer-']
    for tag in footer_tags:
        # Only match if the tag hasn't been closed before our link
        idx = before.rfind(tag)
        if idx >= 0:
            # Check if there's a closing tag after it
            close_idx = before.rfind('</footer', idx)
            if close_idx < 0:
                return 'footer'

    # Check for nav/menu context
    nav_tags = ['<nav', 'id="nav"', 'class="nav"', 'class="menu"', 'id="menu"',
                'class="main-menu"', 'class="navigation"', 'class="navbar"',
                'id="site-navigation"', 'class="primary-menu"', 'class="header-menu"']
    for tag in nav_tags:
        idx = before.rfind(tag)
        if idx >= 0:
            close_idx = before.rfind('</nav', idx)
            if close_idx < 0:
                return 'menu'

    # Check for sidebar context
    sidebar_tags = ['<aside', 'id="sidebar"', 'class="sidebar"', 'class="widget-area"',
                    'id="secondary"', 'class="side-']
    for tag in sidebar_tags:
        idx = before.rfind(tag)
        if idx >= 0:
            close_idx = before.rfind('</aside', idx)
            if close_idx < 0:
                return 'sidebar'

    # Check for article/content context
    article_tags = ['<article', '<main', 'class="entry-content"', 'class="post-content"',
                    'class="article-content"', 'class="content-area"', 'id="content"',
                    'class="single-content"', 'class="the-content"']
    for tag in article_tags:
        idx = before.rfind(tag)
        if idx >= 0:
            return 'article'

    return 'article'  # default — most links in body are article content


def _extract_excerpt(html: str, link_start: int, link_end: int, max_len: int = 500) -> str:
    """Extract text around the link for context."""
    # Get surrounding raw HTML chunk
    start = max(0, link_start - 300)
    end = min(len(html), link_end + 300)
    chunk = html[start:end]

    # Strip HTML tags for readable excerpt
    text = re.sub(r'<[^>]+>', ' ', chunk)
    text = re.sub(r'\s+', ' ', text).strip()

    if len(text) > max_len:
        text = text[:max_len] + '...'
    return text


def _find_links_to_target(html: str, target_domain: str) -> list[dict]:
    """Find all <a> links pointing to the target domain."""
    results = []
    target_lower = target_domain.lower().replace('www.', '')

    # Match <a ... href="..." ...>anchor text</a>
    pattern = re.compile(
        r'<a\s[^>]*href=["\']([^"\']*)["\'][^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL
    )

    for m in pattern.finditer(html):
        href = m.group(1).strip()
        anchor = re.sub(r'<[^>]+>', '', m.group(2)).strip()

        # Check if href points to target domain
        href_lower = href.lower().replace('www.', '')
        if target_lower in href_lower:
            link_start = m.start()
            link_end = m.end()
            location = _classify_link_position(html, link_start)
            excerpt = _extract_excerpt(html, link_start, link_end)
            results.append({
                'link_url': href,
                'anchor_text': anchor[:500],
                'link_location': location,
                'article_excerpt': excerpt,
            })

    return results


def _parse_sitemap_urls(xml_text: str, base_domain: str) -> list[str]:
    """Extract URLs from sitemap XML. Handles both sitemap index and urlset."""
    urls = []

    # Extract <loc> tags
    locs = re.findall(r'<loc>\s*(.*?)\s*</loc>', xml_text, re.IGNORECASE)

    for loc in locs:
        loc = loc.strip()
        if not loc:
            continue
        # Skip sub-sitemaps (we'll handle them separately)
        if loc.endswith('.xml') or 'sitemap' in loc.lower():
            continue
        urls.append(loc)

    return urls


def _extract_sub_sitemaps(xml_text: str) -> list[str]:
    """Extract sub-sitemap URLs from a sitemap index."""
    sitemaps = []
    locs = re.findall(r'<loc>\s*(.*?)\s*</loc>', xml_text, re.IGNORECASE)
    for loc in locs:
        loc = loc.strip()
        if loc.endswith('.xml') or 'sitemap' in loc.lower():
            sitemaps.append(loc)
    return sitemaps


# ── Crawling logic ───────────────────────────────────────────────────
async def _fetch_page(client: httpx.AsyncClient, url: str) -> Optional[str]:
    """Fetch a page with semaphore-controlled concurrency."""
    async with _CRAWL_SEM:
        try:
            resp = await client.get(url, follow_redirects=True)
            if resp.status_code == 200:
                return resp.text
        except Exception as e:
            logger.debug(f"[LinkChecker] Failed to fetch {url}: {e}")
    return None


async def _get_all_page_urls(client: httpx.AsyncClient, domain: str) -> list[str]:
    """Get all page URLs from domain's sitemap."""
    base = domain.rstrip('/')
    if not base.startswith('http'):
        base = f'https://{base}'

    urls = [base + '/']  # always include homepage

    # Try common sitemap paths
    sitemap_paths = ['/sitemap.xml', '/sitemap_index.xml', '/wp-sitemap.xml']
    for path in sitemap_paths:
        xml = await _fetch_page(client, base + path)
        if not xml:
            continue

        # Check if it's a sitemap index (contains sub-sitemaps)
        sub_sitemaps = _extract_sub_sitemaps(xml)
        if sub_sitemaps:
            # Fetch each sub-sitemap
            tasks = [_fetch_page(client, sm_url) for sm_url in sub_sitemaps[:20]]  # cap at 20
            results = await asyncio.gather(*tasks)
            for sm_xml in results:
                if sm_xml:
                    page_urls = _parse_sitemap_urls(sm_xml, domain)
                    urls.extend(page_urls)
        else:
            page_urls = _parse_sitemap_urls(xml, domain)
            urls.extend(page_urls)

        break  # found a valid sitemap, stop trying others

    # Deduplicate
    seen = set()
    unique = []
    for u in urls:
        normalized = u.rstrip('/')
        if normalized not in seen:
            seen.add(normalized)
            unique.append(u)

    return unique[:200]  # cap at 200 pages per domain


async def _scan_single_domain(
    client: httpx.AsyncClient,
    domain_id: int,
    domain: str,
    target_domain: str,
    scan_id: str,
    progress: dict,
):
    """Scan a single PBN domain for links to target_domain."""
    try:
        page_urls = await _get_all_page_urls(client, domain)
        progress['pages_total'] += len(page_urls)

        found_any = False
        for page_url in page_urls:
            html = await _fetch_page(client, page_url)
            progress['pages_checked'] += 1

            if not html:
                continue

            links = _find_links_to_target(html, target_domain)

            if links:
                found_any = True
                async with aiosqlite.connect(DB_PATH) as db:
                    for link in links:
                        await db.execute(
                            """INSERT INTO link_checks
                               (scan_id, domain_id, domain, target_domain, page_url,
                                link_found, link_location, link_url, anchor_text, article_excerpt)
                               VALUES (?,?,?,?,?,1,?,?,?,?)""",
                            (scan_id, domain_id, domain, target_domain, page_url,
                             link['link_location'], link['link_url'],
                             link['anchor_text'], link['article_excerpt']),
                        )
                    await db.commit()

        # If no links found on any page, insert a single "not found" row
        if not found_any:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    """INSERT INTO link_checks
                       (scan_id, domain_id, domain, target_domain, page_url,
                        link_found, link_location, link_url, anchor_text, article_excerpt)
                       VALUES (?,?,?,?,?,0,'','','','')""",
                    (scan_id, domain_id, domain, target_domain, f"https://{domain}/"),
                )
                await db.commit()

        progress['domains_done'] += 1

    except Exception as e:
        logger.error(f"[LinkChecker] Error scanning {domain}: {e}")
        progress['domains_done'] += 1
        progress['errors'].append(f"{domain}: {str(e)[:100]}")
        # Insert error row
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    """INSERT INTO link_checks
                       (scan_id, domain_id, domain, target_domain, page_url,
                        link_found, link_location, link_url, anchor_text, article_excerpt)
                       VALUES (?,?,?,?,?,0,'error','','',?)""",
                    (scan_id, domain_id, domain, target_domain,
                     f"https://{domain}/", str(e)[:500]),
                )
                await db.commit()
        except Exception:
            pass


# ── API models ───────────────────────────────────────────────────────
class ScanRequest(BaseModel):
    target_domain: str
    domain_ids: Optional[List[int]] = None  # None = all active domains
    batch_tag: Optional[str] = None


# ── Endpoints ────────────────────────────────────────────────────────
@router.post("/scan")
async def start_scan(body: ScanRequest):
    """Start a link check scan across PBN domains."""
    await _ensure()

    target = body.target_domain.strip().lower().replace('https://', '').replace('http://', '').replace('www.', '').rstrip('/')
    if not target:
        raise HTTPException(400, "target_domain is required")

    # Get domains to scan
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if body.domain_ids:
            placeholders = ','.join('?' * len(body.domain_ids))
            q = f"SELECT id, domain FROM my_domains WHERE id IN ({placeholders}) AND active=1"
            async with db.execute(q, body.domain_ids) as cur:
                domains = [dict(r) for r in await cur.fetchall()]
        elif body.batch_tag:
            async with db.execute(
                "SELECT id, domain FROM my_domains WHERE active=1 AND batch_tag=?",
                (body.batch_tag,)
            ) as cur:
                domains = [dict(r) for r in await cur.fetchall()]
        else:
            async with db.execute("SELECT id, domain FROM my_domains WHERE active=1") as cur:
                domains = [dict(r) for r in await cur.fetchall()]

    if not domains:
        raise HTTPException(400, "No active domains found")

    scan_id = f"{target}_{int(time.time())}"

    progress = {
        'scan_id': scan_id,
        'target_domain': target,
        'status': 'running',
        'domains_total': len(domains),
        'domains_done': 0,
        'pages_total': 0,
        'pages_checked': 0,
        'errors': [],
        'started_at': time.time(),
    }
    _scan_progress[scan_id] = progress

    # Run scan in background
    async def _run_scan():
        try:
            async with httpx.AsyncClient(
                timeout=20,
                verify=False,
                follow_redirects=True,
                headers={'User-Agent': 'Mozilla/5.0 (compatible; PBNChecker/1.0)'},
            ) as client:
                # Process domains in batches of 5 for controlled parallelism
                batch_size = 5
                for i in range(0, len(domains), batch_size):
                    batch = domains[i:i + batch_size]
                    tasks = [
                        _scan_single_domain(client, d['id'], d['domain'], target, scan_id, progress)
                        for d in batch
                    ]
                    await asyncio.gather(*tasks)

            progress['status'] = 'done'
        except Exception as e:
            logger.error(f"[LinkChecker] Scan error: {e}")
            progress['status'] = 'error'
            progress['errors'].append(str(e)[:200])

    asyncio.create_task(_run_scan())

    return {
        'scan_id': scan_id,
        'target_domain': target,
        'domains_count': len(domains),
        'message': f'Scan started for {len(domains)} domains',
    }


@router.get("/status/{scan_id}")
async def scan_status(scan_id: str):
    """Get progress of a running scan."""
    p = _scan_progress.get(scan_id)
    if not p:
        return {'status': 'not_found'}
    return {
        'scan_id': p['scan_id'],
        'status': p['status'],
        'target_domain': p['target_domain'],
        'domains_total': p['domains_total'],
        'domains_done': p['domains_done'],
        'pages_total': p['pages_total'],
        'pages_checked': p['pages_checked'],
        'errors_count': len(p['errors']),
        'elapsed_sec': round(time.time() - p['started_at'], 1),
    }


@router.get("/results/{scan_id}")
async def scan_results(scan_id: str):
    """Get results for a completed (or in-progress) scan."""
    await _ensure()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT domain_id, domain, page_url, link_found, link_location,
                      link_url, anchor_text, article_excerpt, checked_at
               FROM link_checks
               WHERE scan_id = ?
               ORDER BY link_found DESC, domain""",
            (scan_id,)
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    # Aggregate per domain
    domains_map: dict[str, dict] = {}
    for r in rows:
        d = r['domain']
        if d not in domains_map:
            domains_map[d] = {
                'domain_id': r['domain_id'],
                'domain': d,
                'has_link': False,
                'links': [],
            }
        if r['link_found']:
            domains_map[d]['has_link'] = True
            domains_map[d]['links'].append({
                'page_url': r['page_url'],
                'location': r['link_location'],
                'link_url': r['link_url'],
                'anchor_text': r['anchor_text'],
                'excerpt': r['article_excerpt'],
            })

    domain_results = sorted(domains_map.values(), key=lambda x: (not x['has_link'], x['domain']))

    total = len(domain_results)
    with_links = sum(1 for d in domain_results if d['has_link'])

    return {
        'scan_id': scan_id,
        'total_domains': total,
        'with_links': with_links,
        'without_links': total - with_links,
        'domains': domain_results,
    }


@router.get("/scans")
async def list_scans(limit: int = Query(20)):
    """List recent scans."""
    await _ensure()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT scan_id, target_domain,
                      COUNT(DISTINCT domain) as domains_count,
                      SUM(link_found) as links_found,
                      MIN(checked_at) as started_at
               FROM link_checks
               GROUP BY scan_id
               ORDER BY started_at DESC
               LIMIT ?""",
            (limit,)
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    # Enrich with progress status
    for r in rows:
        p = _scan_progress.get(r['scan_id'])
        r['status'] = p['status'] if p else 'done'

    return rows


@router.get("/export/{scan_id}")
async def export_csv(scan_id: str):
    """Export scan results as CSV."""
    await _ensure()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT domain, target_domain, page_url, link_found,
                      link_location, link_url, anchor_text, article_excerpt, checked_at
               FROM link_checks
               WHERE scan_id = ?
               ORDER BY domain, link_found DESC""",
            (scan_id,)
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    if not rows:
        raise HTTPException(404, "No results for this scan")

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        'Domena PBN', 'Domena docelowa', 'URL strony', 'Link znaleziony',
        'Lokalizacja', 'URL linka', 'Anchor text', 'Fragment artykulu', 'Data sprawdzenia',
    ])
    for r in rows:
        writer.writerow([
            r['domain'],
            r['target_domain'],
            r['page_url'],
            'Tak' if r['link_found'] else 'Nie',
            r['link_location'] or '',
            r['link_url'] or '',
            r['anchor_text'] or '',
            (r['article_excerpt'] or '').replace('\n', ' ')[:1000],
            r['checked_at'] or '',
        ])

    target = rows[0]['target_domain'] if rows else 'export'
    filename = f"link_check_{target}_{scan_id.split('_')[-1]}.csv"

    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/scan/{scan_id}")
async def delete_scan(scan_id: str):
    """Delete scan results."""
    await _ensure()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM link_checks WHERE scan_id = ?", (scan_id,))
        await db.commit()
    _scan_progress.pop(scan_id, None)
    return {'deleted': scan_id}


# Cleanup stale progress entries (older than 1 hour)
def _cleanup_progress():
    now = time.time()
    stale = [k for k, v in _scan_progress.items() if now - v.get('started_at', 0) > 3600]
    for k in stale:
        _scan_progress.pop(k, None)
