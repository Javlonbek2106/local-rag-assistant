# Yuklash hajmi chegarasi: erta rad etish (T0.5)

Manba: audit API-02, ADR-P10 (1-bosqich), yo'l xaritasi T0.5. Kod: [rag/bodylimit.py](../rag/bodylimit.py), ulanishi [rag/api.py](../rag/api.py) (`create_app`), sozlama [rag/config.py](../rag/config.py).

## 1. Muammo

Starlette ko'p qismli (multipart) tanani handler chaqirilishidan **oldin** to'liq o'qib, vaqtinchalik fayllarga yozadi. Hajm chegarasi esa faqat handler ichida, fayl allaqachon yozib bo'lingandan keyin tekshirilardi. Audit E5: 1 MB chegarada 150 MB yuborilganda vaqtinchalik papka 139 MB ga o'sdi.

Qayta o'lchov (shu ish boshida, **eski xatti-harakat**, 1 MB chegara, 150 MB tana): vaqtinchalik papka **149,4 MB** (Content-Length bor) va **149,7 MB** (chunked) ga o'sdi, javob 0,8–1,9 s da 200 (fayl keyin handlerda rad etilgan).

## 2. Sozlama

| | Qiymat | Ma'nosi |
|---|---|---|
| `RAG_MAX_UPLOAD_MB` (`config.MAX_UPLOAD_BYTES`) | 200 MB (o'zgarmagan) | **bitta fayl** chegarasi; handlerdagi mavjud tekshiruv o'zgarmagan |
| `config.UPLOAD_FRAMING_BYTES` | 1 MiB (doimiy) | ko'p qismli chegaralar va qism sarlavhalari uchun zaxira |
| So'rov chegarasi = yuqoridagi ikkisining yig'indisi | 201 MB | **bitta yuklash so'rovining** jami tanasi |

Yagona manba `MAX_UPLOAD_BYTES`; so'rov chegarasi undan hisoblanadi va har so'rovda o'qiladi (testlar `monkeypatch` qila oladi). Zaxira shart: aynan ruxsat etilgan hajmdagi fayl ko'p qismli ramka bilan birga `MAX_UPLOAD_BYTES` dan biroz katta tana bo'ladi; zaxirasiz u noto'g'ri 413 olardi va mavjud xatti-harakat ("Fayl ... MB dan katta", `accepted: false`) buzilardi.

## 3. Qanday ishlaydi

Sof ASGI middleware (`UploadBodyLimit`), faqat `POST /api/documents` uchun; boshqa endpointlarga tegmaydi. U `guard` (Host/Origin/CSP) middleware'idan **ichkarida** turadi: avval xavfsizlik tekshiruvi, keyin hajm.

| Holat | Xatti-harakat |
|---|---|
| `Content-Length` chegaradan katta | tana o'qilmasdan darhol **413**; ilova ishga tushmaydi, `receive()` chaqirilmaydi, bitta bayt yozilmaydi |
| `Expect: 100-continue` bilan chegaradan katta | server "100 Continue" yubormaydi, mijoz tanani umuman yubormaydi |
| **`Content-Length` yo'q (chunked)** | tana o'qilmasdan darhol **411 Length Required**; ilova ishga tushmaydi, bitta bayt yozilmaydi; `Expect: 100-continue` bilan mijoz tanani umuman yubormaydi |
| Sarlavhadan ko'p bayt keladi (boshqa ASGI server uzatsa) | sarlavhaga ishonilmaydi, haqiqiy baytlar sanaladi, chegaradan o'tgan zahoti **413** (middleware darajasida va haqiqiy ko'p qismli tahlilchi bilan isbotlangan) |
| `Content-Length` noto'g'ri (`abc`, `-5`, `1e3`, `1 2`, `+5`, 18 xonadan uzun, ikki xil qiymat) | **400**, ilova ishga tushmaydi |
| Chegaraga teng yoki kichik | o'zgarishsiz o'tadi |

**413 javobi** API'ning boshqa xatolari bilan bir xil shaklda: `{"detail": "Yuklash hajmi chegaradan katta: bitta so'rovda eng ko'pi 200 MB"}`; server yo'li, sozlamasi yoki ichki ma'lumot yo'q.

**411 javobi** xuddi shu shaklda: `{"detail": "Content-Length sarlavhasi kerak: so'rov hajmi oldindan ma'lum bo'lishi shart"}`. Sababi: hajmi e'lon qilinmagan tana chegaradan oshganini faqat chegara hajmidagi baytlar allaqachon vaqtinchalik faylga yozilgandan keyin bilish mumkin (o'lchandi: standart chegarada 250 MB chunked tana rad etilishidan oldin ~195 MB yozilardi). Brauzerlar `FormData` yuklashida `Content-Length` ni doim yuboradi (Edge 157 da tekshirildi: 1 KB, 3 fayl/27 MB va 150 MB fayl; `Transfer-Encoding` yo'q).

Sahifa har fayl uchun shu matnlarni fayl nomi bilan ko'rsatadi ("katta.pdf: Yuklash hajmi chegaradan katta ...").

**Ulanish yopilmaydi** (`Connection: close` yo'q). O'lchandi: `Connection: close` bilan tanani yuborishda davom etayotgan mijoz (brauzer) TCP reset oladi va 413 ni umuman ko'rmaydi; yopmasdan server darhol javob beradi va qolgan tanani o'qib **tashlab yuboradi** (150 MB ≈ 0,13 s), hech narsa saqlanmaydi va xotirada yig'ilmaydi. Muvozanat: rad etilgan tana tarmoq orqali baribir o'qiladi (diskka yozilmaydi).

**Ko'p qismli va tozalash.** Starlette tahlil xato bilan to'xtasa, ochilgan vaqtinchalik fayllarni o'zi yopadi va o'chiradi; 413 ham shu yo'ldan o'tadi. Testlar ochilgan va yopilgan fayllar sonining tengligini, papka bo'shligini va `storage/.uploading` bo'shligini tekshiradi. Mijoz yuklash o'rtasida uzilsa ham shunday.

**Parallel so'rovlar.** Hisob har so'rovning o'z yopilishida; bir so'rovning rad etilishi boshqasiga ta'sir qilmaydi (test: 3 ta katta va 2 ta kichik bir vaqtda: 413, 413, 413, 200, 200).

## 4. O'lchov (izolyatsiyalangan haqiqiy server jarayoni, o'z TEMP papkasi bilan)

Audit stsenariysi, 1 MB chegara, 150 MB tana; mijoz tanani oxirigacha yuboradi (brauzer kabi):

| | Content-Length | Holat | Mijoz yubordi | Javob vaqti | TEMP o'sishi (cho'qqi) | Keyin qolgan fayl |
|---|---|---|---|---|---|---|
| **Oldin** | bor (157 286 536) | 200 | 150 MB | 1,94 s | **149,4 MB** | 0 |
| **Oldin** | yo'q (chunked) | 200 | 150 MB | 0,92 s | **149,7 MB** | 0 |
| **Keyin** | bor | **413** | 150 MB (server o'qib tashladi) | **0,09 s** | **0,0 MB** | 0 |
| **Keyin** | yo'q (chunked) | **413** | 150 MB (server o'qib tashladi) | **0,14 s** | **1,2 MB** | 0 |
| **Keyin** | bor; mijoz javobni ko'rgach to'xtaydi | **413** | 0,5 MB | 0,00 s | **0,0 MB** | 0 |

Standart 200 MB chegarada 250 MB (Content-Length bor): **413**, 0,2 s, TEMP o'sishi 0,0 MB.

**Standart konfiguratsiya (200 MB), `Content-Length`siz 250 MB chunked ko'p qismli tana** (haqiqiy uvicorn, o'z TEMP papkasi, mijoz tanani oxirigacha yuborishda davom etadi):

| | Holat | Javob vaqti | Mijoz yuborgan (javob paytida) | TEMP maksimal | TEMP yakuniy | Qolgan fayl | Server xotirasi |
|---|---|---|---|---|---|---|---|
| 411 dan oldingi kod (sanash orqali 413) | 413 | 1,07 / 0,98 s | 201,9 MB | **197,1 / 194,7 MB** | 0 MB | 0 | 128 → 130 MB |
| **Hozir (411)** | **411** | **0,002 s** | 0,8 / 0,9 MB | **0,0 MB** | **0 MB** | **0** | 128 MB (o'zgarmagan) |

`storage/` va `storage/.uploading` ham o'zgarmagan; chegara ichidagi 150 MB chunked tana ham 411 (0,005 s, TEMP 0). Chunked tana uchun avvalgi 1,2 MB (1 MB chegarada) va ~195 MB (200 MB chegarada) vaqtinchalik yozuv endi yo'q.

Chegara ichidagi katta yuklash (180 MB, 200 MB chegarada) qabul qilindi (200); TEMP cho'qqisi 180 MB (4-bo'limdagi cheklovga qarang).

## 5. Testlar

[tests/unit/test_upload_limit.py](../tests/unit/test_upload_limit.py): 55 test, uch qavatda. 411 siyosati tufayli "chunked qabul qilinadi" deb kodlangan 7 ta test yangi siyosatga moslashtirildi (chunked → 411 va nol yozuv; sanash yo'li "e'lon qilinganidan uzun tana" orqali sinaladi), 10 ta yangi test qo'shildi.
- **Middleware o'zi:** kichik / chegaraga teng / `MAX+zaxira` ga teng / undan 1 bayt ortiq; Content-Length yo'q → 411 va `receive()` hech qachon chaqirilmaydi; yo'q va bo'sh so'rov; e'lon qilinganidan uzun tana; boshqa yo'llar `Content-Length`siz ham o'tadi; 10 xil noto'g'ri qiymat va ikki xil qiymat; boshqa yo'llar (`/api/ask`, `GET`, `DELETE`, rollback, `/healthz`) cheklanmaydi; bir vaqtdagi mustaqil so'rovlar.
- **To'liq ilova (test mijozi):** odatiy va ko'p fayllik yuklash; 413 shakli va hech narsa yozilmasligi; xavfsizlik sarlavhalari va Host/Origin 403 birinchi; aynan ruxsat etilgan hajmdagi fayl qabul qilinadi, 1 bayt ortig'i handler xabarini oladi; so'rov chegarasiga teng/+1 bayt; boshqa endpointlar.
- **Haqiqiy uvicorn + xom soket mijozi:** audit 150 MB stsenariysi; `Expect: 100-continue`; brauzer kabi davom etuvchi mijoz; chunked katta va kichik (hammasi 411, TEMP va storage o'zgarmaydi, mijoz davom etsa ham javob oladi); `Expect: 100-continue` + chunked (tana yuborilmaydi); boshqa endpointlar chunked tana bilan ishlaydi; noto'g'ri sarlavha; "kam deb yolg'on aytgan" sarlavha; ko'p qismli rad etishdan keyin vaqtinchalik fayl qolmasligi; yuklash o'rtasida uzilish; parallel yuklashlar; rad etishdan keyin server boshqa so'rovlarga xizmat qilishda davom etadi. Disk yozuvi ikki usulda o'lchanadi: vaqtinchalik papka hajmi va Starlette tahlilchisi yozgan baytlar soni (Linux'da ochiq vaqtinchalik fayl papkada ko'rinmaydi, shuning uchun ikkinchisi OT'ga bog'liq emas).
- Mutatsiya tekshiruvi: 11 ta backend kafolati buzildi (411 o'tkazib yuboriladi, 411 dan oldin tana o'qiladi, erta 413, sanash, zaxira, noto'g'ri qiymat, ikki qiymat, boshqa endpoint, `Connection: close`, o'rnatilmagan, tartib), **11/11 tutildi**.

## 6. Qaror talab qiladigan xatti-harakat o'zgarishi

Avval bitta so'rovdagi **jami** hajm cheklanmagan edi (har fayl ≤ 200 MB bo'lsa yetarli), endi bitta so'rovning jami tanasi ≤ 201 MB va `Content-Length` shart. Qaror (foydalanuvchi tasdiqlagan): **sahifa har faylni alohida, ketma-ket so'rov bilan yuboradi**, shuning uchun 3 ta 100 MB fayl muammosiz yuklanadi (har biri o'z 201 MB chegarasi ichida; o'lchangan: 3 × 100 MB ketma-ket, har biri 200, diskdagi cho'qqi 200 MB, bitta so'rovdagi 600 MB o'rniga). Backend shartnomasi o'zgarmadi: `files` ro'yxati hamon bir nechta faylni qabul qiladi, lekin **jami tana** ≤ 201 MB va `Content-Length` bilan bo'lishi kerak. API mijozlari (masalan .NET) katta to'plamlarni fayl-fayl yuborishi kerak; `Content-Length`siz (chunked) yuklash 411 oladi.

## 7. Ma'lum cheklovlar

- **Chegara ichidagi yuklash baribir to'liq spool qilinadi** (Starlette handlerdan oldin diskka yozadi), so'ng handler uni `storage/.uploading` ga ikkinchi marta yozadi. 180 MB yuklash ~180 MB vaqtinchalik joy oladi. Buni faqat oqimli ko'p qismli tahlil (fayl to'g'ridan-to'g'ri joyiga) hal qiladi: roadmap T1.3.
- **Chunked mijozlar ishlamaydi:** `Content-Length`siz yuborilgan tana (masalan, oqim generatoridan, `curl -T`, yoki mijoz kutubxonasida chunked yoqilgan bo'lsa) 411 oladi. Brauzer (Edge, o'lchandi), `curl -F`, `requests`/`httpx` `files=` va .NET `MultipartFormDataContent` odatda uzunlikni yuboradi (bu mijozlar bu yerda o'lchanmagan, faqat Edge). Chunked yuklash kerak bo'lsa, u T1.3 (oqimli tahlil) bilan, o'zining chegarasi bilan qayta ko'rib chiqiladi.
- Server o'zi `Content-Length` da ko'rsatilgan baytdan keyingi ma'lumotni so'rov deb hisoblamaydi: "kam deb yolg'on aytgan" tana real serverda 413 emas, tugallanmagan ko'p qismli so'rov sifatida **422** oladi (hech narsa saqlanmaydi, o'lchandi). 413 mantig'i boshqa ASGI server uchun middleware darajasida sinalgan.
- Rad etilgan tananing qolgani tarmoq orqali o'qib tashlanadi (diskka/xotiraga emas); bir mijoz uchun cheklov yo'q (ilova faqat loopback).
- Boshqa endpointlarning tanasi cheklanmagan (masalan, `/api/ask` ning JSON tanasi xotiraga to'liq o'qiladi, so'ng 2000 belgidan uzun savol 422 oladi). Bu vazifa doirasida emas.
- Yuklash paytida Windows'da yopilgan ulanish ba'zi mijozlarda reset ko'rinishida bo'lishi mumkin, shuning uchun ulanish yopilmaydi (3-bo'lim).
