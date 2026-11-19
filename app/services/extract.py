"""Plain-text extraction from uploaded files: PDF, Word (.docx), Markdown and text. Dependency-light."""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile

ALLOWED = {".pdf", ".docx", ".md", ".markdown", ".txt"}
MAX_BYTES = 25 * 1024 * 1024
MAX_PDF_PAGES = 600

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class ExtractError(Exception):
    pass


def extract_text(filename: str, data: bytes) -> str:
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if ext not in ALLOWED:
        raise ExtractError(f"Unsupported file type {ext or '(none)'}. Use PDF, Word (.docx), Markdown or text.")
    if len(data) > MAX_BYTES:
        raise ExtractError("File is larger than 25 MB.")
    if ext == ".pdf":
        return pdf_text(data)
    if ext == ".docx":
        return docx_text(data)
    return data.decode("utf-8", errors="replace")


def pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:  # pragma: no cover
        raise ExtractError("PDF support is not installed.") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ExtractError("This PDF is password protected.")
        parts = []
        for page in reader.pages[:MAX_PDF_PAGES]:
            parts.append(page.extract_text() or "")
    except ExtractError:
        raise
    except (PdfReadError, ValueError, KeyError, OSError) as exc:
        raise ExtractError(f"Could not read the PDF: {exc}") from exc
    text = "\n".join(parts)
    if not text.strip():
        raise ExtractError("No text found in the PDF (is it a scan? OCR is not supported).")
    return text


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


def docx_text(data: bytes) -> str:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        if zf.getinfo("word/document.xml").file_size > 60 * 1024 * 1024:
            raise ExtractError("The Word document is too large.")
        root = ET.fromstring(zf.read("word/document.xml"))
        fmts = _numbering(zf)
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise ExtractError("Could not read the Word document (is it a real .docx?).") from exc
    counters: dict[tuple[str, str], int] = {}
    lines: list[str] = []
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
    return "\n".join(lines)


def clean_markdown(text: str) -> str:
    """Strip emphasis markers that would hide patterns like '**A.** text'."""
    text = re.sub(r"(\*\*|__)", "", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)
    return text.replace("`", "")
