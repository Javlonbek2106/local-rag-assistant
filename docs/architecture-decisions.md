# Arxitektura qarorlari

Manba: [architecture-audit.md](architecture-audit.md). Rejalashtirish: [architecture-roadmap.md](architecture-roadmap.md).

Format: har qaror uchun **Holat**, **Kontekst/dalil**, **Qaror**, **Oqibat**. Dalil belgilari auditdagidek: [T] tasdiqlangan, [O] oldingi o'lchov, [X] taxmin.

## 1. Mavjud qarorlar (qabul qilingan, saqlanadi)

| ID | Qaror | Dalil | Oqibat |
|---|---|---|---|
| ADR-001 | **Modulli monolit** + alohida, nazorat ostidagi worker jarayoni; mikroservis yo'q | Tsiklsiz qatlamlar [T]; to'siqlar tashqi xizmatlarda (Ollama, Qdrant) [T] | Oddiy joylashtirish. Qayta ko'rish sharti: o'lchangan va monolit ichida hal bo'lmaydigan to'siq |
| ADR-002 | **PostgreSQL manba, Qdrant hosila** | Faollashtirish bitta tranzaksiya; qidiruv faol id lar bilan filtrlanadi [T] | Qdrant qayta qurilishi kerak, lekin hozir buning vositasi yo'q (QDR-01/02) |
| ADR-003 | **Versiya qatori o'zi ish (job)** (alohida `jobs` jadvalisiz) | Navbat/heartbeat/lease/qayta urinish versiyada, 129 DB test [T] | Hujjat holati — hosila xulosa (`_sync_document`) |
| ADR-004 | **Faollashtirish avlodi** (`activation_generation`/`uploaded_generation`) + **qat'iy lease** | Tasodifiy interleaving, 30 raund ikki rollback, mutatsiya tekshiruvi [T] | Rollback doimiy qaror; eskirgan nomzod `SUPERSEDED` bo'lib saqlanadi (isrof: oxirigacha ishlanadi) |
| ADR-005 | **Deterministik nuqta id** `uuid5(versiya:indeks)` + faollashtirishdan oldin `verify` + DB da tekshirilgan son | Qayta urinish dublikat bermaydi [T] | Qisman bo'laklardan davom etish mumkin (T1.2) |
| ADR-006 | **Qidiruv faol versiya id lari bilan filtrlanadi, xavfsiz yopiladi** | `answer.py:49-54` [T]; 10 000 id da +8 ms [T] | Hajm hujjat soniga chiziqli (RET-02) |
| ADR-007 | **Faqat loopback, autentifikatsiyasiz** (vaqtinchalik, 3-bosqichgacha) | `HOST` qat'iy, Host/Origin tekshiruvi [T] | Faqat yakka foydalanuvchi; ko'p foydalanuvchida Critical (SEC-01/02) |
| ADR-008 | **Prompt asosidagi himoya + manba ko'rsatish**; modelda vositalar yo'q, UI faqat `textContent` | XSS yo'q [T]; injection 3/4 bajarilmadi [T] | Ta'sir javob mazmuni bilan cheklangan; to'liq himoya emas (LLM-04) |
| ADR-009 | **512 tokenli bo'laklar, "triplet" jadval, gibrid va reranker yo'q** | 256 token 74% < 80%; Markdown jadval, BM25: foyda yo'q; reranker: javob 80→78%, +10 s [O] | Dalilsiz o'zgartirilmaydi; generator o'zgarsa qayta baholash |
| ADR-010 | **Docling + Tesseract worker jarayonida**, Excel uchun o'z o'quvchi | Excel javobi 50% → 100% [O] | Og'ir kutubxonalar faqat worker/`ingest` da (lazy) |
| ADR-011 | **Faqat qo'shimcha SQL migratsiyalar** (advisory lock, bir marta) | Jonli bazada 3 migratsiya xavfsiz qo'llandi [T] | Halokatli o'zgarishlar alohida tasdiq bilan (DOC-07) |
| ADR-012 | **Konfiguratsiya `RAG_*` muhit o'zgaruvchilari, `config.NAME` orqali** | 70 qator, testlar `monkeypatch` qiladi [T] | Import paytida o'qiladi, tekshiruvsiz (ARC-05) |
| ADR-013 | **Vanilla JS UI**, qayta yuklanmaydigan polling, `textContent`, qat'iy CSP | e2e xavfsizlik testi [T] | A11y va yangilanish narxi qarzi (API-04/05, DB-01) |
| ADR-014 | **Baholash ishlab chiqarish kodini o'lchaydi**; ma'lumotlar git dan tashqarida | `eval` `answer/ingest/store/llm` ni ishlatadi [T] | Manifest yo'q (TST-02) |

## 2. Qayta ko'rib chiqilishi kerak bo'lgan qarorlar

| ID | Qaror | Muammo | Taklif |
|---|---|---|---|
| RC-1 | **Qat'iy 30 daqiqalik job timeout** (`config.py:23`) | Katta jadvalli hujjat indekslanmaydi (ING-01) | ADR-P1 |
| RC-2 | **LLM chaqiruvi sinxron, timeoutsiz, bekor qilinmaydi, Ollama ketma-ket** | Osilish, navbat, bekor qilinmaslik (LLM-01/02/03) | ADR-P2 |
| RC-3 | **Sahifalanmagan ro'yxat va polling** | 10 000 hujjatda 3,4 MiB (DB-01) | ADR-P5 |
| RC-4 | **Uch ombor, moslashtirishsiz** | Jim nomuvofiqlik (DOC-03) | ADR-P4 |
| RC-5 | **Global noyob hujjat nomi** | Ko'p foydalanuvchida to'qnashuv/sizish (DOC-06) | ADR-P8 |
| RC-6 | **Embedding identifikatsiyasi saqlanmaydi** | Jim vektor aralashishi (QDR-01) | ADR-P3 |
| RC-7 | **Yuklash Starlette orqali, limit tanadan keyin** | Disk to'ldirish (API-02) | ADR-P10 |
| RC-8 | **Versiyalar abadiy saqlanadi** | Chegarasiz o'sish (DOC-02) | ADR-P7 |
| RC-9 | **Himoya faqat prompt** | Injection (LLM-04) | ADR-P9 |
| RC-10 | **Baholash yaxlitligi va quvvati** | n=69 (±9,4 pp), manifest yo'q (TST-02) | ADR-P11 |

## 3. Taklif etilgan ADR lar (tasdiq kerak)

Hammasi **Taklif** holatida; hech biri amalga oshirilmagan.

### ADR-P1: Job timeout taraqqiyotga asoslanadi
- **Kontekst**: 381 betlik byulleten 49–60 daqiqa [O], chegara 30 daqiqa; hujjat hajmiga qaramay `FAILED` va worker qayta ishga tushadi (ING-01).
- **Qaror (taklif)**: mutlaq timeout o'rniga **taraqqiyot bo'lmagan** vaqt chegarasi (masalan, 10 daqiqa); worker bosqich/sahifa taraqqiyotini DB ga yozadi; yuqori mutlaq chegara (masalan, hajmga qarab) faqat himoya sifatida.
- **Alternativlar**: (a) mutlaq chegarani oshirish (qotgan parserni uzoq kutadi); (b) sahifa × byudjet (sahifa soni oldindan ma'lum bo'lmasa noaniq).
- **Oqibat**: taraqqiyot maydoni va UI ko'rsatkichi; qotgan parser baribir tezroq aniqlanadi. Qabul: T0.3.
- **Dalil kerak**: LT-2 (sahifa/s turli hujjatlar uchun).

### ADR-P2: LLM shlyuzi (navbat, timeout, bekor qilish, isitish)
- **Kontekst**: Ollama so'rovlarni ketma-ket bajaradi, uzilgan mijoz generatsiyani to'xtatmaydi, timeout yo'q, sovuq start 28 s [T, E3].
- **Qaror (taklif)**: `llm` modulida chegaralangan navbat (band bo'lsa aniq javob), so'rov timeouti, `num_predict`, `keep_alive`, mijoz uzilganda oqimni yopish; bir vaqtda 1 generatsiya (Ollama xususiyatiga mos).
- **Alternativlar**: parallel Ollama nusxalari (RAM/CPU ni bo'lishadi, tezlashuv o'lchanmagan [X]); alohida GPU xizmati (o'lchangan asos paydo bo'lganda).
- **Oqibat**: ko'p foydalanuvchida kutish aniq va cheklangan. Qabul: T0.4, T3.5 (LT-1).
- **Dalil kerak**: Ollama oqimi yopilganda generatsiya to'xtashini alohida tekshirish [X].

### ADR-P3: Indeks metama'lumoti va qayta indekslash
- **Kontekst**: embedding modeli o'zgarsa vektorlar jimgina aralashadi; qayta qurish buyrug'i yo'q (QDR-01/02).
- **Qaror (taklif)**: `index_meta` (model, o'lcham, tokenizer, bo'laklash parametrlari, kolleksiya nomi); versiyada `embedded_with`; ishga tushishda nomuvofiqlikni rad etish; `rag reindex` yangi kolleksiyani PG+fayllardan quradi, `verify` qiladi, so'ng atomik almashtiradi (`COLLECTION` o'zgarishi). Faollashtirish yo'li o'zgarmaydi.
- **Alternativlar**: kolleksiya nomiga model nomini kiritish (aralashishni oldini oladi, lekin qayta qurishni hal qilmaydi).
- **Oqibat**: modelni xavfsiz almashtirish mumkin. Qabul: T1.1.

### ADR-P4: Moslashtirish (reconcile) va sog'liq
- **Kontekst**: `READY` versiya vektori yo'qolsa aniqlanmaydi, yetim fayl/nuqta sanalmaydi (DOC-03), kuzatuvchanlik minimal (WRK-02).
- **Qaror (taklif)**: faqat o'qiydigan `reconcile` (versiya vektor soni, fayl mavjudligi/hashi, yetimlar); `/healthz`, `/readyz`, `/api/status`; tuzatish alohida buyruqlar bilan, tasdiq bilan.
- **Oqibat**: jim nomuvofiqlik ko'rinadi; zaxira/tiklash mashqining tekshiruv vositasi. Qabul: T0.2.

### ADR-P5: API v1, xato konverti, sahifalash
- **Kontekst**: versiyasiz yo'llar, uch xil xato shakli, sahifalanmagan ro'yxat (API-01, ARC-06, DB-01).
- **Qaror (taklif)**: `/api/v1/...`; xato `{error:{code,message}}`; ro'yxatda `limit/cursor`, `q`, `If-None-Match`; eski yo'llar o'tish davrida saqlanadi; OpenAPI hujjati ixtiyoriy yoqiladi (autentifikatsiya joriy bo'lguncha o'chiq qoladi).
- **Oqibat**: tashqi mijozlar (.NET) barqaror shartnomaga ega; UI yuki kamayadi. Qabul: T1.5.

