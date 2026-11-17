# Moslashtirish (reconcile), sog'liq va holat (T0.2)

Manba: audit DOC-03, WRK-02, yo'l xaritasi T0.2. Kod: [rag/reconcile.py](../rag/reconcile.py), [rag/health.py](../rag/health.py).

## 1. `python -m rag reconcile` (faqat o'qish)

```powershell
.\.venv\Scripts\python.exe -m rag reconcile                      # odam o'qiydigan hisobot
.\.venv\Scripts\python.exe -m rag reconcile --json               # to'liq JSON
.\.venv\Scripts\python.exe -m rag reconcile --no-hash --quick    # tez: fayl hash va nuqta payloadini o'qimaydi
.\.venv\Scripts\python.exe -m rag reconcile --include-superseded # rollback uchun saqlangan versiyalarni ham
```

Misol (jonli stek):

```
reconcile 2026-11-13T09:36:55+00:00  (0.15 s)
  PostgreSQL 5 documents, 5 versions; Qdrant 345 points; storage 5 files
  checked deeply: 5 versions, 5 files hashed
result: CLEAN
```

Muammo bo'lsa: `[ERROR  ] chunk_count_mismatch: a.txt v1: PostgreSQL says 3 chunks, Qdrant holds 2 (missing from the index)`.

**Chiqish kodi:** 0 toza · 1 kamida bitta xato (ma'lumot noto'g'ri yoki yo'q) · 2 faqat ogohlantirishlar (isrof yoki eski qoldiq) · 3 tekshiruv ishlay olmadi.

### Tekshiruvlar

Har `READY` versiya uchun (va `--include-superseded` bilan `SUPERSEDED` uchun ham): PostgreSQL `chunk_count` = Qdrant dagi shu `version_id` nuqtalari soni; asl fayl mavjud; fayl SHA-256 = `file_hash`; har nuqtada `document_id`, `file_hash`, `chunks_total` va `chunk_index` (takrorsiz, oraliqda) to'g'ri. Hamma hujjat uchun: faol ko'rsatkich mavjud, `READY` va shu hujjatniki; `READY` versiya faol bo'lmasa xato.

| Kod | Og'irlik | Ma'nosi |
|---|---|---|
| `chunk_count_mismatch` | xato | Qdrant dagi nuqtalar soni PostgreSQL dagidan farq qiladi (yo'qolgan vektor yoki ortiqcha) |
| `chunk_count_unknown` | xato | Versiyada `chunk_count` yo'q |
| `collection_missing` | xato | Qdrant kolleksiyasi yo'q |
| `payload_invalid` | xato | Nuqtaning `document_id`/`file_hash`/`chunks_total`/`chunk_index` noto'g'ri yoki takror |
| `missing_file` | xato | PostgreSQL ko'rsatgan fayl diskda yo'q |
| `hash_mismatch` | xato | Fayl yuklanganidan farq qiladi |
| `file_path_outside_storage` | xato | Yozilgan yo'l storage dan tashqariga chiqadi |
| `active_version_missing` / `active_version_not_ready` / `active_version_wrong_document` / `ready_version_not_active` | xato | Faol ko'rsatkich va holatlar nomuvofiq |
| `hash_unknown` | ogohlantirish | Taqqoslash uchun hash yozilmagan (eski, backfill qilinmagan versiya) |
| `orphan_points` | ogohlantirish | PostgreSQL bilmaydigan versiya nuqtalari |
| `failed_version_points` | ogohlantirish | Yiqilgan versiyaning qisman vektorlari |
| `untagged_points` | ogohlantirish | `version_id`siz nuqtalar |
| `orphan_file` | ogohlantirish | Hech bir versiya ko'rsatmaydigan fayl (`.uploading` hisobga olinmaydi) |
| `facet_truncated` | ogohlantirish | 100 000 dan ko'p versiya: sanoq to'liq emas |

Xotira: PostgreSQL qatorlari bir marta o'qiladi (hujjat/versiya metama'lumoti, fayl mazmuni emas); Qdrant sanoqlari bitta `facet`; nuqta payloadlari `scroll` bilan 500 tadan; fayllar oqim bilan hash qilinadi; storage `os.walk` bilan. Hisobotda har kod uchun dastlabki 20 ta misol ko'rsatiladi, jami son doim to'liq.

### Faqat o'qish kafolati (testlar bilan isbotlangan)

