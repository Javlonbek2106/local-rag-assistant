# PostgreSQL qattiqlashtirish (M4.1a, 7-qadam)

Manba: [architecture-roadmap.md](architecture-roadmap.md) 6a-bo'lim (D-7, D-8, D-14), [connector-auth.md](connector-auth.md). Kod: [rag/dbroles.py](../rag/dbroles.py), [rag/dbadmin.py](../rag/dbadmin.py), [rag/db.py](../rag/db.py), [docker-compose.yml](../docker-compose.yml), [docker/postgres/pg_hba.conf](../docker/postgres/pg_hba.conf).

## Qisqacha

- Ilova endi **uchta** PostgreSQL roli bilan ishlaydi; har birining **o'z paroli** bor, parollar **loyiha tashqarisidagi** shaxsiy katalogda fayl bo'lib turadi. Parol kodda, repoda, Docker image'da, komanda qatorida, `docker inspect` da, URL da va jurnallarda **yo'q**.
- Eski `rag/rag` (superuser, paroli `rag`) **yopiladi**. Mavjud ma'lumot saqlanadi: jadvallarning faqat egasi o'zgaradi.
- Ulagich (`RAG_CONNECTOR_ENABLED=1`) shu uch rolli tuzilmasiz **ishlamaydi** (fail-closed). Ulagich o'chiq bo'lsa ham start.bat shu tuzilmani sozlaydi.

## Rollar

| Rol | Vazifa | Huquqlari | Qayerdan ulanadi |
|---|---|---|---|
| `rag_bootstrap` | birinchi sozlash va tuzatish | superuser | **faqat konteyner ichidan** (`docker exec`); tarmoq orqali `pg_hba.conf` rad etadi |
| `rag_migrator` | sxema migratsiyalari | bazaning va jadvallarning egasi (DDL); superuser/CREATEDB/CREATEROLE/REPLICATION/BYPASSRLS yo'q | ilova faqat `db.migrate()` ichida, kutilayotgan migratsiya bo'lsagina, so'ng darrov uzadi |
| `rag_app` | ishlayotgan ilova | faqat quyidagi jadval huquqlari; DDL, temp jadval, rol a'zoligi yo'q | API va worker (pul) |

`rag_app` huquqlari ([RUNTIME_GRANTS](../rag/dbroles.py)): `documents`, `document_versions`: SELECT/INSERT/UPDATE/DELETE; `principals`: faqat SELECT; `schema_migrations`: faqat SELECT. Ro'yxatda yo'q jadval **hech narsa** olmaydi; yangi migratsiya jadval qo'shsa, shu ro'yxatga qator qo'shish shart (aks holda `test_every_table_the_migrations_create_has_its_runtime_grant_decided` yiqiladi).

## Sirlar

