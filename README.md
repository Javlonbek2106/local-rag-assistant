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

`start.bat` ni oching: Qdrant ko'tariladi, ilova ishga tushadi va brauzer ochiladi (`python -m rag`).
Hujjatlar `documents/` papkasida saqlanadi (git'ga kirmaydi). Sahifada ko'rinadigan hujjatlar shu papkadagilar.

Papkadagi hujjatlarni terminaldan qo'shish: `.\.venv\Scripts\python.exe -m rag.ingest <papka>`
(fayllar `documents/` ga nusxalanadi va indekslanadi).

## Sozlamalar

Hammasi [rag/config.py](rag/config.py) da, `RAG_*` muhit o'zgaruvchilari bilan almashtiriladi:
`RAG_QDRANT_URL`, `RAG_COLLECTION`, `RAG_LLM`, `RAG_EMBED_MODEL`, `RAG_TOP_K`, `RAG_MIN_SCORE`, `RAG_DOCS`, `RAG_PORT`,
`RAG_MAX_UPLOAD_MB`, `RAG_TESSERACT`, `RAG_TESSDATA`.

## Tuzilma

```
rag/config.py   sozlamalar          rag/store.py   Qdrant bilan ishlash
rag/ingest.py   o'qish, bo'laklash  rag/llm.py     Ollama (embedding, javob)
rag/answer.py   qidiruv va javob    rag/worker.py  fonda indekslash navbati
rag/api.py      veb API             static/        sahifa
```

## Testlar

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest                       # unit testlar: xizmatlar kerak emas, bir necha soniya
$env:RAG_INTEGRATION = "1"; .\.venv\Scripts\python.exe -m pytest tests/integration   # haqiqiy Qdrant + Ollama + Tesseract
$env:RAG_E2E = "1"; .\.venv\Scripts\python.exe -m pytest tests/e2e                   # brauzerda (Edge) butun oqim
.\.venv\Scripts\python.exe -m ruff check .
```

CI (GitHub Actions) faqat unit testlar va lint ni ishga tushiradi, chunki qolganlariga modellar kerak.

## Xavfsizlik va cheklovlar

- Ilova faqat `127.0.0.1` da tinglaydi, login yo'q: bitta kompyuter uchun mo'ljallangan.
- GPU bo'lmasa, javob 2-30 soniya oladi (qwen2.5:7b CPU'da). Matnli PDF ~1 soniya/sahifa, skan ~4 soniya/sahifa.
- `.doc` fayllar o'qilmaydi (avval `.docx` ga o'tkazing). Tesseract ba'zi kirill harflarni adashtiradi.
- Indekslash holati xotirada saqlanadi: ilova qayta ishga tushsa, `documents/` dagi fayllar qayta tekshiriladi
  (o'zgarmaganlari tez o'tkaziladi).
