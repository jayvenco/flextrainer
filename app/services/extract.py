"""Plain-text extraction from uploaded files: PDF, Word (.docx), Markdown and text. Dependency-light."""

from __future__ import annotations

import base64
import io
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field

from .images import marker, strip_markers

ALLOWED = {".pdf", ".docx", ".md", ".markdown", ".txt"}
MAX_BYTES = 25 * 1024 * 1024
MAX_PDF_PAGES = 600

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_V = "{urn:schemas-microsoft-com:vml}"
MAX_IMAGES = 300


class ExtractError(Exception):
    pass


@dataclass
class Doc:
    """Text with picture markers (see images.marker) plus the pictures: bytes, or a URL to fetch later."""

    text: str
    images: dict[str, bytes | str] = field(default_factory=dict)


def extract_text(filename: str, data: bytes) -> str:
    return strip_markers(extract_document(filename, data).text)


def extract_document(filename: str, data: bytes) -> Doc:
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if ext not in ALLOWED:
        raise ExtractError(f"Unsupported file type {ext or '(none)'}. Use PDF, Word (.docx), Markdown or text.")
    if len(data) > MAX_BYTES:
        raise ExtractError("File is larger than 25 MB.")
    if ext == ".pdf":
        return pdf_doc(data)
    if ext == ".docx":
        return docx_doc(data)
    text = data.decode("utf-8", errors="replace")
    if ext in (".md", ".markdown"):
        return markdown_doc(text)
    return Doc(text)


_MD_IMG = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")


def markdown_doc(text: str) -> Doc:
    """Markdown pictures: data: URIs are decoded, http(s) URLs are fetched at import, relative paths are skipped."""
    images: dict[str, bytes | str] = {}

    def repl(m: re.Match) -> str:
        src = m.group(1)
        if len(images) >= MAX_IMAGES:
            return ""
        key = f"m{len(images) + 1}"
        if src.startswith("data:image/") and ";base64," in src:
            try:
                images[key] = base64.b64decode(src.split(";base64,", 1)[1], validate=False)
            except ValueError:
                return ""
        elif src.lower().startswith(("http://", "https://")):
            images[key] = src
        else:
            return ""  # a relative path cannot be resolved from a single uploaded file
        return "\n" + marker(key) + "\n"

    return Doc(_MD_IMG.sub(repl, text), images)


def _insert_markers(text: str, spots: list[tuple[int, str]]) -> str:
    """Insert picture markers after the n-th non-whitespace character of text (spots: (n, key))."""
    if not spots:
        return text
    spots = sorted(spots, key=lambda x: x[0])
    out, count, si = [], 0, 0
    for ch in text:
        while si < len(spots) and count >= spots[si][0]:
            out.append("\n" + marker(spots[si][1]) + "\n")
            si += 1
        out.append(ch)
        if not ch.isspace():
            count += 1
    for _, key in spots[si:]:
        out.append("\n" + marker(key) + "\n")
    return "".join(out)


def pdf_doc(data: bytes) -> Doc:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:  # pragma: no cover
        raise ExtractError("PDF support is not installed.") from exc
    images: dict[str, bytes | str] = {}
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ExtractError("This PDF is password protected.")
        parts = []
        for page in reader.pages[:MAX_PDF_PAGES]:
            parts.append(_pdf_page(page, images))
    except ExtractError:
        raise
    except (PdfReadError, ValueError, KeyError, OSError) as exc:
        raise ExtractError(f"Could not read the PDF: {exc}") from exc
    text = "\n".join(parts)
    if not strip_markers(text).strip():
        raise ExtractError("No text found in the PDF (is it a scan? OCR is not supported).")
    return Doc(text, images)


def _pdf_page(page, images: dict[str, bytes | str]) -> str:
    """Page text with a marker where each picture is drawn (content-stream order follows reading order)."""
    seen = [0]  # non-whitespace characters emitted so far
    drawn: list[tuple[int, str]] = []

    def on_text(text, *_a):
        seen[0] += sum(1 for c in text if not c.isspace())

    def on_op(op, args, *_a):
        if op == b"Do" and args:
            drawn.append((seen[0], str(args[0]).lstrip("/")))

    try:
        text = page.extract_text(visitor_operand_before=on_op, visitor_text=on_text) or ""
    except Exception:  # noqa: BLE001 - fall back to plain extraction
        text = page.extract_text() or ""
    if len(images) >= MAX_IMAGES:
        return text
    try:
        found = {}
        for img in page.images:
            found[img.name.rsplit(".", 1)[0]] = img
    except Exception:  # noqa: BLE001 - unsupported image encodings are skipped
        return text
    spots: list[tuple[int, str]] = []
    used: set[str] = set()
    for pos, name in drawn:
        img = found.get(name)
        if img is None or name in used or len(images) >= MAX_IMAGES:
            continue
        used.add(name)
        key = f"p{len(images) + 1}"
        images[key] = img.data
        spots.append((pos, key))
    for name, img in found.items():  # pictures inside nested forms: put them at the end of the page
        if name not in used and len(images) < MAX_IMAGES:
            key = f"p{len(images) + 1}"
            images[key] = img.data
            spots.append((10**9, key))
    return _insert_markers(text, spots)