Katalog: `%LOCALAPPDATA%\local-rag-assistant\secrets\` (Linux: `~/.local/share/local-rag-assistant/secrets/`; `RAG_SECRETS_DIR` bilan o'zgartiriladi). Fayllar: `db_bootstrap_password`, `db_migrator_password`, `db_app_password`: har biri `secrets.token_urlsafe(32)` (256 bit), bir-biridan farqli. Katalog faqat joriy foydalanuvchiga (Windows: + SYSTEM, Administrators) ochiq; boshqacha bo'lsa ilova uni **o'qimaydi**.

- `python -m rag db-secrets`: yo'q fayllarni yaratadi (mavjudiga tegmaydi) va katalog nomini chiqaradi. Parolni **hech qachon** chiqarmaydi.
- Docker: `docker-compose.yml` faqat `db_bootstrap_password` ni Docker secret sifatida (`POSTGRES_PASSWORD_FILE`) beradi; muhit o'zgaruvchisida parol yo'q. Kompozitsiya `RAG_SECRETS_DIR` ni talab qiladi (`start.bat` o'zi qo'yadi; Linux: `export RAG_SECRETS_DIR="$(python -m rag db-secrets)"`).
- Parol serverga **ochiq matn bilan bormaydi**: SCRAM-SHA-256 verifier hisoblanib, `ALTER ROLE … PASSWORD 'SCRAM-SHA-256$…'` yuboriladi. SQL `psql` ga **stdin** orqali beriladi (komanda qatorida emas).
- Ilova parolni har yangi ulanishda fayldan qayta o'qiydi, shuning uchun aylantirishdan keyin qayta ishga tushirish shart emas.
- `.gitignore`: `.env`, `.env.*`, `*.secret`, `secrets/`.

## Birinchi ishga tushirish (yangi o'rnatish)

`start.bat` hammasini o'zi qiladi: `db-secrets` → `docker compose up -d --wait` → `db-check` (bajarilmasa `db-setup`) → ilova. Qo'lda:

```powershell
$env:RAG_DB_HARDENED = "1"
$env:RAG_SECRETS_DIR = (.\.venv\Scripts\python.exe -m rag db-secrets)
docker compose up -d --wait
.\.venv\Scripts\python.exe -m rag db-setup
```

`db-setup` bir necha marta ishlatilsa ham xavfsiz (idempotent): rollarni yaratadi/tuzatadi (atributlar, a'zoliklar, parol), baza va sxema egasini `rag_migrator` ga beradi, `PUBLIC` huquqlarini oladi, migratsiyalarni `rag_migrator` bilan bajaradi va `rag_app` huquqlarini qo'yadi, so'ng tekshiradi.

## Mavjud dev bazani ko'chirish (eski `rag/rag`)

Ma'lumot o'chmaydi, destruktiv migratsiya yo'q. Eski hajmda superuser `rag` bor; `db-setup` uni topadi va:

1. `rag_bootstrap`, `rag_migrator`, `rag_app` rollarini yaratadi;
2. jadvallar, baza va sxema egasini `rag_migrator` ga o'tkazadi (qatorlar o'zgarmaydi);
3. eski `rag` ni `NOLOGIN` qiladi va parolini o'chiradi (u PostgreSQL ning "bootstrap" foydalanuvchisi, shuning uchun o'chirib bo'lmaydi, lekin kirib bo'lmaydi);
4. kutilayotgan migratsiyalarni (masalan `0005_principals`) `rag_migrator` bilan qo'llaydi.

Tartib:

```powershell
.\.venv\Scripts\python.exe -m rag backup                  # 1. zaxira (SHART emas, lekin qattiq tavsiya)
$env:RAG_DB_HARDENED = "1"
$env:RAG_SECRETS_DIR = (.\.venv\Scripts\python.exe -m rag db-secrets)
docker compose up -d --wait                               # 2. yangi compose bilan (hajm saqlanadi)
.\.venv\Scripts\python.exe -m rag db-setup                # 3. eski baza topilsa: ogohlantiradi va HECH NARSA O'ZGARTIRMAYDI (chiqish kodi 3)
.\.venv\Scripts\python.exe -m rag db-setup --adopt-legacy # 4. ogohlantirishni o'qib, qaror qilgach
```

Muhim:

- **2-qadamdan keyin** (konteyner yangi `pg_hba.conf` bilan qayta yaratilishi bilan) `postgresql://rag:rag@...` orqali tarmoqdan ulanish **darrov** rad etiladi: bu 3-qadamdan oldin ham sodir bo'ladi. Shu paytgacha `rag/rag` ishlatadigan hamma narsa (skriptlar, qo'lda testlar, boshqa vositalar) ishlamay qoladi.
- `db-setup` eski bazani **faqat `--adopt-legacy` bilan** o'zgartiradi. Bu bayroq "zaxira oldim; `rag` kirishi abadiy yopilishini va jadval egasi o'zgarishini bilaman" degani. `start.bat` bu bayroqni **o'zi qo'ymaydi**: eski baza topilsa to'xtab, nima qilishni aytadi.
- Server tomondagi o'zgarishlar **bitta tranzaksiya**: xato bo'lsa server avvalgi holatda qoladi; qaytadan ishga tushirish xavfsiz.
- Destruktiv buyruq yo'q: `db-setup`, `db-rotate`, `db-check`, `db-secrets` hech qachon qator, jadval yoki hajmni o'chirmaydi. (`docker compose down -v` hajmni o'chiradi: uni haqiqiy loyihada ishlatmang; testlar uni faqat o'zlarining vaqtincha loyihalarida ishlatadi.)


## Migratsiya hayot tsikli

- Kim migratsiya qiladi: API, worker (`rag.worker.setup`), `ingest` va `db-setup`; hammasi `db.migrate()` ni chaqiradi.
- **Tekshir-keyin-migratsiya:** `rag_app` faqat `schema_migrations` ni o'qiydi. Hamma migratsiya yozilgan va `rag_app` huquqlari joyida bo'lsa, hech narsa qilinmaydi va `rag_migrator` **ulanmaydi**.
- Kutilayotgan narsa bo'lsa, `rag_migrator` ulanadi, `pg_advisory_lock(727001)` oladi (bazada bitta), qolganini qayta o'qiydi, qo'llaydi, `rag_app` huquqlarini qayta o'rnatadi (shu qulf ichida) va uziladi. API va worker bir vaqtda ishga tushsa: birinchisi bajaradi, ikkinchisi qulfni kutib, hammasi tayyor ekanini ko'radi (8 oqim va 4 jarayon bilan sinalgan).
- `rag_migrator` paroli yo'q bo'lsa `migrate()` **xato beradi**; boshqa rolga o'tmaydi.
- Ilova ishlayotganda hech qachon DDL bajarilmaydi: `rag_app` ga huquq berilmagan.
- Eslatma: migratsiya yangi jadval qo'shsa va `RUNTIME_GRANTS` da qatori bo'lmasa, `rag_app` unga kira olmaydi va har ishga tushishda migrator yo'li ishlaydi (zararsiz, lekin sekinroq). Test buni oldindan ushlaydi.

