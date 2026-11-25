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


def test_unnumbered_consecutive_questions_are_split():
    text = (
        "Which port does HTTPS use?\nA. 80\nB. 443\nAnswer: B\n"
        "Which layer routes packets?\nA. Data link\nB. Network\nC. Session\nAnswer: B\nExplanation: Layer 3.\n"
        "What does DNS resolve?\nA. Names to addresses\nB. Files to folders\n"
    )
    qs = parse_questions(text)
    assert [q["text"] for q in qs] == [
        "Which port does HTTPS use?",
        "Which layer routes packets?",
        "What does DNS resolve?",
    ]
    assert qs[0]["correct"] == [1] and qs[1]["correct"] == [1] and qs[1]["explanation"] == "Layer 3."
    assert qs[2]["options"] == ["Names to addresses", "Files to folders"]


def test_glued_pdf_lines_are_split():
    text = (
        "1. Which is a mammal? A. Shark B. Dolphin C. Trout Answer: B 2. Capital of Italy? A. Rome B. Milan "
        "Answer: A 3. What is 2+2? A) 3 B) 4 Antwoord: B\n"
    )
    qs = parse_questions(text)
    assert len(qs) == 3
    assert qs[0]["options"] == ["Shark", "Dolphin", "Trout"] and qs[0]["correct"] == [1]
    assert qs[1]["text"] == "Capital of Italy?" and qs[1]["correct"] == [0]
    assert qs[2]["correct"] == [1]


def test_prose_with_letters_is_not_split():
    qs = parse_questions(
        "1. Vitamin A. B. and C are important, so which one is a vitamin found in carrots and oranges?\nA. Vitamin A\nB. Iron\nAnswer: A\n"
    )
    assert len(qs) == 1 and qs[0]["options"] == ["Vitamin A", "Iron"]


def test_corrupt_docx_gives_a_clean_error():
    import pytest

    good = io.BytesIO()
    with zipfile.ZipFile(good, "w") as zf:
        zf.writestr("word/document.xml", "<x/>" * 50)
    broken = good.getvalue()[:-30]  # truncated archive
    with pytest.raises(extract.ExtractError):
        extract.extract_text("broken.docx", broken)
