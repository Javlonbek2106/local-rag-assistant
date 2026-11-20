# Tashqi bazalar ulagichi (M4.1b va M4.2)

Manba: [architecture-roadmap.md](architecture-roadmap.md) 6a-bo'lim, [architecture-decisions.md](architecture-decisions.md) ADR-P12…P16. Autentifikatsiya asosi: [connector-auth.md](connector-auth.md). Ichki bazani qattiqlashtirish: [postgres-hardening.md](postgres-hardening.md).

Bu hujjat M4.1b va M4.2 da **nima qurilganini** va qanday ishlatilishini tasvirlaydi. M4.2 bo'limlari ["Sxema konteksti va metama'lumot (M4.2)"](#sxema-konteksti-va-metamalumot-m42) dan boshlanadi. M4.3 (savoldan SQL **taklifi**) alohida hujjatda: [sql-generation.md](sql-generation.md). Qurilmagan narsa oxirida alohida ro'yxatda.

## Qisqacha

- Foydalanuvchi **PostgreSQL** va **Microsoft SQL Server** bazalariga ulanishlarni yaratadi, parolni **shifrlab** saqlaydi, ulanishni sinaydi, yoqadi/o'chiradi, tahrirlaydi, o'chiradi.
- Ulanish bo'yicha **sxema** (sxemalar, jadvallar, ko'rinishlar, ustunlar, turlar, birlamchi/noyob/tashqi kalitlar, indekslar) aniqlanadi va ichki bazada saqlanadi. **Ma'lumot qatorlari hech qachon o'qilmaydi va nusxalanmaydi.**
- Foydalanuvchi baza, sxema, jadval, ustun uchun **tavsif** yozadi (keyingi bosqichdagi tabiiy tildan SQL ga o'tkazish uchun).
- **M4.2:** har o'zgargan sxema **versiyali snapshot** bo'ladi (barmoq izi bilan), versiyalar orasidagi **o'zgarishlar** aniqlanadi, metama'lumotning **yangiligi** (`current`/`stale`/…) hisoblanadi, foydalanuvchi AI kontekstiga qaysi jadval va ustunlar **ochiq** ekanini tanlaydi va shu tanlovdan **deterministik sxema konteksti** (JSON, o'lcham chegarasi bilan) quriladi. Bu kontekstni hali hech qanday model ishlatmaydi.
- API (`/api/v1/connections`) va sahifadagi **Ulanishlar** bo'limi orqali boshqariladi.
- Hammasi `RAG_CONNECTOR_ENABLED=1` va M4.1a xavfsizlik shartlari bajarilganda ishlaydi; o'chiq bo'lsa hech narsa o'zgarmaydi.

## Qo'llab-quvvatlanadigan bazalar

| Baza | Sinalgan | Drayver | Eslatma |
|---|---|---|---|
| PostgreSQL | **15 va 17** (haqiqiy serverlarda, oddiy va TLS bilan) | `psycopg` 3 (ilovaning o'z bog'liqligi) | Katalog so'rovlari `pg_catalog` ga tayanadi (10+ da mavjud funksiyalar); 15 va 17 dan boshqasi sinalmagan |
| Microsoft SQL Server | **2022** (16.0; oddiy va TLS bilan) | `python-tds` (sof Python, tizim ODBC drayveri kerak emas) | `sys.*` ko'rinishlari ishlatiladi (2017+ da bo'lishi kutiladi); 2022 dan boshqasi sinalmagan |

Boshqa dvigatellar (MySQL, Oracle va h.k.) yo'q: provayder ro'yxatda bo'lmasa `invalid_provider`.

## Sozlash

1. Ixtiyoriy bog'liqliklarni o'rnating (asosiy ilova va CI ularsiz ham ishlaydi):
   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements-connector.txt
   ```
   (`cryptography` shifrlash va CA sertifikatni tekshirish uchun, `python-tds` + `pyOpenSSL` SQL Server uchun.) O'rnatilmagan bo'lsa ulagich `crypto_unavailable` / `driver_missing` deydi.
2. Ichki bazani M4.1a/7-qadam bo'yicha qattiqlashtiring ([postgres-hardening.md](postgres-hardening.md)): ulagich uch rolli tuzilmasiz yoqilmaydi.
3. Shifrlash kalitini yarating (bir marta; mavjudini hech qachon almashtirmaydi):
   ```powershell
   .\.venv\Scripts\python.exe -m rag connector key-init
   ```
   `start.bat` ulagich yoqilganda buni o'zi bajaradi.
4. `set RAG_CONNECTOR_ENABLED=1` va `start.bat`. Sahifada **Ulagich** bo'limida kod bilan kiring; kirgach **Ulanishlar** bo'limi paydo bo'ladi.

Sozlamalar (`RAG_*`, [rag/config.py](../rag/config.py)): `CONNECTOR_MAX_CONNECTIONS` (50, egasi uchun), `CONNECTOR_TEST_TIMEOUT_S` (10), `CONNECTOR_DISCOVERY_TIMEOUT_S` (60), `CONNECTOR_MAX_CONCURRENT` (4 ta tashqi amal bir vaqtda), `CONNECTOR_MAX_OBJECTS` (20 000), `CONNECTOR_MAX_COLUMNS` (300 000), `CONNECTOR_ALLOWED_HOSTS` (ixtiyoriy ruxsat ro'yxati). M4.2: `CONNECTOR_SNAPSHOT_RETENTION` (10, kamida 2: saqlanadigan snapshotlar), `CONNECTOR_SCHEMA_MAX_AGE_S` (7 kun: shundan eski tasdiq = `stale`), `CONNECTOR_MAX_CHANGES` (5000, snapshot uchun), `CONNECTOR_MAX_SELECTION_ITEMS` (50 000), `CONNECTOR_CONTEXT_MAX_CHARS` (24 000, standart o'lcham), `CONNECTOR_CONTEXT_HARD_MAX_CHARS` (200 000, eng katta so'rov).

## Ulanish yaratish

Maydonlar: `name` (egasi uchun noyob, kichik-katta harfga e'tiborsiz), `description`, `provider` (`postgresql` | `mssql`), `host`, `port`, `database_name`, `username`, `password`, `ssl_mode`, `options`.

**Faqat o'qish hisobidan foydalaning.** Ulagich tashqi bazaga faqat `SELECT 1` va katalog so'rovlarini yuboradi, lekin SQL Server da seans darajasida "faqat o'qish" yo'q; himoya hisob huquqlari. Sinov hisob yozishi mumkinligini aniqlasa `account_can_write` ogohlantirishini beradi. Misol (PostgreSQL):
```sql
CREATE ROLE rag_reader LOGIN PASSWORD '...';
GRANT CONNECT ON DATABASE mydb TO rag_reader;
GRANT USAGE ON SCHEMA public TO rag_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO rag_reader;
```
SQL Server: `CREATE LOGIN ...; CREATE USER ...; ALTER ROLE db_datareader ADD MEMBER ...;`

**Shifrlash (`ssl_mode`)**:

| Qiymat | PostgreSQL | SQL Server |
|---|---|---|
| `verify-full` (standart) | TLS, sertifikat va host nomi tekshiriladi | TLS, sertifikat va host nomi tekshiriladi; **`ca_certificate_pem` shart** |
| `verify-ca` | TLS, sertifikat tekshiriladi | TLS, sertifikat tekshiriladi; **CA shart** |
| `require` | TLS, tekshirilmaydi | **yo'q** (drayver qo'llamaydi): `invalid_ssl_mode` |
| `disable` | shifrlanmaydi | shifrlanmaydi (oddiy TDS) |

Boshqa kompyuterdagi baza uchun `require` va `disable` **rad etiladi** (`tls_required`), agar so'rovda `options.accept_insecure_tls: true` bilan xavf aniq qabul qilinmasa. Shu kompyuter (loopback) uchun ruxsat. PostgreSQL da CA ko'rsatilmasa tizim ildiz sertifikatlari ishlatiladi (`sslrootcert=system`). `options`: `connect_timeout_s` (1–30), `accept_insecure_tls`, `ca_certificate_pem` (ochiq sertifikatlar, ≤5, ≤64 KB). Boshqa kalit **rad etiladi**, e'tiborsiz qoldirilmaydi.

## API

Hamma marshrut sessiya talab qiladi (tokensiz 401), egasi **faqat** sessiyadagi principal; tanada egasi maydoni bo'lsa 422. Javoblar `Cache-Control: no-store`. Xato: `{"error": {"code": ...}}` (+ `reason` kodi); noto'g'ri tana ham qiymatni **qaytarmaydi** (faqat maydon nomlari).

| Marshrut | Vazifa | Step-up |
|---|---|---|
| `POST /api/v1/connections` | yaratish (201) | **ha** |
| `GET /api/v1/connections` | o'z ulanishlari | |
| `GET /api/v1/connections/{id}` | bittasi | |
| `PATCH /api/v1/connections/{id}` | o'zgartirish; faqat berilgan maydonlar; `password` berilmasa saqlangani qoladi | **ha**, agar `provider`, `host`, `port`, `database_name`, `username`, `ssl_mode`, `options` yoki `password` o'zgarsa; nom va tavsif uchun yo'q |
| `DELETE /api/v1/connections/{id}` | o'chirish (sxema va tavsiflar bilan) | |
| `POST …/{id}/enable`, `…/disable` | yoqish / o'chirib qo'yish | |
| `POST …/{id}/test` | ulanish sinovi (200; natija `status`, `error`, `warning`, `server_version`) | |
| `POST …/{id}/schema/discover` | sxemani aniqlash; javobda `snapshot` (`version`, `fingerprint`, `changed`, `first`, `changes`, `changes_truncated`) | |
| `GET …/{id}/schema` | sxemalar va ularning jadval/ko'rinish soni | |
| `GET …/{id}/schema/objects?schema=&q=&limit=&offset=` | jadval va ko'rinishlar (sahifalab, ≤500) | |
| `GET …/{id}/schema/object?schema=&name=` | ustunlar, kalitlar, indekslar | |
| `GET …/{id}/schema/relationships` | tashqi kalitlar | |
| `GET`/`PUT …/{id}/descriptions` (`GET …/schema/descriptions` ham) | tavsiflar (`orphaned`: obyekti hozir yo'q); bo'sh matn tavsifni o'chiradi; parolga/kalitga o'xshash matn `description_contains_secret` (400) | |
| `GET …/{id}/schema/snapshots` | snapshotlar ro'yxati (yangisi birinchi): versiya, barmoq izi, sonlar, `is_current` | |
| `GET …/{id}/schema/snapshots/{snapshot_id}` | bitta snapshot va uning sxemalari; noma'lum id `snapshot_not_found` (404) | |
| `GET …/{id}/schema/changes?version=&since_version=&object_type=&change_type=&limit=&offset=` | o'zgarishlar (`total`, `summary`); noto'g'ri filtr `invalid_request` | |
| `POST …/{id}/schema/changes/acknowledge` `{version}` | "o'zgarishlarni ko'rib chiqdim": joriy versiyaga tasdiqlash; eski versiya `snapshot_changed` (409) | |
| `GET …/{id}/schema/freshness` | yangilik holati (pastga qarang) | |
| `GET`/`PUT …/{id}/schema/selections` | ochiq obyektlar tanlovi; `PUT {items, base_revision}`: butunligicha almashtiradi, boshqa joyda o'zgargan bo'lsa `selection_conflict` (409), mavjud bo'lmagan yangi element `object_not_found` (404), qoida buzilsa `invalid_selection` (400, `reason` bilan) | |
| `GET …/{id}/schema/context?max_chars=&allow_stale=` | sxema konteksti (pastga qarang); yangi bo'lmasa `context_stale` (409, `reason` = holat); `allow_stale=true` bo'lsa ko'rish uchun beriladi, lekin `usable: false` | |
| `POST …/{id}/sql/generate` `{question}` | **M4.3:** savoldan **taklif qilingan** SQL (tekshirilmaydi, bajarilmaydi); batafsil: [sql-generation.md](sql-generation.md) | |

**Step-up nima uchun:** saqlangan parol boshqa serverga yo'naltirilishi mumkin emas. Sessiyani o'g'irlagan kishi `host` ni o'zgartirib parolni o'z serveriga yuborishi mumkin edi; shu sababli maqsadni o'zgartirish ham parolni o'zgartirish kabi yangi kod bilan tasdiqlanadi.

**Holatlar:** `ready`; `disabled` (sinov, aniqlash va kelajakdagi so'rovlar rad etiladi: `connection_disabled`); `needs_credentials` (kalit yo'q yoki shifrmatn ochilmaydi: parolni qayta kiriting).

## Hisob ma'lumotini shifrlash

- **AES-256-GCM** (autentifikatsiyalangan shifrlash); har shifrlashga yangi tasodifiy 96-bit nonce. Parol ochiq matn sifatida hech qayerda saqlanmaydi, qaytarilmaydi, jurnalga, xato matniga, xavfsizlik hodisasiga va zaxira manifestiga tushmaydi.
- **Bog'lovchi ma'lumot (AAD) = `connection_id` + `key_id`.** Egasi, nom, host yoki vaqt AAD da **yo'q**: egasi o'zgarsa ham shifrmatn ochilaveradi. Boshqa ulanishga ko'chirilgan shifrmatn ochilmaydi; `key_id` almashtirilgan shifrmatn ochilmaydi.
- **Konvert:** `credential_ciphertext` (shifrmatn + GCM tegi), `credential_nonce`, `credential_key_id`, `credential_version` (konvert formati, hozir 1).
- **Kalit** `encryption.key` faylida, **PostgreSQL, zaxira, repozitoriy va Docker image dan tashqarida** (M4.1a sir katalogi: `%LOCALAPPDATA%\local-rag-assistant\secrets\`, faqat joriy foydalanuvchi). Har qatorda `<key_id>:<32 bayt base64url>`; **birinchi qator faol** kalit, hammasi ochish uchun. Kalit har chaqiruvda fayldan o'qiladi (xotirada saqlanmaydi), shuning uchun olib tashlangan kalit darhol yo'qoladi.
- **Fail closed:** kalit yo'q, noto'g'ri qator, noma'lum `key_id`, noto'g'ri konvert, noto'g'ri kalit, o'zgartirilgan shifrmatn: hammasi qat'iy kodli `VaultError`; xato matnida kalit, shifrmatn, parol yoki yo'l yo'q.
- **Kalit aylantirishga tayyorgarlik** (avtomatik aylantirish yo'q):
  - `python -m rag connector key-rotate`: yangi faol kalit tepaga qo'shiladi, eskilari qoladi;
  - `python -m rag connector rewrap`: hamma shifrmatnni faol kalitga qayta o'raydi (takroran xavfsiz; ochib bo'lmaydigani bo'lsa to'xtaydi);
  - `python -m rag connector verify`: hamma shifrmatn halqa bilan ochilishini tekshiradi (faqat o'qish, faqat sonlar chiqadi);
  - `python -m rag connector key-init`: kalit yo'q bo'lsa yaratadi, **hech qachon almashtirmaydi**.
  Kalit o'zgarishi xavfsizlik jurnaliga yoziladi; jurnalga yozib bo'lmasa o'zgarish **qilinmaydi**.
- **Zaxira va tiklash:** zaxirada faqat shifrmatn bor (`pg_dump` matnida parol va kalit yo'qligi tekshirilgan). Tiklangan tizimda kalit bo'lmasa ulanishlar `needs_credentials` deydi, hujjatlarga ta'sir qilmaydi; kalit bilan hammasi ochiladi. **Kalitning nusxasini ma'lumotlar bazasi zaxirasidan alohida, xavfsiz joyda saqlang.** Kalit yo'qolsa parollarni tiklab bo'lmaydi: ularni qayta kiritasiz.

## Migratsiyalar

Ikkalasi ham qo'shimcha (mavjud jadval va qatorlar o'zgarmaydi, ilova (API yoki worker) birinchi ishga tushganda o'zi qo'llaydi, jarayonlar bir vaqtda ishga tushsa ham bir marta; uch rolli rejimda migratsiyani faqat `rag_migrator` bajaradi; ilova roli `rag_app` yangi jadvallarga faqat `RUNTIME_GRANTS` dagi huquqlarni oladi: SELECT/INSERT/UPDATE/DELETE, hech qachon DDL):

| Migratsiya | Bosqich | Nima qo'shadi |
|---|---|---|
| `0006_connector.sql` | M4.1b | `connections` (egasi `owner_principal_id`, shifrlangan hisob ma'lumoti: `credential_ciphertext`, `credential_nonce`, `credential_key_id`, `credential_version`), `connection_objects`, `connection_columns`, `connection_constraints`, `connection_indexes`, `connection_descriptions` |
| `0007_schema_snapshots.sql` | M4.2 | `schema_snapshots`, `schema_changes`, `connection_selections`; `connections` ga `current_snapshot_id`, `discovery_started_at`, `last_discovery_at/status/error`, `context_confirmed_version`, `selection_revision`; `connection_objects.snapshot_id` (M4.1b qatorlari snapshot 1 bo'ladi); `connection_objects` ning noyoblik qoidasi snapshot bo'yicha aniqroq qoidaga almashtiriladi |

Mavjud bazada yangi versiyani birinchi marta ishga tushirishdan oldin zaxira oling: [backup-restore.md](backup-restore.md).

## Sxema aniqlash

- Faqat katalog: PostgreSQL `pg_catalog` (jadval, bo'lingan jadvalning ota-jadvali, ko'rinish, materiallashtirilgan ko'rinish, tashqi jadval; bo'limlar (partitions) alohida ko'rsatilmaydi), SQL Server `sys.*` (jadval va ko'rinish). Tizim sxemalari (`pg_catalog`, `information_schema`, `sys`) tashqarida.
- Ustun: tartib raqami, nomi, tur (`numeric(12,2)`, `nvarchar(100)`, `varchar(max)`…), bo'sh bo'lishi. Kalitlar: birlamchi, noyob, tashqi; **kompozit kalit bitta yozuv** (ustunlar kalit tartibida); tashqi kalit ko'rsatgan jadval va ustunlar bilan. Indekslar (kalit ustunlari bilan; ifodali indekslarning ifodasi ko'rsatilmaydi).
- **Katta sxema:** katalog so'rovlari tartiblangan va oqim bilan o'qiladi (PostgreSQL: server tomonidagi kursor; SQL Server: `fetchmany`); yozish 500 qatorlik paketlar bilan; xotirada faqat obyektlar identifikatorlari (≤`CONNECTOR_MAX_OBJECTS`) va bitta paket. Chegaradan oshsa `too_large`.
- **Bir butun yozish (M4.2 dan snapshot bilan):** aniqlash bitta tranzaksiyada (alohida ulanishda, puldan tashqari) yangi snapshot qatorlarini yozadi; xato yoki vaqt tugashi bo'lsa hech narsa qolmaydi va **joriy snapshot o'zgarmaydi**. Aniqlangan sxema joriy snapshot bilan bir xil bo'lsa yangi snapshot **yaratilmaydi**. Vaqt chegarasi `CONNECTOR_DISCOVERY_TIMEOUT_S`.
- **Ruxsatlar:** SQL Server metama'lumotni hisob huquqiga qarab ko'rsatadi: huquqsiz hisob bo'sh sxema oladi (xato emas). PostgreSQL katalogi hammaga ochiq.
- **Tavsiflar** aniqlangan metama'lumotdan **alohida** jadvalda, **nom bo'yicha** saqlanadi, shuning uchun yangi aniqlash ularni yo'qotmaydi (obyekt yo'qolsa tavsif **yetim (`orphaned`)** bo'lib qoladi, yangi tavsif faqat mavjud obyektga yoziladi; obyekt qaytsa tavsif ham qaytadi).

## Sxema konteksti va metama'lumot (M4.2)

Bu bo'lim M4.2 da qurilganini tasvirlaydi: sxema snapshotlari, o'zgarishlarni aniqlash, yangilik holati, ochiq obyektlarni tanlash va sxema konteksti. **Hech narsa tashqi bazadan qator o'qimaydi, hech qanday model chaqirilmaydi, SQL yaratilmaydi yoki bajarilmaydi** (M4.3 va undan keyin).

### Snapshot modeli

- **Snapshot** deb tashqi bazaning sxemasi **o'zgargan** har bir muvaffaqiyatli aniqlashiga aytiladi: ulanish bo'yicha raqamlangan (`version` 1, 2, 3…, qayta ishlatilmaydi), o'zgarmas qatorlar to'plami (`connection_objects.snapshot_id` ostida obyektlar, ustunlar, kalitlar, indekslar). `connections.current_snapshot_id` joriy snapshotni ko'rsatadi; ko'rish API lari (`schema`, `objects`, `object`, `relationships`) faqat joriyni ko'rsatadi.
- **Aniqlash bitta tranzaksiya** (alohida ulanishda): avval ulanish qatori qulflanadi, yangi qatorlar yangi snapshot ostida yoziladi (savepoint ichida), barmoq izi hisoblanadi. Joriy snapshot bilan **bir xil** bo'lsa yangi qatorlar savepoint bilan bekor qilinadi va faqat joriy snapshotning `verified_at` (oxirgi tasdiq vaqti) yangilanadi: **hech narsa takrorlanmaydi**. Boshqacha bo'lsa o'zgarishlar yoziladi, yangi snapshot joriy bo'ladi va saqlash chegarasidan oshganlari o'chiriladi.
- **Xato, vaqt tugashi, `too_large` yoki jarayon yiqilishi** bo'lsa tranzaksiya qaytariladi: snapshot qisman bo'lmaydi, joriy snapshot **o'zgarmaydi** va o'qilaveradi. Urinish alohida eslab qolinadi (`last_discovery_at/status/error` — qat'iy kod, drayver matni emas), shuning uchun "yangi snapshot bor, lekin oxirgi urinish muvaffaqiyatsiz" holati ajratib ko'rsatiladi (`failed`).
- **Parallel aniqlash:** ulanish qatori qulflanadi, versiya shu qulf ichida beriladi (ikkita bir xil versiya bo'lmaydi); xizmat darajasida bir ulanishda ikkinchi aniqlash `busy` (409).
- **Saqlash (retention):** eng yangi `CONNECTOR_SNAPSHOT_RETENTION` ta snapshot (standart 10); joriy snapshot hech qachon o'chmaydi. Eskisi bilan birga uning qatorlari va unga olib kelgan o'zgarishlar o'chadi. Ulanish o'chirilsa hammasi (snapshotlar, o'zgarishlar, tanlov) birga ketadi.
- **M4.1b dan o'tish:** `0007_schema_snapshots` migratsiyasi M4.1b da aniqlangan qatorlarni ulanishning **snapshot 1** i qiladi (`fingerprint_version = 0`: barmoq izi keyingi aniqlashda hisoblanadi; sxema bir xil bo'lsa snapshot 1 qoladi, yangi versiya chiqmaydi). Migratsiya qo'shimcha: jadval va ustun qo'shadi, `connection_objects` ning bir noyoblik qoidasini snapshot bo'yicha aniqroq qoidaga almashtiradi; hech qanday ma'lumot yo'qolmaydi. Tavsiflar snapshotga bog'liq emas.

### Barmoq izi (fingerprint)

`schema_model.py` (sof Python; algoritm versiyasi `FINGERPRINT_VERSION = 1`). SHA-256, kiritishi JSON yozuvlar (har satrda bitta, ASCII emas belgilar `\uXXXX` ga o'tkaziladi, shuning uchun baytlar hamma platformada bir xil):

```
["schema-fingerprint", 1, "<provider>"]
["o", schema, object, type]                                   jadval / ko'rinish / materiallashtirilgan ko'rinish
["c", schema, object, column, data_type, nullable]            ustun
["k", schema, object, kind, name, [columns], ref_schema, ref_object, [ref_columns]]   birlamchi / noyob / tashqi kalit
["i", schema, object, name, [columns], unique, primary]       indeks
```

- Turlar shu tartibda (`o`, `c`, `k`, `i`); bir tur ichida yozuvlar **UTF-8 baytlari** bo'yicha tartiblanadi. Ma'lumotlar bazasi tomonida `COLLATE "C"` (bayt tartibi) bilan oqim bo'lib o'qiladi (nomli kursor, xotirada butun sxema yo'q) va `Fingerprinter` tartib buzilsa xato beradi. Shuning uchun tashqi server qanday tartibda sanab bergani muhim emas.
- **Kirmaydi** (ataylab): ustunning tartib raqami; **birlamchi kalit nomi** va uning indeksi nomi (dvigatellar `PK__customer__3213E83FFECE83F9` kabi nom o'ylab topadi); host, ulanish, vaqt. **Kiradi:** provayder, tur va bo'sh bo'lish, kalit ustunlarining tartibi, tashqi kalit nomi va maqsadi, indeks ustunlari va noyobligi.
- Qayta nomlash boshqa tuzilma hisoblanadi (barmoq izi o'zgaradi). Aniq barmoq izi testda qo'lda yozilgan baytlar bilan mustahkamlangan: algoritm sezdirmay o'zgarsa test yiqiladi. Algoritm o'zgarsa `FINGERPRINT_VERSION` oshiriladi va eski snapshot qayta hisoblanadi (hozir shu yo'l 0 → 1 uchun ishlaydi).
- Barmoq izi bazadan oqim bilan hisoblangani sof Python hisobi bilan bir xilligi testda tekshirilgan (haqiqiy PostgreSQL 15/17 va SQL Server 2022 da ham: sxema o'zgartirilib qaytarilganda barmoq izi **birinchisi bilan baytma-bayt bir xil**).

### O'zgarishlarni aniqlash

Ikki snapshot orasidagi farq **SQL to'plam amallari** bilan hisoblanadi (xotirada saqlanmaydi) va `schema_changes` ga yoziladi: `object_type` (`schema`, `table`, `view`, `column`, `constraint`, `relationship`, `index`), `schema_name`, `object_name`, `sub_name` (ustun/kalit/indeks), `change_type` (`added`, `removed`, `modified`), `previous` va `current` (qisqa JSON: masalan ustun uchun `{"data_type", "nullable"}`).

| Nima | Qoida |
|---|---|
| Sxema | yangi sxemada obyekt paydo bo'lsa `schema added`, hammasi yo'qolsa `schema removed` |
| Jadval / ko'rinish | qo'shildi yoki olib tashlandi: **bitta yozuv** (ustunlari va kalitlari alohida yozilmaydi: ular tushuniladi; `current` da ustunlar soni); tur o'zgarsa (jadval → ko'rinish) `modified` |
| Ustun | **ikkala snapshotda mavjud obyektda**: qo'shildi, olib tashlandi; `data_type` yoki `nullable` o'zgarsa `modified`. Tartib raqami e'tiborga olinmaydi |
| Kalit | birlamchi kalit jadvalga bitta (nomi hisobga olinmaydi); noyob va tashqi kalit **nomi** bo'yicha ajratiladi. Tashqi kalit qo'shilsa/olinsa `relationship`, boshqa kalitlar `constraint`. Ustunlar, maqsad jadval yoki maqsad ustunlar o'zgarsa `modified` |
| Indeks | nomi bo'yicha (birlamchi indeks nomi bo'yicha emas); ustunlar yoki noyoblik o'zgarsa `modified` |
| Qayta nomlash | **taxmin qilinmaydi**: olib tashlash + qo'shish |

- Tartib qat'iy: obyekt darajasidagilar oldin (`schema`, `table`, `view`, `relationship`, `constraint`, `column`, `index`), keyin nomlar bo'yicha; shuning uchun ro'yxat takrorlanuvchan.
- Bir snapshot uchun `CONNECTOR_MAX_CHANGES` (5000) dan ko'p bo'lsa **obyekt darajasidagilar saqlanadi, mayda (ustun/indeks) lari qirqiladi** va snapshotda `changes_truncated = true`; umumiy son `changes` da to'g'ri qoladi.
- SQL natijasi sof Python "oracle" bilan 21 xil o'zgarish turi bo'yicha solishtirilgan (`tests/db/test_connector_snapshots.py`).

### Yangilik (freshness) holatlari

`GET …/schema/freshness` (hisob `freshness.py`, sof funksiya). Ustuvorlik tartibida:

| Holat | Ma'nosi |
|---|---|
| `in_progress` | aniqlash hozir ketyapti (boshlanganiga `2 × CONNECTOR_DISCOVERY_TIMEOUT_S + 30` soniyadan kam bo'lsa; yiqilgan jarayon shundan keyin hisobdan chiqadi) |
| `failed` | oxirgi urinish tugamadi; avvalgi snapshot (bo'lsa) saqlangan va o'qiladi; sabab (`last_error`) alohida |
| `never_discovered` | snapshot ham, urinish ham yo'q |
| `stale` | snapshot bor va oxirgi urinish yaxshi, lekin shundayligicha ishonib bo'lmaydi: (a) tanlovga tegadigan o'zgarishlar ko'rib chiqilmagan (`unreviewed_changes`), (b) tanlangan obyekt endi yo'q (`removed_selected`), (c) snapshot `CONNECTOR_SCHEMA_MAX_AGE_S` dan uzoq vaqt tasdiqlanmagan (`age`), (d) ulanish endi snapshot o'qilgan bazadan yoki dvigateldan boshqasiga yo'naltirilgan: `provider` yoki `database_name` o'zgargan (`target_changed`; faqat yangi aniqlash tuzatadi). `host`/`port` o'zgarishi, provayder va baza nomi bir xil qolsa, **aniqlanmaydi**: maqsadni o'zgartirgach sxemani qayta aniqlang |
| `current` | yuqoridagilarning hech biri |

`reasons` hamma amal qiladigan sabablarni beradi (muvaffaqiyatsiz aniqlashda ham ko'rib chiqilmagan o'zgarish bo'lishi mumkin). Javobda `snapshot` (id, versiya, barmoq izi, `verified_at`), `confirmed_version`, `unreviewed_changes`, `affected_selected`, `removed_selected`, `orphaned_descriptions` bor.

- **Ko'rib chiqilmagan o'zgarish** = `context_confirmed_version` dan keyingi snapshotlardagi o'zgarishlar. Faqat **tanlovga tegadiganlari** `stale` qiladi: hech narsa tanlanmagan jadvalga o'zgarish yoki yangi jadval holatni buzmaydi. Tanlangan jadvalning yangi ustuni, o'zgargan turi, olib tashlangan kaliti — tegadi; qisman tanlangan jadvalda tanlanmagan ustun tegmaydi, lekin kalit ustunlari tegadi.
- **Tasdiqlash:** (1) `POST …/schema/changes/acknowledge {version}` (faqat joriy versiya; boshqasi `snapshot_changed`), (2) tanlovni saqlash ham joriy versiyaga tasdiqlaydi. `removed_selected` va `age` tasdiqlash bilan yo'qolmaydi: elementni olib tashlash yoki qayta aniqlash kerak.
- Aniqlash o'zi avtomatik ishlamaydi (jadval/rejalashtirgich yo'q): yangilik faqat foydalanuvchi aniqlagan paytga tayanadi, shuning uchun `age` "oxirgi tasdiq eski" degani, "bazaning o'zi o'zgardi" degani emas.

### Ochiq obyektlarni tanlash

`GET/PUT …/schema/selections`. Tanlov — elementlar ro'yxati (`kind` = `schema` | `table` | `column`; `schema`, `object`, `column`; `mode` = `include` | `exclude`; sxema elementida `exclude` yo'q). **Standart: hech narsa tanlanmagan** — AI kontekstida hech narsa yo'q.

**Meros qoidalari (to'liq):**
1. Jadval **butunlay kiritilgan**: o'zining `include` elementi bor, **yoki** sxemasida element bor va jadvalning `exclude` elementi yo'q. Ustunlari — hammasi, `exclude` qilinganlardan tashqari.
2. Jadvalga `include` ham, sxema elementi ham yo'q, lekin ustun `include` elementlari bor — jadval **qisman**: aynan shu ustunlar (kontekst yana kalit ustunlarini `implicit: true` bilan qo'shadi, shunda kalitlar va bog'lanishlar mazmunli bo'ladi).
3. Qolgani tanlanmagan. Jadval `exclude` si faqat tanlangan sxema ostida ma'noga ega.
4. **Keyin paydo bo'lgan obyektlar avtomatik tanlanmaydi**, faqat sxema elementi ostida (u "hozir va keyin" demak) tushadi. Jadvalni tanlash sxemasini tanlamaydi.

**Rad etiladigan kombinatsiyalar** (`invalid_selection`, 400, `reason` bilan; ikki ma'no berishdan ko'ra rad etiladi): bir element ikki marta (`duplicate_item`); sxemasi tanlanmagan jadval `exclude` si (`exclude_without_scope`); butunlay kiritilmagan jadvalning ustun `exclude` si (`exclude_without_table`); butunlay kiritilgan jadvalning ustun `include` i (`column_include_in_included_table`); `exclude` qilingan jadvalning ustun `include` i (`column_include_in_excluded_table`); noto'g'ri tur, rejim, nom (1–300 belgi, NUL yo'q), qo'shimcha maydon, `CONNECTOR_MAX_SELECTION_ITEMS` dan ko'p element.

- **Yangi element joriy snapshotda mavjud bo'lishi shart** (`object_not_found`, `reason = not_in_snapshot`); **saqlangan** element obyekt yo'qolsa ham o'chmaydi: `available: false` bilan ko'rsatiladi (`unavailable` soni), obyekt qaytsa yana ishlaydi. Qayta aniqlash tanlovni o'zgartirmaydi.
- **Bir vaqtda tahrir:** `PUT` da `base_revision` berilsa va tanlov o'zgargan bo'lsa `selection_conflict` (409), hech narsa yozilmaydi. Almashtirish bitta tranzaksiyada.
- Saqlash xavfsizlik jurnaliga **avval** yoziladi (`schema_selection_changed`), jurnalga yozib bo'lmasa o'zgartirish qilinmaydi (503).

### Tavsiflar

Tavsif aniqlangan metama'lumotdan **alohida**, **nom bo'yicha** saqlanadi. Obyekt yo'qolsa tavsif **yetim** (`orphaned: true`) bo'ladi: o'chmaydi, kontekstga **kirmaydi**, `freshness.orphaned_descriptions` da sanaladi va obyekt qaytsa o'zi tiklanadi. Yangi tavsif faqat mavjud obyektga yoziladi. **Sir ko'rinishidagi matn rad etiladi** (`description_contains_secret`, 400): `scheme://user:parol@…` ko'rinishidagi havola, `password|passwd|pwd|secret|token|api_key = …` va ulanishning o'z paroli (≥ 6 belgi). Bu evristika, DLP emas.

### Sxema konteksti (AI uchun)

`GET …/schema/context?max_chars=&allow_stale=` (`context.py`, sof funksiya: modelni chaqirmaydi, qator o'qimaydi, hisob ma'lumotiga ega emas). Javob (`format: 1`):

- `status`: `ok` | `empty` (hech narsa tanlanmagan) | `budget_too_small` | `stale` | `failed` | `in_progress` | `never_discovered`; `usable` **faqat `ok` da** `true`. `complete`: hech narsa chegara tufayli tashlanmagan.
- `provider`, `snapshot` (versiya, barmoq izi, `verified_at`), `freshness`, `selection` (`revision`, tanlangan va kiritilgan obyektlar soni, mavjud bo'lmagan elementlar), `budget` (`max_chars`, `used_chars`, `context_fingerprint` = JSON baytlarining SHA-256 i).
- `context`: `database` (`name`, tavsif), `schemas[]` (`name`, tavsif, `objects[]`), `omitted`. Obyekt: `name`, `type`, `columns[]` (`name`, `type`, `nullable`, `pk`, `fk`, `implicit`, tavsif), `primary_key`, `unique`, `foreign_keys[]` (`name`, `columns`, `references`), tavsif.
- **Nima kiradi:** faqat tanlangan jadval/ko'rinishlar va ularning tanlangan ustunlari; kalit **faqat barcha ustunlari ko'rinsa**; tashqi kalit — maqsad ham kiritilgan va uning ustunlari ko'rinsa. Tanlanmagan obyektga tashqi kalit **ko'rsatilmaydi va nomi oshkor qilinmaydi**, faqat sanaladi (`omitted.relationships_hidden`). Tavsiflar faqat ko'rinadigan obyektlar uchun va yetim emasi. **Hech qachon:** qator, standart qiymat, ulanish satri, host, foydalanuvchi, parol.
- **Deterministik:** hamma ro'yxat aniq tartibda, JSON kalitlar tartiblangan va ajratgichlar qat'iy; bir xil snapshot + tanlov + chegara = bir xil baytlar (`context_fingerprint`).
- **O'lcham siyosati:** `max_chars` (standart `CONNECTOR_CONTEXT_MAX_CHARS`, eng ko'pi `CONNECTOR_CONTEXT_HARD_MAX_CHARS`, kamida 200) `context` ning JSON uzunligini cheklaydi (konvert hisobga olinmaydi). **Obyekt hech qachon kesilmaydi**: to'liq kiradi yoki `omitted` ga tushadi. Tartib: tanlangan boshqa obyektlar bilan **bog'lanishlar soni ko'p bo'lgani oldin** (hub lar va bog'langan jadvallar birga qoladi), tengida sxema va nom bayt tartibida. Sig'maganini o'tkazib, keyingisini sinaydi; qolgan joy eng kichik ko'rilgan obyektdan kam bo'lsa to'xtaydi. Hammasiga `omitted` ro'yxati ham sig'masa eng past ustuvorlar tashlanadi, keyin `omitted` ro'yxati qisqartiriladi (soni saqlanadi). Tanlangan, lekin birorta obyekt sig'masa `budget_too_small`.
- **Eskirgan kontekst:** `current` bo'lmasa standart javob `context_stale` (409, `reason` = holat). `allow_stale=true` ko'rish uchun beradi (`status` shu holat, `usable: false`): kontekstni ishlatadigan kod `usable` ni tekshirishi shart. O'chirilgan ulanishga `connection_disabled` (409).

### Sahifadagi o'zgarishlar (M4.2)

**Ulanishlar → Sxemani ko'rish** oynasida: yangilik paneli (holat, sabab, versiya, barmoq izining boshi, tasdiq vaqti, oxirgi xato kodi, ko'rib chiqilmagan o'zgarishlar, **Ko'rib chiqdim**); **O'zgarishlarni ko'rish** (ko'rib chiqilmaganlar ro'yxati); sxema, jadval va ustun uchun **katakchalar** (qisman tanlangan jadval — "aniqlanmagan" belgi; **Tanlovni saqlash** `base_revision` bilan; mavjud bo'lmagan elementlar ro'yxati va "Olib tashlash"); yetim tavsiflar ro'yxati; **AI konteksti ko'rinishi** — backend JSON ini matn sifatida ko'rsatadi (`textContent`; sahifa kontekstni o'zi qurmaydi), holat, `usable` va o'lcham. Aniqlash xabari endi yangi versiya va o'zgarishlar sonini aytadi.

