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

`start.bat` ni oching: Qdrant ko'tariladi, ilova ishga tushadi va brauzer ochiladi.
Hujjatlar `documents/` papkasida saqlanadi (git'ga kirmaydi).

Papkadagi hujjatlarni terminaldan yuklash: `.\.venv\Scripts\python.exe ingest.py <papka>`

## Xavfsizlik va cheklovlar

- Ilova faqat `127.0.0.1` da tinglaydi, login yo'q: bitta kompyuter uchun mo'ljallangan.
- GPU bo'lmasa, javob 2-30 soniya oladi (qwen2.5:7b CPU'da). Matnli PDF ~1 soniya/sahifa, skan ~4 soniya/sahifa.
- `.doc` fayllar o'qilmaydi (avval `.docx` ga o'tkazing). Tesseract ba'zi kirill harflarni adashtiradi.
