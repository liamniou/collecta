"""Stage 1: turn a link into consolidated text + downloaded images.

Parsing is strategy-based (see content_types.yaml / content_kb):
  video  -> yt-dlp media + whisper transcript + thumbnails
  images -> post images (+caption) for vision
  text   -> written text only (Reddit / article scrape)
  auto   -> try media, then Reddit/website text (the original behaviour)
"""
import glob
import os
from urllib.parse import urljoin, urlparse

import requests
import trafilatura
import yt_dlp
from bs4 import BeautifulSoup

from . import config, content_kb

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
_MAX_IMAGES = 6


def ingest(url: str, work_dir: str, content_type: str = "auto") -> dict:
    """Return {"source_text": str, "image_paths": [str, ...]} using the chosen strategy."""
    os.makedirs(work_dir, exist_ok=True)
    if content_type == "pasted_text":
        # No link to fetch — `url` holds the raw text the user pasted.
        return _finish([url], [])
    strategy = content_kb.strategy_for(content_type) if content_type else "auto"
    print(f"[ingest] {url} as content_type={content_type} strategy={strategy}")
    if strategy == "video":
        return _ingest_video(url, work_dir)
    if strategy == "images":
        return _ingest_images(url, work_dir)
    if strategy == "reddit_post":
        return _ingest_reddit(url, work_dir)
    if strategy == "text":
        return _ingest_text(url, work_dir)
    if strategy == "list_page":
        return _ingest_list_page(url)
    return _ingest_auto(url, work_dir)


def _finish(texts: list[str], images: list[str], max_images: int = _MAX_IMAGES) -> dict:
    seen, unique = set(), []
    for p in images:
        if p not in seen and os.path.exists(p):
            seen.add(p)
            unique.append(p)
    unique = unique[:max_images]
    source_text = "\n\n".join(t for t in texts if t and t.strip()).strip()
    return {"source_text": source_text, "image_paths": unique}


def _ingest_video(url: str, work_dir: str) -> dict:
    texts: list[str] = []
    media = _download_with_ytdlp(url, work_dir)
    if media["captions"]:
        texts.append(media["captions"])
    audio_paths = list(media["audio_paths"])
    images = list(media["image_paths"])

    # Instagram (and similar) block yt-dlp without login — render the page and pull the
    # caption + embedded video, which whisper-asr can transcribe (ffmpeg-decodes mp4).
    if not audio_paths and not media["had_media"]:
        page = _scrape_video_page(url, work_dir)
        if page["caption"]:
            texts.append(page["caption"])
        if page["video_path"]:
            audio_paths = [page["video_path"]]

    if audio_paths:
        transcript = _transcribe(audio_paths[0])
        if transcript:
            texts.append(f"[transcript]\n{transcript}")
    return _finish(texts, images)


def _scrape_video_page(url: str, work_dir: str) -> dict:
    """Render a blocked video page (e.g. Instagram reel) to grab caption + a progressive mp4."""
    import html as htmllib
    import re

    rendered = _fetch_with_camoufox(url)
    if not rendered:
        return {"caption": "", "video_path": None}

    soup = BeautifulSoup(rendered, "lxml")
    caption_parts = []
    for prop in ("og:title", "og:description"):
        tag = soup.find("meta", attrs={"property": prop})
        if tag and tag.get("content"):
            caption_parts.append(tag["content"].strip())
    caption = "\n".join(dict.fromkeys(caption_parts)).strip()

    raw = rendered.replace("\\u0026", "&").replace("\\/", "/")
    m = re.search(r'"video_versions":\[\{[^\]]*?"url":"([^"]+\.mp4[^"]*)"', raw)
    if not m:
        m = re.search(r'https://[^"\\ ]+?\.mp4[^"\\ ]*', raw)
    video_url = htmllib.unescape(m.group(1) if m and m.groups() else (m.group(0) if m else "")) if m else ""
    video_path = _download_file(video_url, work_dir, "reel.mp4") if video_url else None
    return {"caption": caption, "video_path": video_path}


def _download_file(url: str, work_dir: str, name: str) -> str | None:
    dest = os.path.join(work_dir, name)
    try:
        with requests.get(url, headers={"User-Agent": _UA}, timeout=180, stream=True) as r:
            r.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in r.iter_content(65536):
                    fh.write(chunk)
        return dest if os.path.getsize(dest) > 10000 else None
    except Exception as exc:  # noqa: BLE001
        print(f"[ingest] video download failed: {exc}")
        return None


