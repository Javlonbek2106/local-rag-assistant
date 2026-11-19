# LLM: timeout, bekor qilish va chegaralar (T0.4)

Manba: audit LLM-01, LLM-02, LLM-03, yo'l xaritasi T0.4. Kod: [rag/llm.py](../rag/llm.py), [rag/relay.py](../rag/relay.py), `ask` ([rag/api.py](../rag/api.py)).

## 1. Muammo (o'lchangan)

| Nuqson | Oqibat |
|---|---|
| `ollama` kutubxonasi: timeout yo'q | Ollama osilsa, savol cheksiz kutadi |
| Mijoz uzilganda generatsiya to'xtamaydi | Hech kimga kerak bo'lmagan javob yozilaveradi; Ollama bir vaqtda bitta javob beradi, shuning uchun keyingi savol kutadi |
| `num_predict` yo'q | Javob uzunligi cheksiz |
| `keep_alive` standart (5 daqiqa) | Biroz kutgandan keyin model xotiradan chiqadi, keyingi savol ~28 s yuklashni kutadi |

Nazorat o'lchovi (shu kompyuter, qwen2.5:7b): ulanishni **yopmasdan** tashlab ketilgan generatsiya ortida yangi kichik so'rov **50,4 s** kutdi.

## 2. Nima o'zgardi

**Ollama bilan aloqa `http.client` orqali** (yangi bog'liqlik yo'q; `ollama` kutubxonasi endi ishlatilmaydi). Sababi: tashqaridan to'xtatish uchun soketning o'ziga kirish kerak, kutubxona buni bermaydi.

### Timeoutlar: sekin va o'lik Ollama'ni ajratish

