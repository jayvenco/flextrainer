import base64
import io
import zipfile

from PIL import Image as PILImage

from app.services import extract, images


def _png(color=(200, 30, 30), size=(120, 90)) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _jpeg(color=(30, 30, 200), size=(120, 90)) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def _docx(png: bytes) -> bytes:
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    a = "http://schemas.openxmlformats.org/drawingml/2006/main"

    def para(t):
        return f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>"

    body = (
        para("1. Which shape is shown below?")
        + f'<w:p><w:r><w:drawing><a:graphic xmlns:a="{a}"><a:graphicData><a:blip r:embed="rId5"/></a:graphicData></a:graphic></w:drawing></w:r></w:p>'
        + para("A. Circle")
        + para("B. Rectangle")
        + para("Answer: B")
        + para("2. What is 2+2?")
        + para("A. 3")
        + para("B. 4")
        + para("Answer: B")
    )
    doc = f'<?xml version="1.0"?><w:document xmlns:w="{w}" xmlns:r="{r}"><w:body>{body}</w:body></w:document>'
    rels = (
        '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        'Target="media/image1.png"/></Relationships>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", doc)
        zf.writestr("word/_rels/document.xml.rels", rels)
        zf.writestr("word/media/image1.png", png)
    return buf.getvalue()


def _pdf(jpg: bytes) -> bytes:
    """Tiny one-page PDF: question text, a picture, then the options."""
    content = (
        b"BT /F1 12 Tf 50 740 Td (1. Which colour is the picture?) Tj ET\n"
        b"q 120 0 0 90 50 620 cm /Im0 Do Q\n"
        b"BT /F1 12 Tf 50 590 Td (A. Red) Tj 0 -16 Td (B. Blue) Tj 0 -16 Td (Answer: B) Tj ET\n"
    )
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> /XObject << /Im0 6 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /XObject /Subtype /Image /Width 120 /Height 90 /ColorSpace /DeviceRGB /BitsPerComponent 8 "
        b"/Filter /DCTDecode /Length %d >>\nstream\n" % len(jpg) + jpg + b"\nendstream",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def test_sniff_rejects_svg_and_garbage():
    assert images.sniff(_png()) == "image/png" and images.sniff(_jpeg()) == "image/jpeg"
    assert images.sniff(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>") is None
    assert images.normalise(_png(size=(10, 10))) is None  # too small: icon or bullet
    big = images.normalise(_png(size=(3000, 1000)))
    assert big and max(big[2], big[3]) <= images.MAX_SIDE


def test_docx_picture_sits_with_its_question():
    from app.services.parser import parse_questions

    doc = extract.extract_document("exam.docx", _docx(_png()))
    assert len(doc.images) == 1
    qs = parse_questions(doc.text)
    assert len(qs) == 2 and len(qs[0]["images"]) == 1 and qs[1]["images"] == []
    assert qs[0]["options"] == ["Circle", "Rectangle"] and qs[0]["correct"] == [1]


def test_pdf_picture_between_question_and_options():
    from app.services.parser import parse_questions

    doc = extract.extract_document("exam.pdf", _pdf(_jpeg()))
    assert len(doc.images) == 1
    qs = parse_questions(doc.text)
    assert len(qs) == 1 and len(qs[0]["images"]) == 1 and qs[0]["options"] == ["Red", "Blue"]


def test_markdown_data_uri_and_plain_text_unchanged():
    from app.services.parser import parse_questions

    b64 = base64.b64encode(_png()).decode()
    md = f"1. What is shown?\n![fig](data:image/png;base64,{b64})\nA. Red\nB. Blue\nAnswer: A\n\n![x](relative/path.png)\n"
    doc = extract.extract_document("q.md", md.encode())
    assert len(doc.images) == 1 and "relative/path.png" not in doc.text
    qs = parse_questions(doc.text)
    assert len(qs) == 1 and len(qs[0]["images"]) == 1
    assert extract.extract_text("q.md", md.encode()).count("⟦") == 0  # markers never leak into plain text


def test_picture_upload_flow_and_api(auth):
    c = auth
    exam = c.post("/ui/exams", json={"title": "Pics"}).json()["id"]
    r = c.post(
        f"/ui/exams/{exam}/upload",
        files={"files": ("pics.docx", io.BytesIO(_docx(_png())), "application/octet-stream")},
    ).json()["results"][0]
    assert r["added"] == 2 and r["images"] == 1
    page = c.get(f"/exams/{exam}").text
    assert "/ui/images/" in page
    from app.database import SessionLocal
    from app.models import Image, Question

    with SessionLocal() as db:
        qs = db.query(Question).filter(Question.exam_id == exam).order_by(Question.position).all()
        pic_id = qs[0].images[0]
        q2 = qs[1].id
        q1 = qs[0].id
    img = c.get(f"/ui/images/{pic_id}")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    assert img.headers["x-content-type-options"] == "nosniff"
    # practice payload carries the picture ids
    sid = c.post(f"/ui/exams/{exam}/sessions", json={"mode": "all", "count": None, "shuffle": False}).json()["id"]
    assert c.get(f"/ui/sessions/{sid}/question").json()["question"]["images"] == [pic_id]
    # add and remove a picture by hand
    up = c.post(f"/ui/questions/{q2}/images", files={"file": ("a.jpg", io.BytesIO(_jpeg()), "image/jpeg")})
    assert up.status_code == 200 and len(up.json()["images"]) == 1
    bad = c.post(f"/ui/questions/{q2}/images", files={"file": ("a.svg", io.BytesIO(b"<svg/>"), "image/svg+xml")})
    assert bad.status_code == 422
    new_id = up.json()["images"][0]
    assert c.delete(f"/ui/questions/{q2}/images/{new_id}").json()["images"] == []
    with SessionLocal() as db:
        assert db.get(Image, new_id) is None  # unused pictures are cleaned up
    # duplicating an exam copies the pictures; deleting the question removes the original's picture
    dup = c.post(f"/ui/exams/{exam}/duplicate").json()["id"]
    with SessionLocal() as db:
        assert db.query(Image).filter(Image.exam_id == dup).count() == 1
    assert c.delete(f"/ui/questions/{q1}").status_code == 200
    with SessionLocal() as db:
        assert db.get(Image, pic_id) is None and db.query(Image).filter(Image.exam_id == dup).count() == 1
    c.cookies.clear() if False else None


def test_same_wording_different_picture_is_not_a_duplicate(auth):
    c = auth
    exam = c.post("/ui/exams", json={"title": "Dupes"}).json()["id"]
    docs = [_docx(_png((200, 0, 0))), _docx(_png((0, 200, 0)))]
    added = [
        c.post(
            f"/ui/exams/{exam}/upload", files={"files": ("p.docx", io.BytesIO(d), "application/octet-stream")}
        ).json()["results"][0]["added"]
        for d in docs
    ]
    assert added == [2, 1]  # second file: the picture question is new (other picture), "2+2" is a duplicate


def test_images_require_login(client):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, base_url="http://testserver") as anon:  # no session cookie
        assert anon.get("/ui/images/1", follow_redirects=False).status_code in (401, 303, 307)


def test_html_pictures_are_resolved_against_the_page_url(monkeypatch):
    from app.services import parser, scraper

    html = (
        "<html><body><h1>Quiz</h1><p>1. Which animal is this?</p>"
        '<img src="/img/cat.png"><ul><li>A. Cat</li><li>B. Dog</li></ul><p>Answer: A</p>'
        '<script><img src="/no.png"></script></body></html>'
    )
    doc, _ = scraper.html_to_doc(html, "https://example.com/quiz/page")
    assert list(doc.images.values()) == ["https://example.com/img/cat.png"]
    qs = parser.parse_questions(doc.text)
    assert len(qs) == 1 and len(qs[0]["images"]) == 1
    # without a base URL (plain text conversion) no markers are produced
    assert "⟦" not in scraper.html_to_text(html)[0]