def _ingest_images(url: str, work_dir: str) -> dict:
    img_max = 25  # carousels can have many slides (one movie per card)
    texts: list[str] = []
    images: list[str] = []
    host = urlparse(url).netloc.lower()

    if any(s in host for s in ("threads.net", "threads.com", "instagram.com")):
        meta = _scrape_meta_carousel(url, work_dir, img_max)
        if meta["text"]:
            texts.append(meta["text"])
        images.extend(meta["image_paths"])
    else:
        media = _download_with_ytdlp(url, work_dir)  # carousels / thumbnails
        if media["captions"]:
            texts.append(media["captions"])
        images.extend(media["image_paths"])
        # Social image posts are JS-heavy — render so lazy-loaded images and caption appear.
        rendered = _fetch_with_camoufox(url)
        extracted = _extract_from_html(rendered, url, work_dir) if rendered else _scrape_website(url, work_dir)
        if extracted["text"]:
            texts.append(extracted["text"])
        images.extend(extracted["image_paths"])
    return _finish(texts, images, max_images=img_max)


def _scrape_meta_carousel(url: str, work_dir: str, limit: int) -> dict:
    """Instagram/Threads lazy-load slides but embed them as `image_versions2` JSON.
    Select blocks whose `efg` tag decodes to CAROUSEL_ITEM (a carousel's slides); for a
    single-image post fall back to `regular_photo`. This excludes avatars/feed images."""
    import base64
    import html as htmllib
    import re

    rendered = _fetch_with_camoufox(url)
    if not rendered:
        return {"text": "", "image_paths": []}

    text = ""
    soup = BeautifulSoup(rendered, "lxml")
    for prop in ("og:description", "twitter:description"):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if tag and tag.get("content"):
            text = tag["content"].strip()
            break

    def collect(marker: str) -> dict:
        found: dict[str, str] = {}
        for blk in rendered.split('"image_versions2"')[1:]:
            m = re.search(r'"url":"(https:[^"]+?\.(?:jpg|webp)[^"]*)"', blk)
            if not m:
                continue
            u = htmllib.unescape(m.group(1).replace("\\u0026", "&").replace("\\/", "/"))
            efg = re.search(r"efg=([A-Za-z0-9_\-]+)", u)
            if not efg:
                continue
            pad = efg.group(1) + "=" * (-len(efg.group(1)) % 4)
            try:
                if marker not in base64.urlsafe_b64decode(pad).decode("utf-8", "ignore"):
                    continue
            except Exception:  # noqa: BLE001
                continue
            mid = re.search(r"/([0-9]{8,})_", u)
            found.setdefault(mid.group(1) if mid else u, u)
        return found

    by_id = collect("CAROUSEL_ITEM") or collect("regular_photo")
    image_paths = _download_images(list(by_id.values()), work_dir, limit=limit)
    print(f"[ingest] meta carousel: {len(by_id)} image(s), downloaded {len(image_paths)}")
    return {"text": text, "image_paths": image_paths}


def _ingest_text(url: str, work_dir: str) -> dict:
    host = urlparse(url).netloc.lower()
    if "reddit.com" in host or "redd.it" in host:
        result = _scrape_reddit(url, work_dir)
    else:
        result = _scrape_website(url, work_dir)
    return _finish([result["text"]], [])  # text strategy ignores images


def _ingest_reddit(url: str, work_dir: str) -> dict:
    # Post body only (title + selftext); comments are intentionally excluded.
    result = _scrape_reddit(url, work_dir)
    return _finish([result["text"]], [])


_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_DROP_TAGS = ("script", "style", "sup", "noscript", "figure")
_DROP_CLASSES = ("mw-editsection", "reference", "navbox", "reflist", "mw-references-wrap", "noprint")


def _ingest_list_page(url: str) -> dict:
    """Structured list/table page: keep row structure, optionally scoped to the #fragment section."""
    page_url, _, fragment = url.partition("#")
    html = _http_get_html(page_url) or _fetch_with_camoufox(page_url)
    if not html:
        return _finish([], [])
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(_DROP_TAGS):
        tag.decompose()
    for cls in _DROP_CLASSES:
        for tag in soup.find_all(class_=cls):
            tag.decompose()

    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    nodes = _section_nodes(soup, fragment) if fragment else None
    if nodes is None:
        if fragment:
            print(f"[ingest] section #{fragment} not found; using the whole page")
        root = soup.find(id="mw-content-text") or soup.find("main") or soup.find("article") or soup.body
        nodes = [root] if root else []
    lines: list[str] = [f"Page: {title}" + (f" — section: {fragment.replace('_', ' ')}" if fragment else "")]
    for node in nodes:
        _render_node(node, lines)
    text = "\n".join(ln for ln in lines if ln.strip())
    print(f"[ingest] list page: {len(text)} chars" + (f" (section #{fragment})" if fragment else ""))
    return _finish([text], [])


