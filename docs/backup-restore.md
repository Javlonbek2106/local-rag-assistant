# Zaxira nusxa va tiklash (T0.1)

Manba: audit DOC-08, yo'l xaritasi T0.1. Kod: [rag/backup.py](../rag/backup.py), mashq: [scripts/restore_drill.py](../scripts/restore_drill.py).

## 1. Nima saqlanadi va qayerga

```powershell
.\.venv\Scripts\python.exe -m rag backup                 # standart joy: backups/ (git dan tashqarida)
.\.venv\Scripts\python.exe -m rag backup --to D:\zaxira  # boshqa joy
.\.venv\Scripts\python.exe -m rag backup --wait-idle 600 # navbat bo'shashini 10 daqiqagacha kutadi
.\.venv\Scripts\python.exe -m rag backup --verify D:\zaxira\rag-backup-20261113-094802   # faqat tekshirish
```

Har ishga tushirish **yangi** papka yaratadi (`rag-backup-YYYYmmdd-HHMMSS`), hech narsani ustiga yozmaydi, ko'p marta ishlatish mumkin.

| Fayl | Mazmuni |
|---|---|
| `postgres.dump` | `pg_dump -Fc`: butun baza (sxema, ma'lumot, migratsiya tarixi, versiyalar, faol ko'rsatkichlar, avlodlar) |
| `storage/` | Barcha asl fayllar, `documents/<hujjat>/versions/<versiya>/original.<kengaytma>` yo'llari bilan |
| `qdrant.snapshot` | Qdrant kolleksiya snapshoti (vektorlar, payload, payload indekslari) |
| `manifest.json` | Yaratilgan vaqt, PostgreSQL versiyasi/bazasi, migratsiyalar ro'yxati, Qdrant kolleksiyasi/snapshot nomi/nuqtalar soni/o'lchami, har artefakt va har fayl uchun hajm va SHA-256, `status`, `consistent`, ish boshi/oxiridagi "barmoq izi" |

Manifestda **parol, ulanish matni yoki `.env` yo'q** (faqat host, port, baza nomi). Zaxira barcha hujjatlarni o'z ichiga oladi: uni ma'lumotning o'zidek maxfiy saqlang.

## 2. Zaxira qanday ishlaydi (to'liq bo'lmagan zaxira "muvaffaqiyatli" ko'rinmaydi)