Ollama oqimli javobning sarlavhasini **birinchi token bilan birga** yuboradi (o'lchangan: sovuq model + ~3000 tokenli prompt → sarlavha va birinchi token 32 s da, birga). Shuning uchun oddiy "30 s o'qish timeouti" qonuniy so'rovni o'ldirardi. O'lchangan yana bir narsa: `/api/version` model yuklanayotganda va prompt o'qilayotganda ham 3–18 ms da javob beradi. Shu sababli kutish paytida har 5 s da Ollama'dan "tirikmisan?" deb so'raladi:

| Holat | Natija | Qachon |
|---|---|---|
| Ollama ishlamayapti (ulanish rad) | `LLMUnavailable` → **503** | darrov (Windows'da ~2 s: OS rad etishi) |
| So'rovga ham, "tirikmisan?" ga ham javob yo'q | `LLMUnavailable` → **503** | **20 s** (`RAG_OLLAMA_UNRESPONSIVE_S`) |
| Tirik, lekin birinchi token kelmayapti | `LLMTimeout` → **504** | 120 s (`RAG_LLM_FIRST_TOKEN_TIMEOUT_S`) |
| Javob boshlangan, keyin jim | `LLMTimeout`, oqimda xato qatori | 30 s (`RAG_LLM_IDLE_TIMEOUT_S`) |
| Javob tugamayapti | `LLMTimeout` | 300 s (`RAG_LLM_TOTAL_TIMEOUT_S`) |
| Ollama xato qaytardi (masalan, model yo'q) | `LLMError` → **503** | darrov, Ollama xabari bilan |
| Embedding so'rovi sekin | `LLMTimeout` | 120 s (`RAG_EMBED_TIMEOUT_S`) |

### Bekor qilish

- `llm.Scope`: bir so'rov (savol) ishlatayotgan soketlar to'plami. Boshqa oqimdan `scope.cancel()` chaqirilsa, soketlar `shutdown` qilinadi: bloklangan o'quvchi darrov uyg'onadi, ulanish yopiladi va **Ollama ulanish yopilganini ko'rishi bilan generatsiyani to'xtatadi**. Scope oqimga `bind` qilinadi, shuning uchun `answer.retrieve` / `answer.stream_answer` imzolari o'zgarmagan.
- `Relay` (rag/relay.py): qidiruv va javob yozishni alohida oqimda bajaradi (FastAPI'ning umumiy oqimlar to'plamida emas: u yerda bloklangan oqimni to'xtatib bo'lmaydi).
- `/api/ask`: birinchi tokengacha mijozni tinglovchi vazifa (`request.receive()`) ishlaydi; oqim boshlangach Starlette o'zi uzilishni aniqlaydi va oqimni bekor qiladi. Ikkala holatda ham `relay.cancel()` chaqiriladi.
- **Tozalash javob obyektining o'zida** (`OwnedStream`), generatorning `finally` ida emas. Mijoz uzilganda javobni yuborayotgan vazifa bekor qilinadi, generator esa `yield` da to'xtab qoladi va Python uni faqat axlat yig'ilganda yopadi; bu kechikishi mumkin. Shu sabab to'liq test to'plamining ~5% ida (har 15–20 ishga tushirishda bir marta) birinchi so'rovning generatsiyasi to'xtamay qolgan: stek izida so'rov vazifalari yo'q edi, lekin uning relay oqimi hamon Ollama'dan o'qirdi. Bekor qilish endi `__call__` ning o'z `try/finally` ida bajariladi. Deterministik test (axlat yig'uvchi o'chiq, generator yopilmaydi) eski xatti-harakatda yiqiladi.
- Starlette'ning `request.is_disconnected()` birinchi token fazasida **ishlamadi** (uvicorn soketni o'qishni davom ettirmaydi); shuning uchun doimiy `receive()` kutuvchisi ishlatilgan. Bu real server testi bilan topilgan.

### Chiqish va xotira

- `num_predict` = 512 (`RAG_LLM_NUM_PREDICT`). Javob shu chegaraga yetib kesilsa, oxirgi qatorda `"truncated": true` bo'ladi va sahifa "Javob uzunlik chegarasiga yetdi" deb yozadi. Kesilmasa kalit yo'q (eski javob formati o'zgarmagan).
- `keep_alive` = 30 daqiqa (`RAG_OLLAMA_KEEP_ALIVE`; `-1` cheksiz) javob va embedding so'rovlarida.
- Veb-server yoqilganda orqa fonda ikkala model ham yuklanadi (`RAG_WARMUP=0` bilan o'chiriladi). Xato bo'lsa faqat jurnalga yoziladi.

## 3. API xatolari

Birinchi tokengacha bo'lgan xato — haqiqiy HTTP status:

```json
HTTP 504  {"detail": "Javob yozilmadi: Ollama 120 soniyada javob bermadi (model juda sekin ishlayapti)", "code": "llm_timeout"}
HTTP 503  {"detail": "Javob yozilmadi: Ollama 20 soniyadan beri hech qanday javob bermayapti", "code": "llm_unavailable"}
HTTP 503  {"detail": "Qidiruv ishlamadi (Ollama va Qdrant yoqilganmi?): ..."}        # qidiruv xatosi, avvalgidek (Qdrant va h.k.: "code" yo'q)
```

Birinchi tokendan keyin status allaqachon yuborilgan bo'ladi, shuning uchun xato oqimning qatori: `{"error": "Javob yozilmadi: ...", "code": "llm_timeout"}` (Ollama xatolarida `code` bor, boshqalarida yo'q: avvalgi format). Kodlar: `llm_timeout`, `llm_unavailable`, `llm_error`.

## 4. O'lchangan natijalar (2026-11-13, jonli: haqiqiy uvicorn + haqiqiy Ollama)

| Mezon | Natija |
|---|---|
| Mijoz uzilgach Ollama bo'shashi (5 raund, javob yozilayotganda tashlab ketilgan) | yangi so'rov uzilishdan **0,23–0,26 s** keyin xizmat oldi (bo'sh serverdagi asos 0,12–0,28 s) |
| Nazorat: ulanish yopilmasa | yangi so'rov **50,4 s** kutdi |
| Bekor qilingan savoldan keyingi savol | birinchi token **0,8 s** (kutmadi) |
| Ollama ulanishni qabul qiladi, lekin javob bermaydi (standart sozlamalar) | **503** `llm_unavailable`, aniq xabar, **20,04 s** da |
| Ollama ishlamayapti | **503** `llm_unavailable`, 2,05 s |
| Chiqish chegarasi | "batafsil yozing" savoliga aynan **512 token**, `truncated: true`, jami 73 s |
| Iliq model, **keshlangan** prompt (xuddi shu savol) | birinchi token **0,13 s** |
| Iliq model, **qisqa kontekst** (1 bo'lak) | birinchi token **0,39 s** |
| Iliq model, **yangi katta prompt** (5 bo'lak × 1800 belgi ≈ 3500 token) | birinchi token **27 s** |
| Sovuq → `warm_up` (ikkala model) | 3,7 s (modellar qisman xotirada edi); sovuq yuklash avval 28–32 s o'lchangan |

### "Birinchi token < 5 s" mezoni haqida halol xulosa

Model **iliq** bo'lsa va prompt kichik yoki Ollama keshida bo'lsa, mezon bajariladi (0,13–0,39 s). `keep_alive` + isitish ~28 s li yuklashni olib tashlaydi. Lekin **yangi savol uchun katta prompt** (5 ta bo'lak) CPU'da ~130 token/s tezlikda o'qiladi, shuning uchun birinchi token ~10–27 s da (bo'laklar hajmiga qarab). Bu kodning emas, hisoblash quvvatining chegarasi: uni faqat kontekstni qisqartirish (`RAG_TOP_K`, bo'lak hajmi), kichikroq model yoki GPU o'zgartiradi. Bular javob sifatiga ta'sir qiladi va bu vazifada **o'zgartirilmadi** (qaror kerak: pastdagi xavflarga qarang).

### Birinchi token kechikishi: qayta o'lchov va sabab tahlili (2026-11-16)

Apparat: Intel i9-14900K (24 yadro: 8 P + 16 E, 32 oqim), 64 GB, faqat CPU (Intel UHD 770 Ollama'da ishlatilmaydi). Model qwen2.5:7b Q4_K_M, `num_ctx` 8192. Ollama'ga to'g'ridan-to'g'ri, ilovaning aynan o'zi quradigan xabarlar bilan; har safar yangi matn (kesh bo'lmasin). Ollama o'zining hisobotidagi `prompt_eval_count/duration` ishlatildi.

**Sabab: kechikish promptning token soniga chiziqli bog'liq, prefill CPU'da hisoblash bilan cheklangan.** Taxminan 120–165 token/s.

| Prompt (yangi matn) | Ollama tokenlari | Birinchi token | Prefill tezligi |
|---|---|---|---|
| 1 bo'lak × 1800 belgi | 1047 | 6,3 s | 167 tok/s |
| 2 bo'lak | 1754 | 12,0 s | 146 tok/s |
| 3 bo'lak | 2565 | 18,7 s | 137 tok/s |
| 5 bo'lak (oldingi "~3500 token" prompt) | **4031–4228** (3 ta o'lchov) | **32,0 / 33,3 / 35,3 s** (median 33,3) | 120–126 tok/s |
| 8 bo'lak | 6471 | 56,7 s | 114 tok/s |
| 5 bo'lak, aynan shu prompt ikkinchi marta | 4298 | **0,12 s** | (Ollama keshi) |

Eslatma: avvalgi hisobotdagi "~3500 token / 27 s" bahosi 5×1800 belgi uchun kam baholangan edi: o'zbekcha matn Ollama tokenizatorida ~2,3 belgi/token beradi, ya'ni ~4100 token va ~33 s. Chiziqli bog'liqlik o'zgarmaydi: har bir qo'shimcha bo'lak ≈ +6,5 s. **5 s ga sig'ish uchun prompt ~600 tokendan kichik bo'lishi kerak** (taxminan bitta qisqa bo'lak), bu `TOP_K=5` bilan mumkin emas.

**Sinalgan variantlar (sozlama o'zgartirilmadi; so'rov darajasidagi parametrlar yoki alohida vaqtinchalik Ollama nusxasi, bittadan o'lchov):**

| Variant | Prefill | Xulosa |
|---|---|---|
| Asos (Ollama o'zi tanlaydi) | 119 tok/s | |
| `num_thread` 8 / 16 | 111 / 119 tok/s | foyda yo'q |
| `num_thread` 24 | 141 tok/s | +18% |
| **`num_thread` 32** | **165 tok/s** | **+39%**: 5 bo'lakli prompt ~33 s → ~25 s |
| `num_batch` 1024 / 2048 | 119 / 119 tok/s | ta'sir yo'q |
| `num_ctx` 4096 | 121 tok/s | ta'sir yo'q (prefill narxi kontekst hajmiga emas, token soniga bog'liq) |
| Flash attention (alohida nusxa) | 120 tok/s | ta'sir yo'q |
| Flash attention + KV q8_0 | 82 tok/s | **sekinroq** |
| qwen2.5:3b (faqat ma'lumot uchun) | 256 tok/s, 16 s | 5 s ga baribir sig'maydi; sifat pasayadi |

**Xavfsiz (javob sifatiga ta'sir qilmaydigan) imkoniyatlar:**
1. `num_thread=32` so'rov parametri sifatida: ~25–30% tezroq birinchi token (5 bo'lakli prompt ~25 s). Cheklov: bitta o'lchov, takrorlash kerak; hybrid CPU'da barcha oqimlarni band qiladi (indekslash bilan bir vaqtda sekinlashishi mumkin). Matematik jihatdan modelning o'zi o'zgarmaydi, lekin oqimlar bo'linishi suzuvchi nuqtali yig'indi tartibini biroz o'zgartirishi mumkin: eval bilan tekshirish kerak.
2. Sezilgan kutishni kamaytirish: qidiruv ~0,3 s da tugaydi, manbalar darrov ma'lum. Sahifa ularni va "javob tayyorlanmoqda" holatini oldindan ko'rsatishi mumkin (token kelguncha). Javob sifati o'zgarmaydi.

**Javob sifatiga ta'sir qiladigan (qaror va eval kerak) variantlar:** kontekstni qisqartirish (`TOP_K` 5→3 ≈ −40%: ~19 s; bo'lak hajmi; qayta tartiblash va 2 ta bo'lak), kichikroq model, GPU (prefill 10–50 baravar tezlashadi: 5 s ga sig'ishning yagona real yo'li). Hech biri bajarilmadi.

**Mezonni aniq yozish tavsiyasi:** "iliq model: keshlangan yoki qisqa (≤ ~600 token) prompt < 1 s; to'liq 5 bo'lakli yangi prompt ≈ 33 s (`num_thread=32` bilan ~25 s)".

## 5. Ma'lum cheklovlar

- Ollama ichidagi "qotib qolgan generatsiya" (HTTP tirik, token yo'q) birinchi token limiti (120 s) bilan aniqlanadi, 20 s bilan emas: 20 s faqat butunlay javob bermaydigan Ollama uchun.
- Bir vaqtdagi savollar soni cheklanmagan (har biri bitta oqim); Ollama baribir ularni navbatga qo'yadi.
- Indekslash (embedding) va savol-javob bitta Ollama'ni bo'lishadi: embedding paketi (32 bo'lak, ~10–15 s) ketayotganda savol shuncha kutishi mumkin. Bu T0.4 doirasida o'zgartirilmadi.
- `ollama` Python paketi `requirements.txt` da qolgan, lekin kod uni endi ishlatmaydi (o'chirish alohida, qisqa o'zgarish).
