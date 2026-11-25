import io
import zipfile

from app.services import gpt, parser, scraper

EXAM_MD = b"""1. 2+2?
A. 3
B. 4
Answer: B

2. Capital of Italy?
A. Rome
B. Milan
Answer: A

3. Which is a mammal?
A. Shark
B. Dolphin
"""


def test_health_and_ready(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/ready").json()["status"] == "ready"


def test_requires_login_and_no_public_api(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/login")
    assert client.get("/ui/exams/1/verify/status").status_code == 401
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/api/v1/exams", headers={"Authorization": "Bearer x"}).status_code == 404


def test_security_headers_and_csrf(client):
    r = client.get("/login")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    r = client.post("/login", data={"username": "a", "password": "b"}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_login_rate_limit(client):
    from app.security import login_limiter

    login_limiter.reset("testclient")
    for _ in range(5):
        assert client.post("/login", data={"username": "admin", "password": "wrong"}).status_code == 401
    assert client.post("/login", data={"username": "admin", "password": "wrong"}).status_code == 429
    login_limiter.reset("testclient")


def test_full_flow(auth):
    c = auth
    assert c.get("/").status_code == 200
    exam = c.post("/ui/exams", json={"title": "Demo exam"}).json()["id"]
    res = c.post(f"/ui/exams/{exam}/upload", files={"files": ("demo.md", io.BytesIO(EXAM_MD), "text/markdown")}).json()
    assert res["results"][0]["added"] == 3
    again = c.post(
        f"/ui/exams/{exam}/upload", files={"files": ("demo.md", io.BytesIO(EXAM_MD), "text/markdown")}
    ).json()
    assert again["results"][0]["duplicates"] == 3
    assert "Demo exam" in c.get("/").text and "3 questions" in c.get(f"/exams/{exam}").text.replace(
        "<b>3</b><span>", "3 "
    ).replace("</span>", "")

    sid = c.post(f"/ui/exams/{exam}/sessions", json={"mode": "all", "count": None, "shuffle": False}).json()["id"]
    st = c.get(f"/ui/sessions/{sid}").json()
    assert st["total"] == 3 and st["answered"] == 0
    q = c.get(f"/ui/sessions/{sid}/question").json()["question"]
    assert "correct" not in q  # answer key is not leaked before answering
    r = c.post(f"/ui/sessions/{sid}/answer", json={"question_id": q["id"], "chosen": [1]}).json()
    assert r["correct"] is True and r["correct_indexes"] == [1]
    # same question again is refused; next one answered wrong
    assert c.post(f"/ui/sessions/{sid}/answer", json={"question_id": q["id"], "chosen": [1]}).status_code == 409
    q2 = c.get(f"/ui/sessions/{sid}/question").json()["question"]
    assert (
        c.post(f"/ui/sessions/{sid}/answer", json={"question_id": q2["id"], "chosen": [1]}).json()["correct"] is False
    )
    # unfinished session can be resumed and shows on the menu
    assert f"/practice/{sid}" in c.get("/").text
    q3 = c.get(f"/ui/sessions/{sid}/question").json()["question"]
    assert q3["has_key"] is False  # no key in source, ChatGPT not configured
    r = c.post(f"/ui/sessions/{sid}/answer", json={"question_id": q3["id"], "chosen": [0]}).json()
    assert r["correct"] is None
    done = c.get(f"/ui/sessions/{sid}/summary").json()
    assert done["correct"] == 1 and done["wrong"] == 1 and done["accuracy"] == 50
    assert c.get(f"/ui/sessions/{sid}/question").json()["done"] is True
    wrong = c.post(f"/ui/exams/{exam}/sessions", json={"mode": "wrong", "count": None, "shuffle": True})
    assert wrong.status_code == 200
    assert c.post(f"/ui/exams/{exam}/reset").status_code == 200
    assert c.post(f"/ui/exams/{exam}/sessions", json={"mode": "wrong"}).status_code == 409
    assert c.delete(f"/ui/exams/{exam}").status_code == 200


def test_edit_and_verify_without_key(auth):
    c = auth
    exam = c.post("/ui/exams", json={"title": "Edit"}).json()["id"]
    c.post(f"/ui/exams/{exam}/upload", files={"files": ("e.md", io.BytesIO(EXAM_MD), "text/markdown")})
    html = c.get(f"/exams/{exam}").text
    assert "data-edit=" in html
    qid = int(html.split('data-edit="')[1].split('"')[0])
    r = c.patch(f"/ui/questions/{qid}", json={"text": "Edited?", "options": ["x", "y"], "correct": [1]})
    assert r.status_code == 200 and r.json()["correct"] == [1]
    assert (
        c.patch(f"/ui/questions/{qid}", json={"text": "Bad", "options": ["x", "y"], "correct": [5]}).status_code == 422
    )
    assert c.post(f"/ui/questions/{qid}/verify").status_code == 502  # no key configured
    assert c.post(f"/ui/exams/{exam}/verify").status_code == 409


def test_chatgpt_verification_logic(auth, monkeypatch):
    def fake_chat(cfg, system, user, timeout=90):
        return {
            "correct": ["C"],
            "agrees": False,
            "explanation": "Because.",
            "reference_url": "https://example.org/x",
            "confidence": "high",
        }

    monkeypatch.setattr(gpt, "_chat", fake_chat)
    res = gpt.verify_question(
        {"key": "k"}, {"text": "Q?", "options": ["a", "b", "c"], "correct": [1], "answer_text": "", "qtype": "mc"}
    )
    assert res["status"] == "disputed" and res["ai_correct"] == [2] and res["link"] == "https://example.org/x"
    res = gpt.verify_question(
        {"key": "k"}, {"text": "Q?", "options": ["a", "b", "c"], "correct": [], "answer_text": "", "qtype": "mc"}
    )
    assert res["status"] == "answered"
    monkeypatch.setattr(gpt, "_chat", lambda *a, **k: {"correct": ["B"], "reference_url": "javascript:alert(1)"})
    res = gpt.verify_question(
        {"key": "k"}, {"text": "Q?", "options": ["a", "b"], "correct": [1], "answer_text": "", "qtype": "mc"}
    )
    assert res["status"] == "confirmed" and res["link"] == ""  # unsafe links are dropped


def test_ai_answer_used_when_source_has_no_key(auth, monkeypatch):
    c = auth
    monkeypatch.setattr(
        gpt, "_chat", lambda *a, **k: {"correct": ["B"], "explanation": "Dolphins are mammals.", "reference_url": ""}
    )
    from app.database import SessionLocal
    from app.services import store

    with SessionLocal() as db:
        store.set_openai_key(db, "sk-test")
    try:
        exam = c.post("/ui/exams", json={"title": "AI"}).json()["id"]
        c.post(
            f"/ui/exams/{exam}/upload",
            files={"files": ("m.md", io.BytesIO(b"1. Which is a mammal?\nA. Shark\nB. Dolphin\n"), "text/markdown")},
        )
        sid = c.post(f"/ui/exams/{exam}/sessions", json={"mode": "all", "count": None}).json()["id"]
        q = c.get(f"/ui/sessions/{sid}/question").json()["question"]
        r = c.post(f"/ui/sessions/{sid}/answer", json={"question_id": q["id"], "chosen": [1]}).json()
        assert r["correct"] is True and r["key_source"] == "chatgpt" and "mammals" in r["ai"]["note"]
    finally:
        with SessionLocal() as db:
            store.set_openai_key(db, "")


def test_scraper_blocks_private_hosts():
    import pytest

    for url in ("http://127.0.0.1/", "http://localhost:8000/x", "file:///etc/passwd", "ftp://example.com/"):
        with pytest.raises(scraper.ScrapeError):
            scraper.check_url(url)


def test_html_to_text_and_parse():
    html = (
        "<html><head><title>Quiz</title><script>x=1</script></head><body><nav>menu</nav><p>1. Sky colour?</p>"
        "<ul><li>A. Blue</li><li>B. Green</li></ul><p>Answer: A</p></body></html>"
    )
    text, title = scraper.html_to_text(html)
    assert title == "Quiz" and "menu" not in text
    qs = parser.parse_questions(text)
    assert qs[0]["correct"] == [0]


def test_themes_fonts_persist(auth):
    assert auth.post("/ui/appearance", json={"theme": "nord", "font": "lato"}).status_code == 200
    html = auth.get("/").text
    assert 'data-theme="nord"' in html and 'data-font="lato"' in html
    assert auth.post("/ui/appearance", json={"theme": "nope"}).status_code == 422


def test_backup_roundtrip(auth):
    c = auth
    exam = c.post("/ui/exams", json={"title": "Backup me"}).json()["id"]
    name = c.post("/ui/backups").json()["name"]
    data = c.get(f"/ui/backups/{name}").content
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert {"app.db", "manifest.json"} <= set(zf.namelist())
    c.delete(f"/ui/exams/{exam}")
    assert "Backup me" not in c.get("/").text
    assert (
        c.post("/ui/backups/import", files={"file": ("b.zip", io.BytesIO(data), "application/zip")}).status_code == 200
    )
    assert "Backup me" in c.get("/").text
    assert any(b["name"].startswith("before-restore") for b in c.get("/ui/backups").json())
    assert (
        c.post("/ui/backups/import", files={"file": ("b.zip", io.BytesIO(b"junk"), "application/zip")}).status_code
        == 422
    )
    assert c.get("/ui/backups/../../etc/passwd").status_code in (404, 422)
    c.post("/ui/settings/backup", json={"enabled": True, "keep": 2})
    for _ in range(3):
        c.post("/ui/backups")
    assert len([b for b in c.get("/ui/backups").json() if not b["name"].startswith("before")]) <= 2


def test_pages_render(auth):
    for path in ("/settings", "/logs", "/about", "/manifest.webmanifest", "/sw.js", "/static/css/app.css"):
        assert auth.get(path).status_code == 200, path


def test_generate_multiple_choice_from_open_examples(auth, monkeypatch):
    c = auth
    fake = {
        "questions": [
            {
                "text": "What is the capital of France?",
                "options": ["A. Paris", "Lyon", "Nice", "Lille"],
                "correct": ["A"],
                "explanation": "Paris is the capital.",
            },
            {"text": "No options here", "options": [], "correct": []},
        ]
    }
    monkeypatch.setattr(gpt, "_chat", lambda *a, **k: fake)
    from app.database import SessionLocal
    from app.services import store

    with SessionLocal() as db:
        store.set_openai_key(db, "sk-test")
    try:
        exam = c.post("/ui/exams", json={"title": "Gen"}).json()["id"]
        # open question without options: parser finds no MC, so ChatGPT generates it automatically
        r = c.post(
            f"/ui/exams/{exam}/upload",
            files={
                "files": ("o.md", io.BytesIO(b"1. What is the capital of France?\nAnswer: Paris\n"), "text/markdown")
            },
        ).json()["results"][0]
        assert r["added"] == 1 and not r["error"].startswith("AI-assisted import failed")
        page = c.get(f"/exams/{exam}")
        assert "Paris" in page.text and "Lyon" in page.text and "A. Paris" not in page.text
    finally:
        with SessionLocal() as db:
            store.set_openai_key(db, "")


def test_manage_exam_questions_and_sources(auth):
    c = auth
    exam = c.post("/ui/exams", json={"title": "Manage"}).json()["id"]
    # add manually (MC and open), duplicates and bad input are rejected
    mc = {"text": "Largest planet?", "options": ["Mars", "Jupiter"], "correct": [1]}
    assert c.post(f"/ui/exams/{exam}/questions", json=mc).status_code == 200
    assert c.post(f"/ui/exams/{exam}/questions", json=mc).status_code == 409
    assert c.post(f"/ui/exams/{exam}/questions", json={**mc, "text": "x?", "correct": [5]}).status_code == 422
    assert c.post(f"/ui/exams/{exam}/questions", json={"text": "Name a prime."}).status_code == 200
    # paste import creates a source; rename it; remove it but keep the questions
    r = c.post(f"/ui/exams/{exam}/paste", json={"name": "Notes", "text": EXAM_MD.decode()}).json()["results"][0]
    assert r["added"] == 3
    page = c.get(f"/exams/{exam}").text
    assert "Notes" in page and "Add question" in page
    from app.database import SessionLocal
    from app.models import Question, Source

    with SessionLocal() as db:
        sid = db.query(Source).filter(Source.exam_id == exam).one().id
    assert c.patch(f"/ui/sources/{sid}", json={"name": "Renamed"}).status_code == 200
    assert c.delete(f"/ui/sources/{sid}?keep_questions=true").status_code == 200
    with SessionLocal() as db:
        assert db.query(Question).filter(Question.exam_id == exam).count() == 5
    # progress: answer one, reset it, duplicate the exam, bulk delete
    s = c.post(f"/ui/exams/{exam}/sessions", json={"mode": "all", "count": None}).json()["id"]
    q = c.get(f"/ui/sessions/{s}/question").json()["question"]
    c.post(f"/ui/sessions/{s}/answer", json={"question_id": q["id"], "chosen": [0]})
    assert c.post(f"/ui/questions/{q['id']}/reset").status_code == 200
    dup = c.post(f"/ui/exams/{exam}/duplicate").json()["id"]
    with SessionLocal() as db:
        ids = [x.id for x in db.query(Question).filter(Question.exam_id == dup)]
    assert len(ids) == 5
    assert c.post(f"/ui/exams/{dup}/questions/delete", json={"ids": ids[:2]}).json()["deleted"] == 2
    # ids of another exam are never touched
    assert c.post(f"/ui/exams/{dup}/questions/delete", json={"ids": [q["id"]]}).json()["deleted"] == 0


def test_history_and_text_size(auth):
    c = auth
    exam = c.post("/ui/exams", json={"title": "Hist"}).json()["id"]
    c.post(f"/ui/exams/{exam}/paste", json={"text": EXAM_MD.decode()})
    sid = c.post(f"/ui/exams/{exam}/sessions", json={"mode": "all", "count": None, "shuffle": False}).json()["id"]
    assert c.get(f"/ui/sessions/{sid}/history/0").status_code == 404  # nothing answered yet
    first = c.get(f"/ui/sessions/{sid}/question").json()["question"]
    c.post(f"/ui/sessions/{sid}/answer", json={"question_id": first["id"], "chosen": [0]})
    second = c.get(f"/ui/sessions/{sid}/question").json()["question"]
    c.post(f"/ui/sessions/{sid}/answer", json={"question_id": second["id"], "skip": True})
    h0 = c.get(f"/ui/sessions/{sid}/history/0").json()
    assert h0["question"]["id"] == first["id"] and h0["count"] == 2 and h0["feedback"]["chosen"] == [0]
    assert c.get(f"/ui/sessions/{sid}/history/1").json()["skipped"] is True
    assert c.get(f"/ui/sessions/{sid}/history/2").status_code == 404
    # text size: saved, validated, rendered
    assert c.post("/ui/appearance", json={"size": 130}).json()["size"] == 130
    assert c.post("/ui/appearance", json={"size": 99}).status_code == 422
    assert 'data-size="130"' in c.get("/settings").text
    c.post("/ui/appearance", json={"size": 100})
