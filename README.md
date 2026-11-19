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
# tashqi bazalar ulagichi uchun (ixtiyoriy, docs/connector.md):
.\.venv\Scripts\python.exe -m pip install -r requirements-connector.txt

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

Yuklangan fayllar `storage/documents/<hujjat id>/versions/<versiya id>/original.<kengaytma>` da, hujjatlar, versiyalar
va indekslash navbati PostgreSQL'da turadi. Hujjatni qayta yuklash yangi **versiya** yaratadi (batafsil: [docs/versioning.md](docs/versioning.md)).
Ilova qayta yoqilsa ham navbat va ro'yxat saqlanadi. Eski `documents/` papkasi va `documents` kolleksiyasi
(1-versiya) endi o'qilmaydi, kerak bo'lmasa o'chirib tashlash mumkin.

Papkadagi hujjatlarni terminaldan navbatga qo'yish: `.\.venv\Scripts\python.exe -m rag.ingest <papka>`
(xuddi sahifadan yuklagandek; indekslashni worker bajaradi).

**Indekslash qanday himoyalangan.** Worker alohida jarayon: u hujjatni olib, har 15 soniyada "tirikman" deb xabar beradi.
Worker o'lsa, 90 soniyadan keyin hujjat boshqa worker'ga qaytadi (3 urinishgacha). Hujjat qancha davom etishi
muhim emas, **taraqqiyot to'xtashi** muhim: sahifa, bo'lak yoki bosqich 10 daqiqa (`RAG_JOB_STALL_S`) davomida o'zgarmasa,
ish xato deb belgilanadi va worker qayta ishga tushadi; jami chegara 6 soat (`RAG_JOB_TIMEOUT_S`). Taraqqiyot (bosqich va
"120/381 sahifa") PostgreSQL ga yoziladi va sahifada ko'rinadi. Vaqtinchalik xato (Qdrant, Ollama) qayta uriniladi,
o'qib bo'lmaydigan fayl esa darrov xato bo'ladi. Batafsil: [docs/job-progress.md](docs/job-progress.md).

**Savol-javob qanday himoyalangan.** Ollama javob bermasa, API 20 soniyada aniq xato (503) qaytaradi; Ollama tirik, lekin juda
sekin bo'lsa 504. Brauzer yopilsa yoki uzilsa, generatsiya darhol to'xtatiladi va keyingi savol uni kutmaydi. Javob uzunligi
cheklangan (`RAG_LLM_NUM_PREDICT`, 512 token), modellar xotirada saqlanadi (`RAG_OLLAMA_KEEP_ALIVE`) va server yoqilganda
isitiladi. Batafsil: [docs/llm-timeouts.md](docs/llm-timeouts.md).

**Yuklash hajmi.** Bitta fayl `RAG_MAX_UPLOAD_MB` (200 MB) dan katta bo'lmasligi kerak. Yuklash so'rovi `Content-Length` bilan
kelishi shart (aks holda 411) va jami tanasi shu chegara + 1 MB dan oshmasligi kerak (aks holda 413): ikkalasi ham tana diskka
yozilishidan oldin rad etiladi. Sahifa tanlangan fayllarni **bittadan, ketma-ket** yuboradi ("2/3 fayl yuklanmoqda"), bitta fayl
xatosi qolganlarini to'xtatmaydi va xato fayl nomi bilan ko'rsatiladi. Batafsil: [docs/upload-limits.md](docs/upload-limits.md).

**Klaviatura va ekran o'quvchi.** Sahifa avtomatik yangilanganda fokus (savol maydoni, ro'yxatdagi tugmalar) saqlanadi; savol
maydoni, tugmalar, xato va holat xabarlari ekran o'quvchi uchun nomlangan. Batafsil: [docs/ui-accessibility.md](docs/ui-accessibility.md).

## Zaxira, tekshiruv va holat

```powershell
.\.venv\Scripts\python.exe -m rag backup            # PostgreSQL + fayllar + Qdrant snapshot -> backups/rag-backup-...
.\.venv\Scripts\python.exe -m rag backup --verify <papka>
.\.venv\Scripts\python.exe -m rag restore <papka> --database-url ... --qdrant-url ... --storage ...   # faqat BO'SH muhitga
.\.venv\Scripts\python.exe -m rag reconcile         # faqat o'qish: PostgreSQL, Qdrant va fayllarni solishtiradi
```