## Ishga tushish va tayyorlik tekshiruvlari

`python -m rag db-check` (va `db-setup` oxirida): `app_can_log_in`, `migrator_can_log_in`, `bootstrap_closed_over_network`, `legacy_rag_closed`, `migrator_role_limited` (migratorda superuser/CREATEROLE/... yo'q), `only_bootstrap_is_a_login_superuser`, `app_role_unprivileged`, `app_has_no_ddl`. Bularning tarmoq kirish urinishlari serverda `FATAL` qatori sifatida ko'rinadi: bu kutilgan.

Ulagich yoqilganda `/readyz` yana 7 ta shartni tekshiradi ([connector-auth.md](connector-auth.md)): xususan `db_runtime_permissions` (ilova `rag_app` sifatida ishlayapti; sxema o'zgartira olmaydi, hech narsaga ega emas, `schema_migrations` ni o'zgartira olmaydi) va `db_password_not_default` (uch parol fayli kuchli va farqli). Bittasi bajarilmasa, ulagich **o'chiq**, ilova esa ishlayveradi.

## Xavfsizlik jurnali

`events.log` ([connector-auth.md](connector-auth.md)) ga quyidagilar yoziladi (faqat qat'iy ro'yxatdagi nom va sabab; parol, verifier, yo'l va matn yo'q): `secrets_created`, `db_setup` (`ok`/`failed`), `legacy_role_closed`, `credential_rotated` / `credential_rotation_failed` (sabab: `app`, `migrator` yoki `bootstrap`). Ulagich tomonidagi hodisalar (kod, sessiya, tayyorlik o'zgarishi, ishga tushish tekshiruvi) avvalgidek.

**Jurnalga yozib bo'lmasa kritik amal jim davom etmaydi:** parol fayllarini yaratish, `db-setup` va `db-rotate` jurnal katalogi yaroqsiz yoki yozilmaydigan bo'lsa **boshlanmaydi** (hech narsa o'zgarmaydi). Amal bajarilib, keyin yozuv yozilmasa, buyruq xato bilan tugaydi ("o'zgarish bajarildi, lekin jurnalga yozilmadi"): jim muvaffaqiyat yo'q.

## Parolni aylantirish

```powershell
.\.venv\Scripts\python.exe -m rag db-rotate --role app          # yoki migrator, bootstrap
```

Yangi parol avval `*.new` faylga yoziladi, server qabul qilgandan **keyin** asosiy fayl almashadi; server rad etsa eski fayl va eski parol birga qoladi. `app` va `migrator` uchun qayta ishga tushirish kerak emas (yangi ulanish yangi parolni o'qiydi; ochiq ulanishlar tugaguncha davom etadi, eski parol esa yangi ulanishlarni endi ochmaydi). `bootstrap` paroli faqat konteyner ichida mavjud; almashtirgandan keyin compose'dagi secret fayli o'zgargani uchun konteynerni qayta yaratish ma'nosiz (parol bazada saqlanadi).

## Tiklash (parol yo'qolsa yoki bir-biriga mos kelmasa)

- Parol fayli o'chib ketdi / server paroli o'zgardi: `python -m rag db-rotate --role <rol>` (konteyner ichidagi superuser orqali, parol so'ramaydi) yoki `db-setup` (fayldagi parolni serverga qayta o'rnatadi).
- `rag_app` kira olmayapti: `db-check` qaysi biri ekanini aytadi; `db-setup` takrorlansin.
- Katalog huquqlari buzildi: ilova faylni o'qimaydi va ulagich o'chadi; huquqlarni tuzating (faqat joriy foydalanuvchi) yoki katalogni o'chirib `db-secrets`, `db-setup` ni qayta ishlating (yangi parollar; eski ma'lumot saqlanadi).
- Docker o'chib qolsa: `docker compose up -d --wait` va `db-check`.

## Zaxira va tiklash ta'siri

- `rag backup` hardened rejimda `rag_app` bilan, konteyner ichida mahalliy soket orqali `pg_dump` qiladi (`--no-owner --no-acl`), parol komanda qatorida yo'q. Manifest va hisobotlarda parol yo'q.
- Zaxira **rollarni va parollarni o'z ichiga olmaydi** (`--no-owner --no-acl`): ular `db-secrets`/`db-setup` bilan qayta yaratiladi. Sirlar katalogini alohida, shifrlangan joyda saqlang; usiz tiklangan bazaga ulanib bo'lmaydi (lekin `db-rotate` bilan yangisi beriladi).
- `rag restore` **bo'sh, alohida** muhitga tiklaydi. Uch rolli muhitga tiklash tartibi (sinalgan: `tests/integration/test_pg_hardening_lifecycle.py` va `python scripts/restore_drill.py --hardened`):
  1. yangi bo'sh serverda **o'zining** sirlari bilan: `db-secrets`, so'ng `db-setup --roles-only` (rollar va huquqlar, jadvalsiz);
  2. `python -m rag restore ... --database-url <rag_migrator URL>`: tiklash `rag_migrator` bilan bo'sh bazaga (jadvallar uning egaligida bo'ladi);
  3. `db-setup`: migratsiya tarixini tekshiradi va `rag_app` huquqlarini qo'yadi, so'ng tekshiradi.
  Zaxira rollarni va parollarni **tiklamaydi** (`--no-owner --no-acl`): har muhit o'z parollariga ega.

## Ulagichni yoqish

1. Yuqoridagi birinchi ishga tushirish/ko'chirish tugagan, `db-check` hammasi `ok`.
2. `set RAG_CONNECTOR_ENABLED=1`, `start.bat`.
3. Sahifada **Ulagich** → kod so'rash (kod konsolda). `GET /readyz` da `connector: ok` bo'lishi kerak; aks holda `GET /api/v1/connector/status` (sessiya bilan) qaysi shart bajarilmaganini ko'rsatadi.

## Xavfsizlik qarorlari va chegaralar (ochiq aytilgan)

- **Konteyner ichidagi `local … trust`:** `docker exec` bilan kirgan kishi superuser bo'la oladi. Bu Docker daemon egasi bilan teng: u secret faylni ham o'qiy oladi. Parol bu yerda hech narsa qo'shmaydi, shuning uchun qo'yilmagan. Chegara: Docker administratori = ishonchli.
- **`rag_migrator` ilova jarayonida mavjud:** parol fayli ilovaning o'z foydalanuvchisiga o'qiladi. Ilova kodi buzilsa, migrator parolini o'qishi mumkin; himoya: (a) fayl faqat shu foydalanuvchiga ochiq, (b) ilova uni faqat `migrate()` ichida va faqat kutilayotgan migratsiya bo'lsa ishlatadi, (c) ulagich `rag_app` ni tekshiradi. Migratsiyani alohida buyruqqa ko'chirish ("faqat `db-setup` migratsiya qiladi") foydalanuvchi talabiga (ilova migratsiyani shu rol bilan bajaradi) zid bo'lgani uchun qilinmadi.
- **Eski `rag` superuser bo'lib qoladi (kira olmaydi):** PostgreSQL "bootstrap" foydalanuvchisining SUPERUSER atributini olib tashlashga ruxsat bermaydi (`The bootstrap superuser must have the SUPERUSER attribute`, tekshirilgan). Shuning uchun `rag` `NOLOGIN` va parolsiz, lekin superuser atributi bilan qoladi. Unga faqat boshqa superuser (`rag_bootstrap`, konteyner ichidan) `SET ROLE` qila oladi, ya'ni yangi imkon bermaydi. `db-check` (`only_bootstrap_is_a_login_superuser`) kirish mumkin bo'lgan boshqa superuser yo'qligini tekshiradi. To'liq yo'qotish faqat yangi hajm yaratib, ma'lumotni `rag backup`/`rag restore` bilan ko'chirish orqali mumkin.
- **Tarmoq paroli URL siz:** `RAG_DATABASE_URL` hardened rejimda **ishlatilmaydi** (`RAG_DB_HOST/PORT/NAME` va sir fayllari). Orqaga moslik: `RAG_DB_HARDENED` berilmasa eski bir rolli rejim o'zgarishsiz ishlaydi (CI va testlar shunday), lekin ulagich bunda `db_runtime_permissions` bajarilmagani uchun yoqilmaydi.
- **Testlar:** `RAG_TEST_HARDENED=1` bilan `tests/db` to'plami `rag_app` puli orqali ishlaydi (superuser URL va `RAG_SECRETS_DIR` ko'rsatiladi); o'z sxemasini yoki rollarini yaratadigan testlar (`test_pg_security`, `test_principals`, `test_db`, `test_versions` va `test_job_progress` dagi eski-ma'lumot testlari) faqat bir rolli rejimda ishlaydi. PostgreSQL testlari superuser kerak qiladi; hardened bazada superuser tarmoqdan kirmaydi. Testlar uchun alohida vaqtincha server ishlating: `docker run --rm -d -p 127.0.0.1:55433:5432 -e POSTGRES_PASSWORD=<tasodifiy> postgres:17-alpine` va `RAG_TEST_DATABASE_URL=postgresql://postgres:<parol>@127.0.0.1:55433/postgres`.
