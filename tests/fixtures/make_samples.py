"""Invented test documents, one distinct fact per file. Generated on the fly, so no real data is committed."""
import csv
from pathlib import Path

import pytest

FONT = Path(r"C:\Windows\Fonts\arial.ttf")

# (question, expected file, fact that must appear in the matching text / answer)
CASES = [
    ("Kamalak loyihasini qachongacha topshirish kerak?", "01_loyiha.md", "17-mart"),
    ("Omborning kaliti qayerda turadi?", "02_ombor.docx", "42"),
    ("Serverning backup nusxasi soat nechada olinadi?", "03_server.pdf", "03:40"),
    ("Orion loyihasining byudjeti qancha?", "04_byudjet.xlsx", "7350000"),
    ("What is the codename for the Falcon release?", "05_release.pptx", "Blue Heron"),
    ("Yangi ishchilar uchun tanishtiruv qachon va qayerda?", "06_tanishtiruv.html", "11-xona"),
    ("Buxoro filiali rahbari kim?", "07_filiallar.csv", "Dilnoza"),
    ("O'quv markazida Wi-Fi nomi nima?", "08_wifi.txt", "Lola-5G"),
    ("8841 raqamli shartnoma qayerda imzolangan?", "09_skan_lotin.png", "ijduvon"),
    ("5527 рақамли шартнома қаерда имзоланган?", "10_skan_kirill.png", "Ғиждувон"),
    ("В каком городе подписан договор номер 3319?", "11_skan_rus.pdf", "Самарканд"),
]
UNANSWERABLE = [
    "Direktorning telefon raqami nechchi?",
    "Kompaniyaning 2023 yildagi daromadi qancha?",
    "Python'da ro'yxatni qanday saralash mumkin?",
    "Ertaga ob-havo qanday bo'ladi?",
]


def _need_font():
    if not FONT.exists():
        pytest.skip("Arial font not found; sample documents are generated on Windows")


def _scan(lines):
    """Render text the way a scanner would: slightly rotated and slightly blurred."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    img = Image.new("L", (1700, 110 + 90 * len(lines)), 255)
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(str(FONT), 46)
    for i, line in enumerate(lines):
        draw.text((70, 60 + 90 * i), line, font=font, fill=20)
    return img.rotate(0.7, fillcolor=255).filter(ImageFilter.GaussianBlur(0.6))


def make_samples(out):
    """The 11 good documents from CASES."""
    import docx
    import openpyxl
    import pptx

    _need_font()
    out.mkdir(parents=True, exist_ok=True)
    (out / "01_loyiha.md").write_text(
        "# Kamalak loyihasi\n\nKamalak loyihasining yakuniy topshirish muddati 17-mart.\n", encoding="utf-8")

    d = docx.Document()
    d.add_heading("Омбор қоидалари", 1)
    d.add_paragraph("Омбор калити 42-хонада сақланади.")
    d.save(out / "02_ombor.docx")

    make_pdf(out / "03_server.pdf", pages=5, fact_page=3,
             fact="Laboratoriya serverining zaxira nusxasi har kuni soat 03:40 da olinadi.")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Проект", "Бюджет (сумов)"])
    ws.append(["Орион", 7350000])
    ws.append(["Вега", 1200000])
    wb.save(out / "04_byudjet.xlsx")

    p = pptx.Presentation()
    slide = p.slides.add_slide(p.slide_layouts[1])
    slide.shapes.title.text = "Falcon release"
    slide.placeholders[1].text = "The code name of the Falcon release is Blue Heron."
    p.save(out / "05_release.pptx")

    (out / "06_tanishtiruv.html").write_text(
        "<html><body><h1>Yangi xodimlar</h1><p>Tanishtiruv darsi payshanba kuni 11-xonada bo'ladi.</p></body></html>",
        encoding="utf-8")

    with (out / "07_filiallar.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Filial", "Rahbar"])
        w.writerow(["Buxoro", "Dilnoza Rahimova"])
        w.writerow(["Namangan", "Otabek Yusupov"])

    (out / "08_wifi.txt").write_text("Oʻquv markazining Wi-Fi tarmogʻi nomi Lola-5G.\n", encoding="utf-8")

    latin = ["Shartnoma raqami 8841-B.", "Oʻzbekiston Respublikasi, Gʻijduvon shahrida imzolangan."]
    cyrillic = ["Шартнома рақами 5527-Д.", "Ўзбекистон Республикаси, Ғиждувон шаҳрида имзоланган."]
    russian = ["Договор номер 3319-К.", "Подписан в городе Самарканд."]
    _scan(latin).save(out / "09_skan_lotin.png")
    _scan(cyrillic).save(out / "10_skan_kirill.png")
    _scan(russian).convert("RGB").save(out / "11_skan_rus.pdf")
    return out


def make_bad_files(out):
    """Files that must fail or be skipped without breaking anything."""
    from PIL import Image

    out.mkdir(parents=True, exist_ok=True)
    (out / "20_buzilgan.pdf").write_bytes(b"this is not a real pdf file")
    (out / "21_eski.doc").write_bytes(b"legacy word placeholder")
    Image.new("L", (800, 600), 255).save(out / "22_bosh.png")
    (out / "~$03_vaqtinchalik.docx").write_bytes(b"office lock file")
    return out


def make_pdf(path, pages, fact=None, fact_page=None, dense=False):
    """A text PDF with `pages` pages of filler; `fact` goes on `fact_page`. `dense` fills each page with text."""
    from fpdf import FPDF

    _need_font()
    pdf = FPDF()
    pdf.add_font("arial", fname=str(FONT))
    pdf.set_font("arial", size=11)
    for page in range(1, pages + 1):
        pdf.add_page()
        filler = f"Bu {page}-sahifa, umumiy tashkiliy matn va boshqa ma'lumotlar."
        text = fact if page == fact_page else (filler + " ") * 12 if dense else filler
        pdf.multi_cell(0, 6 if dense else 10, text)
    pdf.output(str(path))
    return path