- **PostgreSQL:** bitta `READ ONLY`, `REPEATABLE READ` tranzaksiya; yozuv serverning o'zi tomonidan rad etiladi. Test: bog'lanishning o'zi `read_only` va yozuv `ReadOnlySqlTransaction` beradi; barcha jadvallarning har bir qatori reconcile dan oldin va keyin **bir xil**, shu jumladan `updated_at`, `status`, faol ko'rsatkichlar, avlodlar; ishlayotgan versiya `PROCESSING` da qoladi (qayta navbatga qo'yilmaydi).
- **Qdrant:** `ReadOnlyQdrant` faqat `count`, `scroll`, `facet`, `get_collection`, `collection_exists` ni o'tkazadi; `delete`, `upload_points`, `set_payload`, `create_snapshot`, `delete_collection`, `create_payload_index`, `recreate_collection` chaqirilsa `ReadOnlyViolation` (testda hammasi sinalgan, nuqtalar o'zgarmaydi).
- **Fayllar:** faqat ro'yxatga olish, `stat` va `"rb"` ochish. Test: reconcile topilmalar bilan ishlagandan keyin storage daraxti (hajm, `mtime`, mazmun) va Qdrant nuqtalari aynan avvalgidek.
- Yagona yoziladigan narsa: oxirgi hisobot `reports/reconcile-last.json` (uch ombordan tashqarida, `/api/status` uchun; `--no-save` bilan o'chiriladi).
- Mutatsiya tekshiruvi: bu kafolatlar vaqtincha buzilganda tegishli testlar yiqiladi.

Reconcile hech narsani **tuzatmaydi**. Tuzatish (qayta indekslash, tozalash) alohida, ruxsat bilan bajariladigan qadam (T1.1, T1.6).

## 2. Endpointlar

Mavjud himoya (faqat loopback, Host/Origin tekshiruvi, CSP, `nosniff`) yangi endpointlarga ham to'liq qo'llanadi (testlangan).

**`GET /healthz`** (liveness): faqat jarayon javob beradi. Hech qaysi bog'liqlikka tegmaydi, shuning uchun PostgreSQL yoki Qdrant o'chsa ham 200.
```json
{"status": "ok"}
```

**`GET /readyz`** (readiness): PostgreSQL (2 s timeout) va Qdrant (kolleksiya mavjudligi) majburiy; Ollama (`/api/tags`: ikkala model borligi) javob berish va indekslash uchun kerak, lekin ro'yxat/yuklash/o'chirish/rollback uchun emas.

| Holat | HTTP | Qachon |
|---|---|---|
| `ready` | 200 | uchalasi `ok` |
| `degraded` | 200 | faqat Ollama ishlamaydi (`unreachable` yoki `models_missing`) |
| `not_ready` | **503** | PostgreSQL yoki Qdrant ishlamaydi (`unreachable`, `collection_missing`) |

```json
{"status": "ready", "checks": {"postgres": "ok", "qdrant": "ok", "ollama": "ok"}}
```
Javoblarda faqat holat so'zlari bor: istisno matni, ulanish matni, parol, server yo'li yoki stack trace **hech qachon** chiqmaydi (sabab jurnalga yoziladi). Testlar tekshiradi.

**`GET /api/status`**: ixcham xulosa (hujjatlar ro'yxatini qaytarmaydi; 300 hujjatda ham javob 4 KB dan kichik).

```json
{
  "generated_at": "2026-11-13T09:37:52+00:00",
  "ready": "ready",
  "dependencies": {"postgres": "ok", "qdrant": "ok", "ollama": "ok"},
  "documents": {"total": 5, "searchable": 5, "by_state": {"done": 5}},
  "versions": {"total": 5, "by_status": {"READY": 5}},
  "queue": {"queued": 0, "oldest_queued_age_seconds": null, "processing": 0, "in_flight": []},
  "last_errors": [],
  "reconcile": null
}
```
Ishlayotgan versiya: `{"document", "version", "stage" (PROCESSING/EMBEDDING/INDEXING/VERIFYING), "attempt", "running_seconds", "heartbeat_age_seconds", "progress"}` (eng ko'pi 20 ta). `progress` (T0.3): `{"unit": "pages" | "assembling" | "chunks", "done", "total", "age_seconds"}`, ya'ni nima nechtasi tayyor va pozitsiya oxirgi marta qachon o'zgargani; bosqichda hali pozitsiya yozilmagan bo'lsa `null` ([job-progress.md](job-progress.md)). `last_errors`: eng ko'pi 10 ta (`FAILED` va xato bilan qayta navbatdagi versiyalar), matn `redact` dan o'tadi (URL dagi parol, `password=`, `token=` va h.k. `***`). `reconcile`: oxirgi `reconcile` ishining vaqti, yoshi, `ok`, xato/ogohlantirish soni va kodlar bo'yicha sanoq (yoki `null`). PostgreSQL ishlamasa, `documents`/`versions`/`queue`/`last_errors` `null`, bog'liqlik holatlari esa javob beradi.

Navbat yoshi `document_versions.updated_at` dan (versiya `QUEUED` ga o'tgan vaqt). Yangi jadval yaratilmadi; hammasi mavjud sxemaning o'qiydigan so'rovlari (`db.status_snapshot`, `db.ping`).

## 3. Tiklash bilan bog'liqligi

`reconcile` zaxira tiklangandan keyin avtomatik ishlaydi va uning "toza" bo'lishi tiklash mashqining qabul mezonlaridan biri ([backup-restore.md](backup-restore.md)).