def _section_nodes(soup, fragment: str):
    """Elements belonging to the section whose heading has id=fragment, or None if not found."""
    from urllib.parse import unquote

    target = soup.find(id=fragment) or soup.find(id=unquote(fragment))
    if target is None:
        return None
    heading = target if target.name in _HEADINGS else target.find_parent(_HEADINGS)
    if heading is None:
        # Not a heading (e.g. an anchored table/list) — use the element itself.
        return [target]
    level = int(heading.name[1])
    # MediaWiki wraps headings in <div class="mw-heading">; siblings live next to the wrapper.
    start = heading.parent if "mw-heading" in (heading.parent.get("class") or []) else heading
    nodes = []
    for sib in start.find_next_siblings():
        h = sib if sib.name in _HEADINGS else (sib.find(_HEADINGS) if "mw-heading" in (sib.get("class") or []) else None)
        if h is not None and int(h.name[1]) <= level:
            break
        nodes.append(sib)
    return nodes


def _render_node(node, lines: list[str]) -> None:
    """Flatten HTML into text lines; table rows become `cell | cell | ...`."""
    if getattr(node, "name", None) is None:
        return
    if node.name == "table":
        for row in node.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["th", "td"])]
            if any(cells):
                lines.append(" | ".join(cells))
        return
    if node.name in _HEADINGS:
        lines.append(f"\n## {node.get_text(' ', strip=True)}")
        return
    if node.name in ("p", "li", "dd", "dt", "caption"):
        lines.append(node.get_text(" ", strip=True))
        return
    children = [c for c in node.children if getattr(c, "name", None)]
    if not children:
        lines.append(node.get_text(" ", strip=True))
        return
    for child in children:
        _render_node(child, lines)


def _ingest_auto(url: str, work_dir: str) -> dict:
    texts: list[str] = []
    images: list[str] = []

    media = _download_with_ytdlp(url, work_dir)
    if media["captions"]:
        texts.append(media["captions"])
    if media["audio_paths"]:
        transcript = _transcribe(media["audio_paths"][0])
        if transcript:
            texts.append(f"[transcript]\n{transcript}")
    images.extend(media["image_paths"])

    host = urlparse(url).netloc.lower()
    if "reddit.com" in host or "redd.it" in host:
        reddit = _scrape_reddit(url, work_dir)
        if reddit["text"]:
            texts.append(reddit["text"])
        images.extend(reddit["image_paths"])
    elif len(" ".join(texts)) < 200 or not media["had_media"]:
        scraped = _scrape_website(url, work_dir)
        if scraped["text"]:
            texts.append(scraped["text"])
        images.extend(scraped["image_paths"])

    return _finish(texts, images)


def _download_with_ytdlp(url: str, work_dir: str) -> dict:
    captions: list[str] = []
    had_media = False
    opts = {
        "outtmpl": os.path.join(work_dir, "%(id)s.%(ext)s"),
        "format": "bestaudio/best",
        "writethumbnail": True,
        "noplaylist": False,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}
        ],
    }
    if config.YTDLP_COOKIES and os.path.exists(config.YTDLP_COOKIES):
        opts["cookiefile"] = config.YTDLP_COOKIES  # login cookies for Instagram/etc.
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:  # noqa: BLE001 - yt-dlp raises many types
        print(f"[ingest] yt-dlp failed: {exc}")
        info = None

    if info:
        entries = info.get("entries") if info.get("entries") else [info]
        for entry in entries:
            if not entry:
                continue
            had_media = True
            for key in ("title", "description"):
                val = (entry.get(key) or "").strip()
                if val:
                    captions.append(val)

    audio_paths = sorted(glob.glob(os.path.join(work_dir, "*.mp3")))
    image_paths = [
        p for p in sorted(glob.glob(os.path.join(work_dir, "*")))
        if p.lower().endswith(_IMAGE_EXTS)
    ]
    return {
        "captions": "\n".join(dict.fromkeys(captions)),
        "audio_paths": audio_paths,
        "image_paths": image_paths,
        "had_media": had_media,
    }


def _transcribe(audio_path: str) -> str:
    try:
        with open(audio_path, "rb") as fh:
            resp = requests.post(
                f"{config.WHISPER_ASR_URL}/asr",
                params={"task": "transcribe", "output": "txt", "encode": "true"},
                files={"audio_file": (os.path.basename(audio_path), fh, "audio/mpeg")},
                timeout=600,
            )
        resp.raise_for_status()
        return resp.text.strip()
    except Exception as exc:  # noqa: BLE001
        print(f"[ingest] transcription failed: {exc}")
        return ""


def _scrape_website(url: str, work_dir: str) -> dict:
    html = _http_get_html(url)
    result = _extract_from_html(html, url, work_dir) if html else {"text": "", "image_paths": []}
    # If plain HTTP was blocked or returned thin content, render with a stealth browser.
    if len(result["text"]) < 200:
        rendered = _fetch_with_camoufox(url)
        if rendered:
            rendered_result = _extract_from_html(rendered, url, work_dir)
            if len(rendered_result["text"]) > len(result["text"]):
                return rendered_result
    return result


