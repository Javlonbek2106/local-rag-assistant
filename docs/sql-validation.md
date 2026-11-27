# SQL ni tekshirish va xavfsiz bajarish (M4.4)

Manba: [architecture-roadmap.md](architecture-roadmap.md) 6a-bo'lim, [sql-generation.md](sql-generation.md) (M4.3: savoldan SQL **taklifi**), [connector.md](connector.md) (ulanishlar, snapshot, tanlov). Bu hujjat M4.4 da **nima qurilganini** tasvirlaydi.

Oqim: savol → M4.3 taklifi → **tekshirish** (`validate`) → foydalanuvchi **o'zi** "Bajarish" ni bosadi → server **qayta tekshiradi** → faqat o'qish rejimida **bajaradi** → chegaralangan natija. Taklif hech qachon o'zi bajarilmaydi.

> **Nima kafolatlanadi va nima kafolatlanmaydi.** Tekshiruv AST (sintaksis daraxti) asosida, **ruxsat ro'yxati** bilan ishlaydi: ro'yxatda yo'q hamma narsa rad etiladi. Bajarish alohida himoya qatlamlari bilan (faqat o'qish hisobi, faqat o'qish tranzaksiyasi, vaqt/qator/hajm chegaralari) cheklanadi. Bu "ixtiyoriy SQL xavfsiz" degani **emas**: ruxsat etilgan funksiyalar va og'ir (lekin o'qiydigan) so'rovlar resurs sarflashi mumkin (vaqt, qator va hajm chegaralari bilan cheklanadi), va himoyaning asosi ulanish hisobining **faqat o'qish huquqli** bo'lishi (pastga qarang). Qarshi sinovlar (bypass urinishlari) testlarda, lekin hech qanday tekshiruv matematik isbot emas.

## Parser tanlovi

**SQLGlot 30.33.0** (`requirements-connector.txt`, sof Python, bog'liqliksiz; ixtiyoriy, boshqa ulagich kutubxonalari kabi lazy import qilinadi: o'rnatilmagan bo'lsa `sql_validator_unavailable`, 503). Sabab: haqiqiy AST, ikkala dialekt (`postgres`, `tsql`), optimizer scope tahlili (alias, CTE, ichki so'rov, `*` kengaytirish) va qayta yozish (generator) bor. Ishga tushirishdan oldin real matnlarda tekshirildi: `SELECT ... INTO`, `FOR UPDATE`, `COPY`, `EXEC`, `OPENROWSET`/`OPENQUERY`, to'rt qismli nomlar, `WITH (UPDLOCK)`, `OPTION`, `DECLARE`/`@@version`, `WITH d AS (DELETE ... RETURNING *)`, ko'p ifoda, `TOP`/`OFFSET FETCH` to'g'ri daraxt beradi.

Cheklovlar: SQLGlot barcha PostgreSQL/T-SQL sintaksisini bilmaydi; bilmagani `Command` yoki xato bo'ladi va **rad etiladi** (masalan `EXPLAIN ANALYZE`). Parser ayrim T-SQL/PostgreSQL xususiyatlarini boshqacha o'qishi mumkin: shuning uchun **server asl matnni emas, daraxtdan qayta yozilgan matnni bajaradi** (pastga qarang). Parser versiyasi qat'iy qotirilgan; yangilash testlarni qayta yurgizishni talab qiladi.

## Tekshiruv (rag/connector/sqlcheck.py, sof funksiya)

1. **O'qish:** bo'sh matn (`empty_sql`), NUL (`invalid_characters`), `RAG_SQL_INPUT_MAX_CHARS` dan uzun (`sql_too_large`), sintaksis xatosi (`syntax_error`, xabarda SQL matni **yo'q**), ko'p ifoda (`multiple_statements`), ildiz `SELECT`/UNION/ichki so'rov emas (`dml_not_allowed`, `ddl_not_allowed`, `exec_not_allowed`, `session_statement_not_allowed`, `transaction_statement_not_allowed`, `copy_not_allowed`, …).
2. **Ruxsat ro'yxati (AST):** daraxtning **har bir tuguni** ma'lum o'qish tugunlaridan biri bo'lishi shart (SELECT, JOIN, WHERE, GROUP/ORDER/LIMIT/OFFSET/FETCH, CTE, ichki so'rov, UNION/INTERSECT/EXCEPT, CASE, CAST, taqqoslash va arifmetika operatorlari, oyna funksiyalari, …). Ro'yxatda yo'q tugun rad etiladi, sabab bilan:
   - `dml_not_allowed`: INSERT/UPDATE/DELETE/MERGE/RETURNING, **ichki** joyda ham (`WITH d AS (DELETE …) SELECT …`);
   - `ddl_not_allowed`, `permission_statement_not_allowed` (GRANT/REVOKE), `exec_not_allowed` (EXEC, protsedura, ochiq `Command`), `copy_not_allowed` (COPY, ATTACH, PRAGMA), `session_statement_not_allowed` (SET, USE, SHOW), `transaction_statement_not_allowed`;
   - `select_into_not_allowed`, `locking_not_allowed` (`FOR UPDATE/SHARE`), `hints_not_allowed` (jadval hintlari, `OPTION`), `variables_not_allowed` (`@x`, `@@version`, `$1`, `:a`, `?`), `table_function_not_allowed` (`generate_series`, `unnest`, `LATERAL`, `CROSS APPLY`, `OPENROWSET`), `cross_database_not_allowed` (to'rt va uch qismli nomlar), `recursive_cte_not_allowed`, `type_not_allowed` (foydalanuvchi turlari), boshqasi `unsupported_construct`.
3. **Funksiya siyosati — ruxsat ro'yxati:** faqat ma'lum, zararsiz funksiyalar (agregatlar, oyna, matn, son, sana, NULL bilan ishlash, `CAST`/`CONVERT`) ruxsat etilgan (`ALLOWED_FUNCTIONS` — SQLGlot sinflari, `ALLOWED_ANONYMOUS` — dialektdagi nomlar). **Ro'yxatda yo'q hamma funksiya rad etiladi** (`function_not_allowed`); `pg_`, `lo_`, `dblink`, `xp_`, `sp_`, `open…`, `fn_` kabilar `dangerous_function` deb ajratiladi (`pg_read_file`, `lo_import`, `pg_sleep`, `set_config`, `nextval`, `xp_cmdshell`, `OPENQUERY`, …). Funksiya SELECT ro'yxatida, WHERE da, ichki so'rovda, CTE da yoki ORDER BY da yashirilgan bo'lsa ham topiladi. Sxema bilan to'ldirilgan funksiya nomi (`pg_catalog.pg_read_file`) rad etiladi. Katta qiymat yasovchi funksiyalar (`repeat`, `lpad`, `rpad`, `generate_series`) ro'yxatda yo'q.
4. **Hajm:** daraxt `RAG_SQL_VALIDATION_MAX_NODES` (3000) tugundan va `RAG_SQL_VALIDATION_MAX_DEPTH` (150) chuqurlikdan oshmasligi kerak, aks holda `too_complex` (Python rekursiya xatosi ham shunday ushlanadi).
5. **`*` va butun qator:** `*` faqat SELECT ro'yxatida (tanlov ko'rsatadigan ustunlarga kengaytiriladi) va `COUNT(*)` da ruxsat etilgan; boshqa har qanday joyda (`concat(t.*)`, `CAST(t.* AS text)`, `count(t.*)`) `star_not_allowed`. Jadval yoki alias nomining o'zi (`SELECT t`, `t::text`, `array_agg(t)`) PostgreSQL da **butun qatorni**, yashirilgan ustunlar bilan birga, beradi: rad etiladi (`unknown_column`). Jadvalsiz qolgan nom faqat natija ustuniga murojaat bo'lsa (so'rovning o'z `ORDER BY`/`DISTINCT ON` elementi) qabul qilinadi. **Optimizer daraxtni qayta yozgandan keyin ruxsat ro'yxati yana bir marta tekshiriladi** (optimizer yangi tugun turlarini yaratadi, masalan butun qator uchun `TableColumn`).
6. **Tur o'lchami** 10000 dan katta bo'lsa `type_not_allowed` (`char(10000000)` qiymatni megabaytlargacha to'ldiradi).
7. **Izohlar** qayta yozishda **olib tashlanadi** (tugunlardan `comments` tozalanadi): izoh ichiga yashirish yoki izoh orqali ikkinchi ifoda kiritish ishlamaydi.

## Sxemaga mos tekshiruv

Faqat shu ulanishning, shu egasining **joriy snapshoti** va **tanlovi** ishlatiladi (M4.2 `SnapshotSource`, `SelectedCatalog`). Model yoki foydalanuvchi bergan sxema nomlari/tavsiflari ishonchli emas: tekshiruv ularni hisobga olmaydi.

- Sxema `current` bo'lmasa `context_stale` (409, `reason`: `stale`/`failed`/`in_progress`/`never_discovered`); tanlov bo'sh bo'lsa `context_empty`; o'chirilgan ulanishda `connection_disabled`; ulanish boshqa baza yoki dvigatelga yo'naltirilgan bo'lsa (`target_changed`) sxema eskirgan hisoblanadi.
- **Jadval/ko'rinish:** snapshotda bo'lishi va **tanlangan** bo'lishi shart: `unknown_table` (mavjud emas, jumladan `pg_catalog.*`, `information_schema.*`), `table_not_selected` (bor, lekin tanlanmagan), `ambiguous_table`. Nom bo'yicha topish: PostgreSQL da qo'shtirnoqsiz nom kichik harfga keltiriladi, qo'shtirnoqli aniq solishtiriladi; SQL Server da harf registriga qaramaydi.
- **Ustun:** faqat tanlov **ko'rsatadigan** ustunlar mavjud hisoblanadi (yashirilgan ustun so'rov uchun yo'q): `unknown_column`, `ambiguous_column`. Alias, CTE, ichki so'rov, UNION, `JOIN` ichidagi ustunlar scope tahlili bilan hal qilinadi; `*` va `t.*` **tanlov ko'rsatadigan ustunlarga kengaytiriladi**, shuning uchun `SELECT *` yashirilgan ustunni qaytara olmaydi.
- **JOIN:** har bir JOIN da shart (ON/USING) bo'lishi kerak (`join_without_condition`: CROSS JOIN, vergul bilan ro'yxat, shartsiz JOIN rad etiladi), NATURAL JOIN rad etiladi. Shart sxemadagi ma'lum tashqi kalitga asoslanmasa — **ogohlantirish** (`join_not_on_known_relationship`), xato emas.
- Tekshiruv butun sxemani emas, **so'rovda ishlatilgan** obyektlarni snapshotdan oladi, shuning uchun katta sxema "to'liqsiz kontekst" bilan tekshirilmaydi (M4.3 dagi o'lcham byudjeti bu yerda ishlatilmaydi); javobda tekshiruv nimaga qarab qilingani yoziladi (`context.scope`).
- Aniq baholab bo'lmaydigan konstruksiya **rad etiladi** (`unsupported_construct`, `unresolved_reference`), "o'tdi" deb belgilanmaydi.

## Bajariladigan matn (kanonik matn)

Bajariladigan SQL — foydalanuvchi yozgan matn emas, **tekshiruv natijasi**: daraxt qayta yoziladi. Har bir jadval haqiqiy `"schema"."jadval"` nomi bilan, haqiqiy ustunlar haqiqiy nomi bilan, hamma nomlar qo'shtirnoq/kvadrat qavsda, `*` ustunlar ro'yxatiga kengaytirilgan, izohlar yo'q, satr chegarasi qo'shilgan. Server shu matnni tahlil qiladi: izohlar, g'alati qo'shtirnoq, parser farqlari orqali narsa o'tkazib bo'lmaydi. Matn qayta tekshirilsa o'zgarmaydi (testlangan).

Qator chegarasi: oddiy `SELECT` ga `LIMIT n+1` (SQL Server da `TOP n+1`) qo'shiladi (foydalanuvchining o'zi kichikroq yozgan bo'lsa, o'sha qoladi): server `n+1` qatorda to'xtaydi (bittasi ortiqcha — kesilganini bilish uchun). UNION kabilarda drayver o'qishni chegarada to'xtatadi.

## Bajarish va bazadagi himoya

`POST …/sql/execute` **mustaqil** operatsiya, har safar boshidan tekshiradi (oldingi tekshiruvga ishonmaydi, mijozning "tekshirilgan" da'vosi qabul qilinmaydi):

1. egalik (404), ulanish yoqilgan, hisob ma'lumoti ochiladi, manzil qayta baholanadi (SSRF siyosati), bir ulanishda bir amal (`busy`, 409), umumiy bajarishlar soni chegarasi (429);
2. **joriy** sxema, tanlov va maqsad (`target_changed`), SQL ni qayta tahlil (`sql_rejected`, 422, `details` xatolar bilan);
3. ixtiyoriy `expected` (`snapshot_fingerprint`, `selection_revision`, `target`): sahifa tekshiruvda ko'rgan qiymatlardan biri o'zgargan bo'lsa `context_changed` (409). Bu **ruxsatnoma emas**, faqat "ko'rganimdan farq qilsa rad et" so'rovi; `target` — `provider/host/port/baza/foydalanuvchi` dan olingan qisqa xesh (host o'zgarishi ham ushlanadi);
4. **xavfsizlik hodisasi bajarishdan OLDIN yoziladi** (`sql_execution_started`); yozib bo'lmasa — bajarilmaydi (503 `security_log_unavailable`);
5. ulanish qatori bajarishdan oldin **qayta o'qiladi**: tayyorgarlik paytida (manzil aniqlanayotganda) o'chirilgan bo'lsa `connection_disabled`, o'zgartirilgan bo'lsa `context_changed`, o'chirib tashlangan bo'lsa `not_found`;
6. drayver `run` (faqat `Service.execute_sql` chaqiradi, AST testi tekshiradi).

**Bazadagi himoya (har biri mustaqil):**

| | PostgreSQL | SQL Server |
|---|---|---|
| Hisob | **har bajarishda, shu ulanishda** tekshiriladi (pastdagi ro'yxat); aniq "yoza olmaydi" javobi bo'lmasa (`NULL`, bo'sh natija ham) hech narsa bajarilmaydi: `account_can_write` (409) | xuddi shunday; tekshiruv **ruxsat ro'yxati** bilan: o'qish ekani ma'lum bo'lmagan har qanday huquq yozish hisoblanadi |
| Sessiya | `default_transaction_read_only=on` va `READ ONLY` tranzaksiya; hisob tekshiruvidan **oldin**: `statement_timeout`, `lock_timeout`, `search_path = pg_catalog`, `standard_conforming_strings = on` (hisobning o'z sozlamasi tekshiruvni aldamaydi va kanonik matndagi literal chegarasini siljitmaydi) | sessiya darajasida "faqat o'qish" **yo'q**: himoya — hisob. `ROLLBACK` faqat qo'shimcha to'r, u hamma narsani qaytarmaydi (sequence qiymati, serverdan tashqariga chiqadigan amal); `LOCK_TIMEOUT` |
| Vaqt | `SET LOCAL statement_timeout` (hisob tekshiruviga ham), taymer orqali bekor qilish so'rovi (`cancel`), batchlar orasida muddat tekshiruvi | soket vaqt chegarasi (har bir kutish uchun) va butun bajarish uchun soketni yopadigan taymer; har qatorda muddat tekshiruvi |
| Qatorlar | server tomonidagi kursor, 20 qatorlik batchlar; chegarada to'xtash | qatorma-qator o'qish; chegarada ulanish uziladi (o'qilmagan qatorlar kutilmaydi) |
| Bekor qilish | mijoz uzilsa `cancel_safe` + ulanish yopiladi | mijoz uzilsa soket yopiladi |

Natijada ulanish, kursor va tranzaksiya doim yopiladi (testlarda har bajarishdan keyin serverda `rag-connector` sessiyasi qolmaganligi tekshirilgan).

**Nima "yozadigan hisob" hisoblanadi.**

- *PostgreSQL* — hisob a'zo bo'lgan **har bir rol** bo'yicha (bevosita, bilvosita, `NOINHERIT` orqali ham): `superuser`/`createdb`/`createrole`/`replication`; `pg_write_all_data`, `pg_write_server_files`, `pg_read_server_files`, `pg_execute_server_program`, `pg_signal_backend`, `pg_maintain`, `pg_checkpoint`, `pg_create_subscription`; bazaga, foydalanuvchi sxemasiga, jadval/ko'rinish/sequence yoki funksiyaga **egalik**; bazada yoki sxemada `CREATE`; jadval, ko'rinish yoki tashqi jadvalda `INSERT/UPDATE/DELETE/TRUNCATE` (**bitta ustunga** berilgan bo'lsa ham); sequence da `USAGE`/`UPDATE`. `TEMPORARY` (standart holda hammada bor) hisobga olinmaydi.
- *SQL Server* — yozadigan server va baza rollari; server, baza, sxema, obyekt va **ustun** darajasida o'qish ro'yxatidan tashqaridagi har qanday huquq; hisobga yoki u a'zo bo'lgan rolga berilgan o'qishdan boshqa har qanday `GRANT` (`IMPERSONATE`, `CREATE SYNONYM`, trigger, tur, sequence, `ALTER ANY LOGIN` …). Fiksed rollarning bo'sh sxemalari (`db_datareader` va h.k.) hisobga olinmaydi.

Bu tekshiruv qat'iy: shubhali holatda hisob rad etiladi. Ulanish sinovi (`test`) ham xuddi shu tekshiruvdan foydalanadi.

**Hisob talablari (muhim).** Himoyaning asosi — ulanish hisobining **faqat o'qish** huquqli bo'lishi: ulagich **o'zi hech qachon huquqni o'zgartirmaydi** va hisob yoza olsa bajarishni **rad etadi** (jimgina pasaytirmaydi). Tavsiya: PostgreSQL da `GRANT CONNECT`, `GRANT USAGE ON SCHEMA …`, `GRANT SELECT ON ALL TABLES IN SCHEMA …` bilan alohida rol (docs/connector.md dagi misol); SQL Server da `db_datareader` a'zosi alohida login. Tanlanmagan obyektlarni bazada ham yopish uchun (ixtiyoriy, kuchliroq) hisobga faqat kerakli jadvallarga `SELECT` bering. Yashirilgan ustun uchun eng kuchli himoya ham bazada: hisobga ustun darajasida `SELECT` bering.

## Resurs chegaralari (server tomonida)

| O'zgaruvchi | Standart | Ma'nosi |
|---|---|---|
| `RAG_SQL_INPUT_MAX_CHARS` | 20000 | tahlil qilinadigan SQL uzunligi |
| `RAG_SQL_VALIDATION_MAX_NODES` / `_MAX_DEPTH` | 3000 / 150 | daraxt tugunlari / chuqurligi |
| `RAG_SQL_EXEC_TIMEOUT_S` | 30 | bitta so'rov, birinchi ifodadan oxirgi qatorgacha |
| `RAG_SQL_EXEC_CONNECT_TIMEOUT_S` | 10 | ulanish olish |
| `RAG_SQL_EXEC_MAX_ROWS` / `_HARD` | 1000 / 10000 | qaytariladigan qatorlar (so'rovda `max_rows` bilan kamaytirish mumkin, oshirib bo'lmaydi) |
| `RAG_SQL_EXEC_MAX_BYTES` | 1 MB | qaytariladigan qiymatlar hajmi (JSON matn) |
| `RAG_SQL_EXEC_MAX_CELL_CHARS` | 2000 | bitta qiymat uzunligi (uzuni kesiladi) |
| `RAG_SQL_EXEC_MAX_CONCURRENT` | 2 | bir vaqtdagi bajarishlar (ortig'iga 429 `busy`) |

Kesilgan natija **har doim** aytiladi: `truncated: true`, `truncated_by` (`rows`/`bytes`), `warnings` da `result_truncated`, sahifada alohida ogohlantirish. Cheksiz natija hech qachon qaytmaydi. So'rov tanasi 256 KB gacha (M4.2 middleware).

Qiymatlar JSON-xavfsiz ko'rinishga keltiriladi: katta butun sonlar matn, Decimal va sana/vaqt matn, ikkilik qiymat hex boshlanishi, `NaN`/`Infinity` matn.

## API

Sessiya shart (tokensiz 401), egasi — sessiya principali, javob `Cache-Control: no-store`, xatolar `{"error": {"code", "reason"?, "details"?}}`.

**`POST /api/v1/connections/{id}/sql/validate`** `{"sql": "…"}` → 200:
`status` (`valid`/`invalid`), `execution_allowed`, `provider`, `dialect`, `connection_id`, `errors[]` (`code`, `message`, …), `warnings[]`, `normalized_sql` (aynan shu matn bajariladi; xato bo'lsa `null`), `referenced` (`tables[]` — sxema, nom, ishlatilgan ustunlar; `functions[]`), `context` (`state`, `snapshot_version`, `snapshot_fingerprint`, `selection_revision`, `target`, `scope`, `selected_objects`), `limits` (`max_rows`, `timeout_s`, `max_result_bytes`, `max_cell_chars`), `validation` (`passed`/`failed`), `executed: false`. Hech narsa bajarilmaydi va tashqi bazaga **murojaat qilinmaydi**. `execution_allowed` — shu daqiqa haqidagi fakt, ruxsat emas.

**`POST /api/v1/connections/{id}/sql/execute`** `{"sql": "…", "max_rows"?: n, "expected"?: {"snapshot_fingerprint"?, "selection_revision"?, "target"?}}` → 200: `status: "executed"`, `request_id`, `executed_sql`, `columns[]` (`name`, `type`), `rows[][]`, `row_count`, `truncated`, `truncated_by`, `warnings[]`, `referenced`, `context`, `limits`, `validation: "passed"`, `duration_ms`.

| Kod | HTTP | Ma'nosi |
|---|---|---|
| `unauthenticated` | 401 | sessiya yo'q |
| `not_found` | 404 | ulanish yo'q yoki boshqa egasiniki |
| `invalid_request` | 422 | tana noto'g'ri (faqat maydon nomlari) |
| `request_too_large` | 413 | tana 256 KB dan katta |
| `connection_disabled` / `context_stale` / `context_empty` | 409 | ulanish o'chirilgan / sxema joriy emas (`reason`) / hech narsa tanlanmagan |
| `context_changed` | 409 | `expected` dagi qiymat o'zgargan |
| `sql_rejected` | 422 | tekshiruvdan o'tmadi; `reason` — birinchi xato kodi, `details` — hamma xatolar |
| `account_can_write` | 409 | hisob yoza oladi: faqat o'qish hisobi kerak |
| `execution_timeout` | 504 | vaqt chegarasi, so'rov to'xtatildi |
| `execution_cancelled` | 499 | mijoz uzildi, so'rov to'xtatildi |
| `permission_denied` | 403 | hisobda obyektga SELECT huquqi yo'q |
| `query_failed` | 422 | baza so'rovni qabul qilmadi (nom, tur, qiymat, bo'lish nolga …); bazaning matni berilmaydi |
| `execution_failed` | 502 | `reason`: `auth_failed`, `unreachable`, `tls_failed`, `failed`, … |
| `busy` | 409/429 | shu ulanishda amal ketyapti / bajarishlar chegarasi |
| `security_log_unavailable` | 503 | xavfsizlik jurnaliga yozib bo'lmadi: bajarilmadi |
| `sql_validator_unavailable` | 503 | SQLGlot o'rnatilmagan |

Hisob ma'lumoti, ulanish satri, stek yo'li va bazaning xato matni javobga hech qachon tushmaydi.

## Audit va jurnal

Xavfsizlik hodisalari (qat'iy so'zlar, `principal_id` va `connection_id` bilan, **SQL matni, qiymat, parol yo'q**): `sql_validated` (`ok`/`rejected`, jurnalga yozilmasa ham tekshiruv ishlaydi; **rad etilgan bajarish urinishi** ham `rejected` bo'lib yoziladi; tekshiruvgacha to'xtagan so'rov — eskirgan sxema, o'chirilgan ulanish — hodisa yozmaydi), `sql_execution_started` (**bajarishdan oldin**; yozilmasa bajarilmaydi), `sql_executed` (`ok`, `truncated`, `timeout`, `cancelled`, `account_can_write`, `permission_denied`, `query_error`, `failed`). Oddiy jurnalga: `request_id`, `connection_id`, qatorlar soni, kesilganmi, davomiylik va **SQL ning qisqa SHA-256 izi** (matni emas). Qaytarilgan ma'lumot va sxema jurnalga yozilmaydi.

**So'rovlar tarixi saqlanmaydi** (migratsiya yo'q): saqlash natija ma'lumotlarini ichki bazaga ko'chirish va ularning egaligi/saqlash muddati masalasini ochadi; bu M4.5 (so'rov auditi) qaroriga qoldirilgan. Hodisa "kim, qaysi ulanishda, qachon, qanday natija bilan" savoliga javob beradi, lekin qaysi SQL ekanini saqlamaydi.

## Sahifa

M4.3 taklifi ostida: holat qatori (**taklif → tekshirildi/rad etildi → bajarildi**), **Tekshirish** tugmasi, tekshiruv natijasi (xatolar va ogohlantirishlar so'z bilan, "aynan shu matn bajariladi", ishlatilgan jadval va ustunlar, chegaralar, sxema versiyasi), va faqat muvaffaqiyatli tekshiruvdan keyin ko'rinadigan **Bajarish**. Bajarish faqat bosilganda yuboriladi; xato (eskirgan sxema, o'zgargan ulanish, vaqt, bekor qilish, yozadigan hisob, …) bo'lsa oldingi tekshiruv bekor bo'ladi va qayta tekshirish kerak. Natija chegaralangan jadval (`textContent`, `NULL` alohida), kesilgan bo'lsa ogohlantirish. Yangi taklif yoki boshqa ulanish tanlansa tekshiruv va natija tozalanadi.

## Cheklovlar va ochiq xavflar

- **M4.4 ixtiyoriy SQL ga qarshi xavfsiz deb da'vo qilinmaydi.** Validator isbot emas: xavfsizlik auditi testlari o'tib turgan kodda ikkita kritik nuqson topgan (butun qator orqali yashirilgan ustun; `standard_conforming_strings=off` orqali inyeksiya). Tashqi bazada **alohida, faqat `SELECT` huquqli hisob majburiy**; nozik ma'lumot uchun eng kuchli himoya bazaning o'zida: hisobga faqat kerakli jadval va ustunlarga `SELECT` bering.
- Ruxsat etilgan funksiyalar ham resurs sarflashi mumkin (regex, katta yig'indi, `string_agg`/`array_agg`, `replace`/`concat` ni ketma-ket CTE larda takrorlab qiymatni eksponensial o'stirish, `ON 1=1` kabi shartli og'ir JOIN); himoya vaqt/qator/hajm chegaralari va `statement_timeout`/soket vaqti (qator chegarasi bazaning ichki hisoblashini cheklamaydi; UNION kabilarda chegarani drayver qo'yadi). Bazadagi boshqa sessiyalarga ta'siri (qulflar) `lock_timeout` bilan cheklangan, lekin nol emas.
- **SQL Server da sessiya darajasida "faqat o'qish" yo'q**: himoya — hisobning huquqlari (har bajarishda tekshiriladi). `ROLLBACK` yetarli himoya **emas**. Tekshiruv `public` ga ochiq tizim protseduralarini (`EXEC` ni validator rad etadi) va **boshqa bazalardagi** huquqlarni qamramaydi; maxsus sozlamalar (masalan trigger yoki imzolangan protseduradagi kutilmagan huquq) uni chetlab o'tishi mumkin: faqat o'qish hisobini ishlating.
- PostgreSQL da funksiyalarga `EXECUTE` odatda hammaga ochiq (hisob bilan yopib bo'lmaydi): bu yerda faqat AST ruxsat ro'yxati himoya qiladi (xavfli funksiyalar rad etiladi). Bu `SECURITY DEFINER` funksiyalarga ham tegishli. `TEMPORARY` huquqi hisob tekshiruvida hisobga olinmaydi. Ko'rinish (view) ichidagi mantiq tekshirilmaydi: ko'rinish o'z egasining huquqi bilan boshqa obyektlarni o'qishi va funksiya chaqirishi mumkin.
- Sinalmagan: PostgreSQL 14/16, SQL Server 2019, Azure SQL va bulutdagi boshqariladigan bazalar; u yerda qat'iy hisob tekshiruvi oddiy o'quvchi hisobni ham rad etishi mumkin.
- Tanlanmagan obyektlarni bazada yopish uchun hisob huquqlarini toraytirish kerak (ixtiyoriy); ulagich faqat so'rov matnida tanlovni majburlaydi.
- Parser farqi: SQLGlot va server bir narsani har xil o'qishi mumkin; kanonik matn buni kamaytiradi, lekin yo'q qilmaydi.
- **Bitta juda katta qiymat** (baza chegarasigacha, PostgreSQL da 1 GB) kesilishidan oldin ilova xotirasiga to'liq o'qiladi; PostgreSQL da bir vaqtda 20 tagacha qator. Vaqt chegarasi buni kamaytiradi, yo'q qilmaydi.
- Ulanishni o'zgartirish/o'chirish bajarish boshlanishidan oldingi qayta o'qishdan keyingi juda qisqa oraliqqa to'g'ri kelsa, boshlangan bajarish eski sozlama bilan tugaydi.
- Ortiqcha qat'iylik (xavfsiz, lekin rad etiladi): `nvarchar(max)`; SQL Server da natija ustuni nomlari kichik harfda qaytadi.
- Katta natija brauzerda ko'rsatiladi (≤ 1000 qator, ≤ 1 MB); eksport, sahifalash, kesh, tarix, so'rovni tahrirlash, avtomatik tuzatish yo'q.
- Host/port o'zgarishi, provayder va baza nomi bir xil qolsa, sxema "eskirgan" bo'lmaydi (M4.2 cheklovi); validate→execute oralig'ida `expected.target` buni ushlaydi, lekin sahifadan tashqari mijoz uni yubormasa — yo'q.
- Bir ulanishda bir vaqtda bitta bajarish (qolganlari `busy`).

## Testlar

`tests/unit/test_sqlcheck.py` (qabul qilinadigan va rad etiladigan konstruksiyalar, funksiya ro'yxati, kanonik matn, bypass urinishlari, hajm, fuzz), `tests/db/test_connector_sqlexec_service.py` (sxema, egalik, qayta tekshirish, `expected`, audit, parallellik), `tests/db/test_connector_sqlexec_api.py` (HTTP), `tests/integration/test_sql_execution.py` (haqiqiy PostgreSQL 15/17 va SQL Server 2022: qatorlar, chegaralar, vaqt, bekor qilish, yozishni bazaning o'zi rad etishi, yozadigan hisob rad etilishi, sessiyalar yopilishi), `tests/e2e/test_ui_connections.py` (sahifa), `tests/unit/test_connector_boundaries.py` (`run` faqat `execute_sql` dan chaqiriladi, validator sof).
