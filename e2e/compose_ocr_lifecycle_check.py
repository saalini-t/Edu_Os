"""Docker Compose check of OCR, the document lifecycle and the parser memory limit.

    # root .env: INSTALL_OCR=true  OCR_ENGINE=rapidocr
    docker compose up -d --build
    python e2e/compose_ocr_lifecycle_check.py

Needs Pillow + reportlab on the host to draw a scanned (image-only) PDF."""
import io
import subprocess
import sys
import time
import urllib.parse
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compose_async_check import ROOT, login, req, wait_done  # noqa: E402


def scanned_pdf(lines):
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    img = Image.new("RGB", (1240, 160 * len(lines) + 80), "white")
    d = ImageDraw.Draw(img)
    try:
        f = ImageFont.truetype("arial.ttf", 44)
    except Exception:
        f = ImageFont.load_default()
    for i, line in enumerate(lines):
        d.text((40, 50 + 150 * i), line, fill="black", font=f)
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawImage(ImageReader(img), 30, 400, width=540, height=540 * img.height / img.width)
    c.showPage()
    c.save()
    return buf.getvalue() + b"\n%" + uuid.uuid4().hex.encode() + b"\n"


def text_pdf(tag):
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate
    buf = io.BytesIO()
    SimpleDocTemplate(buf).build([Paragraph(f"Lifecycle check {tag}. The {tag} protocol uses unique tokens such as {tag}zork "
                                            f"and {tag}quux to test document replacement in the course material.",
                                            getSampleStyleSheet()["BodyText"])])
    return buf.getvalue()


def main():
    student = login("student1@demo.local")
    admin = login("admin@demo.local")
    course = req("GET", "/v1/documents", student)[1]["items"][0]["course_id"]

    def upload(data, name="doc.pdf"):
        return req("POST", "/v1/documents", student, upload=(name, data), form={"course_id": course, "title": name})

    def put(doc, data):
        return req("PUT", f"/v1/documents/{doc}/file", student, upload=("v2.pdf", data), form={})

    def search(q):
        return [h["text"] for h in req("GET", f"/v1/search?q={urllib.parse.quote(q)}&course_id={course}&top_k=20", student)[1]["results"]]

    print("A) scanned PDF is OCRed inside the worker container (sandboxed child process, rapidocr)")
    status, d = upload(scanned_pdf(["The sliding window protocol limits unacknowledged data.",
                                    "Congestion control prevents router queues overflowing."]), "scan.pdf")
    st = wait_done(d["document_id"], student, 240)
    print("   ", status, st["status"], "extraction:", st["extraction"])
    assert st["status"] == "READY" and st["extraction"]["ocr_pages"] == [1] and st["extraction"]["ocr_engine"] == "rapidocr"
    assert any("sliding window" in t.lower() for t in search("sliding window protocol unacknowledged data"))

    print("B) replace -> reindex -> delete")
    s1, d1 = upload(text_pdf("lifeone"))
    doc = d1["document_id"]
    assert wait_done(doc, student)["status"] == "READY" and any("lifeonezork" in t for t in search("lifeonezork"))
    s2, r = put(doc, text_pdf("lifetwo"))
    assert s2 == 202, r
    st = wait_done_version(doc, student, 2)
    print("    after replace: version", st["version"], "| old content searchable:", bool(search("lifeonezork")),
          "| new:", bool(search("lifetwozork")))
    assert st["version"] == 2 and not search("lifeonezork") and search("lifetwozork")
    assert req("POST", f"/v1/documents/{doc}/reindex", student)[0] == 202
    st = wait_done_version(doc, student, 3)
    assert st["version"] == 3 and len(search("lifetwozork")) == len(set(search("lifetwozork")))
    assert req("DELETE", f"/v1/documents/{doc}", student)[0] == 204
    assert not search("lifetwozork")
    ev = req("GET", f"/v1/admin/documents/{doc}/events", admin)[1]["items"]
    print("    audit trail:", [e["event"] for e in ev])
    assert [e["event"] for e in ev][-1] == "deleted"

    print("C) parser memory limit (Linux RLIMIT_AS) inside the worker container")
    code = ("from app.config import Settings; from app.knowledge.pdf import parse_document, PdfError; import sys;"
            "data=open('/app/seed/computer_networks.pdf','rb').read(); s=Settings(parser_isolation='subprocess');"
            "\ntry:\n  e=parse_document(data,s); print('OK pages',len(e.pages))\nexcept PdfError as x:\n  print('ERR',x.code)\n")
    for mb in (32, 1024):
        out = subprocess.run(["docker", "compose", "exec", "-T", "-e", f"PARSER_MEMORY_MB={mb}", "worker", "python", "-c", code],
                             capture_output=True, text=True, cwd=ROOT).stdout.strip().splitlines()[-1]
        print(f"    PARSER_MEMORY_MB={mb}: {out}")
        assert (out.startswith("ERR PARSER_") if mb == 32 else out.startswith("OK"))
    print("ALL COMPOSE OCR / LIFECYCLE CHECKS PASSED")


def wait_done_version(doc, token, version, secs=90):
    end, st = time.time() + secs, None
    while time.time() < end:
        st = req("GET", f"/v1/documents/{doc}", token)[1]
        if st["version"] >= version and st["job"]["status"] in ("DONE", "FAILED"):
            return st
        time.sleep(1)
    return st


if __name__ == "__main__":
    main()
