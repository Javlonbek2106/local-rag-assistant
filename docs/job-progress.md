# Taraqqiyotga asoslangan job timeout (T0.3)

Manba: audit ING-01, WRK-04, yo'l xaritasi T0.3. Kod: [rag/worker.py](../rag/worker.py) (`JobProgress`, `JobGuard`), [rag/ingest.py](../rag/ingest.py), [rag/db.py](../rag/db.py), migratsiya [0004_job_progress.sql](../rag/migrations/0004_job_progress.sql).

## 1. Muammo

Bitta hujjat uchun qattiq 30 daqiqalik chegara bor edi (`JOB_TIMEOUT_S=1800`). 381 betlik statistik byulleten 49–60 daqiqa oladi (faqat bo'laklash + embedding 29 daqiqa), shuning uchun u har safar `FAILED` bo'lardi va worker qayta ishga tushardi. Chegara hujjat hajmiga bog'liq emas edi va ishning o'zi hech qanday belgi bermasdi.

## 2. Yechim

**Timeout endi ish *qancha davom etgani* bo'yicha emas, *taraqqiyot to'xtagani* bo'yicha.** Ish ketayotgan bo'lsa, soatlab davom etishi mumkin; hech narsa o'zgarmasa, 10 daqiqada to'xtatiladi.

| Chegara | Standart | Nima |
|---|---|---|
| `RAG_JOB_STALL_S` | 600 s | bosqich yoki pozitsiya (sahifa, bo'lak soni) shuncha vaqt o'zgarmasa, ish `FAILED` ("Vaqt tugadi: 10 daqiqa davomida hech qanday taraqqiyot bo'lmadi (EMBEDDING, bo'lak 120/4104)"), qayta urinilmaydi, worker qayta ishga tushadi |
| `RAG_JOB_TIMEOUT_S` | 21600 s (6 soat) | faqat xavfsizlik to'ri: "abadiy harakatlanadigan" ish uchun. Avval 1800 edi. Mavjud timeout testlari shu o'zgaruvchi bilan o'zgarishsiz o'tadi |

Heartbeat ("jarayon tirik") **taraqqiyotdan mustaqil**: heartbeat yuborib turadigan, lekin hech narsa qilmaydigan (qotib qolgan parser) ish aynan shu yo'l bilan topiladi. Aynan bir pozitsiyani qayta-qayta xabar qilish (`sahifa 5/20`, `sahifa 5/20`, ...) "harakat" hisoblanmaydi.

### Nima xabar qilinadi

| Bosqich | `progress_unit` | `done` / `total` | Manba |
|---|---|---|---|
| `PROCESSING` (o'qish) | `pages` | tugagan sahifalar / jami sahifalar | Docling `progress_callback` (`PageCompletedProgress`) |
| `PROCESSING` (sahifalardan keyin) | `assembling` | — | Docling `ASSEMBLE` bosqichi |
| `PROCESSING` (bo'laklash) | `chunks` | har 25 bo'lakda; jami noma'lum | `document_items` |
| `EMBEDDING` | `chunks` | har 32 lik paketdan keyin / jami | `ingest._embed` |
| `INDEXING` | `chunks` | har 64 lik paketdan keyin / jami | `ingest._upload` |
| `VERIFYING` | — | — | qisqa, pozitsiya yo'q |

Yangi bosqichga o'tish ham "harakat" va pozitsiyani tozalaydi. Docling callback ichidagi istisnoni yutib yuboradi (o'lchangan), shuning uchun da'vo yo'qolganda parsingni o'rtada to'xtatib bo'lmaydi; keyingi bosqichga o'tishda (`set_stage`) ish to'xtaydi va hech narsa yozilmaydi (avvalgi kafolat).

Docx, pptx, html, md, csv, txt, xlsx kabi sahifasiz formatlarda parsing davomida pozitsiya yo'q (bosqichning o'zi xabar qilinadi). Ular odatda tez; juda katta fayl uchun `RAG_JOB_STALL_S` ni oshirish mumkin.

## 3. Migratsiya 0004 (faqat qo'shiladi)

```sql
ALTER TABLE document_versions
    ADD COLUMN progress_unit text, ADD COLUMN progress_done integer,
    ADD COLUMN progress_total integer, ADD COLUMN progress_at timestamptz,
    ADD CONSTRAINT document_versions_progress_valid CHECK (done >= 0 va total >= 0 yoki NULL);
```

Mavjud ustun yoki qator o'zgarmaydi; yangi ustunlar hamma qatorda `NULL`. Test: 0001–0003 bilan to'ldirilgan bazada 0004 qo'llanadi, eski ustunlarning barcha qiymatlari **aynan teng**, faqat 4 ta yangi ustun paydo bo'ladi, navbat ishlashda davom etadi. Ustunlar faqat versiyani worker ushlab turganda ma'noga ega (`PROCESSING`…`VERIFYING`): `claim_next`, `set_stage`, `activate`, `fail` va `requeue_stale` ularni tozalaydi, shuning uchun navbatdagi, yiqilgan yoki tugagan versiyada eskirgan pozitsiya ko'rinmaydi.

## 4. Lease/heartbeat kafolatlari saqlangan

- `db.set_progress` boshqa worker'ning yozuvini va **muddati o'tgan lease** bilan yozishni rad etadi (`False`); worker `ClaimLost` bilan to'xtaydi. Test: lease muddati o'tgach pozitsiya o'zgarmaydi, natija bekor qilinadi, versiya faollashmaydi.
- `set_progress` lease'ni **uzaytirmaydi**: faqat `heartbeat()` va `set_stage()` uzaytiradi. Test: pozitsiya yozilgach `heartbeat_at` o'zgarmagan.
- Qayta urinish va idempotentlik: yiqilgan urinish pozitsiyani qoldirmaydi; qayta urinish toza boshlanadi (test: ikkinchi urinish birinchisining pozitsiyasini ko'rmaydi); nuqta id'lari avvalgidek `uuid5(versiya:bo'lak)`, `store.delete_version` avval.
- Eski faol versiya: yangi versiya ishlayotgan va to'xtab `FAILED` bo'lganda ham eski versiya `READY`, faol va qidiruvda; `activated_at` o'zgarmaydi (test).

### Kompyuter uyquga ketsa (haqiqiy sinovda topilgan kamchilik)

381 betlik hujjatning birinchi haqiqiy sinovi 217/381 sahifada `FAILED` bo'ldi: noutbuk 15 daqiqa uxlagan (Windows jurnali: uyquga kirdi 16:17:08, uyg'ondi 16:32:15), guard uyg'onib buni "10 daqiqadan beri taraqqiyot yo'q" deb o'qigan va ishni qayta urinishsiz to'xtatgan. Bu noto'g'ri: jarayon muzlagan vaqtda hech narsa harakatlana olmaydi.

Tuzatish: guard har `JOB_HEARTBEAT_S` da uyg'onadi; agar oxirgi uyg'onishdan beri kutilganidan ancha ko'p vaqt o'tgan bo'lsa (> heartbeat + max(2×heartbeat, 2 s)), jarayon muzlagan deb hisoblanadi va o'sha ortiqcha vaqt **ikkala chegaraga ham hisoblanmaydi** (`JobGuard._forgive_pause`). Lease esa kechirilmaydi: `JOB_STALE_AFTER_S` (90 s) dan uzoq uyqu da'voni baribir yo'qotadi, heartbeat buni aytadi va versiya qayta navbatga olinib qayta uriniladi (strict lease o'zgarmagan). Testlar: uyqu stall emas; uyqudan keyin haqiqiy stall baribir topiladi; tuzatish olib tashlansa test yiqiladi.

## 5. Ko'rinish

- `GET /api/documents`: har qatorda `stage` (PROCESSING/EMBEDDING/INDEXING/VERIFYING yoki `null`) va `progress` (`{"unit","done","total"}` yoki `null`).
- `GET /api/documents/{nom}/versions`: har versiyada `progress`.
- `GET /api/status`: `queue.in_flight[].progress` = `{"unit","done","total","age_seconds"}` (T0.2 da `null` edi).
- Sahifa: "Tahlil qilinmoqda: 120/381 sahifa (31%)" + chiziq; "Bo'laklarga bo'linmoqda: 250 ta"; "Embedding hisoblanmoqda: 640/4104 bo'lak (15%)"; "Indekslanmoqda: …". Yangi versiya ishlayotganda eski versiya qatorida: "v2: Tahlil qilinmoqda: … (hozircha v1 dan javob beriladi)".

## 6. 381 betlik hujjat bilan tekshiruv

Haqiqiy worker jarayoni (haqiqiy Docling, Tesseract, Ollama bge-m3, Qdrant, PostgreSQL), **standart** sozlamalar (`JOB_STALL_S=600`, `JOB_TIMEOUT_S=21600`), alohida sxema/kolleksiya/storage; hujjat: `eval/data/meniki/docs/05_hisobot_cbu_statbyulleten_2021.pdf` (381 bet, jadvalli statistik byulleten; nusxasi bilan ishlandi, asl fayl o'zgarmadi). Skript sinov oxirida hammasini o'chirdi.

**Natija (2026-11-13, 3-urinish, `READY`):**

| Ko'rsatkich | Qiymat |
|---|---|
| Holat | `READY`, faol versiya 1, hujjat `done`, **1 urinish**, xato yo'q |
| Jami vaqt | **4251 s (71 daqiqa)**: eski 30 daqiqalik chegaradan 2,4 baravar uzun, lekin to'xtatilmadi |
| Bo'laklar | PostgreSQL `chunk_count` = **4104** = Qdrant'dagi shu versiya nuqtalari **4104** (oldingi o'lchov bilan bir xil son) |
| Bosqichlar | parsing (sahifalar) 20 s → 2461 s (381/381); yig'ish 2461 s; bo'laklash 2471 s; embedding 2551 s dan (4104 bo'lak, 129 paket); `READY` 4263 s da |
| Taraqqiyot kuzatuvi | har 10 s da PostgreSQL dan o'qildi (426 namuna): bosqich, `done/total`, heartbeat va taraqqiyot yoshi doim mavjud |
| Taraqqiyot o'zgarishlari orasidagi eng uzun tanaffus | **80 s** (bo'laklash bosqichida; sahifalarda 70 s, embeddingda 20 s). `JOB_STALL_S=600` bundan 7,5 baravar katta, shuning uchun bu hujjat uchun soxta "stall" xavfi yo'q |
| Heartbeat yoshi | butun sinov davomida ≤ 15 s |

Sinovning dastlabki ikki urinishi hujjat bilan emas, **muhit bilan** to'xtagan va foydali bo'ldi: kompyuter "System Idle" bilan uxlagan (16:17 va 16:48). Birinchisi yuqoridagi uyqu kamchiligini ochdi (tuzatilgan). Ikkinchisida uyqu 90 s dan uzoq davom etdi, lease yo'qoldi va ish qayta navbatga olinishi kerak edi (strict lease kutilganidek ishladi: `ClaimLost`, hech narsa yozilmadi). Uchinchi urinishda bo'sh turish taymeri qayta o'rnatib turildi.

**Muhim cheklov (T1.2 gacha):** lease uzilgan (uyqu > 90 s, yoki worker boshqasiga o'tgan) ish allaqachon bajarilgan qismni **qayta ishlatmaydi**: u yangi urinishda boshidan boshlanadi. 71 daqiqalik hujjat uchun bu muhim: noutbuk bo'sh turib uxlasa, indekslash tugamaydi. Hozirgi yechim: indekslash paytida kompyuterni uxlatmaslik. Doimiy yechim — partiyalab yuklash va qayta boshlash nuqtasi (yo'l xaritasi T1.2).