### M4.2 ma'lum cheklovlari

- Aniqlash qo'lda (rejalashtirgich yo'q); `stale` "oxirgi tasdiq eski" yoki "ko'rilgan o'zgarish tanlovga tegadi" degani, bazaning hozirgi holatini bilishni anglatmaydi.
- O'lcham siyosatida erta to'xtash: qolgan joy kichik obyektdan kam bo'lsa, keyingi, yanada kichik obyekt tashlanib qolishi mumkin; `complete: false` buni ko'rsatadi.
- Qisman tanlangan jadvalning kalit ustuni o'zgarishi **joriy** kalitlarga qarab aniqlanadi (olib tashlangan kalit ustuni bu hisobga kirmasligi mumkin).
- Yiqilgan aniqlash `in_progress` ni lease muddati (`2 × timeout + 30` s) davomida ushlab turadi.
- Ko'p o'zgarishda mayda o'zgarishlar qirqiladi (`changes_truncated`); saqlash chegarasidan eski snapshotlar va ularning o'zgarishlari o'chadi.
- Tana chegarasi (`rag/connector/bodylimit.py`, ASGI middleware): `PUT …/schema/selections` ≤ 8 MB (faqat `Authorization: Bearer` bilan; tokensiz 256 KB), boshqa ulagich marshrutlari ≤ 256 KB, `/api/v1/session/…` ≤ 16 KB. Chegara FastAPI tanani o'qishidan **oldin** va **o'qilayotganda** qo'llanadi: e'lon qilingan `Content-Length` chegaradan katta bo'lsa tana o'qilmaydi (413), noto'g'ri qiymat 400 (`invalid_content_length`), `Content-Length` yo'q (chunked) yoki yolg'on bo'lsa kelgan baytlar sanaladi va chegaradan o'tganda 413; ko'pi bilan chegara hajmicha bayt xotirada turadi. (Avval chegara FastAPI tanani o'qigandan keyin ishlaydigan bog'liqlikda edi: bu tuzatildi.)
- Indeks ifodalari, qismiy indekslar sharti, tur izohlari, standart qiymatlar, triggerlar, protseduralar saqlanmaydi va kuzatilmaydi.
- Sir rad etish evristikasi (yuqorida) hamma sirni ushlamaydi; tavsif va metama'lumot ichki bazada shifrlanmagan.
- Hech qanday embedding va qidiruv indeksi yo'q; kontekst har so'rovda snapshotdan quriladi.

