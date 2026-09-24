"""
DiskWala resolver/downloader.

The resolver deliberately rejects analytics, tracking, share-page and API
URLs as media. It prefers direct media URLs discovered from API JSON, page
metadata and Playwright network responses.
"""

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urljoin, urlparse

import aiofiles
import aiohttp
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

BLOCKED_HOSTS = {
    "google-analytics.com",
    "www.google-analytics.com",
    "googletagmanager.com",
    "www.googletagmanager.com",
    "doubleclick.net",
    "www.doubleclick.net",
    "googleadservices.com",
    "facebook.com",
    "connect.facebook.net",
    "hotjar.com",
    "clarity.ms",
}

MEDIA_EXTENSIONS = (
    ".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".wmv",
    ".ts", ".m3u8", ".mpd", ".mp3", ".m4a", ".aac", ".flac", ".ogg",
    ".wav", ".opus",
)

MEDIA_KEYS = (
    "downloadUrl", "download_url", "directUrl", "direct_url",
    "streamUrl", "stream_url", "mediaUrl", "media_url",
    "fileUrl", "file_url", "videoUrl", "video_url", "sourceUrl",
    "source_url", "src", "url", "link",
)


def _is_valid_media_url(value: Any, page_url: Optional[str] = None) -> bool:
    """Return True only for plausible direct media URLs."""
    if not isinstance(value, str):
        return False
    value = value.strip()
    if not value or len(value) > 8192:
        return False

    # Resolve protocol-relative / relative URLs when a page URL is known.
    if value.startswith("//"):
        value = "https:" + value
    elif page_url and value.startswith("/"):
        value = urljoin(page_url, value)

    try:
        parsed = urlparse(value)
    except Exception:
        return False

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False

    host = (parsed.hostname or "").lower().rstrip(".")
    path = (parsed.path or "").lower()
    full = value.lower()

    if any(host == h or host.endswith("." + h) for h in BLOCKED_HOSTS):
        return False

    # Tracking/telemetry endpoints are never download sources.
    blocked_parts = (
        "/g/collect", "/collect", "/analytics", "/telemetry",
        "/pixel", "/beacon", "/gtag", "google-analytics",
    )
    if any(part in full for part in blocked_parts):
        return False

    # The normal DiskWala share page is HTML, not a media file.
    if host.endswith("diskwala.com") and (
        path.startswith("/app/") or path.startswith("/file/")
        or path.startswith("/watch/") or path.startswith("/v/")
    ):
        return False

    # Obvious media files are accepted.
    if path.endswith(MEDIA_EXTENSIONS):
        return True

    # Signed CDN/storage links often have no extension.
    markers = (
        "download", "stream", "media", "videoplayback", "video",
        "storage", "cdn", "object", "blob",
    )
    if any(marker in full for marker in markers):
        return True

    # Some providers use query parameters such as ?download=1.
    query = (parsed.query or "").lower()
    if any(k in query for k in ("download=", "filename=", "mime=video", "type=video")):
        return True

    return False


def _find_media_url(obj: Any, page_url: Optional[str] = None, depth: int = 0) -> Optional[str]:
    """Recursively find the first plausible media URL in JSON/HTML-derived data."""
    if depth > 8 or obj is None:
        return None

    if isinstance(obj, str):
        return obj.strip() if _is_valid_media_url(obj, page_url) else None

    if isinstance(obj, dict):
        # Prefer semantically named fields over generic url/link fields.
        for key in MEDIA_KEYS:
            if key in obj:
                found = _find_media_url(obj[key], page_url, depth + 1)
                if found:
                    return found
        for value in obj.values():
            found = _find_media_url(value, page_url, depth + 1)
            if found:
                return found

    elif isinstance(obj, (list, tuple)):
        for item in obj:
            found = _find_media_url(item, page_url, depth + 1)
            if found:
                return found

    return None


