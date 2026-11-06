# Local RAG assistant

Hujjatlarni yuklab, ular bo'yicha savol beradigan **to'liq lokal** dastur. Hech narsa tashqi servisga yuborilmaydi.
Fayllar (PDF, Word, Excel, PowerPoint, HTML, rasm, matn; o'zbek lotin/kirill va rus skanlari ham) o'qiladi,
javob faqat hujjatlardan olinadi va manbasi (fayl, sahifa, parcha) ko'rsatiladi.

```
Docling + Tesseract (o'qish, OCR) -> bge-m3 (embedding, Ollama) -> Qdrant (Docker) -> qwen2.5:7b (Ollama)
FastAPI + oddiy HTML sahifa: http://127.0.0.1:8765
```

## O'rnatish (Windows)

Kerak: Python 3.12+, Docker Desktop, [Ollama](https://ollama.com), [Tesseract](https://github.com/UB-Mannheim/tesseract/wiki).

```powershell
ollama pull bge-m3
ollama pull qwen2.5:7b

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# Tesseract til fayllari (loyiha ichidagi tessdata/ papkasiga)
mkdir tessdata
foreach ($l in 'eng','osd','rus','uzb','uzb_cyrl') {
  Invoke-WebRequest "https://raw.githubusercontent.com/tesseract-ocr/tessdata/main/$l.traineddata" -OutFile "tessdata\$l.traineddata"
}
Copy-Item "C:\Program Files\Tesseract-OCR\tessdata\configs" tessdata\configs -Recurse
```

## Ishga tushirish

`start.bat` ni oching: Qdrant va PostgreSQL ko'tariladi, veb-ilova va worker ishga tushadi, brauzer ochiladi
(`python -m rag`). Faqat ilova: `python -m rag api`, faqat worker: `python -m rag worker`.

Yuklangan fayllar `storage/<hujjat id>/<nom>` da, hujjatlar ro'yxati va indekslash navbati PostgreSQL'da turadi.
Ilova qayta yoqilsa ham navbat va ro'yxat saqlanadi. Eski `documents/` papkasi va `documents` kolleksiyasi
(1-versiya) endi o'qilmaydi, kerak bo'lmasa o'chirib tashlash mumkin.

Papkadagi hujjatlarni terminaldan navbatga qo'yish: `.\.venv\Scripts\python.exe -m rag.ingest <papka>`
(xuddi sahifadan yuklagandek; indekslashni worker bajaradi).

**Indekslash qanday himoyalangan.** Worker alohida jarayon: u hujjatni olib, har 15 soniyada "tirikman" deb xabar beradi.
Worker o'lsa, 90 soniyadan keyin hujjat boshqa worker'ga qaytadi (3 urinishgacha). Bir hujjat 30 daqiqadan oshsa,
xato deb belgilanadi va worker qayta ishga tushadi. Vaqtinchalik xato (Qdrant, Ollama) qayta uriniladi, o'qib bo'lmaydigan
fayl esa darrov xato bo'ladi.

## Sozlamalar

Hammasi [rag/config.py](rag/config.py) da, `RAG_*` muhit o'zgaruvchilari bilan almashtiriladi:
`RAG_QDRANT_URL`, `RAG_COLLECTION`, `RAG_DATABASE_URL`, `RAG_STORAGE`, `RAG_LLM`, `RAG_EMBED_MODEL`, `RAG_TOP_K`,
`RAG_MIN_SCORE`, `RAG_PORT`, `RAG_MAX_UPLOAD_MB`, `RAG_TESSERACT`, `RAG_TESSDATA`, `RAG_JOB_TIMEOUT_S`,
`RAG_JOB_MAX_ATTEMPTS`.

## Tuzilma

```
rag/config.py   sozlamalar          rag/store.py    Qdrant bilan ishlash
rag/ingest.py   o'qish, bo'laklash  rag/llm.py      Ollama (embedding, javob)
rag/answer.py   qidiruv va javob    rag/worker.py   indekslovchi jarayon va nazoratchi
rag/api.py      veb API             rag/db.py       PostgreSQL: hujjatlar va navbat
rag/storage.py  fayllar diskda      rag/migrations  SQL migratsiyalar
static/         sahifa
```

## Testlar

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest                       # unit testlar: xizmatlar kerak emas, bir necha soniya
$env:RAG_TEST_DATABASE_URL = "postgresql://rag:rag@127.0.0.1:5433/rag"            # + PostgreSQL testlari (docker compose up -d postgres)
$env:RAG_INTEGRATION = "1"; .\.venv\Scripts\python.exe -m pytest tests/integration   # haqiqiy Qdrant + Ollama + Tesseract
$env:RAG_E2E = "1"; .\.venv\Scripts\python.exe -m pytest tests/e2e                   # brauzerda (Edge) butun oqim
.\.venv\Scripts\python.exe -m ruff check .
```

CI (GitHub Actions) unit testlar, PostgreSQL testlari va lint ni ishga tushiradi; qolganlariga modellar kerak.
Testlar PostgreSQL'da vaqtinchalik sxema yaratadi va tugagach o'chiradi, sizning ma'lumotlaringizga tegmaydi.

## Sifatni o'lchash

```powershell
.\.venv\Scripts\python.exe -m eval.run --set synthetic            # kod bilan yaratiladigan namunaviy hujjatlar
.\.venv\Scripts\python.exe -m eval.run --set eval/sets/injection  # hujjat ichidagi yashirin buyruqlarga bo'ysunish
.\.venv\Scripts\python.exe -m eval.run --set eval/data/meniki     # o'z hujjatlaringiz: docs/ va questions.jsonl
```

Qidiruv (to'g'ri fayl va parcha topilishi, sahifa), ball chegarasi, javob (fakt, manba, rad etish), tezlik hisoboti
`eval/reports/` ga yoziladi. Savollar formati [eval/run.py](eval/run.py) boshida. `eval/data/` va `eval/reports/` git'ga
kirmaydi, chunki ularda real hujjatlar bo'lishi mumkin.

## Ma'lum xavf: hujjat ichidagi buyruqlar

Hujjat matniga yashirilgan "javobga ... qo'shing" kabi buyruqlarga model ba'zan bo'ysunadi
(`eval/sets/injection` da 4 tadan 3 tasida to'g'ri javob beradi). Bir necha prompt variantini sinadim, hech biri
ishonchli yaxshilamadi. Ilova matnni faqat matn sifatida ko'rsatadi (havola, buyruq bajarish yo'q) va manbani ko'rsatadi,
shuning uchun eng yomon holat noto'g'ri javob. Hujjatlarni faqat ishonchli foydalanuvchilar yuklasin.

## Xavfsizlik va cheklovlar

- Ilova faqat `127.0.0.1` da tinglaydi, login yo'q: bitta kompyuter uchun mo'ljallangan.
- GPU bo'lmasa, javob 2-30 soniya oladi (qwen2.5:7b CPU'da). Oddiy matnli PDF ~1 soniya/sahifa, skan ~4 soniya/sahifa; jadvalli PDF (statistik byulleten kabi) 5-10 soniya/sahifa:
  381 betlik byulleten ~30-50 daqiqa oladi.
- `.doc` fayllar o'qilmaydi (avval `.docx` ga o'tkazing). Tesseract ba'zi kirill harflarni adashtiradi.
- Worker jarayoni qattiq o'ldirilsa (masalan `taskkill /F` bilan faqat ilovaga qarshi), uning bolasi qolib ketishi mumkin.
  Konsol oynasini yopish ikkalasini ham to'xtatadi.