`GET /healthz` (jarayon tirik), `GET /readyz` (PostgreSQL va Qdrant tayyor; Ollama yo'q bo'lsa "degraded"), `GET /api/status`
(navbat yoshi, bosqichlar, oxirgi xatolar, bog'liqliklar, oxirgi reconcile). Batafsil:
[docs/backup-restore.md](docs/backup-restore.md) (izchillik, tiklash, o'lchangan RPO/RTO) va
[docs/reconcile-and-status.md](docs/reconcile-and-status.md). Zaxira papkasi barcha hujjatlarni o'z ichiga oladi: maxfiy saqlang.

## Sozlamalar

Hammasi [rag/config.py](rag/config.py) da, `RAG_*` muhit o'zgaruvchilari bilan almashtiriladi:
`RAG_QDRANT_URL`, `RAG_COLLECTION`, `RAG_DATABASE_URL`, `RAG_STORAGE`, `RAG_BACKUP_DIR`, `RAG_REPORT_DIR`, `RAG_LLM`, `RAG_EMBED_MODEL`, `RAG_TOP_K`,
`RAG_MIN_SCORE`, `RAG_PORT`, `RAG_MAX_UPLOAD_MB`, `RAG_TESSERACT`, `RAG_TESSDATA`, `RAG_JOB_STALL_S`, `RAG_JOB_TIMEOUT_S`,
`RAG_JOB_MAX_ATTEMPTS`. Ollama: `OLLAMA_HOST`, `RAG_LLM_NUM_PREDICT`, `RAG_OLLAMA_KEEP_ALIVE`, `RAG_WARMUP`,
`RAG_LLM_FIRST_TOKEN_TIMEOUT_S`, `RAG_LLM_IDLE_TIMEOUT_S`, `RAG_LLM_TOTAL_TIMEOUT_S`, `RAG_EMBED_TIMEOUT_S`,
`RAG_OLLAMA_UNRESPONSIVE_S`, `RAG_OLLAMA_PROBE_INTERVAL_S`, `RAG_OLLAMA_CONNECT_TIMEOUT_S`.

## Tuzilma

```
rag/config.py   sozlamalar          rag/store.py    Qdrant bilan ishlash
rag/ingest.py   o'qish, bo'laklash  rag/llm.py      Ollama (embedding, javob)
rag/answer.py   qidiruv va javob    rag/worker.py   indekslovchi jarayon va nazoratchi
rag/api.py      veb API             rag/db.py       PostgreSQL: hujjatlar, versiyalar, navbat
rag/backup.py   zaxira va tiklash   rag/reconcile.py  faqat o'qiydigan moslashtirish
rag/health.py   healthz/readyz/status  rag/target.py  qaysi baza/kolleksiya/papka bilan ishlash
rag/storage.py  fayllar diskda      rag/migrations  SQL migratsiyalar
rag/relay.py    savolni alohida oqimda bajarish va bekor qilish
rag/bodylimit.py  yuklash tanasi hajmini erta tekshirish (413)
rag/versioning.py  indeksni tekshirish, rollback, eski hujjatlarni moslash
static/         sahifa
```

## Testlar

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest                       # unit testlar: xizmatlar kerak emas, bir necha soniya
$env:RAG_TEST_DATABASE_URL = "postgresql://postgres:<parol>@127.0.0.1:55433/postgres"   # + PostgreSQL testlari: alohida vaqtincha server (docs/postgres-hardening.md)
$env:RAG_INTEGRATION = "1"; .\.venv\Scripts\python.exe -m pytest tests/integration   # haqiqiy Qdrant + Ollama + Tesseract
$env:RAG_E2E = "1"; .\.venv\Scripts\python.exe -m pytest tests/e2e                   # brauzerda (Edge) butun oqim
.\.venv\Scripts\python.exe -m ruff check .
```

CI (GitHub Actions) unit testlar, PostgreSQL testlari va lint ni ishga tushiradi; qolganlariga modellar kerak.
Testlar PostgreSQL'da vaqtinchalik sxema yaratadi va tugagach o'chiradi, sizning ma'lumotlaringizga tegmaydi.

Qattiqlashtirilgan bazada (`docs/postgres-hardening.md`) superuser tarmoqdan kirmaydi, shuning uchun PostgreSQL testlari uchun alohida vaqtincha server ishlating (o'sha hujjatning oxirgi bandi). `tests/integration/test_pg_hardening.py` o'zining vaqtincha Docker serverini o'zi ko'taradi.

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