def _http_get_html(url: str) -> str:
    try:
        resp = requests.get(url, headers={"User-Agent": _UA}, timeout=30)
        resp.raise_for_status()
        return resp.text
    except Exception as exc:  # noqa: BLE001
        print(f"[ingest] page fetch failed: {exc}")
        return ""


def _fetch_with_camoufox(url: str) -> str:
    """Render a page with the Camoufox stealth browser and return its HTML."""
    import asyncio

    from camoufox.async_api import AsyncCamoufox

    async def _run() -> str:
        async with AsyncCamoufox(humanize=True, geoip=True, headless=True) as browser:
            page = await browser.new_page()
            await page.goto(url, timeout=45000, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:  # noqa: BLE001
                pass
            return await page.content()

    try:
        print(f"[ingest] rendering with camoufox: {url}")
        return asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        print(f"[ingest] camoufox render failed: {exc}")
        return ""


def _extract_from_html(html: str, url: str, work_dir: str) -> dict:
    text = ""
    image_urls: list[str] = []

    extracted = trafilatura.extract(html, include_comments=False, favor_recall=True)
    if extracted:
        text = extracted.strip()

    soup = BeautifulSoup(html, "lxml")
    # Prefer social/meta description and image when main text is thin.
    for prop in ("og:description", "twitter:description", "description"):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if tag and tag.get("content") and len(text) < 200:
            text = f"{text}\n{tag['content'].strip()}".strip()
    for prop in ("og:image", "twitter:image"):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if tag and tag.get("content"):
            image_urls.append(urljoin(url, tag["content"]))
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if src:
            image_urls.append(urljoin(url, src))

    image_paths = _download_images(image_urls, work_dir)
    return {"text": text, "image_paths": image_paths}



def _scrape_reddit(url: str, work_dir: str) -> dict:
    """Render the Reddit post with Camoufox and read the full selftext + comments.

    Reddit gates every plain-HTTP path from a datacenter IP (`.json` -> 403, old.reddit ->
    login wall), but the new web UI ("shreddit") serves the complete post body once rendered.
    We resolve /s/ share links, then pull the title, `[slot="text-body"]`, and comments.
    """
    final_url = url
    try:
        resp = requests.get(url, headers={"User-Agent": _UA}, timeout=30, allow_redirects=True)
        final_url = resp.url.split("?")[0]
    except Exception as exc:  # noqa: BLE001
        print(f"[ingest] reddit resolve failed: {exc}")

    text = _render_reddit_with_camoufox(final_url)
    if not text:
        # Fall back to the generic renderer if the selectors changed.
        return _scrape_website(final_url, work_dir)
    return {"text": text, "image_paths": []}


def _render_reddit_with_camoufox(url: str) -> str:
    import asyncio

    from camoufox.async_api import AsyncCamoufox

    extract_js = """() => {
        const title = (document.querySelector('h1') || {}).innerText || '';
        const bodyEl = document.querySelector('shreddit-post [slot="text-body"]')
            || document.querySelector('[slot="text-body"]');
        const body = bodyEl ? bodyEl.innerText : '';
        return { title, body };
    }"""

    async def _run() -> dict:
        async with AsyncCamoufox(humanize=True, geoip=True, headless=True, locale="en-US") as browser:
            page = await browser.new_page()
            await page.goto(url, timeout=45000, wait_until="domcontentloaded")
            # The post body lazy-loads; wait for it explicitly before reading.
            try:
                await page.wait_for_selector(
                    'shreddit-post [slot="text-body"], [slot="text-body"]', timeout=20000
                )
            except Exception:  # noqa: BLE001
                pass
            return await page.evaluate(extract_js)

    try:
        print(f"[ingest] rendering reddit with camoufox: {url}")
        data = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        print(f"[ingest] reddit camoufox render failed: {exc}")
        return ""

    parts = [data.get("title", ""), data.get("body", "")]
    return "\n\n".join(p for p in parts if p and p.strip()).strip()


def _download_images(urls: list[str], work_dir: str, limit: int = _MAX_IMAGES) -> list[str]:
    paths: list[str] = []
    for i, u in enumerate(dict.fromkeys(urls)):
        if len(paths) >= limit:
            break
        ext = os.path.splitext(urlparse(u).path)[1].lower()
        if ext not in _IMAGE_EXTS:
            ext = ".jpg"
        dest = os.path.join(work_dir, f"web_{i}{ext}")
        try:
            resp = requests.get(u, headers={"User-Agent": _UA}, timeout=30)
            resp.raise_for_status()
            if len(resp.content) < 4096:  # skip icons/spacers
                continue
            with open(dest, "wb") as fh:
                fh.write(resp.content)
            paths.append(dest)
        except Exception:  # noqa: BLE001
            continue
    return paths
