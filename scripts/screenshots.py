"""Take README screenshots with demo data only.

Usage (a throw-away server with an empty data dir, never your real one):
    DATA_DIR=/tmp/ft-demo UPDATE_CHECK=false ./.venv/bin/uvicorn app.main:app --port 8765 &
    pip install playwright   # uses your installed Chrome via channel="chrome", no browser download
    python scripts/screenshots.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8765"
OUT = Path(__file__).resolve().parent.parent / "docs" / "screenshots"
PASSWORD = "demo-password-1"
DEMO = """# Cloud basics practice exam

1. Which AWS service provides object storage?
A. EC2
B. S3
C. RDS
D. Lambda
Answer: B
Explanation: Amazon S3 is the object storage service.

2. Which of the following are compute services? (select two)
A. EC2
B. S3
C. Lambda
D. Route 53
Answer: A, C

3. What does the shared responsibility model say about physical data centre security?
A. The customer is responsible
B. The provider is responsible
C. Both share it equally
Answer: B

4. Which service is a managed relational database?
A. DynamoDB
B. RDS
C. SQS
D. SNS
Answer: B

5. Explain what a VPC is?
Answer: A logically isolated virtual network in the cloud where you launch resources.
"""


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        ctx = browser.new_context(viewport={"width": 1100, "height": 760})
        page = ctx.new_page()
        page.goto(BASE + "/login")
        page.fill("#u", "admin")
        page.fill("#p", "admin")
        page.click("main button[type=submit]")
        page.wait_for_load_state("networkidle")
        if "/account/password" in page.url:
            page.fill("#c", "admin")
            page.fill("#n", PASSWORD)
            page.fill("#n2", PASSWORD)
            page.click("main button[type=submit]")
        page.wait_for_load_state("networkidle")
        r = ctx.request
        resp = r.post(BASE + "/ui/exams", data={"title": "AWS Cloud Practitioner", "description": "Demo data"})
        exam = resp.json()
        r.post(BASE + f"/ui/exams/{exam['id']}/upload", multipart={
            "files": {"name": "cloud-basics.md", "mimeType": "text/markdown", "buffer": DEMO.encode()}})
        r.post(BASE + "/ui/exams", data={"title": "Networking Basics"})
        sid = r.post(BASE + f"/ui/exams/{exam['id']}/sessions", data={"mode": "all", "count": None, "shuffle": False}).json()["id"]
        page.goto(BASE + "/")
        page.screenshot(path=OUT / "exams.png")
        page.goto(BASE + f"/practice/{sid}")
        page.wait_for_selector(".opt")
        page.keyboard.press("b")
        page.keyboard.press("Enter")
        page.wait_for_selector(".verdict")
        page.screenshot(path=OUT / "practice.png")
        page.goto(BASE + f"/exams/{exam['id']}")
        page.screenshot(path=OUT / "exam.png", full_page=True)
        page.goto(BASE + "/settings")
        page.screenshot(path=OUT / "settings.png", full_page=True)
        page.select_option("#theme-sel", "daylight")
        page.wait_for_timeout(300)
        page.goto(BASE + "/")
        page.screenshot(path=OUT / "exams-light.png")
        browser.close()
    print("Screenshots written to", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
