# Hujjat versiyalari va xavfsiz rollback

Hujjatning yangi nusxasi yuklanganda eski versiya to'liq qidiruvda qoladi; yangi versiya alohida indekslanadi,
tekshiriladi va **faqat tekshiruvdan o'tgandan keyin** faol bo'ladi. Biror qadam yiqilsa, eski versiya hech
o'zgarmay ishlashda davom etadi. Oldingi versiyaga qaytish qayta embedding talab qilmaydi.

## 1. Asosiy qarorlar va sabablari

| Qaror | Nima uchun |
|---|---|
| **PostgreSQL — yagona haqiqat manbai.** Faol versiya `documents.active_version_id` da. Qdrant faqat hosila indeks. | Faollashtirish bitta tranzaksiya bo'lishi uchun. Qdrant ga bir necha nuqtani "faol" deb belgilash atomik emas. |
| **Versiya qatori = uning ishi (job).** `claimed_by`, `attempts`, `heartbeat_at`, `started_at` `document_versions` da. | Navbat, heartbeat, qayta urinish va "o'lik worker" mexanizmlari avvalgidek ishlaydi, faqat hujjat o'rniga versiya bo'yicha. Alohida `jobs` jadvali bu bosqichda ortiqcha murakkablik. |
| **`documents.state / chunks / error / size_bytes` — versiyalardan hosil qilingan xulosa.** Har bir versiya holati o'zgargan tranzaksiyada `_sync_document` qayta yozadi. | "Hujjat holati va versiya holati izchil" sharti konstruksiya bo'yicha bajariladi. API dagi `state` shartnomasi o'zgarmaydi. |
| **Faol versiya o'sha hujjatning o'zidagi versiya bo'lishi DB darajasida kafolatlangan:** `FOREIGN KEY (active_version_id, id) REFERENCES document_versions (id, document_id)`, `DEFERRABLE INITIALLY DEFERRED`. | Boshqa hujjat versiyasini faol qilib bo'lmaydi (aralashish yo'q), va hujjat o'chirilganda versiyalari bilan birga ketadi (aylanma bog'lanish). |
| **Qidiruv faqat faol versiya id lari bo'yicha filtrlanadi** (`version_id MatchAny`), id lar DB dan **bir marta har savolga** olinadi. Faol versiya yo'q bo'lsa, hech narsa topilmaydi (bo'sh filtr "hammasi" degani bo'lardi). | Klient filtrga ta'sir qila olmaydi; bir hujjatning ikki versiyasi aralashmaydi; xavfsiz yopiladi. |
| **Eski ma'lumot — migratsiyada 1-versiya.** | Mavjud hujjatlar qayta indekslanmaydi. |
| **Aynan bir xil fayl** (oxirgi muvaffaqiyatsiz bo'lmagan versiya bilan bir xil hash) yangi versiya yaratmaydi. | Avvalgi "o'zgarmagan fayl hash bo'yicha o'tkazib yuboriladi" xulq-atvorini saqlaydi. |
| **Bir hujjatning versiyalari ketma-ket ishlanadi** (bir vaqtda bittasi), va faollashtirishda `versiya raqami > faol versiya raqami` sharti bor. | Eski versiya kech tugab, yangisini bosib ketmasligi uchun. Kech tugagani `SUPERSEDED` bo'lib rollback uchun saqlanadi. |
| **Rollback — doimiy foydalanuvchi qarori:** hujjatda `activation_generation` hisoblagichi, har versiyada `uploaded_generation`. Rollback hisoblagichni oshiradi; undan oldin yuklangan va tugamagan versiya faollashmaydi. | Ishlanayotgan yangi versiya tugab, foydalanuvchi tanlagan versiyani bosib ketmasligi uchun. 12-bo'limga qarang. |

## 2. Versiya hayot sikli

```
UPLOADED -> QUEUED -> PROCESSING -> EMBEDDING -> INDEXING -> VERIFYING -> READY
                                                                      \-> SUPERSEDED  (yangi versiya faollashganda)
                  (har qanday ish bosqichidan) -> FAILED  yoki qayta -> QUEUED (urinishlar tugamaguncha)
```

- Yuklangan versiya `UPLOADED`, fayl saqlangach `QUEUED` (worker olmaguncha ko'rinmaydi).
- Worker: `PROCESSING` (o'qish va bo'laklash) → `EMBEDDING` → `INDEXING` (Qdrant ga yozish) → `VERIFYING`.
- `READY` **faqat** `activate()` orqali, bitta tranzaksiyada. Faol versiyani almashtirganda eskisi `SUPERSEDED` bo'ladi.
- `FAILED` versiya hech qachon faol bo'lmaydi va faol versiyaga tegmaydi.
- Hujjat xulosasi: bironta versiya ish bosqichida → `processing`; aks holda `queued` → `uploading` → faol bor bo'lsa `done` → aks holda `failed`.
  Faol versiya ishlayotgan paytda yangisi yiqilsa, hujjat `done` bo'lib qoladi va sabab `update_error` da ko'rinadi.

## 3. PostgreSQL sxemasi

`rag/migrations/0002_document_versions.sql`:

- `document_versions`: `id`, `document_id` (→ `documents`, `ON DELETE CASCADE`), `version_number`, `file_name`,
  `file_hash`, `file_size`, `file_path`, `status` (CHECK, 9 holat), `chunk_count`, `error_message`, ish ustunlari
  (`attempts`, `claimed_by`, `heartbeat_at`, `started_at`), `created_at`, `updated_at`, `indexed_at`, `activated_at`.
- `UNIQUE (document_id, version_number)`, `UNIQUE (id, document_id)`, indekslar: `(document_id, status)`,
  navbat (`status = 'QUEUED'`), ish bosqichidagilar heartbeat bo'yicha.
- `documents.active_version_id` va yuqoridagi aralash tashqi kalit.
- `file_hash` faqat migratsiya ro'yxatga olgan versiyalar uchun vaqtincha `NULL` (SQL fayl hashini hisoblay olmaydi);
  `versioning.backfill_legacy` uni to'ldiradi.
- `0003_rollback_generation.sql` (qo'shimcha): `documents.activation_generation` va `document_versions.uploaded_generation`
  (`bigint NOT NULL DEFAULT 0`; mavjud qatorlar 0 dan boshlanadi).
- Versiya raqami `create_version` da hujjat qatorini qulflab (`INSERT … ON CONFLICT DO UPDATE`) beriladi, shuning
  uchun parallel yuklashlarda raqamlar to'qnashmaydi.

**Qulf tartibi** hamma joyda bir xil: o'zgartirilayotgan versiya → uning hujjati → almashtirilayotgan versiya.
Shu tufayli worker, timeout nazoratchisi va rollback bir-birini o'ldirmaydi (deadlock yo'q).

## 4. Qdrant

Har bir bo'lak payloadida: `document_id`, `version_id`, `file_hash`, `file_name`, `chunk_index`, `chunks_total`,
`pages`, `headings`, `text` va avvalgidek `source` (= hujjat id; `delete_source`, `purge_orphans` va hujjatni
o'chirish shu bilan ishlaydi). `document_id` va `version_id` uchun payload indekslari yaratiladi.

Point id = `uuid5(NAMESPACE_URL, "<version_id>:<chunk_index>")`: bir versiyani qayta yozish xuddi o'sha
nuqtalarni almashtiradi (dublikat yo'q), boshqa versiya boshqa nuqtalar to'plamiga ega. Qayta urinishdan oldin
versiyaning o'z nuqtalari o'chiriladi (oldingi urinish ko'proq bo'lak yozgan bo'lsa, qoldiq qolmasin). Boshqa
versiyalarga tegilmaydi.

## 5. Indekslash va tekshiruv

`rag/ingest.py::index_version` bosqichma-bosqich ishlaydi va worker bosqichlarni DB ga yozadi (da'vo yo'qolgan
bo'lsa, `ClaimLost` bilan to'xtaydi). Keyin `versioning.verify`:

1. `version_id` bo'yicha Qdrant dagi soni == kutilgan bo'laklar soni;
2. shu `version_id` + `document_id` + `file_hash` + `chunks_total` bo'yicha soni ham shunga teng.

Yetishmagan, ortiqcha, boshqa hujjat yoki boshqa fayl ma'lumoti bo'lsa, `IndexIncomplete`: versiya faollashmaydi,
ish siyosatga ko'ra qayta uriniladi yoki `FAILED` bo'ladi.

Indekslashdan oldin worker diskdagi faylning SHA-256 ini DB dagi `file_hash` bilan solishtiradi; mos kelmasa
(fayl yuklangandan keyin o'zgargan) versiya hech qachon indekslanmaydi va qayta urinilmaydi.

Tekshiruv natijasi DB da saqlanadi: `verify` o'tgach worker `db.record_verified` bilan `chunk_count` ni yozadi
(har yangi da'voda u `NULL` ga qaytariladi). `VERIFYING` faqat bosqich nomi; faollashtirish tekshiruv yozilmasa yoki
son mos kelmasa rad etiladi.

## 6. Atomik faollashtirish (`db.activate`)

Bitta tranzaksiyada: versiyani qulflash → hujjatni qulflash → joriy faol versiyani qulflash; versiya shu worker
da'vosida, `VERIFYING` da va tekshirilgan `chunk_count` yozilgan hamda mos ekanini tekshirish; **keyin qaror:**
agar versiya yuklangandan keyin rollback bo'lgan bo'lsa (`uploaded_generation < activation_generation`) yoki faol
versiya raqami kattaroq bo'lsa, versiya `SUPERSEDED` bo'lib saqlanadi (`"superseded"`); aks holda eskisi `SUPERSEDED`,
yangisi `READY` bo'ladi, `active_version_id` almashadi (`"activated"`); hujjat xulosasi yangilanadi; ish (da'vo)
bo'shatiladi. Biror qadam yiqilsa, hammasi qaytadi.
Almashtirilgan (o'lik deb hisoblangan) worker `claimed_by` mos kelmagani uchun hech narsa yoza olmaydi.

## 7. Qidiruv

`answer.retrieve(question, client, repo)` → `repo.active_version_ids()` (bir marta) → `answer.search(...)` mandatory
filtr bilan. Baholash (`eval/`) versiyasiz kolleksiyalarda ishlaydi va buni `answer.UNVERSIONED` bilan **ochiq**
tanlaydi; ishlab chiqarish yo'lida bunday parametr yo'q.

## 8. API

| Yo'l | Vazifa |
|---|---|
| `POST /api/documents` | Fayl yuklash. Natijada `version` va `unchanged` (aynan shu fayl allaqachon bor) qaytadi. Hujjat o'qilayotgan bo'lsa ham qabul qilinadi (keyingi versiya bo'ladi). |
| `GET /api/documents` | Avvalgi maydonlar + `version` (faol raqam), `latest_version`, `update_error`. |
| `GET /api/documents/{nom}/versions` | Barcha versiyalar (yangisi birinchi): holat, faolmi, hajm, bo'laklar, xato, sanalar. |
| `POST /api/documents/{nom}/rollback` | `{"version": 1}`: shu versiyani faol qiladi. |
| `DELETE /api/documents/{nom}` | Hamma versiya, fayllar va vektorlar bilan o'chiradi; biror versiya o'qilayotgan bo'lsa 409. |

## 9. Rollback (yo'riqnoma)

```powershell
# versiyalarni ko'rish
Invoke-RestMethod http://127.0.0.1:8765/api/documents/hisobot.pdf/versions
# 1-versiyaga qaytish
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/api/documents/hisobot.pdf/rollback `
  -ContentType application/json -Body '{"version": 1}'
```

Qaytish mumkin bo'lishi uchun versiya bir paytlar faol bo'lgan (`SUPERSEDED`) bo'lishi kerak. Muvaffaqiyatli rollback
**doimiy qaror**: undan oldin navbatda turgan yoki ishlanayotgan versiyalar tugagach ham tanlangan versiyani almashtirmaydi
(12-bo'lim). Rad etilgan rollback hech narsani o'zgartirmaydi, shuning uchun kutayotgan versiyalar odatdagidek faollashadi.
Qadamlar: versiyaning
vektorlari Qdrant da to'liq ekani tekshiriladi (5-bo'limdagi tekshiruv, qayta embedding yo'q); keyin PostgreSQL da
tranzaksiya (holat qulf ostida qayta tekshiriladi). Rad etish holatlari (hech narsa o'zgarmaydi): versiya yo'q (404),
allaqachon faol, hech qachon faol bo'lmagan yoki yiqilgan (409), vektorlar yetishmaydi yoki noto'g'ri (409,
"Versiya indeksi to'liq emas").

## 10. Migratsiya yo'riqnomasi

Migratsiya ilova yoki worker ishga tushganda avtomatik qo'llanadi (`db.migrate`, advisory lock bilan, bir marta).
U **faqat qo'shimcha**: jadval va ustun qo'shadi, hech narsa o'chirmaydi; `documents` ning eski ish ustunlari
(`attempts`, `claimed_by`, `heartbeat_at`, `started_at`) joyida qoladi, ishlatilmaydi.

1. Zaxira: `docker exec rag-postgres-1 pg_dump -U rag -d rag > rag_oldin.sql`
2. Ilovani to'xtating (eski worker yangi sxemada ishlamaydi), yangi kodni oling.
3. `start.bat` (yoki `python -m rag`). Migratsiya qo'llanadi; har bir mavjud hujjat 1-versiya bo'ladi
   (`done` → `READY` va faol; `queued/processing/failed/uploading` → mos holat).
4. Worker/API ishga tushganda `backfill_legacy` eski bo'laklarga `version_id` va `document_id` yozadi va fayl hashini
   to'ldiradi. Bu takrorlansa zarar yo'q. **Shu ish tugamaguncha eski hujjatlar qidiruvda ko'rinmaydi** (xavfsiz yopiladi);
   Qdrant ishlamay turgan paytda ilova ochilsa, worker uni o'z ishga tushishida bajaradi.
5. `0003_rollback_generation.sql` ham avtomatik qo'llanadi (ikki ustun, hech narsa o'chmaydi). Ishlayotgan eski worker
   yangi ustunlarni bilmaydi, shuning uchun ilova va workerni birga qayta ishga tushiring.
6. Tekshiruv: `SELECT d.name, v.version_number, v.status, d.active_version_id = v.id AS active FROM documents d JOIN document_versions v ON v.document_id = d.id;`

Orqaga qaytarish avtomatik emas. Faqat yangi yuklash bo'lmagan bo'lsa, zaxiradan tiklash mumkin; yangi versiyalar
yangi fayl joylashuvida (`storage/documents/...`) turadi va eski kod uni o'qimaydi.

## 11. Ma'lum cheklovlar

- **Avtomatik tozalash yo'q.** Eskirgan (`SUPERSEDED`) versiyalarning fayllari va vektorlari saqlanadi, disk o'sadi.
  Yiqilgan versiyaning qisman vektorlari ham qoladi (qidiruvga ko'rinmaydi; qayta urinishda yoki hujjat o'chirilganda ketadi).
- **Rollbackdan oldin boshlangan versiyalar baribir oxirigacha ishlanadi** (parserni ish o'rtasida xavfsiz to'xtatib
  bo'lmaydi). Ular faollashmaydi, `SUPERSEDED` bo'lib saqlanadi va keyin qo'lda "oldinga qaytish" mumkin; ish vaqtida
  hujjat `processing` deb ko'rinadi. (Avvalgi poyga, ya'ni rollbackni bosib ketish, 12-bo'limdagi usul bilan bartaraf etilgan.)
- **Rollback tekshiruvi (Qdrant) va almashtirish (PostgreSQL) ikki alohida qadam.** Orada vektorlar qo'lda
  o'chirilsa, bu aniqlanmaydi (tozalash xizmati yo'qligi sababli xavf past).
- **Eski (migratsiyadan oldingi) 1-versiya** nuqta id lari yangi sxemada emas, `file_hash` esa backfill gacha noma'lum.
- **Hajm.** Faol versiyalar ro'yxati har savolda `MatchAny` ga beriladi; minglab hujjatdan katta hajmda sinalmagan.
- **Aynan bir xil fayl** faqat oxirgi muvaffaqiyatsiz bo'lmagan versiya bilan solishtiriladi.
- **UI** versiyalarni ko'rsatmaydi (faqat API). Hujjat nomi hali global unikal (3-bosqichda bo'limlar bo'yicha bo'ladi).
- `documents` dagi eski ish ustunlari keyingi migratsiyada, sizning ruxsatingiz bilan, olib tashlanadi.

## 12. Rollback va tugamagan versiyalar: parallellik strategiyasi

**Muammo (ildiz sabab).** Faollashtirish faqat `versiya raqami > faol versiya raqami` ni tekshirardi. v3 ishlanayotganda
foydalanuvchi v1 ga qaytsa (faol raqam 1 bo'ladi), v3 tugagach `3 > 1` bo'lgani uchun faol bo'lib, foydalanuvchi
tanlovini jimgina bosib ketardi. Rollback faqat `active_version_id` ni almashtirardi va tugamagan versiyalarga
"qaror bo'lgan" degan belgi qoldirmasdi.

**Yechim: faollashtirish avlodi (activation generation).**

- `documents.activation_generation` rollbackni sanaydi. Faqat `db.switch_active` (muvaffaqiyatli rollback) uni 1 ga oshiradi;
  oddiy faollashtirish oshirmaydi (aks holda ketma-ket yuklangan v2, v3 bir-birini yaroqsiz qilardi).
- `create_version` hujjat qatorini qulflab, o'sha paytdagi avlodni versiyaning `uploaded_generation` iga yozadi.
- `db.activate` hujjat qulfi ostida `uploaded_generation < activation_generation` ni tekshiradi. Shart bajarilsa, versiya
  `SUPERSEDED` bo'ladi (indeksi va `chunk_count` saqlanadi), hech qachon `READY` emas.
- Rollbackdan keyin yuklangan versiya yangi avlodni oladi va odatdagidek (navbat bilan) faollashadi.

**Poyga nima uchun yo'q.** Rollback va faollashtirish bir xil qulf tartibini ishlatadi (o'z versiyasi → hujjat →
almashtirilayotgan versiya), ya'ni hujjat qatorida ketma-ketlashadi:

| Tartib | Natija |
|---|---|
| Rollback avval | Avlod oshadi; faollashtirish uni ko'radi va nomzodni `SUPERSEDED` qiladi. Faol: foydalanuvchi tanlovi. |
| Faollashtirish avval | Yangi versiya faol bo'ladi, so'ng rollback uni (keyingi qaror sifatida) almashtiradi. Faol: foydalanuvchi tanlovi. |
| Yuklash va rollback bir vaqtda | Hujjat qulfi ketma-ketlashtiradi: rollbackdan oldin kelgan yuklash eski avlod (yaroqsiz nomzod), keyin kelgani yangi avlod oladi. |

Ikkala tartibda ham oxirgi holat bir xil (test: 25 marta parallel takrorlanadi). Tekshiruv hujjat qulfi ostida bo'lgani uchun
"tekshirdim, keyin almashtirdim" oralig'i yo'q.

**Nima uchun aynan shunday (muqobillar).**

- *Rollbackda barcha tugamagan versiyalarni qatorma-qator bekor qilish* worker lar band qilgan qatorlarni yangilashni
  talab qiladi (qulf tartibini buzadi, deadlock xavfi) va ishlayotgan parserni to'xtata olmaydi. Hisoblagich esa bitta qatorni
  o'zgartiradi va hamma nomzodni bir lahzada yaroqsiz qiladi.
- *Faqat versiya raqami bo'yicha qaror* — aynan shu xato manbai edi.
- *Rollbackni tugamagan versiya bor paytda rad etish* — foydalanuvchini yomon yangi versiya tugashini kutishga majburlardi.

**Siyosat:** rollbackdan oldin boshlangan nomzodlar ish bilan bekor qilinmaydi: ular tugaydi va `SUPERSEDED` bo'lib
saqlanadi (yo'qotish yo'q, keyin qo'lda "oldinga qaytish" mumkin). Bunga mos ravishda hech qanday tarixiy versiya
o'chirilmaydi va hech narsa qayta embedding qilinmaydi.
