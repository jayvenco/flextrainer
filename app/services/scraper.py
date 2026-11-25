"""Fetch exam material from online pages. SSRF-safe: only http(s), public addresses unless allowed."""

from __future__ import annotations

import base64
import ipaddress
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from ..config import settings
from .extract import Doc, ExtractError, extract_document
from .images import MAX_INPUT_BYTES, marker, sniff

MAX_BYTES = 4 * 1024 * 1024
MAX_REDIRECTS = 4
UA = f"FlexTrainer/{settings.version} (+self-hosted exam trainer)"


class ScrapeError(Exception):
    pass


def check_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ScrapeError("Only http:// and https:// URLs are supported.")
    if settings.allow_private_scrape:
        return parsed.geturl()
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise ScrapeError(f"Could not resolve {parsed.hostname}.") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ScrapeError("That address is not public. Set ALLOW_PRIVATE_SCRAPE=true to allow local addresses.")
    return parsed.geturl()


class _Html(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form", "iframe"}
    BLOCK = {
        "p",
        "div",
        "br",
        "li",
        "tr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "section",
        "article",
        "ul",
        "ol",
        "table",
        "dd",
        "dt",
        "label",
        "option",
        "blockquote",
        "pre",
    }

    def __init__(self, base_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.images: dict[str, bytes | str] = {}
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "img" and not self._skip and len(self.images) < 60:
            a = dict(attrs)
            src = (a.get("src") or a.get("data-src") or a.get("data-original") or "").strip()
            key = f"h{len(self.images) + 1}"
            if src.startswith("data:image/") and ";base64," in src:
                try:
                    self.images[key] = base64.b64decode(src.split(";base64,", 1)[1])
                except ValueError:
                    key = ""
            elif src and self.base_url and not src.lower().startswith(("data:", "javascript:")):
                self.images[key] = urljoin(self.base_url, src)
            else:
                key = ""
            if key:
                self.parts += ["\n", marker(key), "\n"]
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP:
            self._skip += 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if not self._skip:
            self.parts.append(data)


def html_to_doc(html: str, base_url: str = "") -> tuple[Doc, str]:
    """Plain text with picture markers (when base_url is given) and the picture sources, plus the page title."""
    p = _Html(base_url)
    p.feed(html)
    text = "".join(p.parts)
    lines = [" ".join(ln.split()) for ln in text.split("\n")]
    return Doc("\n".join(ln for ln in lines if ln), p.images), " ".join(p.title.split())


def html_to_text(html: str) -> tuple[str, str]:
    doc, title = html_to_doc(html)
    return doc.text, title


def _get(url: str, accept: str, max_bytes: int) -> tuple[bytes, str, str, str]:
    """SSRF-safe GET (every redirect target is checked). Returns (data, content type, final URL, encoding)."""
    current = check_url(url)
    headers = {"User-Agent": UA, "Accept": accept}
    try:
        with httpx.Client(timeout=15, follow_redirects=False, headers=headers) as client:
            for _ in range(MAX_REDIRECTS + 1):
                with client.stream("GET", current) as resp:
                    if resp.is_redirect:
                        current = check_url(urljoin(current, resp.headers.get("location", "")))
                        continue
                    if resp.status_code >= 400:
                        raise ScrapeError(f"The site answered with HTTP {resp.status_code}.")
                    chunks, size = [], 0
                    for chunk in resp.iter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise ScrapeError(f"The response is larger than {max_bytes // (1024 * 1024)} MB.")
                        chunks.append(chunk)
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    return b"".join(chunks), ctype, current, resp.encoding or "utf-8"
            raise ScrapeError("Too many redirects.")
    except httpx.HTTPError as exc:
        raise ScrapeError(f"Could not fetch the page: {exc.__class__.__name__}") from exc


def fetch_image(url: str) -> bytes | None:
    """Download one picture (public hosts only); None when it is not a usable PNG/JPEG/GIF/WebP."""
    try:
        data, _, _, _ = _get(url, "image/png,image/jpeg,image/gif,image/webp,*/*;q=0.2", MAX_INPUT_BYTES)
    except ScrapeError:
        return None
    return data if sniff(data) else None


def fetch(url: str) -> tuple[Doc, str]:
    """Return (document with text and picture sources, title) for a URL (HTML, PDF, Markdown or text)."""
    data, ctype, current, enc = _get(
        url, "text/html,application/pdf,text/plain,text/markdown;q=0.9,*/*;q=0.5", MAX_BYTES
    )
    name = urlparse(current).path.rsplit("/", 1)[-1] or urlparse(current).hostname or "page"
    try:
        if ctype == "application/pdf" or current.lower().split("?")[0].endswith(".pdf"):
            return extract_document("page.pdf", data), name
        if ctype in {"text/plain", "text/markdown"}:
            return Doc(data.decode(enc, errors="replace")), name
        doc, title = html_to_doc(data.decode(enc, errors="replace"), current)
        return doc, title or name
    except ExtractError as exc:
        raise ScrapeError(str(exc)) from exc