def _numbering(zf: zipfile.ZipFile) -> dict[tuple[str, str], str]:
    """Map (numId, ilvl) -> numFmt so auto-numbered Word lists keep their A./B./1. labels."""
    if "word/numbering.xml" not in zf.namelist():
        return {}
    root = ET.fromstring(zf.read("word/numbering.xml"))
    abstract: dict[str, dict[str, str]] = {}
    for an in root.iter(_W + "abstractNum"):
        levels = {}
        for lvl in an.iter(_W + "lvl"):
            fmt = lvl.find(_W + "numFmt")
            levels[lvl.get(_W + "ilvl", "0")] = fmt.get(_W + "val", "") if fmt is not None else ""
        abstract[an.get(_W + "abstractNumId", "")] = levels
    out: dict[tuple[str, str], str] = {}
    for num in root.iter(_W + "num"):
        aid = num.find(_W + "abstractNumId")
        if aid is None:
            continue
        for ilvl, fmt in abstract.get(aid.get(_W + "val", ""), {}).items():
            out[(num.get(_W + "numId", ""), ilvl)] = fmt
    return out


def _rels(zf: zipfile.ZipFile) -> dict[str, str]:
    """Relationship id -> path inside the .docx for pictures."""
    name = "word/_rels/document.xml.rels"
    if name not in zf.namelist():
        return {}
    out = {}
    for rel in ET.fromstring(zf.read(name)).iter(_REL + "Relationship"):
        if rel.get("Type", "").endswith("/image") and rel.get("TargetMode") != "External":
            target = rel.get("Target", "")
            out[rel.get("Id", "")] = (
                target.lstrip("/") if target.startswith("/") else posixpath.normpath("word/" + target)
            )
    return out


def _label(fmt: str, n: int) -> str:
    if fmt == "upperLetter":
        return f"{chr(64 + (n - 1) % 26 + 1)}. "
    if fmt == "lowerLetter":
        return f"{chr(96 + (n - 1) % 26 + 1)}. "
    if fmt == "decimal":
        return f"{n}. "
    if fmt == "bullet":
        return "- "
    return ""


def docx_doc(data: bytes) -> Doc:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        if zf.getinfo("word/document.xml").file_size > 60 * 1024 * 1024:
            raise ExtractError("The Word document is too large.")
        root = ET.fromstring(zf.read("word/document.xml"))
        fmts = _numbering(zf)
        rels = _rels(zf)
    except (zipfile.BadZipFile, KeyError, ET.ParseError, ValueError, OSError, NotImplementedError) as exc:
        raise ExtractError("Could not read the Word document (is it a real .docx?).") from exc
    counters: dict[tuple[str, str], int] = {}
    lines: list[str] = []
    images: dict[str, bytes | str] = {}
    for p in root.iter(_W + "p"):
        prefix = ""
        num_pr = p.find(f"{_W}pPr/{_W}numPr")
        if num_pr is not None:
            ilvl_el, nid_el = num_pr.find(_W + "ilvl"), num_pr.find(_W + "numId")
            ilvl = ilvl_el.get(_W + "val", "0") if ilvl_el is not None else "0"
            nid = nid_el.get(_W + "val", "") if nid_el is not None else ""
            if nid and nid != "0":
                counters[(nid, ilvl)] = counters.get((nid, ilvl), 0) + 1
                for key in [k for k in counters if k[0] == nid and int(k[1]) > int(ilvl)]:
                    counters[key] = 0
                prefix = _label(fmts.get((nid, ilvl), ""), counters[(nid, ilvl)])
        buf = []
        for el in p.iter():
            if el.tag == _W + "t" and el.text:
                buf.append(el.text)
            elif el.tag in (_W + "tab",):
                buf.append(" ")
            elif el.tag == _W + "br":
                buf.append("\n")
        text = "".join(buf).strip()
        if text:
            lines.append(prefix + text)
        for el in (e for e in p.iter() if e.tag in (_A + "blip", _V + "imagedata")):
            rid = el.get(_R + "embed") or el.get(_R + "id")
            path = rels.get(rid or "")
            if path and path in zf.namelist() and len(images) < MAX_IMAGES:
                key = f"w{len(images) + 1}"
                images[key] = zf.read(path)
                lines.append(marker(key))
    return Doc("\n".join(lines), images)


def clean_markdown(text: str) -> str:
    """Strip emphasis markers that would hide patterns like '**A.** text'."""
    text = re.sub(r"(\*\*|__)", "", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)
    return text.replace("`", "")