### ADR-P6: Suhbat modeli
- **Kontekst**: funksiya mavjud emas (CNV-01); vazifa matnida bor deb ko'rsatilgan.
- **Qaror (taklif, funksiya kerak bo'lsagina)**: PostgreSQL da `conversations(id, owner_id nullable, created_at)` va `messages(id, conversation_id, seq, role, content, sources, created_at)`; `seq` suhbat qulfi ostida beriladi (tartib kafolati); retrieval hujjat holatiga tegmaydi (`active_version_ids` o'zgarishsiz); tarix oynasi token byudjeti bilan; ixtiyoriy savolni qayta yozish (qo'shimcha LLM chaqiruvi: avval foydasini baholash).
- **Alternativlar**: brauzerda saqlash (izolyatsiya va persistentlik yo'q).
- **Oqibat**: `owner_id` 3-bosqich bilan; LLM navbati (ADR-P2) hal bo'lishi kerak. Qabul: T2.5.
- **Dalil kerak**: baholash to'plamida davomli savollarda yaxshilanish.

### ADR-P7: Versiyalarni saqlash siyosati
- **Kontekst**: `SUPERSEDED`/`FAILED` versiyalar abadiy (DOC-02).
- **Qaror (taklif)**: `rag prune`: har hujjat uchun oxirgi N (standart 3) `SUPERSEDED` saqlanadi, qolganining fayli va vektorlari o'chiriladi (qator va tarix saqlanadi, holat `PURGED` emas, balki `chunk_count` va fayl yo'li bo'shatiladi: **ochiq savol**); avtomatik emas.
- **Oqibat**: rollback kafolati N ichida saqlanadi; N dan tashqariga qaytish rad etiladi (aniq xabar). Qabul: T1.6.
- **Ochiq savol**: tarixiy qatorni saqlash/saqlamaslik, N ni sozlash.

### ADR-P8: Ko'p foydalanuvchi ma'lumotlar modeli va autentifikatsiya
- **Kontekst**: egasi tushunchasi yo'q, global nom, autentifikatsiya yo'q (SEC-01/02, DOC-06).
- **Qaror (taklif)**: `documents.owner_id`; `(owner_id, name)` noyob; nom NFC ga normallashtiriladi; `active_version_ids(owner)` va Qdrant filtri egasi bo'yicha (fail-closed saqlanadi); ruxsat modeli (egasi, ko'rish) alohida tasdiq bilan; sessiya/token.
- **Alternativlar**: har foydalanuvchiga alohida kolleksiya (izolyatsiya kuchli, lekin ko'p kolleksiya va qayta indekslash narxi [X]).
- **Oqibat**: 0004+ migratsiya (faqat qo'shimcha; mavjud hujjatlar bitta "merosxo'r" egaga). Qabul: T3.1, T3.2.

### ADR-P9: Prompt-injection chuqurlashtirilgan himoya
- **Kontekst**: faqat prompt; 1/4 bajarildi [T]; fayl nomi ham promptda (LLM-04).
- **Qaror (taklif)**: hujjat bloklarini aniq ajratuvchilar bilan belgilash va "ma'lumot, ko'rsatma emas" qoidasi; fayl nomini tozalash/qisqartirish; chiqish tekshiruvi (mavjud manba raqamlari); kengaytirilgan injection to'plami; modelga vosita bermaslik (hozirgi holat saqlanadi).
- **Oqibat**: xavf kamayadi, yo'qolmaydi; hech qachon "yechildi" deb belgilanmaydi. Qabul: T1.9.

### ADR-P10: Yuklashni qayta ishlash
- **Kontekst**: limit tana to'liq spool qilingandan keyin tekshiriladi (API-02).
- **Qaror (taklif)**: 1-bosqich `Content-Length` bo'yicha oldindan rad etish; 2-bosqich: oqimli ko'p qismli tahlil (chegaraga yetganda uzish); sinxron qismni threadpool ga (API-03).
- **Oqibat**: disk to'ldirish yo'li yopiladi. Qabul: T0.5, T1.3.

### ADR-P11: Baholash boshqaruvi
- **Kontekst**: n=69 (±9,4 pp), n=9 rad etish, n=4 injection; manifest yo'q (TST-02).
- **Qaror (taklif)**: baholash ma'lumotlari manifesti (SHA-256) va `eval.run` boshida tekshiruv; kengaytirilgan to'plam (≥200) mavjudini o'zgartirmasdan; hisobotda 95% oraliq; qarorlar juft taqqoslash bilan; CI da kechasi/qo'lda.
- **Oqibat**: sifat qarorlari statistik himoyalangan. Qabul: T1.8.

### Faza 4: universal ma'lumotlar bazasi ulagichi (ADR-P12…P16)

Reja: [architecture-roadmap.md, 6a-bo'lim](architecture-roadmap.md). Hammasi **Taklif**; **hozir hech narsa amalga oshirilmaydi** va Faza 0, Faza 1 (T1.x) tugab, asoslar barqaror bo'lmaguncha boshlanmaydi.

### ADR-P12: Ichki va tashqi bazalarni qat'iy ajratish; ulagich alohida modul
- **Kontekst**: ilovaning yagona bazasi `rag.db` (bitta pul, `RAG_DATABASE_URL`, `DB_POOL_SIZE=5`), u haqiqat manbai (ADR-002) [T]. Foydalanuvchi boshqa loyihalarning bazalariga (PostgreSQL, SQL Server) ulanishni xohlaydi. Modullar tsiklsiz, `answer` persistenceni `repo` parametri orqali oladi [T, audit E9].
- **Qaror (taklif)**: ikki toifa: **ichki ilova bazasi** (ilova egasi; hujjat, versiya, job, metama'lumot, audit, holat, ulagichning o'z jadvallari; yagona manba) va **tashqi loyiha bazalari** (faqat ulagich orqali o'qiladi; ilovaning hech qanday saqlash vazifasini bajarmaydi; sxema/ma'lumot o'zgartirilmaydi; ichkiga nusxalanmaydi, faqat metama'lumot va foydalanuvchi konteksti saqlanadi). Ulagich: yangi paket `rag/connector/` (modulli monolit, mikroservis yo'q): `registry`, `vault`, `drivers` (postgres, mssql; torg'ina interfeys, dvigatel nomi bo'yicha ro'yxat), `schema`, `context`, `sqlcheck`, `executor`, `pipeline`, `audit`, `repository` (ichki bazaga tegadigan **yagona** qism). Har tashqi ulanishga **alohida** pul; ulagich `RAG_DATABASE_URL` ni tashqi ulanish sifatida rad etadi. `ingest/store/versioning/worker/storage/answer` bilan o'zaro import yo'q (AST testi bilan majburlanadi); faqat `api` ularni ulaydi.
- **Alternativlar**: (a) tashqi bazani ichki PG ga import qilish (nusxa, eskirish, maxfiylik xavfi; rad etildi); (b) alohida ulagich xizmati (mikroservis, ADR-001 ga zid, o'lchangan asos yo'q); (c) `rag.db` ni umumlashtirib ko'p bazali qilish (ichki persistence mas'uliyatini aralashtiradi; rad etildi).
- **Oqibat**: ichki bazaning mas'uliyati o'zgarmaydi; audit 17-bo'lim kafolatlari va hujjat RAG yo'li tegilmaydi. Ulagich jadvallari ichki bazaga **faqat qo'shimcha** migratsiya bilan (ADR-011). Qabul: roadmap 6a.11 (1).
- **Ochiq savol**: ulanish egaligi (T3.2/ADR-P8), o'chirilganda kontekst va audit saqlanishi (OQ-3, OQ-7).

### ADR-P13: Xavfsiz tabiiy tildan SQL ga (Text-to-SQL)
- **Kontekst**: model chiqishi bajariladigan kodga aylanadi. Bugungi tizimda modelda vositalar yo'q va UI faqat `textContent` (ADR-008) [T]; injection 3/4 bajarilmadi (LLM-04) [T]. Ollama ketma-ket ishlaydi (RC-2), 7B model SQL aniqligi o'lchanmagan [X].
- **Qaror (taklif)**: quvur: savol → sxema/kontekst tanlash → SQL yaratish (model faqat **matn** chiqaradi, hech narsani bajarmaydi) → **tuzilma bo'yicha tekshiruv** (lahjali parser, AST; kalit so'z emas) → faqat o'qish bajarish → sharhlash → manba bilan javob. Tekshiruv: bitta ifoda; ildiz `SELECT`/`WITH…SELECT`; ma'lumot o'zgartiruvchi CTE, `SELECT INTO`, qulflovchi bandlar, xavfli funksiyalar yo'q; faqat tanlangan ob'ektlar; tahlil qilib bo'lmasa rad (fail-closed); modelga bitta tuzatish urinishi. Chegaralar: server va ilova tomonida timeout (tashqi serverda ham bekor qilinadi), sozlanadigan qator chegarasi (SQL ga va kursor darajasida). **Faqat o'qish hisob ma'lumoti chuqurlashtirilgan himoya, tekshiruvning o'rnini bosmaydi**; PostgreSQL da read-only tranzaksiya qo'shimcha. Model yaratgan SQL tekshiruvsiz **hech qachon** bajarilmaydi.
- **Alternativlar**: (a) kalit so'zli qora ro'yxat (osongina aylanib o'tiladi, rad etildi); (b) faqat DB huquqlariga tayanish (SQL Server da seans read-only yo'q, huquq noto'g'ri sozlanishi mumkin; rad etildi); (c) modelga vosita (function calling) berish (ADR-008 ni buzadi, rad etildi).
- **Oqibat**: ADR-008 bilan taranglik: model baribir bajariladigan matn chiqaradi, lekin **bajaruvchi** model emas, deterministik tekshirilgan komponent; ADR-008 ning "modelda vositalar yo'q" qoidasi o'zgarmaydi, bu qaror uni **toraytirmaydi**, lekin uning shartini aniq ko'rib chiqishni talab qiladi (qarang: pastdagi munosabat jadvali). Qabul: roadmap 6a.11 (2)(3). Bajaruvchidan oldin tekshiruv tugaydi (M4.4).
- **Dalil kerak**: M4.0 da boshlang'ich SQL aniqligi; parser va DB dvigateli o'qishi farqi (OQ-6, R-3); dushmanona to'plam natijasi.

### ADR-P14: Tashqi ulanish hisob ma'lumotlari va audit
- **Kontekst**: ilova autentifikatsiyasiz va faqat loopback (ADR-007); sirlar siyosati: zaxira manifestida parol yo'q, zaxira ma'lumotdek maxfiy (T0.1) [T]. Hisob ma'lumoti hozir faqat muhit o'zgaruvchisida (ADR-012).
- **Qaror (taklif)**: hisob ma'lumoti ichki bazada **shifrlangan** saqlanadi (autentifikatsiyalangan shifrlash), asosiy kalit bazadan va zaxiradan tashqarida (muhit/secret; joyi OQ-2). API faqat **yozadi**, hech qachon qaytarmaydi (`has_credentials`); jurnal, UI, xato matni, audit va manifestda yo'q; drayver istisnolari kodga aylantiriladi. Audit ichki bazada: kim (identifikatsiya bo'lsa), ulanish, savol, **tekshirilgan** SQL, davomiylik, qatorlar soni, holat; **natija ma'lumoti, parol, token yozilmaydi**. Ilova jurnali alohida: faqat id, davomiylik, son, xato kodi.
- **Alternativlar**: hisob ma'lumotini faqat muhitda saqlash (UI siz, bir nechta ulanishga noqulay, lekin ichki bazada sir yo'q; MVP uchun variant); OS kalit ombori (Windows ga bog'liq).
- **Oqibat**: kalit yo'qolsa hisob ma'lumotlari tiklanmaydi (R-11); zaxira va tiklash tartibi yangilanadi. ADR-007 bilan **taranglik**: autentifikatsiyasiz ilovada saqlangan ulanishdan shu mashinadagi har qanday jarayon foydalana oladi. Bu taranglik **ADR-P16** da ko'rib chiqiladi (taklif); ADR-P14 ADR-P16 bilan birga qabul qilinishi kerak.
- **Ochiq savol**: audit da SQL literallari/savol matni maxfiy bo'lishi mumkin (OQ-8); kalit aylantirish.
- **ADR-P16 bilan munosabat**: shifr bog'lovchi ma'lumoti (`connection_id` + `key_id`, egasi emas), asosiy kalitni boshqarish va zaxira/tiklash, audit maxfiyligi rejimlari va saqlash muddati **ADR-P16 da aniqlangan va shu yerdagi umumiy bayonotlardan ustun**; ikki manbada takrorlanmaydi.

### ADR-P15: Gibrid RAG (hujjat RAG + baza RAG), kelajak
- **Kontekst**: foydalanuvchi ikkala manbadan birga javob kutadi.
- **Qaror (taklif, **hozir qaror emas**)**: ikki qidiruv yo'li alohida qoladi (hujjat: `answer.retrieve`; baza: `connector.pipeline`) va faqat `api`/javob qatlamida birlashadi; javob manbalarini ajratib ko'rsatadi: hujjatdan olingan fakt (nom, sahifa) va bazadan olingan fakt (ulanish, SQL); yo'naltirish va birlashtirish usuli baholash bilan M4.6 dan keyin tanlanadi (OQ-11).
- **Oqibat**: hujjat yo'li va uning kafolatlari o'zgarmaydi. Qabul: alohida keyingi vazifa.

### ADR-P16: Bosqichli autentifikatsiya chegarasi, egalik modeli va ulagich xavfsizligi (OQ-1)
- **Holat**: **Taklif (Proposed)** (umumiy holat o'zgarmadi). Mahsulot egasi qabul qildi (2026-11-17): **D-1, D-2, D-15 (A5 talqini), D-3, D-4, D-5, D-7, D-8, D-14** hamda **ADR-P16 ning faqat M4.1a scope'iga tegishli qismlari** (P16.1–P16.6 ning M4.1a qismi va P16.7 ning M4.1a xavfsizlik hodisalari qismi; aniq chegara: pastdagi "ADR-P16 ning M4.1a bandlari" jadvali). M4.1b va keyingi bosqichlarga tegishli talablar, P16.7 ning so'rov auditi (M4.5) qismi, P16.8 va P16.9 **Taklif** bo'lib qoladi. **D-6, D-9, D-10, D-11, D-12, D-13 va D-16 hal qilinmagan.** Bu ADR **ADR-007 va ADR-001…014 matnini o'zgartirmaydi**. Manba: xavfsizlik review'i (ADR-P16 ning birinchi variantiga 9 ta majburiy o'zgartirish).
- **Kontekst** [T: kod va shu mashinadagi o'lchovlar]:
  - Mahsulot yo'nalishi: hozir yagona foydalanuvchi, lokal; arxitektura ko'p foydalanuvchiga tayyor; hozirgi bosqichda ko'p foydalanuvchi amalga oshirilmaydi.
  - ADR-007: faqat loopback, autentifikatsiyasiz. `guard` faqat `Host` ruxsat ro'yxati va `Origin` (faqat yozuvchi so'rovlarda, sarlavha bo'lsa) tekshiradi; `Origin`siz so'rov o'tadi. Kodda foydalanuvchi, egasi, sessiya, token tushunchasi **yo'q**.
  - Ichki PG: `rag` roli **superuser**; `rag/rag` ishlaydi; parol `docker inspect` da ochiq; `pg_hba` da konteyner ichidagi `local` va `127.0.0.1` uchun `trust`, tashqaridan `scram-sha-256` (port orqali noto'g'ri/parolsiz ulanish rad etiladi). Port `127.0.0.1:5433` ga bog'langan, lekin shu sessiyada WSL2 va boshqa konteynerdan loopbackka bog'langan portga yetib borildi. Qdrant API kalitisiz. `.gitignore` da `.env`/sir uchun qoida yo'q.
  - `start.bat` brauzerni `cmd /c "… & start <URL>"` bilan ochadi (URL buyruq satrida). Ildiz logger `INFO`, uvicorn `warning`; istisno matnini `%s` bilan jurnalga yozadigan joylar bor (`api.py:278,320`).
- **Qaror (taklif)**:

  **P16.1 Haqiqiy xavfsizlik chegarasi** (8-talab) **[M4.1a uchun Qabul qilindi]**. Quyidagilar aniq e'lon qilinadi va hujjat/UI da "himoyalangan" deb ko'rsatilmaydi:
  - Loopback **OS-foydalanuvchi chegarasi emas**: shu mashinadagi har qanday jarayon, WSL2 distributivi va boshqa Docker konteyneri loopbackka bog'langan portlarga yetadi. Ichki PG va ilova uchun yagona to'siq: parol yoki token.
  - Docker daemoniga kirish **ma'mur darajasiga teng**: `docker exec` (konteyner ichida `trust`), `docker inspect` (muhit, shu jumladan `POSTGRES_PASSWORD`), hajmlarga kirish.
  - **Kafolat qamrab oladi**: shu mashinadagi *boshqa OS-foydalanuvchilar* (foydalanuvchi profili fayllariga kira olmaganlar), sessiyasiz (pairing code bilan juftlashmagan) jarayonlar, brauzerdan boshlangan hujumlar (CSRF, DNS-rebinding), tasodifan ochiq qolish. **Qamrab olmaydi**: **shu OS-foydalanuvchi ostidagi zararli dastur** (faylni va xotirani o'qiydi), keng ruxsatli brauzer kengaytmalari (qisman), Docker/ma'mur huquqiga ega kishi, jismoniy kirish.
  - Bu bayonot ADR-007 ni o'zgartirmaydi; SEC-01 qoldiq xavfini aniqlashtiradi (WSL2, konteynerlar, Docker ma'murligi).

  **P16.2 Bosqichlar** [S0 va S1: M4.1a uchun Qabul qilindi; S2: Taklif]. **S0** (hozir): ADR-007 o'zgarishsiz, ulagich yo'q. **S1** (Faza 4 ning M4.1a bosqichi, Faza 3 emas): faqat **ulagich yuzasi** (`/api/v1/connections*`, `/api/v1/db/*`) autentifikatsiyalangan; mavjud hujjat va `ask` marshrutlari ADR-007 ostida qoladi (**D-1 qabul qilindi**; D-6 ochiq, lekin D-1 bilan cheklangan). **S2** (Faza 3: T3.1–T3.3): haqiqiy foydalanuvchilar; hujjat marshrutlari ham shu chegaraga o'tadi; ADR-007 ni bekor qilish alohida qaror.

  **P16.3 Principal va egalik modeli** (1 va 2-talab) **[M4.1a qismi Qabul qilindi; M4.1b/M4.5 qismi Taklif]**.
  - `principals` (kontseptual, DDL Faza 4 da, faqat qo'shimcha migratsiya): `id` uuid, `kind`, `display_label`, `user_id` (S2 gacha NULL), `claimed_at`, `disabled_at`. `kind='local-owner'` uchun noyob qisman indeks (bitta qator). Qator migratsiyada `INSERT … ON CONFLICT DO NOTHING` bilan (migratsiya advisory lock ostida, takroran xavfsiz).
  - Ulagich jadvallari: `owner_principal_id uuid NOT NULL REFERENCES principals(id)`. **`owner_id = 'local'` matni ishlatilmaydi.** Yetim qator mumkin emas.
  - `authorize(principal, action, resource)` yagona nuqta; S1 da faqat `local-owner` ruxsat. `repository` har so'rovda egasi bo'yicha filtrlaydi (hozir bitta qiymat, so'rovlar keyin qayta yozilmaydi).
  - Egalik **hech qachon qayta yozilmaydi**: S2 da principal "da'vo qilinadi" (P16.6), `owner_principal_id` ustunlari o'zgarmaydi.
  - **Shifr bog'lovchi ma'lumoti (AAD) = `connection_id` + `key_id`. Egasi AAD ga kirmaydi:** egalik avtorizatsiya vazifasi, kriptografiya emas, shuning uchun egalik yoki foydalanuvchi o'zgarishi shifrmatnga ta'sir qilmaydi va qayta shifrlash kerak bo'lmaydi. Saqlanadi: `ciphertext`, `nonce`, `key_id`.
  - Audit `principal_id` ga murojaat qiladi (matn emas); foydalanuvchi o'chirilsa principal qoladi (taxallus), tarix o'zgarmaydi.

  **P16.4 Bootstrap (pairing code), sessiyalar va sir fayllari joyi** (3-talab; D-3, D-4, D-5 qabul qilingan) **[M4.1a uchun Qabul qilindi; 4-bandning hisob ma'lumotiga qo'llanishi va 7-band tashqari]**.
  1. **Bootstrap: bir martalik pairing code (D-3, Qabul qilindi).** Kod kriptografik xavfsiz tasodifiy generator (`secrets`) bilan yaratiladi va **qisqa muddat** amal qiladi. U **bir marta** ishlatiladi: almashtirish (exchange) urinishi **muvaffaqiyatli ham, muvaffaqiyatsiz ham** yakunlansa, kod qayta ishlatilmaydi. Kod faqat xotirada saqlanadi; **loglarga, URL (query parametri ham, fragment ham), buyruq satri argumentlariga, fayllarga va repozitoriyga yozilmaydi.** Taxmin qilish (brute-force) urinishlariga qarshi **rate limit** bor; solishtirish `hmac.compare_digest`.
     - **Pairing code session token emas.** U faqat bir martalik bootstrap almashinuvi uchun: `POST /api/v1/session/exchange` kodni sessiya tokeniga almashtiradi (kod tanada, `Cache-Control: no-store`). Kod hech bir endpointda hisob ma'lumoti sifatida qabul qilinmaydi; sessiya tokeni pairing code o'rnida ishlamaydi.
     - Loyihalash parametrlari (kod uzunligi va alfaviti, amal muddati [dastlabki taklif ≤60 s], urinishlar chegarasi va qulflash) M4.1a loyihalashida belgilanadi va taxmin qilish ehtimoli shu parametrlar bo'yicha hisoblanib hujjatlashtiriladi; ular yuqoridagi qabul qilingan cheklovlar (qisqa muddat, bir martalik, rate limit) doirasida qoladi.
  2. **Pairing code ni yetkazish va ko'rsatish (alohida xavfsizlik talabi, roadmap SR-15).**
     - Kod faqat **ilova jarayonining o'z konsoliga** (ilovani ishga tushirgan oyna) chiqariladi; URL, buyruq satri argumenti, fayl, xabarnoma, clipboard yoki brauzer tarixi orqali yetkazilmaydi.
     - Konsol chiqishi xavfsizlik jurnaliga (D-14), umumiy jurnalga va hech qanday faylga yo'naltirilmaydi va yozilmaydi.
     - Kod ilova ishga tushganda yoki foydalanuvchi aniq so'raganda hosil qilinadi; so'rov tezligi va bir vaqtda faol kodlar soni cheklanadi (so'rov mexanizmi konsolni to'ldirib yubora olmaydi).
     - Brauzerda kod kiritish maydoni faqat ulagich bo'limida; mavjud hujjat sahifalari o'zgarmaydi (D-1); kiritilgach maydon tozalanadi va saqlanmaydi (`autocomplete="off"`).
     - Qoldiq xavf: konsolni ko'ra oladigan shaxs yoki jarayon (ekran ulashish, terminal tarixi). So'rov usuli (UI tugmasi yoki CLI) M4.1a loyihalash tafsiloti.
  3. **Sessiyalar (D-4, Qabul qilindi):** faqat **server xotirasida**. Mutlaq umr **12 daqiqa**, bo'sh turish (idle) **2 daqiqa**, step-up autentifikatsiya amal qilish oynasi **10 daqiqa**. Barcha vaqt chegaralari **server tomonidan** tekshiriladi (mijoz vaqtiga ishonilmaydi). Server qayta ishga tushganda barcha faol sessiyalar bekor bo'ladi. Sessiya aylantirish (rotation) va bekor qilish (revocation) mexanizmi bor (bitta sessiya va hammasi; UI da "Chiqish"). Brauzerda sessiya tokeni **faqat `sessionStorage`** da; `localStorage` va doimiy cookie **ishlatilmaydi** (cookie larda port izolyatsiyasi yo'q, RFC 6265). Bu qiymatlar MVP uchun qabul qilingan boshlang'ich xavfsizlik parametrlari.
     - **Yakuniy (Stage 7):** qiymatlar **daqiqalarda** (soat emas); dastlabki 12 soat / 2 soat taklifi **bekor qilingan**. **Step-up amal qilish oynasi asosiy sessiyaning mutlaq umridan oshmaydi:** step-up sessiya tugashi bilan tugaydi (samarali oyna = min(10 daqiqa, sessiyaning qolgan mutlaq umri)). Oqibat (ma'lumot uchun, qaror emas): bo'sh turish 2 daqiqa ulagich bo'limida tez-tez qayta juftlashni talab qiladi (faollik faqat tokenli ulagich so'rovlaridan hisoblanadi; mavjud endpointlar token ishlatmaydi va sessiyani uzaytirmaydi).
  4. **Step-up:** sessiya ichida pairing code bilan qayta tasdiqlash; amal qilish oynasi 10 daqiqa, lekin sessiyaning mutlaq umridan oshmaydi (D-4). **Mexanizm M4.1a ga kiradi; uni hisob ma'lumotini yaratish/o'zgartirishga qo'llash M4.1b da** (M4.1a da bunday endpoint yo'q).
  5. **Sarlavhalar va jurnal:** `Referrer-Policy: no-referrer`; `Authorization`, almashtirish tanasi, pairing code va sessiya tokeni hech qachon jurnallanmaydi; token so'rov qatorida qabul qilinmaydi.
  6. **Sir fayllari joyi (D-5, Qabul qilindi):** sir fayllari **repozitoriydan tashqarida** saqlanadi; Windows da asosiy joy `%LOCALAPPDATA%` ostidagi ilovaga xos katalog; fayl va katalog ACL orqali faqat tegishli OS-foydalanuvchi uchun cheklanadi; sirlar repozitoriy, jurnal, Docker image yoki oddiy konfiguratsiya fayllariga kiritilmaydi; T0.1 zaxirasi (PostgreSQL dampi, `storage/`, Qdrant snapshoti) bu katalogni o'z ichiga olmaydi. WSL2 yoki boshqa runtime ishlatilsa, haqiqiy fayl tizimi ruxsati va ACL xatti-harakati **alohida tekshiriladi**; ACL yoki ruxsat talablarini **kafolatlab bo'lmasa, tizim fail-closed** ishlaydi.
  7. **Brauzersiz mijozlar (.NET, skript):** xuddi shu pairing code almashtirishi ishlatilishi mumkin. Uzoq muddatli mahalliy sir fayli (ADR-P16 ning dastlabki taklifi) D-3 bilan almashtirilgan bootstrap modelida **talab qilinmaydi**; uni qayta kiritish alohida qaror (D-16, ochiq).
  - Qoldiq xavf (P16.1 ga ko'ra): kengaytmalar va shu OS-foydalanuvchi zararli dasturi sessiya tokenini o'qiy oladi; konsolni ko'ra oladigan kishi pairing code ni ko'rishi mumkin.

  **P16.5 Ichki PostgreSQL talablari va uzluksiz tayyorlik tekshiruvi** (4-talab; D-7, D-15 qabul qilingan) **[M4.1a uchun Qabul qilindi; R5 va ta'minot buyrug'i shakli ochiq]**. Hisob ma'lumoti saqlanishidan **oldin**:
  - **D-7 (Qabul qilindi): PostgreSQL rol modeli.**
    - Ilova uchun **alohida** PostgreSQL roli ishlatiladi; u **superuser emas** (shuningdek `CREATEROLE`, `CREATEDB`, `REPLICATION`, `BYPASSRLS` huquqlari yo'q) va faqat zarur database/schema/table huquqlariga ega (minimal privilege).
    - Default yoki zaif credential ishlatilmaydi; **kuchli tasodifiy** credential (kamida 128 bit) ishlatiladi; credential Docker image yoki repozitoriy ichiga yozilmaydi (sirlar fayli/Docker secret; `docker inspect` da ko'rinmaydi; joyi D-5).
    - Ishga tushishda rol va privilege talablari tekshiriladi (fail-closed tayyorlik, pastda).
    - Credential rotation uchun **hujjatlashtirilgan runbook** bor (mavjud hajmda `ALTER ROLE`; `POSTGRES_PASSWORD` yangi hajmga amal qiladi [X: upstream hujjat bo'yicha, bu muhitda sinalmagan]).
    - **Migratsiya roli (ixtiyoriy, majburiy emas):** migratsiya va sxema boshqaruvi uchun zarur bo'lsa, runtime ilova rolidan **alohida** migratsiya roli ishlatilishi mumkin (minimal privilege tamoyili): migratsiya roli sxema DDL huquqiga ega; runtime roli DDL huquqiga ega emas, faqat zarur DML va ketma-ketlik (sequence) huquqlari. Bo'lish-bo'lmaslik amalga oshirish tafsiloti (M4.1a loyihalashi).
    - Qabul qilinmagan (ochiq qism): ta'minot buyrug'i/skripti shakli (masalan `rag init-secrets`).
  - **R3** `.gitignore` ga `.env` va sir fayllari qo'shilishi (amalga oshirishda).
  - **R5** Qdrant API kaliti: hosila sxema/kontekst embeddingi Qdrant ga yoziladigan bo'lsa (hisob ma'lumoti Qdrant ga hech qachon kirmaydi; D-13 ochiq).
  - **Fail-closed tayyorlik** (`RAG_CONNECTOR_ENABLED` standart **o'chiq**). Shartlar: ilova roli superuser emas va ortiqcha huquqlari yo'q; parol ma'lum standart (`rag`) emas; sir fayllari katalogi (D-5) repozitoriydan tashqarida va ACL/ruxsat kafolatlanadi; shifrlash kaliti joyi (D-8) repozitoriydan, Docker image dan va PostgreSQL dan tashqarida; xavfsizlik hodisalari jurnali (D-14) yozilishi mumkin. Shartlar ishga tushishda **va** har `/readyz`/`/api/status` da qayta baholanadi, har ulagich so'rovidan oldin ≤60 s kesh bilan; buzilsa ulagich marshrutlari kod bilan 503 (`connector_unavailable`, sabab kodi, qiymat emas) qaytaradi va **tayyorlik "tayyor" demaydi** (fail-closed); status faqat mantiqiy qiymatlarni ko'rsatadi.
  - **D-15 (A5 talqini, Qabul qilindi): qamrov.** Default credential bilan ishga tushishni rad etish talabi (va yuqoridagi tayyorlik shartlari) **faqat `RAG_CONNECTOR_ENABLED=true`** bo'lganda majburiy. Ulagich o'chiq (standart) rejimda mavjud tizimning xatti-harakati, shu jumladan `/readyz`/`/api/status` javobi va ishga tushish, **o'zgarmaydi**; mavjud document/ask endpointlariga yangi autentifikatsiya talabi qo'shilmaydi. Ulagich yoqilganda xavfsizlik shartlari bajarilmasa, tayyorlik fail-closed. Majburlashni ulagich o'chiq rejimga ham kengaytirish bu qaror bilan **qabul qilinmagan** va rejada yo'q.
  - **Cheklov (halol):** ilova parol *kuchini* tasdiqlay olmaydi; u faqat o'zi biladigan standartlarni va rol huquqini tekshiradi (kuchli parol ta'minot talabi, tekshiruv evristik).
  - Ma'lum qoldiq chegara (qaror emas, e'lon qilingan xavf): `docker exec`/`trust` Docker ma'murligi darajasida qoladi (P16.1).

  **P16.6 Birinchi admin da'vosi (Faza 3)** (5-talab) **[faqat M4.1a sxema shartlari Qabul qilindi; da'vo oqimi Taklif (Faza 3)]**. "Xaritalash emas, da'vo qilish":
  - `users` jadvali `principal_id UNIQUE NOT NULL REFERENCES principals(id)` ga ega. Da'vo **bitta tranzaksiyada**: foydalanuvchini yaratish **va** `UPDATE principals SET user_id = :u, claimed_at = now() WHERE kind = 'local-owner' AND user_id IS NULL` (compare-and-set). 0 qator yangilansa, tranzaksiya bekor, javob 409 (poygada yutqazgan yoki allaqachon da'vo qilingan).
  - Da'vo faqat haqiqiy S1 sessiyasi va yangi step-up bilan mumkin (ochiq ro'yxatdan o'tish emas); principal da'vo qilinmaguncha endpoint mavjud, keyin yo'q.
  - Hech qanday `owner_principal_id` UPDATE qilinmaydi, hech narsa qayta shifrlanmaydi, audit qayta yozilmaydi.
  - Da'vodan keyin eski bootstrap (pairing code) shu principal sifatida kira olmaydi (standart; da'vodan keyingi kirish va CLI davomiyligi D-9, ochiq).
  - Faqat o'qish tekshiruvi (`reconcile` kabi): har ulagich qatorining egasi mavjud, har shifrmatn kalit bilan ochiladi (sir chiqarilmaydi), audit murojaatlari butun; da'vodan oldin va keyin ishlatiladi.

  | Nosozlik/rollback holati | Natija |
  |---|---|
  | Da'vo tranzaksiyasi o'rtada uziladi | to'liq bekor, hech narsa o'zgarmagan |
  | Ikki parallel da'vo | bittasi yutadi, ikkinchisi 409 |
  | Commitdan keyin ilova qulaydi | holat izchil (da'vo qilingan) |
  | Faza 3 migratsiyasidan keyin N−1 versiyaga qaytish | qo'shimcha jadvallar e'tiborsiz qoladi, ulagich jadvallari o'zgarmagan, N−1 ishlaydi |
  | Da'voga qadar olingan eski zaxira tiklandi | principal da'vo qilinmagan, da'vo qayta bajariladi |
  | Da'vodan keyingi zaxira tiklandi | da'vo qilingan holat saqlanadi |
  | Tizim buzilgan | T0.1 zaxirasi + kalit bilan tiklash (P16.8) |

  **P16.7 Audit maxfiyligi** (6-talab) **[faqat M4.1a xavfsizlik hodisalari qismi Qabul qilindi; so'rov auditi Taklif (M4.5)]**.
  - **Doim saqlanadi (metama'lumot):** `principal_id`, ulanish, vaqt, davomiylik, qatorlar soni, holat/xato kodi.
  - **Matnli maydonlar rejimi** (sozlanadigan; standart `redacted`, D-10): `minimal` (faqat xeshlar), `redacted` (savol matni "maxfiy" belgisi bilan; SQL **literallari parser bilan `?` ga almashtiriladi**, literallar uchun faqat kalitli xesh/HMAC), `full` (xom SQL). Tahlil muvaffaqiyatsiz bo'lsa **xom SQL saqlanmaydi** (faqat xesh, fail-closed).
  - **Saqlash muddati** (taklif, D-10): matnli maydonlar 90 kun, metama'lumot 1 yil; matnni tozalash buyrug'i/vazifasi (`rag audit prune`) ADR-P7 kabi **tasdiq bilan**, jim avtomatik o'chirish emas.
  - **Kirish:** faqat autentifikatsiyalangan egasi o'qiydi (Faza 3 da admin roli); eksport yo'q; yozuvlar qo'shimcha-yozish, matn ustunlarini faqat tozalash vazifasi nolga tushiradi.
  - **O'chirish:** ulanish o'chirilganda metama'lumot "qabr toshi" sifatida qoladi (D-11, OQ-7); "unutish" buyrug'i matn maydonlarini tozalaydi; zaxiralardagi nusxalar haqida ogohlantirish (T0.1 saqlash muddatiga ega emas, P16.8).
  - **Jurnal:** faqat ruxsat etilgan maydonlar (id, davomiylik, son, xato kodi). Tashqi drayver istisnolari **hech qachon** xom `%s`/`exc_info` bilan yozilmaydi (xato matnida qiymatning o'zi bo'lishi mumkin: masalan `invalid input syntax for type integer: '...'` ichida foydalanuvchi qiymati): kodga aylantiriladi. Parol, token, kod, natija qiymatlari jurnal va auditga **kirmaydi**.
  - **Xavfsizlik hodisalari bilan munosabat (D-14, Qabul qilindi):** M4.1a xavfsizlik hodisalari (bootstrap, sessiya, tayyorlik) alohida himoyalangan log faylida saqlanadi va yuqoridagi "Jurnal" qoidalariga mos (sir, token, pairing code, kalit, SQL literal, xom istisno yozilmaydi). **So'rov auditi** (savol, SQL, davomiylik; M4.5) ADR-P14 bo'yicha ichki bazada va D-10 rejimlariga bo'ysunadi: D-14 uning saqlash joyini hal qilmaydi.
  - T3.3 (yuklash/o'chirish/rollback auditi) alohida; bu so'rov auditi.

  **P16.8 Zaxira, tiklash va tashqi kalit boshqaruvi** (7-talab; D-8 qabul qilingan) **[D-8 qarori Qabul qilindi; band Taklif (M4.1b)]**.
  - **D-8 (Qabul qilindi): shifrlash kalitini saqlash va tiklash.**
    - Shifrlash kaliti **PostgreSQL ichida saqlanmaydi** va **zaxira (backup) tarkibiga kiritilmaydi**; u repozitoriy va Docker image tashqarisida saqlanadi (kalit fayl ko'rinishida saqlansa, D-5 cheklovlari ham unga tatbiq etiladi: izchillik talqini).
    - Kalit uchun **alohida recovery/backup siyosati** belgilanadi (amalga oshirishdan oldin hujjatlashtiriladi); recovery nusxasi asosiy kalitdan **alohida himoyalanadi**.
    - Kalit yo'qolsa tegishli shifrlangan credential larni tiklash **imkonsiz bo'lishi mumkin** (ochiq hujjatlashtiriladi): ulanishlar `needs_credentials` holatiga o'tadi, foydalanuvchi qayta kiritadi; hujjat va ichki holat ta'sirlanmaydi.
    - Kalit aylantirish (rotation) va recovery **`key_id` versiyalanishi bilan mos** ishlaydi.
    - **AAD talabi o'zgarmaydi:** `connection_id` + `key_id`; egasi (owner id) AAD ga kirmaydi (P16.3).
  - T0.1 zaxirasi (to'liq `pg_dump`) shifrmatnni va ichki bazadagi auditni **o'z ichiga oladi**, kalitni **o'z ichiga olmaydi**; manifest o'zgarmaydi (parol, kalit, shifrmatn yo'q). Boshqa mashinada tiklash uchun kalit **alohida** beriladi.
  - **Izchillik sharti** (D-8 ning "rotation va recovery `key_id` bilan mos" bandidan kelib chiqadi): zaxira shifrmatnni `key_id` bilan olgani uchun, shu `key_id` ga murojaat qiladigan zaxira saqlanar ekan, tegishli kalit versiyasining recovery nusxasi ham saqlanishi kerak; aks holda o'sha zaxiradagi credential lar tiklanmaydi. Saqlash muddatlari recovery siyosatida belgilanadi (M4.1a/M4.1b dizayniga qadar).
  - Aylantirish: `key_id` bilan yangi kalit qo'shiladi, qayta o'rash buyrug'i qator bo'yicha takroran xavfsiz (`WHERE key_id = eski`), eski kalit tugamaguncha saqlanadi (M4.1b).
  - Tiklash mashqi kengaytiriladi: tiklash → `connector verify` (hamma shifrmatn kalit bilan ochiladi) → da'vo holati; kalit yo'q holat ham sinaladi (M4.1b).
  - Zaxira papkasi huquqlari va saqlash muddati hujjatlashtiriladi; audit matnini tozalash zaxiradagi nusxalarga **ta'sir qilmasligi** aniq yoziladi. `docs/backup-restore.md` amalga oshirishda yangilanadi (hozir o'zgartirilmadi).

  **P16.9 Hujjat egaligi bilan moslik** (9-talab) **[Taklif]**. `documents.owner_id` (ADR-P8) ni `principals(id)` ga bog'lash va ADR-P8 dagi "merosxo'r egasi" ni `local-owner` bilan bir tushuncha qilish — **alohida kelajak qaror** (D-12, T3.2 da). ADR-P8 **o'zgartirilmaydi**; S1 hujjat marshrutlarini egalik bo'yicha cheklamaydi.
- **Alternativlar** (rad etilgan): (A) ADR-007 o'zgarishsiz va ulagich ham ochiq: har mahalliy jarayon tashqi bazaga kiradi, auditda shaxs yo'q. (B) To'liq autentifikatsiya Faza 4 dan oldin: mahsulot yo'nalishiga zid, hajmi L–XL [X], hamma endpoint va testlarni o'zgartiradi. Shuningdek: `owner_id='local'` matni va xaritalash; `owner_id` ni AAD ga kiritish (xaritalash shifrni buzadi); tekshiruv xeshini ichki bazada saqlash (bazaga yozish = autentifikatsiyani aylanib o'tish); cookie sessiyasi (port izolyatsiyasi yo'q); `localStorage` da token (uzoq muddatli ta'sir).
- **Oqibat**: bitta foydalanuvchi tajribasi (login oynasi yo'q) saqlanadi; ko'p foydalanuvchiga o'tishda ulagich so'rovlari, shifr va audit qayta yozilmaydi. **Ikki darajali yuza** (hujjat marshrutlari ochiq, ulagich yuzasi himoyalangan) S2 gacha qoladi. `owner_principal_id` bor, lekin izolyatsiya yo'q (bitta qiymat): "ko'p foydalanuvchi tayyor" deb da'vo qilinmaydi. Qo'shimcha ish: bootstrap, sir/kalit/parol tekshiruvlari, `current_principal`/`authorize`, DB ta'minoti (~M, [X]); Faza 4 hajmiga qo'shiladi. Kalit yo'qolishi hisob ma'lumotlarini qaytarib bo'lmas yo'qotadi (qayta kiritish).
- **Review topilmalari → qayerda hal qilingan**:

  | # | Talab | ADR-P16 | Roadmap (6a) |
  |---|---|---|---|
  | 1 | `owner_id=local` o'rniga principals | P16.3 | 6a.7 (jadvallar), SR-11, M4.1a, AC-12 |
  | 2 | AAD = `connection_id + key_id` | P16.3 | SR-13, M4.1b, AC-14 |
  | 3 | Bootstrap, bir martalik almashtirish, xotira sessiyalari, aylantirish/bekor qilish | P16.4 | SR-11, M4.1a, AC-11 |
  | 4 | PG non-superuser, sirlar, uzluksiz tayyorlik | P16.5 | SR-12, M4.1a, AC-12 |
  | 5 | Atomik da'vo (CAS, bitta tranzaksiya) | P16.6 | 6a.14 (OQ-3), M4.1a (tayyorlov), AC-13 |
  | 6 | Audit maxfiyligi: rejim, muddat, kirish, o'chirish | P16.7 | SR-14, FR-5, AC-15 |
  | 7 | Zaxira/tiklash, tashqi kalit | P16.8 | SR-13, AC-14, R-16 |
  | 8 | Haqiqiy xavfsizlik chegarasi | P16.1 | SR-11, R-4, R-17 |
  | 9 | ADR-P8/hujjat egaligi: alohida qaror | P16.9 | OQ-14 |

- **Qabul qilingan qarorlar** (mahsulot egasi, 2026-11-17; Stage 5 ko'rsatmasi). ADR-P16 umumiy holati **Taklif** bo'lib qoladi; faqat quyidagilar qabul qilingan (D-1 va D-2: Stage 5; D-15, D-3, D-4, D-5, D-7, D-8, D-14: Stage 6):
  - **D-1: Ikki darajali API yuzasi — Qabul qilindi (Accepted).**
    - Yangi Database Connector API endpointlari autentifikatsiya bilan himoyalanadi (S1, P16.4).
    - Mavjud document/ask endpointlari hozircha ADR-007 doirasida qoladi.
    - Bu ikki darajali xavfsizlik modeli **vaqtinchalik** arxitektura qaroridir.
    - Barcha API endpointlarini yagona autentifikatsiya modeliga o'tkazish kelajakda **alohida qaror va bosqich** orqali amalga oshiriladi (hozir muddat belgilanmagan).
    - Connector autentifikatsiyasi mavjud endpointlarning xatti-harakatini **o'zgartirmaydi**: mavjud endpointlar token talab qilmaydi, ularga yangi sarlavha, cookie yoki ro'yxatdan o'tish shartlari qo'yilmaydi, `guard` (Host/Origin) o'zgarmaydi.
    - **ADR-007 bilan munosabat:** ADR-007 matni **o'zgartirilmaydi**. U amaldagi barcha endpointlar uchun (hujjat, `ask`, versiyalar, rollback, `healthz`/`readyz`/`status`, statik sahifalar) avvalgidek kuchda qoladi va "vaqtinchalik, 3-bosqichgacha" bo'lib qoladi. Ulagich endpointlari hozir mavjud emas va ADR-007 qamrovida bo'lmagan yangi yuza; shuning uchun ADR-007 ning "autentifikatsiyasiz" bayonoti ularga tegishli emas. D-1 ADR-007 ni **bekor qilmaydi, toraytirmaydi va kengaytirmaydi**; ADR-007 ning almashtirilishi D-1 ga kirmaydi. Mavjud endpointlarning qoldiq xavfi (C-2) o'zgarmaydi.
  - **D-2: Faza 3 ishlarining zarur qismini Faza 4 darvozasiga ko'chirish — Qabul qilindi (Accepted).** M4.1a boshlanishidan **oldin** quyidagilar bajarilishi shart:
    1. S1: minimal autentifikatsiya va principal modeli.
    2. PostgreSQL uchun non-superuser roli.
    3. Default va zaif parollarni bartaraf etish, maxfiy ma'lumotlarni xavfsiz saqlash.
    4. Ishga tushish vaqtidagi xavfsizlik konfiguratsiyasi va readiness tekshiruvlari.
    5. Ulagich endpointlari uchun principal asosidagi authorization chegarasi.
    - Faqat M4.1a uchun zarur qism ko'chiriladi. Faza 3 ning qolgan vazifalari (to'liq autentifikatsiya va foydalanuvchi boshqaruvi, rol/ruxsat tizimi, hujjat egaligi va izolyatsiyasi, kvota/tezlik cheklovi/hujjat auditi, Docker qattiqlashtirishning qolgani, yuk testlari) **o'z joyida qoladi**. Butun loyiha uchun to'liq autentifikatsiya tizimi bu qarorga **kirmaydi**.
    - M4.1a ning aniq chegarasi: roadmap 6a.10a; qabul mezonlari: 6a.11a. Yo'l xaritasining Faza 3 bo'limi matni o'zgartirilmadi (ko'chirish faqat 6a da qayd etilgan).
    - Aniqlik: 3-band "zaif parol" ilova parol *kuchini* tasdiqlay olmasligi (P16.5 cheklovi) bilan o'qiladi: ta'minot talabi + evristik tekshiruv.
  - **D-15 (A5 talqini): default credential talqini — Qabul qilindi (Accepted).** Default credential bilan ishga tushishni rad etish talabi faqat `RAG_CONNECTOR_ENABLED=true` bo'lganda majburiy. Ulagich o'chiq bo'lsa mavjud tizimning xatti-harakati o'zgarmaydi; mavjud document/ask endpointlariga yangi autentifikatsiya talabi qo'shilmaydi; ulagich yoqilganda xavfsizlik shartlari bajarilmasa tayyorlik fail-closed. To'liq matn: P16.5. (Roadmap 6a.11a dagi "A5" mezoni shu qarorga tegishli.)
  - **D-3: Bootstrap usuli — Qabul qilindi (Accepted): bir martalik pairing code.** Kod kriptografik xavfsiz tasodifiy generator bilan yaratiladi; bir martalik; qisqa muddat amal qiladi; muvaffaqiyatli yoki muvaffaqiyatsiz yakunlangan exchange dan keyin qayta ishlatib bo'lmaydi; loglarga, URL query parametrlariga yoki repozitoriyga yozilmaydi; brute-force ga qarshi rate limit; **pairing code session token emas** (faqat bir martalik bootstrap almashinuvi uchun). Yetkazish va ko'rsatish mexanizmi alohida xavfsizlik talabi sifatida hujjatlashtirildi (P16.4 band 2, roadmap SR-15). Aniq parametrlar (uzunlik, muddat, urinishlar chegarasi) loyihalash qiymatlari, M4.1a da belgilanadi.
  - **D-4: Session parametrlari — Qabul qilindi (Accepted); yakuniy (Stage 7).** Mutlaq umr **12 daqiqa**, idle timeout **2 daqiqa**, step-up amal qilish oynasi **10 daqiqa**; qiymatlar **daqiqalarda** (soat emas), dastlabki 12 soat / 2 soat taklifi bekor qilingan. Step-up oynasi asosiy sessiyaning mutlaq umridan oshmaydi. MVP uchun boshlang'ich xavfsizlik parametrlari. Sessiyalar server xotirasida; brauzerda faqat `sessionStorage`; `localStorage` va doimiy cookie yo'q; server restartida hamma sessiya bekor; rotation/revocation bor; vaqt chegaralari server tomonidan tekshiriladi.
  - **D-5: Secret saqlash joyi — Qabul qilindi (Accepted).** Sir fayllari repozitoriydan tashqarida; Windows da `%LOCALAPPDATA%` ostidagi ilovaga xos katalog; fayl va katalog ACL orqali faqat tegishli OS-foydalanuvchiga; sirlar repozitoriy, jurnal, Docker image yoki oddiy konfiguratsiya fayllariga kiritilmaydi; WSL2 yoki boshqa runtime da haqiqiy ruxsat/ACL xatti-harakati alohida tekshiriladi; kafolatlab bo'lmasa fail-closed (P16.4 band 6).
  - **D-7: PostgreSQL role modeli — Qabul qilindi (Accepted).** Ilova uchun alohida, superuser bo'lmagan rol; faqat zarur database/schema/table huquqlari; default yoki zaif credential yo'q, kuchli tasodifiy credential; credential Docker image/repozitoriyga yozilmaydi; ishga tushishda rol va privilege tekshiriladi; credential rotation runbooki. Zarur bo'lsa runtime roldan alohida migratsiya roli mumkin (minimal privilege). Ta'minot buyrug'i shakli qabul qilinmagan (P16.5).
  - **D-8: Encryption key saqlash va recovery — Qabul qilindi (Accepted).** Kalit PostgreSQL da saqlanmaydi, zaxiraga kirmaydi, repozitoriy va Docker image dan tashqarida; alohida recovery/backup siyosati; recovery nusxasi asosiy kalitdan alohida himoyalanadi; kalit yo'qolsa credential lar tiklanmasligi mumkinligi ochiq hujjatlashtiriladi; rotation va recovery `key_id` versiyalanishi bilan mos. **AAD o'zgarmaydi: `connection_id` + `key_id`; owner ID AAD ga kirmaydi** (P16.3, P16.8).
  - **D-14: Security events saqlash joyi — Qabul qilindi (Accepted): himoyalangan log fayli.** MVP da security events alohida himoyalangan log faylida; secret, password, token, pairing code, encryption key, SQL literal, raw query va maxfiy ma'lumot loglanmaydi; raw exception/stack trace ichidagi sezgir ma'lumot logga chiqmaydi; fayl repozitoriydan tashqarida, yozish huquqi cheklangan; rotation va retention siyosati hujjatlashtiriladi; yozish xatosi security-critical hodisalarni jim yo'qotmaydi; audit storage ni keyin PostgreSQL yoki boshqa mexanizmga ko'chirish alohida qaror. D-14 faqat M4.1a xavfsizlik hodisalariga tegishli: so'rov auditi (M4.5) ADR-P14 va D-10 bo'yicha (P16.7).
  - **ADR-P16 ning M4.1a bandlari — Qabul qilindi (Accepted), faqat M4.1a scope'i (Stage 7).** Bu qabul ADR-P16 ning umumiy holatini o'zgartirmaydi (**Taklif**) va faqat M4.1a security gate ni yopish uchun zarur doiraga tegishli. Aralash bandlar to'liq qabul qilinmadi: M4.1a qismi qabul qilindi, M4.1b va keyingi bosqichga tegishli qismi **Taklif** qoldi.

    | Band | M4.1a uchun Qabul qilindi | Taklif (qoladi) |
    |---|---|---|
    | P16.1 | butun band (haqiqiy xavfsizlik chegarasi bayonoti) | — |
    | P16.2 | S0 va S1 ta'rifi (faqat ulagich yuzasi, D-1) | S2 (Faza 3: haqiqiy foydalanuvchilar, hujjat marshrutlarining o'tishi, ADR-007 ni bekor qilish) |
    | P16.3 | `principals` va yagona `local-owner` (takroran xavfsiz seed, barqaror id), `current_principal`, `authorize()` yagona nuqta, `owner_id='local'` matni ishlatilmaydi, egalik hech qachon qayta yozilmaydi | ulagich jadvallaridagi `owner_principal_id` FK va `repository` egasi filtri (M4.1b); shifr/AAD ning amalga oshirilishi (M4.1b; AAD talabi D-8 bilan allaqachon qabul qilingan); audit `principal_id` murojaati (M4.5) |
    | P16.4 | 1–3 (pairing code, yetkazish, sessiyalar, D-4 qiymatlari va step-up ≤ mutlaq umr), 4 (step-up **mexanizmi**), 5 (sarlavhalar va jurnal), 6 (sir joyi), qoldiq xavf | 4 ning hisob ma'lumotini yaratish/o'zgartirishga **qo'llanishi** (M4.1b); 7 (brauzersiz mijozlar; D-16 ochiq) |
    | P16.5 | D-7 rol modeli, R3 (`.gitignore`), fail-closed tayyorlik, D-15 qamrovi, cheklov va qoldiq chegara | R5 Qdrant API kaliti (D-13 ochiq); ta'minot buyrug'i shakli (ochiq) |
    | P16.6 | faqat sxema shartlari: `principals` da `user_id` (NULL) va `claimed_at`, `kind='local-owner'` noyob qisman indeks; compare-and-set tamoyili va uning DB darajasidagi sinovi; "claim, not remap" (hech qanday owner ustuni UPDATE qilinmaydi, qayta shifrlash va audit qayta yozish yo'q) | da'vo oqimi (`users`, bitta tranzaksiya, S1 sessiya va step-up sharti, endpoint, 409), da'vodan keyingi kirish (D-9), `connector verify` shifrmatn tekshiruvi (M4.1b), nosozlik/rollback jadvali (Faza 3) |
    | P16.7 | faqat xavfsizlik hodisalari: "Xavfsizlik hodisalari bilan munosabat (D-14)" bandi va "Jurnal" qoidalarining M4.1a hodisalar jurnaliga tegishli qismi (sir, token, pairing code, kalit, xom istisno yozilmaydi) | so'rov auditi (M4.5): rejimlar (D-10), saqlash muddati, kirish, o'chirish (D-11), literal redaksiyasi |
    | P16.8 | — (kalit joyi qoidasi D-8 qarori bilan allaqachon qabul qilingan) | butun band Taklif (M4.1b: tiklash mashqi, aylantirish, izchillik sharti) |
    | P16.9 | — | Taklif (D-12) |
- **Hal qilinmagan qarorlar (D-6, D-9…D-13, D-16; mahsulot egasi; hech biri Qabul qilinmagan)**:
  - **D-6** S1 qamrovi: faqat ulagich yuzasimi yoki butun API. Mazmunan D-1 bilan cheklangan (M4.1a faqat ulagich yuzasini qamraydi), lekin rasmiy ravishda **ochiq**.
  - **D-9** Da'vodan keyin birinchi admin principalga kirish va CLI/skript davomiyligi (dastlabki matnda "mahalliy sirning taqdiri" deyilgan; D-3 dan keyin pairing code bilan o'qiladi).
  - **D-10** Audit standart rejimi, savol matnini saqlash, muddatlar (90 kun / 1 yil), matnni shifrlash.
  - **D-11** Ulanish o'chirilganda audit "qabr toshi" siyosati.
  - **D-12** Hujjat egaligini principalga bog'lash (ADR-P8 bilan moslik).
  - **D-13** Qdrant API kaliti sharti (hosila sxema embeddingi yoziladigan bo'lsa).
  - **D-16** Brauzersiz mijozlar (.NET, skript) uchun kirish usuli: pairing code oqimi yetarlimi yoki uzoq muddatli mahalliy sir fayli kerakmi (D-3 dan keyin paydo bo'lgan savol; dastlabki taklifdagi mahalliy sir D-3 modelida talab qilinmaydi). M4.1a ni **bloklamaydi**: pairing code har ikki mijoz turi uchun ishlaydi.
- **Mavjud hujjatlar bilan ziddiyatlar** (yashirilmaydi; mavjud matn o'zgartirilmadi):
  - **C-1** ADR-007 "autentifikatsiyasiz" deydi; ADR-P16 ulagich yuzasiga erta autentifikatsiya qo'shadi. ADR-007 matni o'zgarmaydi va mavjud marshrutlarga amal qiladi; ikki darajali yuza **D-1 bilan qabul qilindi** (2026-11-17, vaqtinchalik model; yagona modelga o'tish alohida qaror).
  - **C-2** ADR-007 dalili (`HOST` qat'iy, Host/Origin) loopbackni himoya deb ko'rsatadi; tasdiqlangan: WSL2, konteynerlar va Docker ma'murligi yetib boradi. SEC-01 "har qanday jarayon/OS-foydalanuvchi" ni qayd etgan, lekin WSL2/konteyner/Docker ni nomlamagan. Qo'shimcha dalil P16.1 da; ADR-007 o'zgarmaydi.
  - **C-3** Yo'l xaritasi T3.1/T3.3/T3.4 ni Faza 3 (P3) ga qo'ygan; S1 va DB qattiqlashtirish M4.1 dan oldin kerak. Faza joylashuvi o'zgarishi **D-2 bilan qabul qilindi** (2026-11-17): faqat M4.1a uchun zarur qism ko'chirildi, Faza 3 ning qolgani o'z joyida; yo'l xaritasining Faza 3 matni o'zgartirilmagan.
  - **C-4** ADR-P8 `owner_id` (turi aniq emas, "merosxo'r egasi") va "0004+ migratsiya" (0004 band) principals modeli bilan va hozirgi migratsiya raqamlari bilan nomuvofiq; D-12 va alohida tuzatish.
  - **C-5** ADR-012 konfiguratsiyani import paytida `RAG_*` muhitidan o'qiydi; sir fayllari (`RAG_*_FILE` yo'l o'zgaruvchisi) va ish vaqtidagi tayyorlik tekshiruvi mos, lekin "tekshiruvsiz o'qish" qoidasidan chiqadi (kichik taranglik).
  - **C-6** Audit SEC-03 (standart parollar) joriy og'irligini Low deb bergan; ulagich bilan u **shart** bo'ladi. Qayta baholash kutilyapti (audit hujjati o'zgartirilmadi).
  - **C-7** T0.1 zaxira hujjati saqlash muddatini belgilamaydi; audit saqlash muddati va kalitni zaxiradan chiqarish talabi uni yangilashni talab qiladi (amalga oshirishda).
  - **C-8** ADR-P14 ning avvalgi matni "OQ-1 hal qilinmaguncha qabul qilinmaydi" degan edi; ADR-P16 bilan almashtirildi (ikkalasi ham Taklif holatida, tahrir qilindi).

#### Faza 4 ning mavjud qarorlar bilan munosabati (nomuvofiqliklar yashirilmaydi)

| Mavjud qaror/qoida | Munosabat |
|---|---|
| ADR-001 modulli monolit | **Mos**: ulagich yangi paket, mikroservis yo'q |
| ADR-002 PostgreSQL manba, Qdrant hosila | **Mos**: ichki PG manba qoladi; sxema embeddingi kerak bo'lsa, u alohida **hosila** kolleksiya (hujjat kolleksiyasidan ajratilgan), ichki PG dan qayta quriladi |
| ADR-006 faol versiya filtri | **O'zgarmaydi**: hujjat qidiruv yo'liga tegilmaydi |
| ADR-007 faqat loopback, autentifikatsiyasiz | **Taranglik (C-1, C-2)**: hisob ma'lumoti saqlash va "kim so'radi". ADR-007 **o'zgarmaydi**; ADR-P16 (Taklif) faqat yangi ulagich yuzasiga S1 chegarasini qo'shadi. Ikki darajali yuza **D-1 bilan qabul qilindi** (vaqtinchalik; mavjud endpointlar ADR-007 doirasida, xatti-harakati o'zgarmaydi) |
| ADR-008 modelda vositalar yo'q | **Taranglik**: model chiqishi bajariladi (tekshiruvdan keyin). Bu holat ADR-P13 da aniq ko'rib chiqiladi; qaror tasdiqlanmaguncha ADR-008 o'zgartirilmaydi |
| ADR-011 faqat qo'shimcha migratsiya | **Mos**: ulagich jadvallari qo'shimcha; raqam mavjud migratsiyalardan keyingi bo'sh raqam |
| ADR-012 konfiguratsiya `RAG_*` muhitda | **Qisman mos (C-5)**: ulanishlar **ma'lumot** (ichki bazada), konfiguratsiya emas; sirlar fayllarda, ularning **yo'li** `RAG_*_FILE` muhit o'zgaruvchisida; tayyorlik tekshiruvi ish vaqtida (ADR-012 ning "tekshiruvsiz o'qiladi" qoidasidan chiqadi); kalit joyi D-8 (D-5 va D-8 qabul qilingan) |
| Roadmap tamoyil 5 (yangi bog'liqlik yo'q, aksincha ko'rsatilsa) | **Taranglik**: SQL parser, SQL Server drayveri, shifrlash ilovada hozir **e'lon qilinmagan**; ixtiyoriy qo'shimcha sifatida alohida tasdiq (OQ-5). PostgreSQL uchun mavjud `psycopg` yetadi |
| ADR-P2 (LLM shlyuzi), ADR-P5 (API v1), ADR-P9 (injection) | **Bog'liq**: bitta savol ikki LLM chaqiruvi; yangi marshrutlar `/api/v1`; natija/sxema matni ishonchsiz kirish |
| ADR-P8 (ko'p foydalanuvchi) | **Bog'liq, o'zgarmaydi (C-4)**: ulanish egaligi `principals` modeli bilan (ADR-P16); hujjat egaligi (`documents.owner_id`) bilan moslik **alohida kelajak qaror** (D-12) |
| Yo'l xaritasi T3.1/T3.3/T3.4 (Faza 3, P3) | **Taranglik (C-3)**: ADR-P16 ning S1 chegarasi va DB qattiqlashtirish M4.1a dan oldin kerak; **D-2 bilan qabul qilindi**: faqat M4.1a uchun zarur qism Faza 4 darvozasiga ko'chirildi, Faza 3 ning qolgani o'z joyida |
| Audit 17-bo'lim kafolatlari | **Saqlanadi**: Faza 4 ularni o'zgartirmasligi qabul mezoni (6a.11) |

**Mavjud hujjatdagi eslatma (o'zgartirilmadi):** ADR-P8 "0004+ migratsiya" deydi, lekin `0004` (`0004_job_progress.sql`, T0.3) allaqachon band. Yangi migratsiya raqami amalga oshirish paytidagi navbatdagi bo'sh raqam bo'ladi; ADR-P8 matni alohida tuzatiladi.

## 4. Qarorlar jurnali

| Sana | Hodisa |
|---|---|
| 2026-11-13 | Audit o'tkazildi; barcha ADR-P1…P11 **Taklif** holatida, tasdiq kutilyapti |
| 2026-11-17 | Faza 4 (universal ma'lumotlar bazasi ulagichi) rejalashtirildi: ADR-P12…P15 **Taklif**; faqat hujjat, amalga oshirish Faza 0 va Faza 1 dan keyin, OQ-1 va OQ-5 hal bo'lgandan so'ng |
| 2026-11-17 | ADR-P16 (bosqichli autentifikatsiya chegarasi, principals egaligi, ulagich xavfsizligi) xavfsizlik review'i asosida 9 ta o'zgartirish bilan yozildi: **Taklif**; D-1…D-13 mahsulot egasi qarori kutilyapti; ADR-007 va ADR-001…014 o'zgartirilmagan |
| 2026-11-17 | Mahsulot egasi **D-1** (ikki darajali API yuzasi, vaqtinchalik) va **D-2** (M4.1a uchun zarur Faza 3 qismini Faza 4 darvozasiga ko'chirish) ni **qabul qildi**; ADR-P16 umumiy holati **Taklif** qoladi; D-3…D-14 ochiq; M4.1a chegarasi roadmap 6a.10a da; ADR-007 va ADR-001…014 o'zgartirilmagan |
| 2026-11-17 | Mahsulot egasi **D-15** (A5 talqini: default credential faqat `RAG_CONNECTOR_ENABLED=true` da), **D-3** (bir martalik pairing code), **D-4** (12/2/10 daqiqa), **D-5** (sir joyi), **D-7** (PG rol modeli), **D-8** (kalit saqlash va recovery), **D-14** (himoyalangan log fayli) ni **qabul qildi**; ADR-P16 umumiy holati **Taklif** qoladi; D-6, D-9…D-13, D-16 ochiq; ADR-007 va ADR-001…014 o'zgartirilmagan |
| 2026-11-17 | **D-4 yakuniy:** 12 daqiqa mutlaq, 2 daqiqa bo'sh turish, 10 daqiqa step-up (daqiqalarda, soat emas; 12 soat / 2 soat taklifi bekor qilingan; step-up mutlaq umrdan oshmaydi). **ADR-P16 ning M4.1a scope'iga tegishli qismlari qabul qilindi** (P16.1–P16.6 ning M4.1a qismi, P16.7 ning xavfsizlik hodisalari qismi); M4.1b, M4.5 va Faza 3 qismlari, P16.8, P16.9 **Taklif**; ADR-P16 umumiy holati **Taklif**; D-6, D-9…D-13, D-16 ochiq; M4.1a security gate **Ready for implementation** (kod yozish alohida tasdiq talab qiladi); ADR-007 va ADR-001…014 o'zgartirilmagan |
| 2026-11-18 | **M4.1a amalga oshirildi va qabul qilindi:** bir martalik pairing, xotiradagi sessiyalar, `principals`/`local-owner` (0005), xavfsizlik hodisalari jurnali, 7 ta tayyorlik sharti, uch rolli PostgreSQL (`rag_bootstrap`/`rag_migrator`/`rag_app`), sir fayllari, migratsiya qulfi, parol aylantirish, zaxira/tiklash uch rolli rejimda. Bayon: `docs/connector-auth.md`, `docs/postgres-hardening.md`. Qabul auditi topgan kamchiliklar (eski bazani so'rovsiz egallash, uch rolli tiklash, setup/aylantirish hodisalari) tuzatildi |
| 2026-11-19 | **M4.1b amalga oshirildi:** ulanishlar CRUD, shifrlangan hisob ma'lumoti (AES-256-GCM, AAD = `connection_id` + `key_id`, kalit sir katalogida), ulanish sinovi, PostgreSQL va SQL Server adapterlari, sxema aniqlash va tavsiflar, UI ([connector.md](connector.md)). Amalga oshirishda qabul qilingan qarorlar: (1) **maqsadni o'zgartirish ham step-up talab qiladi** (host, port, baza, foydalanuvchi, TLS, parol): saqlangan parol boshqa serverga yo'naltirilmasligi uchun; (2) **tarmoq siyosati:** link-local/aniqlanmagan/multicast/zaxiralangan va ilovaning o'z bazasi bloklanadi, loopback va xususiy tarmoqlar ruxsat (mahalliy foydalanish), ixtiyoriy ruxsat ro'yxati; baholangan manzil bilan ulanadi (DNS rebinding); (3) boshqa kompyuter uchun TLS majburiy, zaif rejim so'rovda aniq qabul qilinadi; (4) SQL Server drayveri `python-tds` (sof Python; TLS faqat CA bilan tekshirilgan rejimda, `require` yo'q), ixtiyoriy `requirements-connector.txt`, lazy import (OQ-5 ga javob); (5) mavjud ulanishni o'zgartirish xavfsizlik jurnaliga **avval** yoziladi, yozib bo'lmasa qilinmaydi |
| 2026-11-19 | **M4.2 amalga oshirildi** (sxema konteksti va metama'lumot; batafsil: [connector.md](connector.md) "Sxema konteksti va metama'lumot (M4.2)"). Amalga oshirishda qabul qilingan qarorlar: (1) **snapshot = sxemasi o'zgargan aniqlash**: ulanish bo'yicha raqamlangan o'zgarmas qatorlar to'plami (`0007_schema_snapshots`); bir xil sxema yangi snapshot yaratmaydi (savepoint bilan qaytariladi, faqat `verified_at` yangilanadi); xato yoki vaqt tugashi qisman snapshot qoldirmaydi; M4.1b qatorlari snapshot 1 bo'ladi; (2) **barmoq izi** SHA-256, kanonik JSON yozuvlar, UTF-8 bayt tartibi (`COLLATE "C"`), ustun tartibi va birlamchi kalit nomi kirmaydi (dvigatel o'ylab topgan nomlar yolg'on o'zgarish bermasin), provayder kiradi; algoritm `FINGERPRINT_VERSION` bilan versiyalangan; (3) **o'zgarishlar** SQL to'plam amallari bilan, butun obyekt qo'shilishi/olinishi bitta yozuv, qayta nomlash taxmin qilinmaydi, `CONNECTOR_MAX_CHANGES` dan oshsa obyekt darajasidagilar saqlanib mayda lari qirqiladi va bayroq qo'yiladi; (4) **yangilik holatlari** `in_progress` > `failed` > `never_discovered` > `stale` > `current`; faqat **tanlovga tegadigan** ko'rib chiqilmagan o'zgarish `stale` qiladi; yiqilgan aniqlash lease bilan hisobdan chiqadi; (5) **tanlov** standart bo'sh, yangi obyekt avtomatik tanlanmaydi, ikki ma'noli kombinatsiyalar rad etiladi, yo'qolgan target elementi o'chirilmay `unavailable` ko'rsatiladi, `base_revision` bilan optimistik parallel tahrir; (6) **tavsiflar** nom bo'yicha qoladi (yetim), kontekstga kirmaydi, sirga o'xshash matn rad etiladi (evristika); (7) **kontekst** sof funksiya, deterministik baytlar, obyekt kesilmaydi, bog'lanishlari ko'p obyekt oldin, tanlanmagan obyektga tashqi kalit nomi oshkor qilinmaydi, `usable` faqat `current` da; (8) tanlovni saqlash va o'zgarishni tasdiqlash xavfsizlik jurnaliga **avval** yoziladi (jurnal yo'q = o'zgarish yo'q); (9) `connector/` paketida ichki bazaga tegadigan ikkinchi modul `snapshots.py` (AST testi yangilandi: egasi filtri, faqat belgilangan yordamchilar tranzaksiya ichida id bo'yicha). **M4.3 va M4.4 bajarilmagan deb belgilanmaydi**; NL→SQL, SQL tekshiruvi/bajarish, kesh, gibrid RAG, embedding, ko'p foydalanuvchi, avtomatik kalit aylantirish yo'q |
| 2026-11-19 | **M4.1b + M4.2 yakuniy tekshiruv** (Windows, Linux Python 3.12 va 3.14, baholash): birlashgan regressiya o'tdi; haqiqiy muammolar topilib tuzatildi: (1) **tana chegarasi** FastAPI tanani o'qigandan keyin ishlardi (bog'liqlik): endi ASGI middleware (`rag/connector/bodylimit.py`), chunked va yolg'on `Content-Length` ham sanaladi, tokensiz so'rov katta chegarani olmaydi, `/api/v1/session/…` ≤ 16 KB; (2) `db.pool()` sovuq keshda parallel chaqiruvlar bir nechta pool ochardi (12 ta tekshirilgan): qulf qo'shildi; (3) aniqlash ketayotganda tavsif, tanlov va tasdiqlash ulanish qatorining qulfini kutib pul ulanishini band qilardi: endi `busy` (409) va `lock_timeout` 5 s; (4) diapazondan tashqari son va NUL bayt 500 berardi: endi 422/400. Baholash (retrieval 97/99/99, parcha 70/90/93, MRR 0,978, 78 savol) har savol bo'yicha avvalgi bilan bir xil. Ma'lum, oldindan bor cheklov: `RAG_TEST_HARDENED=1` bilan butun `tests/db` ni ishga tushirishda 23 test + 10 xato M4.1a commit'ida ham bir xil chiqadi (test superuser kutadi); haqiqiy qattiqlashtirish testlari (`tests/integration/test_pg_hardening*.py`) o'tadi |
| 2026-11-20 | **M4.3 amalga oshirildi: savoldan SQL taklifi (faqat generatsiya)** ([sql-generation.md](sql-generation.md)). Qarorlar: (1) taklif **matn**: SQL tahlili, obyektlar mavjudligi va SELECT ekanini tekshirish **yo'q** (M4.4), bajarish **yo'q**; javobda `validation: not_performed`, `executed: false`; tashqi bazaga murojaat qilinmaydi; (2) kontekst faqat M4.2 quruvchisidan va faqat `current` bo'lganda (`context_stale`/`context_empty`/`context_unusable`), egalik model chaqirilishidan oldin; (3) prompt: o'zgarmas qoidalar `system` da, sxema va savol `user` da bir qator JSON sifatida belgilar orasida (in'ektsiyani yumshatish, kafolat emas); chiqish ishonchsiz: JSON sifatida o'qiladi, o'lchami cheklanadi, o'zgartirilmay qaytariladi; (4) model Ollama orqali `rag/llm.py` ga ixtiyoriy parametrlar (model, oyna, chiqish chegarasi, JSON) qo'shib; savol-javob xatti-harakati o'zgarmagan; model nomi sozlanadi (`RAG_SQL_MODEL`); (5) `connector` paketi model kodini import qilmaydi: model funksiyasi `rag/sqlmodel.py` dan xizmatga beriladi, bekor qilish `rag/connector_wiring.py` dagi `Relay` orqali; (6) hech narsa saqlanmaydi (migratsiya yo'q), jurnalga savol matni yozilmaydi. **M4.4 (tekshirish) va M4.5 (bajarish) bajarilmagan** |
