import io
import zipfile

from app.services import extract
from app.services.parser import parse_questions

SAMPLE = """# Practice exam

1. What is the capital of France?
A. Berlin
B. Paris
C. Madrid
Answer: B
Explanation: Paris is the capital.

2) Which are prime numbers? (select two)
a) 2
b) 4
c) 3
d) 9
Correct answer: A, C

Question 3
Which protocol is connectionless?
A. TCP
B. UDP
C. FTP

4. Explain what a database index is for?
Answer: It speeds up lookups at the cost of extra writes.

Answer Key
3. B
"""


def test_parses_mc_open_and_key():
    qs = parse_questions(SAMPLE)
    assert len(qs) == 4
    assert qs[0]["options"] == ["Berlin", "Paris", "Madrid"] and qs[0]["correct"] == [1]
    assert qs[0]["explanation"] == "Paris is the capital."
    assert qs[1]["correct"] == [0, 2]
    assert qs[2]["text"].startswith("Which protocol") and qs[2]["correct"] == [1]  # from the answer key
    assert qs[3]["qtype"] == "open" and "speeds up" in qs[3]["answer_text"]


def test_markdown_checkboxes_and_dutch():
    text = "1. Welke kleur heeft gras?\n- [ ] Blauw\n- [x] Groen\n- [ ] Rood\n\n2. Hoofdstad van Nederland?\nA. Amsterdam\nB. Den Haag\nAntwoord: A\n"
    qs = parse_questions(text)
    assert qs[0]["correct"] == [1]
    assert qs[1]["correct"] == [0]


def test_unnumbered_blocks_and_noise():
    text = "Some intro paragraph.\n\nWhat does CPU stand for?\nA. Central Processing Unit\nB. Computer Personal Unit\nAnswer: A\n"
    qs = parse_questions(text)
    assert len(qs) == 1 and qs[0]["correct"] == [0]
    assert parse_questions("Nothing useful here.\nJust prose.") == []


def test_docx_numbered_options():
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    numbering = (
        f'<w:numbering xmlns:w="{w}"><w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:numFmt w:val="upperLetter"/>'
        f'</w:lvl></w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>'
    )

    def p(text, num=False):
        pr = '<w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>' if num else ""
        return f"<w:p>{pr}<w:r><w:t>{text}</w:t></w:r></w:p>"

    doc = (
        f'<w:document xmlns:w="{w}"><w:body>{p("1. Largest planet?")}{p("Earth", True)}{p("Jupiter", True)}'
        f'{p("Answer: B")}</w:body></w:document>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", doc)
        zf.writestr("word/numbering.xml", numbering)
    text = extract.extract_text("exam.docx", buf.getvalue())
    qs = parse_questions(text)
    assert qs and qs[0]["options"] == ["Earth", "Jupiter"] and qs[0]["correct"] == [1]


def test_rejects_bad_files():
    import pytest

    with pytest.raises(extract.ExtractError):
        extract.extract_text("x.exe", b"abc")
    with pytest.raises(extract.ExtractError):
        extract.extract_text("x.docx", b"not a zip")
