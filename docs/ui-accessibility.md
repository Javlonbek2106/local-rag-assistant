# UI: fokusni saqlash va foydalanish imkoniyati (T0.6)

Manba: audit API-04, ADR-013, yo'l xaritasi T0.6. Kod: [static/app.js](../static/app.js), [static/index.html](../static/index.html), [static/app.css](../static/app.css). Dizayn va foydalanuvchi oqimi o'zgarmagan (vanilla JS, polling, `textContent`, qat'iy CSP).

## 1. Muammo (koddan tekshirildi)

- `renderDocs` har yangilashda (8 s, indekslash paytida 1,5 s) `list.replaceChildren()` bilan **butun ro'yxatni** qayta yaratardi. Klaviatura foydalanuvchisi ro'yxatdagi tugmada turgan bo'lsa, fokus `BODY` ga tushardi (audit: 10 s kutishda tasdiqlangan). Ochiq versiyalar paneli ham har safar qayta yaratilib, "Yuklanmoqda…" bilan miltillardi va ikki bosishli tasdiqlash (`Aniqmi?`) holati yo'qolardi.
- Savol maydonining o'zi ro'yxat qayta chizilganda almashtirilmasdi (u ro'yxatdan tashqarida); lekin javob tugagach `$("question").focus()` **shartsiz** chaqirilardi, ya'ni foydalanuvchi javob yozilayotganda boshqa joyga o'tgan bo'lsa, fokus majburan qaytarilardi.
- Savol maydonida kirish nomi yo'q (faqat `placeholder`), xato paneli `role="alert"` emas, holat o'zgarishlari e'lon qilinmaydi, tugmalar hujjat nomisiz ("O'chirish" × N), taraqqiyot chizig'ida nom yo'q.

## 2. Fokusni saqlash

**Asosiy yechim: DOM joyida yangilanadi, qayta chizilmaydi.** Har hujjat uchun bitta qator (`views` xaritasi, nom bo'yicha) bir marta yaratiladi va keyin yangilanadi:

- matn, klass va atributlar faqat **haqiqatan o'zgargan bo'lsa** yoziladi (`setText/setClass/setAttr`): bir xil javob uch marta kelsa, `#docs` da **nol** DOM o'zgarishi bo'ladi (testda MutationObserver bilan isbotlangan);
- ixtiyoriy qismlar (taraqqiyot chizig'i, yangilanish qatori, xato qatori, versiyalar paneli) kerak bo'lsa qo'shiladi, kerak bo'lmasa olib tashlanadi, qolgan tugunlar o'z joyida qoladi;
- faqat o'z o'rnida bo'lmagan qator ko'chiriladi (ko'chirilgan tugun fokusni yo'qotadi); yangi qator boshqalarni ko'chirmasdan kiritiladi;
- versiyalar paneli ham joyida: versiya raqami bo'yicha qatorlar saqlanadi, "Yuklanmoqda…" faqat birinchi yuklashda; tasdiqlash tugmalari (`Aniqmi?`) endi yangilanishda tiklanmaydi va 3 s dan keyin o'zi qaytadi (avval har poll uni bekor qilib yuborardi).

**Ikkinchi qatlam (xavfsizlik to'ri): `withFocusKept(update)`.** Yangilashdan oldin fokuslangan elementning `data-focus-key` i (va matn tanlovi) eslab qolinadi; yangilashdan keyin fokus yo'qolgan bo'lsa (tugun almashtirilgan, `activeElement` = `BODY`), shu kalitli tugun topilib fokus va tanlov tiklanadi. Foydalanuvchi fokusni ataylab boshqa joyga o'tkazgan bo'lsa, **hech qachon qaytarilmaydi**. Kalitlar: `doc:<nom>:versions`, `doc:<nom>:delete`, `ver:<nom>#<versiya>`.

**Javobdan keyingi fokus.** Savol maydoniga fokus faqat (a) yo'qolgan bo'lsa (foydalanuvchi "Yuborish" ni bosgan va u javob paytida o'chirilgan bo'lsa) yoki (b) savol maydonida qolgan bo'lsa qaytariladi. Boshqa boshqaruv elementiga o'tgan bo'lsa, tanlov hurmat qilinadi.

**Versiyalar paneli va sarlavha bir xil holatni ko'rsatadi.** Panel o'z `/versions` so'rovi bilan yangilanadi, sarlavha esa ro'yxat so'rovi bilan: ikki alohida javob. Haqiqiy E2E testi (`test_versions_are_visible…`, to'liq to'plamning ~8 tadan 2 tasida) rollbackdan keyin sarlavha "Tayyor · v1" ga o'tgani, panel esa o'z javobini kutib hamon eski holatni ko'rsatayotgan ~100 ms oralig'ini tutdi. Ikki o'zgarish:
- **Rollbackdan keyin avval panel, keyin ro'yxat:** muvaffaqiyatli rollbackdan so'ng panel darhol qayta so'raladi va faqat u yangilangach `refreshDocs()` chaqiriladi, shuning uchun sarlavha paneldan oldin yangilanmaydi.
- **So'rovlar raqamlanadi:** har `/versions` so'rovi raqam oladi va javob faqat panelda ko'rsatilganidan yangi bo'lsa qo'llanadi (`view.asked` / `view.shown`). Eski so'rovning kech kelgan javobi (poll rollbackdan sal oldin so'ralgan bo'lsa, undan keyingi so'rovdan keyin kelishi mumkin) yoki eski xato paneldan yangi holatni qaytarib olmaydi. Panel yopilib qayta ochilsa, yopishdan oldin so'ralganlar yangi panelga kirmaydi. Kutish yoki qo'shimcha polling qo'shilmadi.

Cheklov: bu rollback tomonini yopadi. Worker yangi versiyani faollashtirganda sarlavha (ro'yxat javobi) va panel (versiyalar javobi) hamon ikki alohida so'rov bo'lib qoladi va ular orasida qisqa farq bo'lishi mumkin; bu o'zgartirilmagan.

Savol maydoni: polling uni umuman o'zgartirmaydi, shuning uchun uning matni, kareti va fokusi saqlanadi (testda 3 majburiy yangilash va kamida 2 haqiqiy polling bilan tekshirilgan).

## 3. Accessibility

| Element | O'zgarish |
|---|---|
| Savol maydoni | haqiqiy `<label for="question">Savolingiz</label>` (ko'rinmas, `.sr-only`) + `aria-describedby` yordam matni ("Enter — yuborish, Shift+Enter — yangi qator"); `placeholder` yagona nom emas |
| Xato (javob) | yangi `<span role="alert">Xato: ...</span>`; matn "Xato:" bilan boshlanadi (faqat rang emas); `aria-busy` xatodan **oldin** o'chiriladi, shunda darhol o'qiladi |
| Xato (aloqa bannerini, yuklash xabari, hujjat/versiya xato qatori, qaytarish xatosi) | `role="alert"` |
| Fayl yuklash | har fayl alohida so'rov bilan, ketma-ket; `#upload-status` (`role="status"`, `aria-live="polite"`): "1/3 fayl yuklanmoqda: a.pdf", "3/3 fayl yuklandi" yoki "2 ta fayl muvaffaqiyatli, 1 ta faylda xatolik"; `#upload-progress` (`aria-valuemin/max/now/text`); har xato `#upload-msg` (`role="alert"`) da **fayl nomi bilan** ("katta.pdf: Yuklash hajmi chegaradan katta ..."); fokus o'zgarmaydi |
| Savol-javob tarixi | `role="log"`, `aria-label`, `aria-live="polite"`; javob yozilayotganda `aria-busy="true"`: ekran o'quvchi har bir so'zni emas, tugagach bir marta o'qiydi |
| Holat | bitta ko'rinmas `#sr-status` (`role="status"`, `aria-live="polite"`, `aria-atomic`). Ko'rinadigan taraqqiyot matnlari live region **emas** (har sekundda o'zgaradi) |
| Taraqqiyot chizig'i | `aria-label` ("<nom>: indekslash taraqqiyoti"), `aria-valuemin=0`, `aria-valuemax=<jami>`, `aria-valuenow=<tayyor>`, `aria-valuetext` ("Embedding hisoblanmoqda: 64/100 bo'lak (64%)") |
| Tugmalar | "O'chirish: <nom>", "Versiyalar: <nom>" (`aria-expanded`), "Shu versiyani faol qilish: <nom> v<n>"; ko'rinadigan matn nomning ichida; tasdiqlash holatida "Aniqmi? ..." |
| Yuklash zonasi | `role="button"` (allaqachon `tabindex=0`, Enter/Space) |
| Belgilar | `aside` va savol bo'limi nomli, ro'yxat nomli, versiyalar paneli `role="group"` |
| Fokus ko'rinishi | global `:focus-visible { outline: 2px solid var(--accent) }`; yuklash zonasidagi `outline: none` olib tashlandi |

**E'lonlar (`#sr-status`) faqat haqiqiy o'zgarishlarda:** hujjat navbatga tushdi / ishlashni boshladi / chorak (25%) chegarasidan o'tdi / tayyor bo'ldi / xato; sahifa ochilganda butun ro'yxat o'qilmaydi; bir xil poll takroriy e'lon bermaydi (testda: bir xil javob 4 marta va bir chorak ichidagi siljish → 0 e'lon; keyingi chorak → 1; tayyor → 1).

## 4. Testlar

[tests/e2e/test_ui_focus_a11y.py](../tests/e2e/test_ui_focus_a11y.py): 17 test, haqiqiy Edge'da. Sahifaning statik fayllari kichik mahalliy serverdan, API javoblari `page.route` bilan skript qilingan (bazasiz, Ollama'siz): test serverning keyingi javobini aynan kerakli joyda o'zgartiradi. Haqiqiy ilova bilan oqimlar `test_ui.py` da qoldi. Ketma-ket yuklash: [tests/e2e/test_ui_upload_sequence.py](../tests/e2e/test_ui_upload_sequence.py) (9 test; har yuklash so'rovi sahifada ushlab turiladi: 3 fayl = 3 so'rov, bir vaqtda faqat bittasi, tanlangan tartibda; 2-fayl xatosi 3-faylni to'xtatmaydi va 1-natija saqlanadi; har xil xato turi o'z fayl nomi bilan; taraqqiyot va status; bitta fayl avvalgidek; yuklash paytida tanlangan ikkinchi to'plam navbat kutadi; fokus, karet va matn saqlanadi, foydalanuvchi o'tgan fokus qaytarilmaydi) va `test_ui.py` dagi yangi test (haqiqiy server: 3 so'rov, har biri `Content-Length` bilan, javobdan keyin keyingisi, serverda `created_at` tartibi, hammasi indekslangan). Mutatsiya tekshiruvi: 8 ta yuklash kafolati (parallel yuborish, birinchi xatoda to'xtash, fayl nomisiz xato, aria qiymati, jonli hudud, navbat, har fayldan keyin yangilash, fokusni tortib olish), **8/8 tutildi**.

1. Savol: fokus, matn va karet 3 majburiy + 2+ haqiqiy pollingdan keyin saqlanadi; yozish karet joyidan davom etadi.
2. Ro'yxatdagi fokuslangan tugma bir xil tugun bo'lib qoladi (yuqorida yangi qator, matnlar o'zgargan, qatorlar kelib-ketgan).
3. Foydalanuvchi o'tkazgan fokus (yuklash zonasi, qo'shilgan `<input>`) qaytarilmaydi; matn yo'qolmaydi.
4. Almashtirilgan tugunga fokus va tanlov tiklanadi (xavfsizlik to'ri).
5. Javobdan keyin fokus: savolda qolsa qoladi; "Yuborish" bosilsa qaytadi; boshqa joyga o'tilsa o'sha joyda qoladi.
6. Hech narsa o'zgarmasa DOM o'zgarishi nol; faqat taraqqiyot o'zgarsa element qo'shilmaydi/olib tashlanmaydi.
7. Ochiq versiyalar paneli: tugma fokusi, tugun va "Aniqmi?" holati yangilanishlardan o'tadi va 3 s da o'zi qaytadi.
8. Savol maydonining nomi, `label`, `aria-describedby`.
9. Har tugmaning nomi bor va hujjatlarni ajratadi; `aria-expanded`.
10. Belgilar va yuklash zonasi nomi.
11. Javob xatosi `role="alert"`, "Xato:" so'zi bilan.
12. Aloqa bannerini, rad etilgan yuklash va 413 xabari `role="alert"`.
13. Hujjat va versiya xato qatori `role="alert"`.
14. Taraqqiyot chizig'ining qiymatlari va nomi, yangilanishga ergashadi.
15. `#sr-status`: o'zgarmagan pollda jim, chorak o'tganda 1 e'lon, tayyor/yangi yuklashda e'lon, ochilishda jim.
16. Tab bilan barcha boshqaruv elementlariga yetiladi va har birida fokus halqasi bor.
17. Klaviatura: Enter (panel), Space (o'chirish tasdig'i), Enter (fayl tanlash), Shift+Enter (yangi qator), Enter (yuborish).

Versiyalar paneli: [tests/e2e/test_ui_versions_panel.py](../tests/e2e/test_ui_versions_panel.py) (6 test; sahifaning `/versions` va ro'yxat so'rovlari ushlab turiladi va test ularni xohlagan tartibda javoblaydi, sahifa javob bilan ishini tugatganini `window.__handled` orqali kutadi, hech qachon kechikish bilan emas): rollbackdan keyin so'rov tartibi (avval panel, keyin ro'yxat) va sarlavha v1 bo'lgan paytda panel ham v1; ushlamasdan rollback; eski javob yangisini bosmaydi (ikkala tartibda); panel yopilib qayta ochilganda eski so'rov javobi e'tiborga olinmaydi; eski xato raqamlashni orqaga qaytarmaydi. Tuzatishsiz kodda 4 tasi yiqildi, tuzatish bilan o'tadi; tuzatishning 4 ta qismi buzilganda **4/4 tutildi**.

Mutatsiya tekshiruvi: 12 ta T0.6 kafolati buzildi (ro'yxat har safar qayta chiziladi, tiklash to'ri, javobdan keyin majburiy fokus, savol yorlig'i, xato `role`, banner `role`, har pollda e'lon, status region, taraqqiyot qiymatlari, bir xil tugma nomlari, fokus halqasi, tasdiqlash tiklanishi), **12/12 tutildi**.

## 5. Ma'lum cheklovlar

- Faqat **Edge (Chromium)** da sinalgan (mavjud e2e bilan bir xil). Firefox/Safari tekshirilmagan.
- **Haqiqiy ekran o'quvchi (NVDA/JAWS/VoiceOver) bilan eshitib sinalmadi.** Semantika (rol, nom, `aria-*`, jonli hudud o'zgarishlari) brauzer DOM'i orqali tekshirilgan; ekran o'quvchilar `aria-busy` va jonli hududlarni turlicha qo'llaydi.
- Fokuslangan elementning o'zi o'chirilsa (masalan, hujjat o'chirilganda uning tugmasi), fokus `BODY` ga tushadi; qayerga qo'yish bu holatda aniq emas va o'zgartirilmagan.
- Rang kontrasti qayta baholanmadi; faqat rangga bog'liq bo'lmaslik (matn "Xato:", holat matni, taraqqiyot raqami) ta'minlandi.
- Versiyalar paneli hamon har pollda `/versions` ni so'raydi (audit API-05, P2): faqat DOM yangilash tartibi o'zgardi.
- Sichqoncha bilan tashlab yuklash (drag-and-drop) klaviatura muqobili: yuklash zonasi (Enter/Space bilan fayl tanlash).
