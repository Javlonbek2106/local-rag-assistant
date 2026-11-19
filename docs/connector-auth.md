# Ulagich autentifikatsiyasi (M4.1a poydevori)

Manba: [architecture-roadmap.md](architecture-roadmap.md) 6a-bo'lim, [architecture-decisions.md](architecture-decisions.md) ADR-P16. Bu hujjat nima **qurilganini** va qanday ishlatilishini tasvirlaydi. Ulagichning o'zi (tashqi bazalarga ulanish, SQL, hisob ma'lumotlari ombori) **hali yo'q**: bu M4.1b va keyingi bosqichlar.

## Qisqacha

- **Standart o'chiq.** `RAG_CONNECTOR_ENABLED` berilmasa yoki `1`/`true` dan boshqa bo'lsa, hech narsa o'zgarmaydi: hujjat va savol endpointlari, `/readyz`, `/api/status` avvalgidek (ADR-007, D-1, D-15). Yagona qo'shimcha: har doim `200` beradigan `GET /api/v1/connector` (`{"enabled": false}`); sahifa shu bilan ulagich yoqilganini biladi, 404 va konsol xatosisiz.
- **Yoqilganda** faqat yangi `/api/v1/session*` va `/api/v1/connector/status` marshrutlari sessiya talab qiladi. Mavjud endpointlar token talab qilmaydi va ularga yangi sarlavha qo'shilmaydi.
- Yoqish uchun: `set RAG_CONNECTOR_ENABLED=1` (start.bat dan oldin yoki muhitda).

## Yoqilganda ishga tushish shartlari

Ishga tushishda va har `/readyz`/`/api/status` da (≤60 s kesh bilan) 7 ta shart tekshiriladi. Birortasi bajarilmasa **ilova ishlayveradi**, lekin ulagich marshrutlari `503 connector_unavailable` beradi va `/readyz` `not_ready` (503) deydi:

| Shart | Ma'nosi |
|---|---|
| `principal_ok` | `principals` jadvalida aynan bitta `local-owner`, o'chirilmagan |
| `db_role_unprivileged` | ilova ulanayotgan PostgreSQL roli superuser emas; `CREATEROLE`, `CREATEDB`, `REPLICATION`, `BYPASSRLS` yo'q; fayl o'qish/yozish, dastur ishga tushirish, "hammasini o'qish/yozish" rollarida a'zo emas |
| `db_runtime_permissions` | ilova `rag_app` sifatida ishlayapti (migrator yoki bootstrap emas); sxemada ob'ekt yarata olmaydi, hech narsaga ega emas, `schema_migrations` ni o'zgartira olmaydi. Uch rolli rejimsiz (`RAG_DB_HARDENED`) bajarilmaydi |
| `db_password_not_default` | uch parol fayli (`db_*_password`) ma'lum standart emas, kamida 22 belgi, bir-biridan farqli (bir rolli rejimda: `RAG_DATABASE_URL` dagi parol). Ilova kuchini **kafolatlay olmaydi**, faqat qo'pol tekshiradi |
| `secrets_dir_private` | sirlar katalogi loyiha tashqarisida va faqat shu foydalanuvchiga ochiq |
| `key_location_ok` | kelajakdagi shifrlash kaliti fayli (agar bor bo'lsa) shunday katalogda |
| `security_log_writable` | xavfsizlik jurnaliga yozish mumkin |

Eski dev bazada (`rag/rag`, superuser) `db_role_unprivileged`, `db_runtime_permissions` va `db_password_not_default` bajarilmaydi, ya'ni ulagich ishlamaydi. Bu ataylab: [postgres-hardening.md](postgres-hardening.md) bo'yicha bazani ko'chiring.

## Joylar (loyihadan tashqarida)

`%LOCALAPPDATA%\local-rag-assistant\` (Linux: `~/.local/share/local-rag-assistant/`): `secrets\` va `logs\security\events.log`. O'zgartirish: `RAG_STATE_DIR`, `RAG_SECRETS_DIR`, `RAG_SECURITY_LOG_DIR`. Loyiha papkasi ichidagi yo'l rad etiladi. Katalog ilova tomonidan yaratiladi va faqat joriy foydalanuvchi (Windows: shu foydalanuvchi, SYSTEM, Administrators) kira oladigan qilib tekshiriladi. Kafolatlab bo'lmasa (masalan WSL2 da `/mnt/c/...`) ulagich ishlamaydi (fail-closed).

## Kirish: bir martalik kod

1. Sahifadagi **Ulagich** bo'limida «Kod so'rash».
2. 8 xonali kod **faqat ilovani ishga tushirgan konsol oynasida** (start.bat) chiqadi: javobda, jurnalda, faylda yoki manzilda emas.
3. Kodni 60 soniya ichida kiriting. U **bir marta** ishlaydi: har urinish (to'g'ri yoki noto'g'ri) kutayotgan kodni sarflaydi. Daqiqasiga 5 urinish, undan keyin 30 soniya kutish; kodlar orasi kamida 10 soniya.
4. Sessiya: mutlaq **12 daqiqa**, bo'sh turish **2 daqiqa**, step-up **10 daqiqa** (sessiya tugashidan oshmaydi). Faqat server xotirasida; ilova qayta ishga tushsa hamma sessiya yo'qoladi. Brauzerda token faqat `sessionStorage` da (shu tab); `localStorage`, cookie va manzilda emas.

## Marshrutlar (`/api/v1`)

| Marshrut | Sessiya | Vazifa |
|---|---|---|
| `GET /connector` | yo'q | ulagich yoqilganmi |
| `POST /session/pairing` | yo'q | kod so'rash (kod konsolga) |
| `POST /session/exchange` | yo'q | kodni sessiyaga almashtirish |
| `GET /session` | ha | holat (bo'sh turishni **uzaytirmaydi**) |
| `POST /session/step-up` | ha | yangi kod bilan qayta tasdiq, token almashadi |
| `POST /session/rotate` | ha | token almashtirish (tugash vaqti o'zgarmaydi) |
| `DELETE /session`, `DELETE /sessions` | ha | joriy / hamma sessiyani bekor qilish |
| `GET /connector/status` | ha | shartlar holati (faqat true/false) |

Hamma xato `{"error": {"code": ...}}`; noto'g'ri, eskirgan va ishlatilgan kod bir xil `401 pairing_invalid`.

## Xavfsizlik hodisalari

`events.log` ga faqat ruxsat etilgan hodisalar yoziladi (kod yozish, almashtirish, sessiya yaratish/aylantirish/bekor qilish, step-up, tayyorlik o'zgarishi, ishga tushish tekshiruvi; PostgreSQL sozlash va parol almashtirish: [postgres-hardening.md](postgres-hardening.md)), har biri qat'iy sabab kodi bilan, matnsiz. Parol, token, kod, kalit, SQL va istisno matni yozilmaydi. 5 MiB da aylantiriladi, 5 ta eski fayl saqlanadi (muddat bo'yicha saqlash hali hal qilinmagan). Yozib bo'lmasa sessiya **berilmaydi** va ulagich "tayyor emas" bo'ladi.

## Hali qilinmagan

- PostgreSQL qattiqlashtirish **qilindi** (D-7): [postgres-hardening.md](postgres-hardening.md). Ochiq chegaralar o'sha hujjat oxirida.
- Ulanishlar, hisob ma'lumotlari ombori va step-up ni hisob ma'lumotiga qo'llash **qilindi** (M4.1b): [connector.md](connector.md). SQL yaratish va bajarish keyingi bosqichlarda.
- Birinchi administrator da'vosi oqimi (faqat sxema shartlari bor: `principals.user_id`, `claimed_at`, noyob indeks).
- Brauzersiz mijozlar uchun alohida kirish usuli (D-16, ochiq); pairing kodi curl bilan ham ishlaydi.
