"""Fetch exam material from online pages. SSRF-safe: only http(s), public addresses unless allowed."""

from __future__ import annotations

import ipaddress
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from ..config import settings
from .extract import ExtractError, extract_text

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

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
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


def html_to_text(html: str) -> tuple[str, str]:
    p = _Html()
    p.feed(html)
    text = "".join(p.parts)
    lines = [" ".join(ln.split()) for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln), " ".join(p.title.split())


def fetch(url: str) -> tuple[str, str]:
    """Return (plain text, title) for a URL (HTML, PDF, Markdown or text)."""
    current = check_url(url)
    headers = {"User-Agent": UA, "Accept": "text/html,application/pdf,text/plain,text/markdown;q=0.9,*/*;q=0.5"}
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
                        if size > MAX_BYTES:
                            raise ScrapeError("The page is larger than 4 MB.")
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    enc = resp.encoding or "utf-8"
                    break
            else:
                raise ScrapeError("Too many redirects.")
    except httpx.HTTPError as exc:
        raise ScrapeError(f"Could not fetch the page: {exc.__class__.__name__}") from exc
    name = urlparse(current).path.rsplit("/", 1)[-1] or urlparse(current).hostname or "page"
    try:
        if ctype == "application/pdf" or current.lower().split("?")[0].endswith(".pdf"):
            return extract_text("page.pdf", data), name
        if ctype in {"text/plain", "text/markdown"}:
            return data.decode(enc, errors="replace"), name
        text, title = html_to_text(data.decode(enc, errors="replace"))
        return text, title or name
    except ExtractError as exc:
        raise ScrapeError(str(exc)) from exc