1. Oldindan tekshiruv: Qdrant kolleksiyasi va storage papkasi bor.
2. Papka `<nom>.partial` sifatida quriladi; manifest avval `in_progress` holatida yoziladi.
3. "Barmoq izi" olinadi (hujjat/versiya soni, oxirgi o'zgarish vaqti, avlodlar yig'indisi, Qdrant nuqtalari, storage fayl soni/hajmi).
4. `pg_dump` (bir tranzaksiya: bitta vaqt nuqtasi) → fayllar nusxasi (hash bilan) → Qdrant snapshot yaratilib yuklab olinadi (server nusxasi o'chiriladi).
5. Barmoq izi qayta olinadi va solishtiriladi.
6. Manifest `complete` bo'ladi, zaxira o'zini `verify` bilan tekshiradi, shundan keyingina papka `.partial` nomsiz nomni oladi.

Chiqish kodlari: **0** tayyor, **1** xato (papka `.failed` bo'lib qoladi, manifestda sabab, sir yo'q), **2** zaxira paytida ma'lumot o'zgargan (papka `.inconsistent`). `.partial`, `.failed`, `.inconsistent` papkalarni `verify` va `restore` rad etadi.

## 3. Izchillik (consistency) strategiyasi

Ishlayotgan worker yoki API **to'xtatilmaydi va o'ldirilmaydi**. Izchillik quyidagicha ta'minlanadi:

- PostgreSQL dumpi bir tranzaksiya, Qdrant snapshoti kolleksiya bo'yicha atomik, asl fayllar saqlangandan keyin o'zgarmaydi (versiya fayli hech qachon ustiga yozilmaydi).
- Uch nusxa bir necha soniya-daqiqada olinadi. Shu oraliqda yuklash, faollashtirish, rollback yoki o'chirish bo'lsa, **barmoq izi o'zgaradi** va zaxira `inconsistent` bo'ladi (chiqish kodi 2). Bu to'sib qo'ymaydi, lekin **aniqlaydi**: noizchil zaxira hech qachon tiklash uchun qabul qilinmaydi (`--allow-inconsistent` bilan ataylab saqlash mumkin).
- `--wait-idle SEKUND` navbat va indekslash tugashini kutib, keyin boshlaydi.
- PostgreSQL va Qdrant bir xil vaqt nuqtasiga mos: PostgreSQL nuqtasi `postgres.dump_started_at`, Qdrant snapshoti undan keyin, barmoq izi tengligi bu ikkisi orasida o'zgarish bo'lmaganini tasdiqlaydi.
- Tavsiya: zaxirani hech kim yuklamaydigan vaqtga (masalan, tunga) rejalashtiring.

Eng oddiy jadval (Windows Task Scheduler, har kuni 03:00): `.\.venv\Scripts\python.exe -m rag backup --to D:\zaxira --wait-idle 900`; chiqish kodi 0 dan boshqa bo'lsa, ogohlantiring. Eski zaxiralarni tozalash qo'lda (avtomatik tozalash yo'q).

## 4. Tiklash

Faqat **alohida, bo'sh** muhitga. Uch maqsad ham majburiy, hech biri standart emas:

```powershell
.\.venv\Scripts\python.exe -m rag restore D:\zaxira\rag-backup-20261113-094802 `
    --database-url postgresql://<foydalanuvchi>:<parol>@127.0.0.1:15433/rag `
    --qdrant-url http://127.0.0.1:16333 `
    --storage D:\yangi-storage `
    --pg-container yangi-pg-konteyner
```

Qattiqlashtirilgan (uch rolli) PostgreSQL ga tiklash: avval yangi serverda `db-setup --roles-only`, tiklashda `rag_migrator` URL, keyin `db-setup`; [postgres-hardening.md](postgres-hardening.md) "Zaxira va tiklash ta'siri". Zaxira rollarni va parollarni o'z ichiga olmaydi.

Qadamlar: (1) zaxira SHA-256 bilan to'liq tekshiriladi; (2) maqsad bo'shligi tekshiriladi, aks holda **rad etiladi va hech narsaga tegilmaydi**: bazada jadval bo'lmasligi, Qdrant kolleksiyasi mavjud emasligi, storage papkasi bo'sh yoki yo'qligi; (3) `pg_restore` (`--single-transaction --exit-on-error`); (4) migratsiya tarixi manifest bilan solishtiriladi; (5) fayllar hash tekshiruvi bilan nusxalanadi; (6) Qdrant snapshoti yuklanadi va nuqtalar soni solishtiriladi; (7) maqsadda `reconcile` ishlaydi. Chiqish kodi: 0 hammasi toza, 1 rad etildi/xato, 2 tiklandi, lekin `reconcile` muammo ko'rsatdi.

Tiklangan muhitda ilovani ishga tushirish uchun: `RAG_DATABASE_URL`, `RAG_QDRANT_URL`, `RAG_STORAGE` (va kerak bo'lsa `RAG_COLLECTION`, `RAG_PORT`) ni o'sha muhitga ko'rsatib `python -m rag` ni ishga tushiring. Fayl yo'llari bazada nisbiy, shuning uchun storage boshqa joyga ko'chsa ham ishlaydi. Qdrant versiyasi bir xil minor bo'lishi kerak (farq bo'lsa ogohlantirish chiqadi).

`pg_dump`/`pg_restore` hostda o'rnatilmagan bo'lsa, konteyner ichida ishlaydi (`--pg-container NOM`, aks holda compose xizmati `postgres`). Konteyner ichida ular ulanish uchun parol ishlatmaydi; hostdagi binar uchun parol faqat o'sha jarayonning `PGPASSWORD` muhit o'zgaruvchisida.

## 5. Tiklash mashqi va o'lchangan RPO/RTO

`python scripts/restore_drill.py` (haqiqiy Docling, Tesseract, Ollama konveyeri; ikkita yangi bo'sh stek; ishlayotgan tizimga tegmaydi) quyidagini bajaradi: manba tizimni qurish, zaxira, **zaxiradan keyin** yangi hujjat qo'shish, bo'sh stekka tiklash, tiklangan ma'lumotda haqiqiy ilovani ishga tushirish, qidiruv va Q&A, versiya tarixi va rollback.

**Natija (2026-11-13, `drill_passed: true`):**

| Ko'rsatkich | Qiymat |
|---|---|
| Muhit | Windows 11, 32 mantiqiy CPU, Docker Desktop, PostgreSQL 17.11, Qdrant 1.20.0, Python 3.14 |
| Ma'lumot hajmi | 11 hujjat, 12 versiya, 13 Qdrant nuqtasi, 12 fayl (162 497 bayt); zaxira papkasi 1,66 MB |
| Zaxira vaqti | **1,43 s** |
| Tiklash (bosqichlar) | tekshiruv 0,01 s, PostgreSQL 0,13 s, storage 0,02 s, Qdrant 0,81 s, reconcile 0,36 s; **jami 1,41 s** |
| Ilova tayyor (`/readyz`) | 1,6 s |
| Birinchi qidiruv | 0,56 s |
| **RTO** (tiklash boshidan ishlayotgan qidiruvgacha) | **3,6 s** |
| Tiklashdan keyin `reconcile` | toza (0 xato, 0 ogohlantirish), versiyalar bilan ham |
| Qidiruv | 11 savolning natijasi manba bilan **aynan bir xil** (10 tasida fakt birinchi bo'lakda; 08_wifi bu mashqda ataylab v2 ga o'zgartirilgan, ikkala tomonda ham bir xil) |
| Q&A (API orqali, 2 savol) | ikkalasi to'g'ri javob berdi |
| Versiya tarixi | `08_wifi.txt`: v2 faol, v1 `SUPERSEDED`, avlod = 2 saqlandi; tiklangan tizimda rollback (v1) ishladi, so'ng `reconcile` toza |
| **RPO** | tiklangan holat = dump boshlangan vaqt (`2026-11-13T09:48:02Z`); zaxiradan keyin qo'shilgan hujjat tiklangan tizimda **yo'q** (isbotlandi). Eng yomon yo'qotish = zaxira oralig'i + zaxira davomiyligi |

**Cheklov (halol):** bu o'lchov **kichik ma'lumotda** (megabaytlar). Katta hajmda (GB fayl, million nuqta) vaqt hajmga qarab o'sadi; bu hozir o'lchanmagan va ekstrapolyatsiya qilinmaydi. Haqiqiy foydalanuvchi ma'lumotining ustida tiklash sinovi **bajarilmadi** (qoida bo'yicha); faqat undan zaxira olish sinab ko'rildi: jonli dev stek 1,32 s da zaxiralandi (5 hujjat, 7,6 MB, 345 nuqta), tekshiruvdan o'tdi, vaqtinchalik nusxa o'chirildi.

## 6. Qo'lda bajariladigan va ma'lum cheklovlar

- Zaxira shifrlanmagan; saqlash joyini (disk/bulut) o'zingiz himoyalang.
- Zaxira jadvali va eski zaxiralarni tozalash avtomatik emas (yuqoridagi Task Scheduler misoli).
- Zaxira bir mashinadagi ikkinchi diskka yoki boshqa mashinaga ko'chirilmasa, disk nosozligidan himoya qilmaydi.
- Qdrant snapshoti faqat bir xil minor versiyada kafolatlanadi.
- Izchillik "aniqlash" usuli bilan (to'sish emas): band vaqtda zaxira `inconsistent` bo'lishi mumkin va qayta urinish kerak.