## Xavfsizlik qarorlari

- **O'qish:** PostgreSQL seansi `default_transaction_read_only=on` va `READ ONLY` tranzaksiya bilan ochiladi (to'liq huquqli hisobda ham CREATE/INSERT/UPDATE/DELETE/DROP/TRUNCATE/CREATE ROLE rad etilishi haqiqiy serverda tekshirilgan). SQL Server da seans darajasida yo'q (yuqoriga qarang). Ikkala drayver ham faqat o'zining qat'iy matnli `SELECT` larini yuboradi; foydalanuvchi SQL i yo'q (bu bosqichda umuman yo'q).
- **Ulanish satri in'ektsiyasi:** host, port, baza, foydalanuvchi, parol drayverga **alohida parametr** sifatida beriladi, hech qachon satrga biriktirilmaydi; `options` yopiq to'plam; host qat'iy sintaksis (harf, raqam, nuqta, defis, pastki chiziq; IPv6 literal). `x sslmode=disable host=evil` kabi qiymat shunchaki g'alati baza nomi (testlangan).
- **SSRF siyosati (qaror):** *har doim bloklanadi*: aniqlanmagan manzil (0.0.0.0, ::), link-local (169.254.0.0/16, bulutli metama'lumot xizmatlari shu yerda, va fe80::/10), multicast, zaxiralangan va broadcast; IPv4-mapped IPv6 ichidagi IPv4 bo'yicha; nom bo'lsa **hamma** aniqlangan manzillar baholanadi (bittasi bloklangan bo'lsa butun nom bloklanadi). *Har doim bloklanadi:* ilovaning o'z bazasi (shu manzil, port va baza nomi; yoki ilovaning o'z rollari). *Ruxsat:* loopback va xususiy tarmoqlar (10/8, 172.16/12, 192.168/16, fc00::/7). Sabab: ilova mahalliy; foydalanuvchilar boshqa loyihalarining bazalarini shu kompyuterda yoki ofis tarmog'ida saqlaydi, "xususiy manzil yo'q" qoidasi ulagichni foydasiz qilardi. *Ixtiyoriy:* `RAG_CONNECTOR_ALLOWED_HOSTS` (nomlar yoki `*.suffix`) bo'lsa boshqasiga ulanib bo'lmaydi. Baholangan manzil bilan ulanadi (PostgreSQL `hostaddr`, SQL Server ochiq soket), shuning uchun DNS rebinding maqsadni o'zgartira olmaydi; manzil har sinov va aniqlashda qayta baholanadi. IPv4 manzillar avval, vaqt chegarasi manzillar orasida bo'linadi.
- **Egalik:** har repository funksiyasi `owner_principal_id` bo'yicha filtrlaydi (AST testi tekshiradi); egasi hech qachon so'rovdan olinmaydi; boshqa egasining ulanishi "mavjud emas" (404) bilan bir xil.
- **Xatolar:** drayver xabari hech qachon chiqmaydi (u ulanish satri, foydalanuvchi, serverning "Login failed for user …" matnini o'z ichiga olishi mumkin); u qat'iy kodga aylantiriladi. `python-tds` jurnali (u xatolarni matni bilan yozadi) o'chirilgan va ildiz jurnalidan uzilgan: testda tekshirilgan.
- **Resurs:** sinov va aniqlash vaqt bilan cheklangan; bir vaqtda `CONNECTOR_MAX_CONCURRENT` ta tashqi amal (ortig'iga 429 `busy`), bir ulanishda ikkita amal bir vaqtda emas (409 `busy`); o'lchamga chegaralar; sahifalash ≤500; o'chirilgan ulanishga hech narsa yuborilmaydi.
- **Tana hajmi:** yuqoridagi "Ma'lum cheklovlar" dagi tana chegarasi ro'yxati; ulagich marshrutlari tanani oqim paytida sanaydi (chunked va yolg'on `Content-Length` ham o'tmaydi). Yuklash (`/api/documents`) chegarasi (`rag/bodylimit.py`, [upload-limits.md](upload-limits.md)) o'zgarmagan.
- **Raqam va matn qiymatlari:** so'rovdagi son int32 oralig'idan tashqarida bo'lsa 422 (qiymatsiz, faqat maydon nomi); ma'lumotlar bazasi rad etgan qiymat (masalan nomda NUL bayt) ulagich yo'llarida 400 `invalid_request`, hech qachon 500 emas.
- **Qisqa o'zgartirishlar va aniqlash:** aniqlash ketayotganda tavsif, tanlov va tasdiqlash o'zgartirishi, tahrirlash, yoqish/o'chirish va o'chirish darhol `busy` (409) oladi; boshqa jarayon ulanish qatorini ushlab tursa bazada ko'pi bilan 5 soniya kutiladi (`lock_timeout`), keyin `busy`: pul ulanishi aniqlash tugashini kutib band bo'lmaydi.
- **Xavfsizlik jurnali:** `connection_created/updated/deleted/enabled/disabled/tested`, `schema_discovered`, `schema_selection_changed`, `schema_changes_acknowledged`, `key_created`, `key_rewrapped` (id va qat'iy so'zlar; parol, kalit, host, matn yo'q). Mavjud ulanishni o'zgartirish, yoqish/o'chirish, o'chirish **avval** yoziladi (tanlovni saqlash va o'zgarishni tasdiqlash ham) va yozib bo'lmasa **qilinmaydi** (503 `security_log_unavailable`); yangi ulanish yaratilib, yozilmasa qaytarib olinadi.

**Ma'lum cheklovlar (ochiq aytilgan):**
- Foydalanuvchi bir egasi bilan ishlaydi (`local-owner`); ko'p foydalanuvchi va ro'yxatdan o'tish yo'q.
- Sessiya tokenini o'g'irlagan kishi, step-up siz, ulanishlarni ko'ra oladi, sinay oladi, aniqlay oladi, tavsif yoza oladi, o'chira oladi (parolni olib bo'lmaydi va maqsadni o'zgartirib bo'lmaydi).
- Xususiy tarmoq manzillari ruxsat (yuqorida: qaror). Kerak bo'lsa `RAG_CONNECTOR_ALLOWED_HOSTS` bilan toraytiring.
- Ulanish sinovi xato kodlari (`unreachable`, `timeout`, `auth_failed`, `tls_failed`) ruxsat etilgan manzillardagi portlarning ochiq-yopiqligini ko'rsatadi, ya'ni sessiyaga ega kishi ulagichni port skaneri sifatida ishlata oladi. Himoya: sessiya, bir vaqtdagi amallar chegarasi, bloklangan manzillar va `RAG_CONNECTOR_ALLOWED_HOSTS`.
- SQL Server: yozishdan himoya faqat hisob huquqlari; TLS faqat CA bilan tekshirilgan rejimda (drayver chegarasi); `python-tds` hamma TDS xususiyatini qo'llamaydi (masalan Windows integrated auth, Azure AD yo'q: faqat SQL login).
- Tizim ildiz sertifikatlari bilan tekshirish PostgreSQL da (`sslrootcert=system`) libpq versiyasiga bog'liq.
- Ulanish parolini mijoz xotirasiga qo'yish: parol brauzerda faqat formada yoziladi, yuborilgach tozalanadi; formada yozilgan paytda sahifadagi skript uni ko'ra oladi (CSP `default-src 'self'` bilan cheklangan).
- Metama'lumot (jadval va ustun nomlari, tavsiflar) ichki bazada **shifrlanmagan** saqlanadi.

## Muammolarni hal qilish

| Kod | Ma'nosi | Nima qilish |
|---|---|---|
| `key_unavailable` (`key_missing`) | shifrlash kaliti yo'q | `python -m rag connector key-init` |
| `needs_credentials` | kalit bor, lekin shifrmatn ochilmaydi (boshqa kalit, tiklash) | parolni qayta kiriting (tahrirlash); yoki to'g'ri kalitni qaytaring |
| `crypto_unavailable`, `driver_missing` | ixtiyoriy kutubxona yo'q | `pip install -r requirements-connector.txt` |
| `auth_failed` | foydalanuvchi yoki parol noto'g'ri; PostgreSQL da `pg_hba.conf` rad etgan bo'lishi mumkin | hisobni tekshiring |
| `database_not_found` | baza yo'q yoki hisobga ochiq emas | baza nomini tekshiring |
| `timeout`, `unreachable` | javob yo'q / ulanib bo'lmadi | host, port, xavfsizlik devori; Windows da yopiq mahalliy port to'liq vaqt chegarasini kutishi mumkin |
| `tls_failed` | shifrlash yoki sertifikat | CA to'g'riligi, host nomi sertifikatdagi nomga mosligi (`verify-full`); server TLS ni yoqqanmi |
| `tls_required` | boshqa kompyuter uchun zaif rejim | `verify-full` + CA, yoki `accept_insecure_tls` bilan xavfni qabul qiling |
| `ca_required` | SQL Server TLS uchun CA kerak | serverning CA sertifikatini (PEM) kiriting |
| `host_blocked`, `host_not_allowed`, `host_unresolvable`, `internal_database` | manzil siyosati (yuqorida) | boshqa manzil; yoki ruxsat ro'yxatini tekshiring |
| `permission_denied` | metama'lumotga ruxsat yo'q | hisobga huquq bering |
| `too_large` | sxema chegaradan katta | `RAG_CONNECTOR_MAX_OBJECTS/COLUMNS` yoki hisob ko'rishini toraytiring |
| `security_log_unavailable` | jurnalga yozib bo'lmadi | jurnal katalogi va diskni tekshiring; o'zgartirish qilinmagan |
| `context_stale` | kontekst `current` emas (`reason`: `stale`, `failed`, `in_progress`, `never_discovered`) | sxemani qayta aniqlang, o'zgarishlarni ko'rib chiqing; ko'rish uchun `allow_stale=true` |
| `selection_conflict` | tanlov boshqa joyda o'zgartirilgan | sahifani yangilang va qayta saqlang |
| `snapshot_changed` | tasdiqlanayotgan versiya endi joriy emas | yangilang |
| `invalid_selection` | tanlov qoidasi buzilgan (`reason`: `exclude_without_scope`, `exclude_without_table`, `column_include_in_included_table`, `duplicate_item`, …) | tanlovni tuzating |
| `description_contains_secret` | tavsifda parol yoki kalit bor | matnni o'zgartiring |
| `busy` | band (aniqlash yoki sinov ketyapti) | biroz kutib qayta urining |
| `request_too_large` (413), `invalid_content_length` (400) | tana chegaradan katta yoki `Content-Length` noto'g'ri | tanani kichraytiring |
| `invalid_request` (400/422) | qiymat yaroqsiz (diapazondan tashqari son, NUL bayt, noma'lum maydon) | so'rovni tuzating |
| `step_up_required` | yangi kod kerak | **Ulanishlar** bo'limidagi tasdiqlash oynasi |

## Qurilmagan (keyingi bosqichlar)

SQL **tekshiruvi va bajarish** (M4.4 va keyin; M4.3 faqat taklif yaratadi: [sql-generation.md](sql-generation.md)), SQL tahlili, natija keshi, gibrid RAG, ma'lumotni ichki bazaga yoki RAG ga kiritish, sxema uchun embedding, so'rov auditi, biznes lug'ati, **avtomatik** kalit aylantirish, ko'p foydalanuvchi, umumiy autentifikatsiya (Faza 3). Bular M4.4 va keyingi bosqichlar; bu hujjat ularni bajarilgan deb belgilamaydi. Sxema konteksti (M4.2) faqat M4.3 savoldan SQL taklifi uchun modelga beriladi; hech qanday SQL bajarilmaydi.