class DiskWalaDownloader:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/json,"
                      "application/json,text/plain,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.diskwala.com/",
        }

    async def get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=60)
            self.session = aiohttp.ClientSession(headers=self.headers, timeout=timeout)
        return self.session

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None

    def extract_file_id(self, url: str) -> Optional[str]:
        patterns = (
            r"/app/([A-Za-z0-9_-]+)",
            r"/file/([A-Za-z0-9_-]+)",
            r"/watch/([A-Za-z0-9_-]+)",
            r"/v/([A-Za-z0-9_-]+)",
        )
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                file_id = match.group(1)
                logger.info("Extracted file ID: %s", file_id)
                return file_id
        logger.error("Could not extract file ID from URL")
        return None

    async def get_video_info(self, url: str) -> Dict:
        file_id = self.extract_file_id(url)
        if not file_id:
            return {"error": "Invalid DiskWala URL"}

        try:
            session = await self.get_session()
            async with session.get(url, allow_redirects=True) as response:
                html = await response.text(errors="ignore")
            soup = BeautifulSoup(html, "html.parser")

            title = soup.title.get_text(" ", strip=True) if soup.title else "DiskWala Video"

            # Prefer OpenGraph/video metadata if available.
            for selector, attr in (
                ('meta[property="og:title"]', "content"),
                ('meta[property="twitter:title"]', "content"),
            ):
                node = soup.select_one(selector)
                if node and node.get(attr):
                    title = node.get(attr).strip()
                    break

            logger.info("Found video title: %s", title)
            return {"file_id": file_id, "url": url, "title": title, "download_url": None}
        except Exception as exc:
            logger.error("Error getting video info: %s", exc)
            return {"error": str(exc)}

    async def try_api_endpoints(self, file_id: str) -> Optional[str]:
        """
        Probe known public-style endpoints, but NEVER trust a generic URL
        field unless it passes media URL validation.
        """
        session = await self.get_session()
        endpoints = [
            f"https://www.diskwala.com/api/files/{file_id}",
            f"https://www.diskwala.com/api/v1/files/{file_id}",
            f"https://www.diskwala.com/api/file/{file_id}",
            f"https://www.diskwala.com/api/download/{file_id}",
            f"https://api.diskwala.com/files/{file_id}",
            f"https://api.diskwala.com/v1/files/{file_id}",
            f"https://www.diskwala.com/api/file/info/{file_id}",
            f"https://www.diskwala.com/api/stream/{file_id}",
        ]

        logger.info("Method 1: Trying API endpoints...")
        for endpoint in endpoints:
            try:
                async with session.get(endpoint, allow_redirects=True) as response:
                    if response.status != 200:
                        continue

                    content_type = (response.headers.get("content-type") or "").lower()
                    body = await response.text(errors="ignore")

                    if "json" not in content_type:
                        # Occasionally an endpoint returns a media redirect.
                        final_url = str(response.url)
                        if _is_valid_media_url(final_url):
                            logger.info("API endpoint redirected to media: %s", final_url)
                            return final_url
                        continue

                    try:
                        data = json.loads(body)
                    except json.JSONDecodeError:
                        continue

                    found = _find_media_url(data, str(response.url))
                    if found:
                        logger.info("✓ Valid media URL found from API endpoint")
                        return found

            except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
                logger.debug("API endpoint failed %s: %s", endpoint, type(exc).__name__)
            except Exception as exc:
                logger.debug("API parse error %s: %s", endpoint, exc)

        return None

    async def extract_with_playwright_enhanced(
        self, url: str, file_id: str
    ) -> Optional[str]:
        """
        Open the share page and collect:
        - media responses
        - response JSON containing media URLs
        - video/source tags
        - common data attributes
        """
        logger.info("Method 2: Using enhanced Playwright...")
        candidates = []

        def add_candidate(value: Any, source: str = ""):
            if not isinstance(value, str):
                return
            value = value.strip()
            if _is_valid_media_url(value, url):
                if value not in candidates:
                    logger.info("Captured valid media candidate (%s)", source or "unknown")
                    candidates.append(value)

        async def on_response(response):
            try:
                response_url = response.url
                resource_type = response.request.resource_type
                ctype = (response.headers.get("content-type") or "").lower()

                if _is_valid_media_url(response_url):
                    if (
                        resource_type in {"media", "xhr", "fetch", "document"}
                        or "video/" in ctype
                        or "mpegurl" in ctype
                        or "dash" in ctype
                    ):
                        add_candidate(response_url, "network response")

                # API/XHR JSON can contain the real CDN URL.
                if resource_type in {"xhr", "fetch"} and (
                    "json" in ctype or "javascript" in ctype or "text" in ctype
                ):
                    try:
                        text_body = await response.text()
                        if len(text_body) <= 2_000_000:
                            try:
                                parsed = json.loads(text_body)
                                add_candidate(_find_media_url(parsed, response_url), "response JSON")
                            except Exception:
                                # Also catch absolute URLs embedded in JSON/text.
                                for match in re.findall(
                                    r'https?://[^\s"\'<>\\]+', text_body
                                ):
                                    add_candidate(match, "response text")
                    except Exception:
                        pass
            except Exception:
                pass

        async def on_request(request):
            # Requests are useful when a player requests the media directly.
            try:
                if _is_valid_media_url(request.url):
                    add_candidate(request.url, "network request")
            except Exception:
                pass

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                    ],
                )
                context = await browser.new_context(
                    user_agent=self.headers["User-Agent"],
                    viewport={"width": 1365, "height": 900},
                    java_script_enabled=True,
                )
                page = await context.new_page()
                page.on("response", on_response)
                page.on("request", on_request)

                logger.info("Navigating to %s", url)
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                except PlaywrightTimeout:
                    logger.warning("Page load timeout; continuing with loaded content")

                # Let SPA/API calls settle.
                await page.wait_for_timeout(5000)

                # Try common player controls without assuming a specific UI.
                selectors = [
                    "video",
                    "button[aria-label*='play' i]",
                    "[class*='play' i]",
                    "[id*='play' i]",
                ]
                for selector in selectors:
                    try:
                        elements = await page.query_selector_all(selector)
                        for element in elements[:3]:
                            try:
                                await element.click(timeout=1500, force=True)
                                await page.wait_for_timeout(1500)
                            except Exception:
                                pass
                    except Exception:
                        pass

                # DOM/media metadata.
                dom_urls = await page.evaluate("""
                    () => {
                      const out = [];
                      document.querySelectorAll('video, audio, source').forEach(el => {
                        for (const k of ['src', 'currentSrc']) {
                          if (el[k]) out.push(el[k]);
                        }
                      });
                      document.querySelectorAll(
                        '[data-src],[data-video],[data-file],[data-url],[data-download]'
                      ).forEach(el => {
                        for (const k of ['data-src','data-video','data-file',
                                         'data-url','data-download']) {
                          const v = el.getAttribute(k);
                          if (v) out.push(v);
                        }
                      });
                      document.querySelectorAll('a[href]').forEach(a => {
                        const h = a.href || '';
                        if (/\\.(mp4|mkv|webm|mov|m4v|avi|m3u8|mpd)(\\?|$)/i.test(h) ||
                            /download|stream|media|cdn|storage/i.test(h)) out.push(h);
                      });
                      return out;
                    }
                """)
                for item in dom_urls or []:
                    add_candidate(item, "DOM")

                # Inspect performance entries too.
                perf_urls = await page.evaluate("""
                    () => performance.getEntriesByType('resource')
                        .map(x => x.name).filter(Boolean)
                """)
                for item in perf_urls or []:
                    add_candidate(item, "performance")

                await page.wait_for_timeout(2000)
                await browser.close()

        except Exception as exc:
            logger.error("Playwright extraction failed: %s", exc)

        # Prefer explicit media extensions, then streaming/download markers.
        def score(candidate: str) -> int:
            low = candidate.lower()
            score_value = 0
            if any(ext in low for ext in MEDIA_EXTENSIONS):
                score_value += 100
            if "download" in low:
                score_value += 30
            if "stream" in low or "media" in low:
                score_value += 20
            if "cdn" in low or "storage" in low:
                score_value += 10
            if file_id in low:
                score_value += 5
            return score_value

        candidates.sort(key=score, reverse=True)
        if candidates:
            logger.info("✓ Selected media URL from %d candidates", len(candidates))
            return candidates[0]

        return None

    async def get_download_link(self, url: str) -> Optional[str]:
        file_id = self.extract_file_id(url)
        if not file_id:
            return None

        download_url = await self.try_api_endpoints(file_id)
        if download_url and _is_valid_media_url(download_url, url):
            return download_url

        download_url = await self.extract_with_playwright_enhanced(url, file_id)
        if download_url and _is_valid_media_url(download_url, url):
            return download_url

        logger.error(
            "No valid media URL found. Analytics/tracking/page URLs are rejected."
        )
        return None

    async def download_video(
        self, url: str, output_path: Path, progress_callback=None
    ) -> bool:
        try:
            download_url = await self.get_download_link(url)
            if not download_url:
                return False

            session = await self.get_session()
            request_headers = {
                "User-Agent": self.headers["User-Agent"],
                "Referer": url,
                "Accept": "*/*",
            }

            async with session.get(
                download_url,
                headers=request_headers,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=900),
            ) as response:
                if response.status not in (200, 206):
                    logger.error("Download failed with HTTP %s", response.status)
                    return False

                content_type = (response.headers.get("content-type") or "").lower()
                final_url = str(response.url)

                # If a CDN redirects to HTML/analytics, do not save that page as MP4.
                if not _is_valid_media_url(final_url, url):
                    logger.error("Final download URL rejected: %s", final_url)
                    return False
                if "text/html" in content_type or "application/json" in content_type:
                    logger.error("Download endpoint returned %s, not media", content_type)
                    return False

                total_size = int(response.headers.get("content-length") or 0)
                downloaded = 0

                output_path = Path(output_path)
                output_path.parent.mkdir(parents=True, exist_ok=True)

                async with aiofiles.open(output_path, "wb") as file_obj:
                    async for chunk in response.content.iter_chunked(1024 * 256):
                        if not chunk:
                            continue
                        await file_obj.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback and total_size > 0:
                            progress = downloaded / total_size * 100
                            await progress_callback(downloaded, total_size, progress)

                # Basic sanity check: never report an empty/HTML file as success.
                if downloaded < 1024:
                    logger.error("Downloaded file is too small (%d bytes)", downloaded)
                    return False

                with open(output_path, "rb") as check:
                    head = check.read(512).lower()
                if b"<html" in head or b"<!doctype" in head:
                    logger.error("Downloaded response is HTML, not a video")
                    try:
                        output_path.unlink()
                    except Exception:
                        pass
                    return False

                logger.info("✓ Download completed: %s (%d bytes)", output_path, downloaded)
                return True

        except Exception as exc:
            logger.error("Download error: %s", exc, exc_info=True)
            try:
                if Path(output_path).exists():
                    Path(output_path).unlink()
            except Exception:
                pass
            return False


async def extract_video_info(url: str) -> Dict:
    downloader = DiskWalaDownloader()
    try:
        return await downloader.get_video_info(url)
    finally:
        await downloader.close()


async def get_download_link(url: str) -> Optional[str]:
    downloader = DiskWalaDownloader()
    try:
        return await downloader.get_download_link(url)
    finally:
        await downloader.close()
