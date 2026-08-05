# Friday Presentation Deck v1

**Document version:** 1.2
**Date:** 2026-08-05
**Source baseline:** `456f0e9a6576aab912f5af5980d756ff4e1e9dc3` is the accepted presentation plan/deck/cue delivery commit and the source baseline for this task's final-delivery candidate; campaign infrastructure accepted through `0abb588`; V4 identity correction accepted through `fc7c85b`. Version 1.1 was prepared from `fc7c85b`; version 1.2 updates the baseline identity and links the delivery bundle (`docs/FRIDAY_DELIVERY_MANIFEST_V1.md`, `docs/FRIDAY_PREFLIGHT_CHECKLIST_V1.md`, `docs/FRIDAY_STATUS_HANDOFF_V1.md`) — an uncommitted candidate built on top of `456f0e9` during review, whose integration commit is not yet known. On presentation day, run from clean `main` matching `origin/main`, containing the delivery bundle files and descending from `456f0e9`.
**Purpose:** direct source for a 20–25-minute Turkish presentation plus a short deterministic demo and Q&A. Every claim traces to accepted tracked repository evidence; every slide defines its content, speaker notes, evidence paths, and its overclaim boundary.
**Relation to the plan:** this deck operationalizes `docs/FRIDAY_PRESENTATION_PLAN_V1.md`; the plan remains the runbook, evidence index, and Q&A reference. This document adds no new methodology and no new execution authority.

---

## Nasıl kullanılır

- **Ana anlatım (toplam Q&A hariç 24,5 dk ≤ 25 dk):** Slayt 1 → 17 sırasıyla; konuşma bölümü 21,5 dk, deterministik demo + geçişler 3,0 dk, Q&A ayrı ve toplama dahil değildir. Süre tablosu Ek B'dedir.
- **Kısa anlatım (toplam Q&A hariç 11,5 dk; 10–12 dk aralığında):** Slayt 1 → 2 → 3 → 4 → 8 → 10 → 11 → 13 → 14 → 16 → 17; konuşma 9,5 dk, tek-task demo + geçişler 2,0 dk, Q&A hariç. Sıra ve süreler Ek A'dadır; slayt düzeyinde `Kısa Anlatım Durumu` alanı bu sırayı işaretler.
- **Kısa Anlatım Durumu anlamları:** `ZORUNLU` = hem ana hem kısa anlatımda gösterilir; `OPSİYONEL` = ana anlatımda da zaman baskısında atlanabilir, kısa anlatımda yoktur; `ATLAMA` = ana anlatımda korunur, kısa anlatımda yoktur (içerik diğer slaytlara katlanır).
- **QLoRA güncellemesi:** Slayt 13'teki sınırlandırılmış `QLORA RESULT UPDATE BLOCK` yalnızca FirstMate kabulünden sonra doldurulur; başka slayta dokunmadan tek yerden güncelleme yapılır.
- **Dil:** sunum Türkçe anlatılır; controller, verifier, policy, PDB, unified diff, fail-to-pass, pass-to-pass, QLoRA, adapter, held-out gibi yerleşik İngilizce teknik terimler korunur.

---

## Slayt 1 — Kapak

**Başlık:** Agentic Debugging: Altyapı, İlk Canlı Bulgular ve Fine-Tuning Yolculuğu

**Ekranda Görünecek İçerik:**

- Başlık: "Agentic Debugging Stajı — Tek Controller, Bağımsız Doğrulayıcı, Gerçek PDB Yolu"
- Alt satır: "7 Ağustos 2026 — Staj Sunumu"
- Tek cümle kapsam: "Model önerir; bağımsız doğrulayıcı karar verir."
- Alt not: "Sunumda her sayısal iddia, repository'deki kabul edilmiş kayıtlara dayanır; kısa bir deterministik demo gösterilecektir."

**Konuşmacı Notları:**

30 saniyede üç şey söyleyin: (1) bu staj, bir agentic debugging platformu üzerine kurulu bir araştırma/ürün prototipidir; (2) sunumun iki ana kanıtı deterministik offline demo ve kaydedilmiş gerçek model denemesidir; (3) hiçbir iddia uydurulmuş sayıya dayanmaz. QLoRA sonuçlarının beklemede olduğunu baştan söylemek, sonraki dürüstlük çerçevesini kurar.

**Kanıt / Kaynak Yolları:**

- `docs/FRIDAY_PRESENTATION_PLAN_V1.md` (sunum runbook'u ve kanıt indeksi)
- `docs/FINAL_TECHNICAL_REPORT_V1.md` §1 (hedef ve araştırma sorusu)

**Overclaim Sınırı:** Bu slayt "bitmiş otomatik onarım sistemi" imajı veremez. Sunum, altyapı + değerlendirme metodolojisi + dürüst ilk bulgular sunar; tamamlanmış genel amaçlı onarım sistemi değil.

**Kısa Anlatım Durumu:** ZORUNLU

---

## Slayt 2 — Staj hedefi ve 27 maddelik kapsam

**Başlık:** Staj Hedefi ve 27 Maddelik Kapsam: Kontrat ve Dürüst Durum Haritası

**Ekranda Görünecek İçerik:**

- Danışmanın orijinal TODO'su 27 maddedir; byte-identical olarak korunuyor (`docs/INSTRUCTOR_AGENTIC_DEBUGGING_TODO.md`).
- Durum haritası her maddeye tam olarak bir durum atar (kanıt zorunlu):
  - COMPLETED: 7
  - PARTIAL: 10
  - IN PROGRESS: 3
  - NOT STARTED: 7
  - BLOCKED: 0
  - Toplam: 27
- COMPLETED maddeler yalnızca kanıtın karşıladığı alanlardır: beş sistem incelemesi, veri seti araştırma/karşılaştırma, dört tool, ajan, model seçimi, demo + teknik rapor.
- IN PROGRESS: corpus/audit, instruction-response dönüşümü, QLoRA fine-tuning.

**Konuşmacı Notları:**

"Danışmanın 27 maddelik listesi bizim sözleşmemiz; her madde repository kanıtına eşleniyor ve kanıt olmadan hiçbir madde tamamlanmış işaretlenmiyor." Durum dağılımının ne anlama geldiğini bir cümleyle açıklayın: mimari ve değerlendirme altyapısı ayakta; model eğitimi ve canlı onarım kanıtı eksik. `FRIDAY` horizon'ının "aktif çalışma veya dürüst sınırlama" anlamına geldiğini, "tamamlanmış" anlamına gelmediğini belirtin.

**Kanıt / Kaynak Yolları:**

- `docs/INSTRUCTOR_AGENTIC_DEBUGGING_TODO.md` (27 madde, değişmemiş)
- `docs/INSTRUCTOR_AGENTIC_DEBUGGING_STATUS_MAP.md` §3 (özet tablo, satır 106: COMPLETED 7 — PARTIAL 10 — IN PROGRESS 3 — NOT STARTED 7 — BLOCKED 0) ve §2 (kanıt katmanı kuralları)

**Overclaim Sınırı:** "27 maddenin çoğu aslında bitti, sadece değerlendirme kaldı" denemez. PARTIAL/IN PROGRESS/NOT STARTED durumları yükseltilemez; BLOCKED sayısı 0'dır ve bu bir başarı değil, sınıflandırma sonucudur.

**Kısa Anlatım Durumu:** ZORUNLU

---

## Slayt 3 — Dürüst durum özeti

**Başlık:** Bugün Ne Durumdayız? Üç Cümle

**Ekranda Görünecek İçerik:**

1. Deterministic platform uçtan uca çalışıyor: Task 9 demo — 5 curated task, 2 policy, 10 case; verifier 10/10 RESOLVED; F2P 10/10; P2P 22/22; localization 10/10.
2. Gerçek model etkileşimi gerçekleşti (OpenCode Go / DeepSeek V4 Flash, protocol 1.3) ve doğru teşhis gözlendi; ancak verifier-confirmed live repair yok, live PDB observation yok.
3. QLoRA metodolojisi dondu; gerçek, sızıntı kontrollü corpus hazır; final eğitim sonuçları bekleniyor (kabul edilmiş checkpoint yok).

**Konuşmacı Notları:**

Bu slayt sunumun tamamının çerçevesidir: "altyapı kanıtı güçlü, canlı-model sonuç kanıtı henüz yok, eğitim sonuçları beklemede." Sayıları Slayt 8'de detaylandıracağınızı, V4 bulgularını Slayt 10–11'de anlatacağınızı söyleyin. QLoRA için reçete edilmiş cümle (cue sheet) burada da geçerli: final training yetkilendirildi, sonuç yok.

**Kanıt / Kaynak Yolları:**

- `docs/DEMO_TASK9.md`; `docs/DEMO_GUIDE_V1.md` §2 (Task 9 sayıları)
- `docs/INSTRUCTOR_AGENTIC_DEBUGGING_STATUS_MAP.md` §5 (campaign ve QLoRA sınırları)
- `docs/FRIDAY_PRESENTATION_PLAN_V1.md` §8A (QLoRA güncel durum)

**Overclaim Sınırı:** Bu slayt "sistem hataları onarıyor" izlenimi veremez; "10/10" sayısı yalnızca scripted stand-in ile deterministik demoya aittir.

**Kısa Anlatım Durumu:** ZORUNLU

---

## Slayt 4 — Sistem mimarisi

**Başlık:** Sistem Mimarisi: Tek Controller, Deterministik Tool'lar, Bağımsız Doğrulayıcı

**Ekranda Görünecek İçerik:**

- Tek controller agent (multi-agent değil): `agentic_debugger/agent/` — controller, state machine, policy, tool registry.
- Typed, deterministic tool'lar: file read, code search, test run, patch apply (unified diff).
- Disposable per-case workspace; canonical kaynak asla doğrudan yazılmaz.
- Gerçek PDB yolu: worker subprocess üzerinde pdb session (Slayt 6).
- Event trajectory + replay doğrulaması: her koşu kaydedilir, tekrar oynatılabilir.
- Independent verifier: her yaşam döngüsünün sonunda otorite (Slayt 6).
- Yaşam döngüsü üç koşulda da aynıdır: offline scripted stand-in, canlı model, gold-patch baseline.

**Konuşmacı Notları:**

Mimaride tek karar mesajı: "model önerir, bağımsız doğrulayıcı karar verir — model iddiası asla başarı kanıtı sayılmaz." Bileşenlerin nerede yaşadığını gösterin (`agentic_debugger/` altında agent/runtime/evaluation/events). Aynı yaşam döngüsünün scripted model, canlı model ve gold patch için geçerli olduğunu vurgulayın: bu, altyapı sonuçlarını model sonuçlarından ayıran tasarımdır.

**Kanıt / Kaynak Yolları:**

- `docs/FINAL_TECHNICAL_REPORT_V1.md` §2 (bileşen haritası ve yaşam döngüsü)
- `agentic_debugger/agent/controller.py`, `state_machine.py`, `controller_policy.py`, `tool_registry.py`
- `agentic_debugger/runtime/workspace.py`, `patcher.py`; `agentic_debugger/events/replay.py`

**Overclaim Sınırı:** Mimari "debugger-destekli ajanların statik onarımdan üstün olduğunu kanıtladı" iddiası taşıyamaz; bu karşılaştırma yapılmadı.

**Kısa Anlatım Durumu:** ZORUNLU

---

## Slayt 5 — Controller state machine ve typed tools

**Başlık:** Controller: State Machine ve Typed Deterministik Tool'lar

**Ekranda Görünecek İçerik:**

- State machine: reproduce → understand → (PDB gate) → patch → validate.
- PDB gate: `decide_pdb_access` — controller state, reproduction durumu, PDB budget, patch denemeleri ve aktif hipotez üzerinden karar; her karar kaydedilir.
- Typed tools ve doğrulama: her direktif state allowlist'i, argüman şeması ve yasaklı path kurallarından geçer.
- Budget'lar ve allowlist'ler task manifest'inden gelir; demo bunları asla yükseltmez.

**Konuşmacı Notları:**

Slayt 4'ün "nasıl" kısmı. State machine'i tek satırla anlatın: önce hata yeniden üretilir, sonra hipotez kurulur, belirsizlik varsa PDB kapısı devreye girebilir, patch önerilir ve en sonda bağımsız doğrulama yapılır. Tool'ların typed olmasının ne demek olduğunu söyleyin: modelin serbest araç çağrısı yok, her direktif şemaya uymak zorunda; uymayan direktifler reddedilir ve red nedenleri kaydedilir.

**Kanıt / Kaynak Yolları:**

- `agentic_debugger/agent/controller.py`, `state_machine.py`, `controller_policy.py`, `tool_registry.py`
- `agentic_debugger/skills/file_skills.py`, `search_skills.py`; `agentic_debugger/runtime/test_runner.py`, `patcher.py`
- `docs/INSTRUCTOR_AGENTIC_DEBUGGING_STATUS_MAP.md` madde 16 (COMPLETED)

**Overclaim Sınırı:** Tool'ların var olması ve çalışması, modelin bu tool'larla hata bulduğu anlamına gelmez.

**Kısa Anlatım Durumu:** OPSİYONEL (ana anlatımda da zaman baskısında atlanabilir; state machine demo sırasında canlı görülür)

---

## Slayt 6 — PDB yolu ve independent verifier

**Başlık:** Gerçek PDB Yolu ve Bağımsız Doğrulayıcı

**Ekranda Görünecek İçerik:**

- Gerçek PDB session: worker subprocess üzerinde pdb — breakpoint, stack/frame/locals incelemesi, güvenli AST-allowlist'li ifade değerlendirme, stepping.
- Model ham pdb prompt'u görmez; yalnızca typed, sınırlı gözlemler alır.
- Independent verifier (otorite): temiz baseline'dan — baseline reproduction, syntax check, F2P/P2P/full suite, canonical-fixture immutability, workspace cleanup.
- "Model önerir; doğrulayıcı karar verir."

**Konuşmacı Notları:**

PDB yolunun gerçek olduğunu ama "live model henüz PDB açmadı" gerçeğini birlikte söyleyin: mekanizma scripted trajektörlerle gösterildi; canlı denemelerde PDB observation sıfır. Verifier'ın neden "independent" olduğunu vurgulayın: controller'ın kendi iddiasına değil, temiz baseline'dan tekrar çalıştırmaya güvenir. README'deki "Reusing the curated-task correctness authority" bölümü bu tasarımın küçük giriş noktasıdır.

**Kanıt / Kaynak Yolları:**

- `agentic_debugger/runtime/pdb_session.py`, `pdb_worker.py`, `pdb_protocol.py`
- `agentic_debugger/evaluation/verifier.py`, `outcome_taxonomy.py`, `task_schema.py`
- `README.md` ("Reusing the curated-task correctness authority")
- `docs/DEMO_TASK9.md` §8 (scripted PDB sınırları)

**Overclaim Sınırı:** "PDB, onarım performansını artırdı" denemez: geçerli static-versus-PDB karşılaştırması yok, live PDB observation yok.

**Kısa Anlatım Durumu:** ATLAMA (ana anlatımda korunur; kısa anlatımda "gerçek PDB mekanizması var, canlı PDB yok" mesajı Slayt 8 ve demo sırasında söylenir)

---

## Slayt 7 — Dataset ve model kararları

**Başlık:** Dataset ve Model Kararları: Lisans Disiplini ile Seçim

**Ekranda Görünecek İçerik:**

- Değerlendirme: BugsInPy birincil (gerçek proje hataları) ama execution license-blocked; QuixBugs Python lisanslı (MIT) fallback; 5 curated fixture mimari smoke gate; SWE-bench DEFER; Defects4J NO-GO.
- Fine-tuning: CommitPackFT Python (lisans allowlist'i, pinned revision); 5 curated task held-out.
- Model: Qwen/Qwen2.5-Coder-7B-Instruct, exact revision pinned, Apache-2.0 (freeze record, experiment branch).
- Kararlar: RAG NO-GO-FOR-NOW; DPO NO-GO-FOR-NOW (kayıtlı kararlar, tamamlama değil).

**Konuşmacı Notları:**

Seçim kriteri: lisans, Python/PDB uyumu ve oracle kalitesi. BugsInPy'nin önce seçildiğini ama lisans kapısı açılmadan çalıştırılamadığını net söyleyin. QuixBugs'un "fallback" olduğunu, iki no-model baseline'ının yalnız altyapıyı doğruladığını vurgulayın. RAG/DPO'nun NO-GO-FOR-NOW kararları olduğunu, ilerleme değil kayıtlı erteleme olduğunu belirtin.

**Kanıt / Kaynak Yolları:**

- `docs/DATASET_EVALUATION_DECISION_V1.md` (§2–§3, §10)
- `docs/BUGSINPY_LICENSE_GATE_V1.md`; `docs/MODEL_RAG_SFT_DPO_DECISION_GATE_V1.md`
- `experiments/qlora_patch_pilot_v1/freeze_record.json` (branch `experiment/qlora-patch-pilot-v1`, commit `3f0d3e7`)

**Overclaim Sınırı:** "BugsInPy üzerinde değerlendirme yaptık" denemez (preflight-only); RAG veya DPO implementasyonu denmez.

**Kısa Anlatım Durumu:** OPSİYONEL (ana anlatımda da zaman baskısında atlanabilir; kısa anlatımda tek satır Slayt 3'te; detay Q&A'ya bırakılır)

---

## Slayt 8 — Deterministic Task 9 evidence

**Başlık:** Deterministic Task 9 Kanıtı: Platform Uçtan Uca Çalışıyor

**Ekranda Görünecek İçerik:**

- 5 curated task × 2 policy (static-baseline, pdb-on-uncertainty) = 10 case.
- Tümü verifier COMPLETED / RESOLVED; F2P 10/10; P2P 22/22; localization `CORRECT_TARGET_SYMBOL` 10/10.
- 21 scripted PDB observation (pdb-on-uncertainty policy altında).
- Canonical fixture değişmedi; her workspace temizlendi; trajectory'ler replay-valid.
- Offline guard (ölçülen): 0 provider attempt, 0 network attempt — demo sınırı içinde.
- Model, offline scripted stand-in'dir; bu platform kanıtıdır, model kalitesi değildir.

**Konuşmacı Notları:**

Sayıları tablo olarak gösterin. Hemen sınırı söyleyin: "scripted stand-in ile 10/10, model başarısı değil." PDB gözlemlerinin bir driver script üzerinde, önceden bilinen breakpoint ile alındığını (DEMO_TASK9.md §8) dürüstçe belirtin; determinizm garantisini (environment/timing dışında byte-stabil) ve offline guard'ın ölçülen sayaçlarını vurgulayın.

**Kanıt / Kaynak Yolları:**

- `docs/DEMO_TASK9.md` (§2, §5, §6, §7)
- `docs/DEMO_GUIDE_V1.md` §2 (success criteria)
- `tests/golden_trajectories/data/pdb-gated-successful-repair.json` (kayıtlı PDB-gated yol)

**Overclaim Sınırı:** "%100 onarım başarısı" denemez; demo, hata bulma/onarma yeteneğini değil platformu ölçer.

**Kısa Anlatım Durumu:** ZORUNLU

---

## Slayt 9 — QuixBugs infrastructure evidence

**Başlık:** QuixBugs Altyapı Kanıtı: Gerçek Görevler, Literal Gold Patch'ler, Model Yok

**Ekranda Görünecek İçerik:**

- Tek-task smoke (`gcd`, pinned revision `4257f44b…`): verdict `ACCEPT CANDIDATE — REAL SMOKE PASSED`; 6 node; post-patch F2P 1/1, P2P 1/1, full suite 2/2.
- Sekiz-task gold baseline: 8/8 task çözüldü, 49/49 node geçti; aday her zaman literal upstream diff (model üretimi değil).
- Containment: WSL2 Ubuntu-22.04 + Bubblewrap + `prlimit`; fail-closed gate; source ve venv `/mnt/c` dışında pinned.
- Sonuç yalnızca altyapıyı doğrular: adapter, sandbox, patch/test/verifier yaşam döngüsü, temizlik.

**Konuşmacı Notları:**

Bu slaytın tek cümlesi: "QuixBugs sonuçları altyapı doğrulamasıdır, model performansı değildir." Adayların literal upstream diff olmasının neden kritik olduğunu söyleyin: gold-patch baseline "model çözdü" demek için kullanılamaz. Containment'ın (Bubblewrap + prlimit, 7/7 canlı self-test) bu denemeleri güvenli yapan parça olduğunu kısaca belirtin.

**Kanıt / Kaynak Yolları:**

- `docs/QUIXBUGS_SMOKE_USAGE_V1.md`; `docs/QUIXBUGS_EIGHT_TASK_BASELINE_V1.md`
- `docs/FINAL_TECHNICAL_REPORT_V1.md` §6–§7
- `docs/DEMO_GUIDE_V1.md` §3–§4 (already-accepted entry point'ler)

**Overclaim Sınırı:** Gold-patch 8/8 asla "model 8/8 çözdü" olarak sunulamaz; bu koşulda model ve PDB yoktu.

**Kısa Anlatım Durumu:** OPSİYONEL (ana anlatımda da zaman baskısında atlanabilir; kısa anlatımda tek satır Slayt 10'un girişinde)

---

## Slayt 10 — Recorded real-model V4 findings

**Başlık:** Kaydedilmiş Gerçek Model Bulguları: V4 Denemesi (Canlı Değil, Kayıt)

**Ekranda Görünecek İçerik:**

- Gerçek OpenCode Go / DeepSeek V4 Flash etkileşimi (protocol 1.3, subscription route, variant `max`, no fallback).
- Case 1 — `quixbugs-find-in-sorted-smoke-v1`, `pdb-on-uncertainty`, order 1: model doğru teşhisi ve doğru tek satırlık düzeltmeyi unified diff olarak önerdi; patch, strict hunk-header doğrulamasında reddedildi (`old_count=7`, gövde 6 satır). 10 provider process; 26.139 public-evidence byte; candidate uygulanmadı; 0 verifier run; maliyet `$0.007378`.
- Case 2 — `quixbugs-find-in-sorted-smoke-v1`, `static-baseline`, order 2: patch uygulandı ve Validate ziyaret edildi; 38.534 byte ile frozen public-evidence bütçesi verifier'dan önce kesildi; run interrupted. 15 provider process; 0 verifier run; maliyet `$0.012323`.
- Kampanya: `ABORTED / BUDGET_EXCEEDED`; case 3–6 başlamadı; 0 verifier-confirmed repair; 0 PDB observation; geçerli static-versus-PDB karşılaştırması yok.

**Konuşmacı Notları:**

Bunu "canlı demo" olarak değil, kaydedilmiş deney olarak sunun. Güçlü sinyal: gerçek model, protokol üzerinden doğru teşhis ve doğru düzeltme önerisi üretti. Ama iki dürüst terminal: Case 1'de format hatası (semantik hata değil), Case 2'de bütçe kesintisi. Kimlik eşlemesinin (Case 1/Case 2 ↔ frozen case'ler) korunmuş kampanya kaydından düzeltilip `fc7c85b` ile kabul edildiğini söyleyebilirsiniz. Maliyetler provider-reported'dur.

**Kanıt / Kaynak Yolları:**

- `research/quixbugs/PAIRED_PILOT_V4.json` (frozen kontrat, canonical SHA-256 `020dfc1f…`)
- `tests/fixtures/quixbugs_v4_budget_verifier_attempt_fixture.json`; `tests/unit/test_quixbugs_v4_budget_verifier_path.py` (kayıt kimlikleri)
- `docs/INSTRUCTOR_AGENTIC_DEBUGGING_STATUS_MAP.md` §5 (campaign boundary); `docs/PROJECT_TRACKER.md` 2026-08-05

**Overclaim Sınırı:** "Model QuixBugs görevini onardı" denemez; "patch uygulandı" verifier onayı değildir.

**Kısa Anlatım Durumu:** ZORUNLU

---

## Slayt 11 — Neden verifier-confirmed repair oluşmadı

**Başlık:** Neden Verifier-Confirmed Repair Yok? Protokol ve Bütçe Gerçeği

**Ekranda Görünecek İçerik:**

- Protokol gerçeği: Case 1'deki (semantik olarak doğru) diff, strict hunk-header doğrulamasında reddedildi; candidate uygulanamadı.
- Bütçe gerçeği: frozen 20.000-byte public-evidence limiti; Case 2 verifier çalışmadan bütçe kesintisine uğradı.
- Önceden kayıtlı (preregistered) stop kontratı: kampanya dürüstçe `ABORTED / BUDGET_EXCEEDED` oldu; case 3–6 başlamadı.
- Kayıtlı terminaller schema-valid olarak materialize edildi (`INFRASTRUCTURE_ERROR`, `ABORTED / INTERRUPTED`; `0abb588` altyapısı + `fc7c85b` kimlik düzeltmesi).
- Bunlar gizli model başarısızlıkları değil; kayıtlı, dürüst protokol/bütçe terminalleridir.

**Konuşmacı Notları:**

"Ne öğrendik" sorusunun dürüst cevabı: canlı yol gerçekten çalıştı (route, protokol, teşhis), ama tek bir tamamlanmış doğrulama bile yok — çünkü format doğrulaması katı, bütçe donmuş ve stop kontratı önceden kayıtlı. Bu üçü bilinçli tasarım kararıdır: kolaya kaçıp bir doğrulamayı "geçirerek" sunmak yasaktı. Sonuç: kampanya altyapısı bu terminalleri artık schema-valid şekilde kaydedip doğrulayabiliyor (0abb588), kimlik eşlemesi düzeltildi (fc7c85b).

**Kanıt / Kaynak Yolları:**

- `docs/INSTRUCTOR_AGENTIC_DEBUGGING_STATUS_MAP.md` §5; `docs/PROJECT_TRACKER.md` 2026-08-05
- `docs/FRIDAY_PRESENTATION_PLAN_V1.md` §5.3 (precise statements)
- `tests/unit/test_quixbugs_v4_budget_verifier_path.py` (terminal kuralları)

**Overclaim Sınırı:** Bu bir "kampanya başarısı" anlatısı değildir; "altyapı yüzünden model sonuçlanamadı" gibi bir suçlama çerçevesi de kurulamaz — terminaller kontrat gereği dürüsttür.

**Kısa Anlatım Durumu:** ZORUNLU

---

## Slayt 12 — Protocol, budget ve evidence-contract dersleri

**Başlık:** Dersler: Protokol, Bütçe ve Kanıt Kontratı Disiplini

**Ekranda Görünecek İçerik:**

- Fail-closed bir house style: bilinmeyen/eksik/çelişkili kanıt asla başarıya çevrilmez.
- `0abb588` (kabul edildi): typed terminal handling, exact-identity validation, fail-closed budget-exhaustion provenance — run persistence, campaign-record validation, attempt-package verification.
- `fc7c85b` (kabul edildi): sanitized fixture ve replay-test'lerin iki kayıtlı V4 şeklini yanlış frozen case'lere bağlayan eşlemesi, korunmuş kampanya kanıtıyla düzeltildi.
- Kanıt katmanları: Layer 1 — tracked repository kanıtı; Layer 2 — FirstMate-review'lu dış deney kanıtı (yalnızca IN PROGRESS iddialarını destekler).
- Kayıt disiplini: kimlik, byte-count provenance, maliyet (provider-reported), sayaç anlambilimi.

**Konuşmacı Notları:**

Bu slayt, metodolojik katkının özüdür: sonuç yokluğunu bile doğru kaydetmek. İki commit'i ayırın: 0abb588 terminal/provenance altyapısını, fc7c85b kayıt-kimlik düzeltmesini getirdi. Kanıt katmanı kuralını bir cümleyle anlatın: dış kanıt yalnızca "devam ediyor" iddialarını destekler, asla COMPLETED iddiasını.

**Kanıt / Kaynak Yolları:**

- `docs/PROJECT_TRACKER.md` 2026-08-05; `README.md` ("Current status (2026-08-05)")
- `docs/INSTRUCTOR_AGENTIC_DEBUGGING_STATUS_MAP.md` §2 (kanıt katmanları) ve §5 (campaign-infrastructure boundary)
- `tests/fixtures/quixbugs_v4_budget_verifier_attempt_fixture.json`; `tests/unit/test_quixbugs_v4_budget_verifier_path.py`

**Overclaim Sınırı:** Bu dersler "canlı onarım başarısı" iddiası değildir; altyapı ve kayıt disiplini katkısıdır.

**Kısa Anlatım Durumu:** OPSİYONEL (ana anlatımda da zaman baskısında atlanabilir; kısa anlatımda Slayt 11'in kapsamındadır)

---

## Slayt 13 — QLoRA methodology and current status

**Başlık:** QLoRA: Dondurulmuş Metodoloji, Bekleyen Sonuçlar

**Ekranda Görünecek İçerik:**

- Frozen metodoloji: Qwen/Qwen2.5-Coder-7B-Instruct, exact revision `c03e6d35…` (Apache-2.0); CommitPackFT Python config; deterministik split.
- Gerçek minimum-tier corpus: 56.025 adaydan 1.000 train / 150 validation; sıfır held-out exact/near sızıntı; sıfır repository overlap.
- Implementasyon kabul edildi: commit `3f0d3e7` (branch `experiment/qlora-patch-pilot-v1`, unmerged) — tracked `independent_ai` audit kontratı ve run-provenance dahil. Owner suite: 3457 passed / 3 skipped / 36 ilişkisiz önceden var olan OpenCode failure; QLoRA odaklı failure yok.
- Bağımsız FirstMate AI audit (dış, owner-delegated): 75 frozen satır — 39 ACCEPT / 36 REJECT; bu bir AI audit'tir, insan review değildir; corpus değişmedi; corpus acceptance beklemede.
- Final training: 2026-08-05'te FirstMate tarafından dışarıdan yetkilendirildi; şu anda çalışıyor veya artifact review bekliyor; kabul edilmiş final checkpoint/result YOK.
- Held-out generation yetkisiz; base-versus-tuned performans bilinmiyor.

**Konuşmacı Notları:**

Slayt 3'teki üçüncü cümleyi açın: metodoloji ve corpus kanıtı var, sonuç yok. Reçete edilmiş cümleyi birebir kullanın (cue sheet): "…Final training 2026-08-05'te dışarıdan yetkilendirildi; kabul edilmiş bir final-training artifact'i yok ve sonuçlar FirstMate artifact review'ini bekliyor. Held-out generation yetkisiz; base-versus-tuned karşılaştırması bilinmiyor." Audit'in AI audit olduğunu, insan review olmadığını açıkça söyleyin. Tarihsel freeze flag'inden söz edilecekse "tarihsel kayıt, güncel yetki kanıtı değil" etiketiyle söyleyin.

**Kanıt / Kaynak Yolları:**

- `experiments/qlora_patch_pilot_v1/freeze_record.json`, `training_config.json`, `transformation_config.json`, `SMOKE_EVIDENCE.md` (branch `experiment/qlora-patch-pilot-v1`, commit `3f0d3e7`)
- `docs/INSTRUCTOR_AGENTIC_DEBUGGING_STATUS_MAP.md` §5 (QLoRA boundary, Layer 2); `docs/FRIDAY_PRESENTATION_PLAN_V1.md` §8
- `docs/PROJECT_TRACKER.md` 2026-08-05 (3457/3/36)

**Overclaim Sınırı:** "Fine-tuning modeli iyileştirdi" denemez; metrik uydurulamaz; kabul edilmemiş hiçbir checkpoint değeri sunulamaz.

### QLORA RESULT UPDATE BLOCK — UPDATE ONLY AFTER FIRSTMATE ACCEPTANCE

Aşağıdaki alanlar, kabul edilmiş bir final-training paketi ve FirstMate onayı olmadan doldurulamaz. Şu an bilinçli olarak boştur; tahmin, benzetme veya smoke kanıtıyla değer konulamaz. Kabul sonrasında yalnızca şu alanlar güncellenir (Slayt 13 gövdesiyle çelişmeyecek şekilde):

| Alan | Durum | Kabul sonrası değiştirilecek kaynak |
|---|---|---|
| Accepted run identity | BOŞ — kabul edilene kadar doldurmayın | Colab notebook eğitim kaydı; dış artifact manifest |
| Accepted checkpoint/adapter identity | BOŞ — kabul edilene kadar doldurmayın | `external_artifacts.json` adapter girişi + saved-adapter reload kaydı |
| Accepted training completion status | BOŞ — kabul edilene kadar doldurmayın | FirstMate artifact review sonucu |
| Accepted training metrics | BOŞ — kabul edilene kadar doldurmayın | TRL/SFT trainer log'u (kabul edilmiş kayıt) |
| Held-out authorization status | BOŞ — ayrı yetki belgesi olmadan doldurmayın | Ayrı held-out yetkilendirme kaydı |

Ek not: `3f0d3e7`'deki tarihsel freeze record hâlâ `final_training_authorized: false` içerir; bu tarihsel branch-bound kayıttır, 2026-08-05 dış yetkilendirmesi hakkında kanıt değildir ve held-out'un şu an yetkili olduğu anlamına gelmez (yetkili değildir).

**Kısa Anlatım Durumu:** ZORUNLU (kısa anlatımda tek cümlelik reçete sıkıştırılır)

---

## Slayt 14 — Explicit limitations

**Başlık:** Açık Sınırlamalar: Söylediklerimiz Kadar Söylemediklerimiz

**Ekranda Görünecek İçerik:**

- Verifier-confirmed live repair yok; live PDB observation yok; geçerli static-versus-PDB karşılaştırması yok; tamamlanmış six-case kampanya yok.
- QuixBugs baselineleri gold-patch, model'siz — yalnızca altyapı kanıtı.
- BugsInPy execution license-blocked (preflight-only).
- Demo scripted stand-in; PDB yolu driver script'e pre-known breakpoint ile bağlanır, failing test'e değil.
- RAG NO-GO-FOR-NOW ve DPO NO-GO-FOR-NOW kayıtlı kararlardır, tamamlama değildir.
- Altyapı hataları ve bütçe sınırları, model-performans sonucu olarak sunulamaz.

**Konuşmacı Notları:**

Pozitif çerçeveden önce sınırları söyleyin: bu, sunumun güvenilirlik anahtarıdır. Her sayının neyi kanıtlayıp neyi kanıtlamadığının etiketlendiğini vurgulayın. "Kalan iş sadece polisaj" cümlesi yasak — açık işler gerçek ve maddidir (Slayt 15).

**Kanıt / Kaynak Yolları:**

- `docs/FINAL_TECHNICAL_REPORT_V1.md` §7.4, §9; `docs/DEMO_TASK9.md` §8
- `docs/INSTRUCTOR_AGENTIC_DEBUGGING_STATUS_MAP.md` §5; `docs/BUGSINPY_LICENSE_GATE_V1.md`

**Overclaim Sınırı:** Sınırlamalar listesi "kalan iş küçük" olarak yorumlanamaz; eksikler maddidir.

**Kısa Anlatım Durumu:** ZORUNLU

---

## Slayt 15 — Near-term roadmap

**Başlık:** Yakın Dönem Yol Haritası: Ölçülebilir Sonraki Adımlar

**Ekranda Görünecek İçerik:**

- Post-Friday yakın dönem:
  - Fail-closed audit doğrulaması ve corpus acceptance kararı (maddeler 9, 11).
  - Final-training artifact review ve tamamlama (madde 12).
  - Frozen held-out base-versus-tuned karşılaştırma (madde 13; ayrı yetki gerektirir).
  - Fine-tuned model debugger-komut üretimi/yorumlama (madde 23).
  - Yetkili altı-case QuixBugs kampanyası, verifier-authoritative sonuçlarla (maddeler 18, 26) — `research/quixbugs/PAIRED_PILOT_V4.json` açıkça kullanılarak.
  - BugsInPy execution, license gate açılırsa.
- Uzun dönem: konsolide literatür survey (1–4); RAG ve entegrasyon (14–15); preference veri seti ve DPO/RLHF (19–21); GDB/LLDB adapter (22).

**Konuşmacı Notları:**

Yol haritasını instructor maddelerine bağlayın: her adımın karşılık geldiği madde numarası vardır. Kritik not: bu slayt hiçbir şeyi yetkilendirmez — kampanya, eğitim ve held-out ayrı yetki belgeleri gerektirir. "Üç ölçülebilir sonraki adım" olarak özetleyin: eğitim artifact review + corpus acceptance, held-out karşılaştırma, yetkili altı-case kampanya.

**Kanıt / Kaynak Yolları:**

- `docs/FRIDAY_PRESENTATION_PLAN_V1.md` §12 (post-Friday near term tablosu)
- `docs/INSTRUCTOR_AGENTIC_DEBUGGING_STATUS_MAP.md` §6 (Friday/post-Friday/long-term split)

**Overclaim Sınırı:** Yol haritası kendi başına çalıştırma yetkisi değildir; held-out ve kampanya hâlâ ayrı yetki gerektirir.

**Kısa Anlatım Durumu:** ATLAMA (ana anlatımda korunur; kısa anlatımda "üç ölçülebilir adım: eğitim artifact review + corpus acceptance, held-out karşılaştırma, yetkili altı-case kampanya" tek cümleyle Slayt 17'de söylenir)

---

## Slayt 16 — Deterministic demo transition

**Başlık:** Deterministik Demo: Platform Canlı Gösterimi

**Ekranda Görünecek İçerik:**

- Geçiş cümlesi: "Şimdi size platformu canlı göstereyim: tek bir curated hata üzerinde, model yerine offline scripted stand-in ile controller, tool'lar, PDB-capable yol, patch ve bağımsız doğrulayıcı uçtan uca çalışacak."
- Komut (her koşuda taze, benzersiz bir çıktı dizini üretir; mevcut bir dizini silmez/üzerine yazmaz):
  ```powershell
  $demoOut = "demo-out-friday-" + (Get-Date -Format "yyyyMMdd-HHmmss")
  python -m agentic_debugger.demo --output-dir $demoOut --task-id curated-off-by-one-002
  ```
- Açılacaklar: `$demoOut\results.json` (sayılar), `$demoOut\technical-evaluation-summary.md`, `$demoOut\trajectories\<case>.events.jsonl` (state geçişleri + typed direktifler), `.semantic.json` (stabil projeksiyon), `$demoOut\REPRODUCE.md`.
- Demo'nun desteklediği iddialar: task yükleme, controller state machine, typed tool'lar, disposable workspace'te unified-diff patch, bağımsız verifier (F2P/P2P/full suite), fixture immutability, workspace cleanup, offline guard (0 provider / 0 network), replay-valid trajectory.
- Desteklemediği iddialar: model kalitesi, PDB faydası, canlı onarım.
- Fallback: komut başarısız olursa korunmuş kayıtlı çıktılar gösterilir — `docs/DEMO_GUIDE_V1.md` §2'nin kayıtlı sonuçları ve yerel korunmuş rehearsal çıktısı (yalnızca **yerel operasyonel fallback**; durable iddia kaynağı değildir).
- Bağımlılık yok: provider yok, internet yok, WSL yok.

**Konuşmacı Notları:**

Komutları önceden bir kez provasını yaptığınız terminalde çalıştırın; her adımda ekranda neye işaret edeceğinizi biliyorsunuz (cue sheet'teki sıra). Demo sırasında scripted stand-in sınırını tekrar söyleyin. `--strict` tam koşu opsiyonu sunum öncesi provadır; canlıda tek-task formu kullanın. Başarısızlıkta sakin kalın: kayıtlı çıktıya geçin, verifier/regresyon kapılarını gevşetmeyin.

**Kanıt / Kaynak Yolları:**

- `docs/DEMO_GUIDE_V1.md` §2; `docs/DEMO_TASK9.md` §2 (CLI kontratı, exit code'lar, artifact'lar)
- `agentic_debugger/demo/cli.py` (tek doğru CLI; `--output-dir` zorunlu)
- Kanıtı gösterilen sayılar (F2P/P2P, `CORRECT_TARGET_SYMBOL`, fixture immutability, cleanup, offline guard) `docs/DEMO_GUIDE_V1.md` §2 ve `docs/DEMO_TASK9.md` §5–§7'deki kabul edilmiş kayıtlardır; canlı koşudan üretilen çıktılar sunum anında gösterilir, durable iddia kaynağı değildir.

**Overclaim Sınırı:** "Model canlı hata düzeltiyor" denemez; "iki policy'nin aynı sonucu" structural'dır (aynı candidate diff), bulgu değildir.

**Kısa Anlatım Durumu:** ZORUNLU (kısa anlatımda tek-task formu kullanılır)

---

## Slayt 17 — Contribution summary and closing

**Başlık:** Katkı Özeti ve Kapanış

**Ekranda Görünecek İçerik:**

- Verifier-backed, fail-closed, tek-controller agentic debugging platformu; gerçek PDB entegrasyonu; replay-verified trajectory sistemi.
- Lisanslı, altyapı-doğrulanmış dış dataset yolu (QuixBugs, 8/8 gold) ve tam belgelenmiş ama license-blocked birincil dataset (BugsInPy).
- Gerçek modelin ilk dürüst gözlemleri: doğru teşhis kanıtı, sıfır verifier-confirmed repair, sıfır PDB observation.
- Dondurulmuş QLoRA metodolojisi ve sızıntı kontrollü gerçek corpus; sonuçlar beklemede.
- Ana katkı: altyapı ve değerlendirme metodolojisi — sonraki deney iyi tanımlı ve ucuz.
- Teşekkür; sorular.

**Konuşmacı Notları:**

Kapanış 30–40 saniye: katkıyı tek cümlede toplayın ("altyapı ve metodoloji"), sonraki adımı tek cümlede söyleyin ("final eğitim artifact review ve corpus acceptance"), sorulara geçin. Beklenen sorular ve cevaplar `docs/FRIDAY_PRESENTATION_PLAN_V1.md` §10'dadır.

**Kanıt / Kaynak Yolları:**

- `docs/FINAL_TECHNICAL_REPORT_V1.md` §13 (final contribution); `docs/FRIDAY_PRESENTATION_PLAN_V1.md` §10 (Q&A)

**Overclaim Sınırı:** "Bitmiş genel amaçlı onarım sistemi" veya "PDB faydası gösterildi" denemez; katkı altyapı + metodoloji + dürüst ilk bulgulardır.

**Kısa Anlatım Durumu:** ZORUNLU (kısa anlatımda yol haritası özeti bu slaytta tek cümleyle söylenir)

---

## Ek A — Kısa Anlatım (10–12 dk) Sırası ve Süreleri

Tek deck, iki anlatım. Aşağıdaki sıra, `Kısa Anlatım Durumu` alanlarıyla tutarlıdır; süreler `docs/FRIDAY_PRESENTATION_CUE_SHEET_V1.md` §2 ile aynıdır.

| Sıra | Slayt | Durum | Konuşma süresi |
|---|---|---|---|
| 1 | 1 — Kapak | ZORUNLU | 0,5 dk |
| 2 | 2 — Kapsam ve durum haritası | ZORUNLU | 1,0 dk |
| 3 | 3 — Dürüst durum özeti | ZORUNLU | 0,5 dk |
| 4 | 4 — Mimari | ZORUNLU | 1,0 dk |
| 5 | 8 — Task 9 kanıtı | ZORUNLU | 1,0 dk |
| 6 | 10 — V4 bulguları | ZORUNLU | 1,5 dk |
| 7 | 11 — Neden repair yok | ZORUNLU | 1,0 dk |
| 8 | 13 — QLoRA durumu | ZORUNLU | 1,0 dk |
| 9 | 14 — Sınırlamalar | ZORUNLU | 1,0 dk |
| 10 | 16 — Demo geçişi (konuşma kısmı) | ZORUNLU | 0,5 dk |
| 11 | 17 — Kapanış | ZORUNLU | 0,5 dk |
| | Konuşma alt toplamı | | 9,5 dk |
| | Tek-task demo + geçişler (Slayt 16 demo bloğu) | | 2,0 dk |
| | **Toplam (Q&A hariç)** | | **11,5 dk** |
| | Q&A | | Ayrı — toplama dahil değil |

Kısa anlatımda atlanan slaytlar ve katlanma yeri: 5 (state machine demo'da canlı görülür), 6 ("gerçek PDB mekanizması var, canlı PDB yok" — Slayt 8 ve demo sırasında), 7 (dataset tek satır Slayt 3'te), 9 (QuixBugs altyapısı tek satır Slayt 10 girişinde), 12 (dersler Slayt 11 kapsamında), 15 (üç ölçülebilir adım tek cümleyle Slayt 17'de).

## Ek B — Ana Anlatım (20–25 dk) Süre Tablosu

Süreler `docs/FRIDAY_PRESENTATION_CUE_SHEET_V1.md` §1 ile aynıdır.

| Slayt | Konu | Konuşma süresi |
|---|---|---|
| 1 | Kapak | 0,5 dk |
| 2 | Kapsam: 27 madde, 7/10/3/7/0 | 1,5 dk |
| 3 | Dürüst durum özeti | 1,0 dk |
| 4 | Mimari | 2,0 dk |
| 5 | State machine ve typed tools | 1,0 dk |
| 6 | PDB yolu ve independent verifier | 2,0 dk |
| 7 | Dataset ve model kararları | 1,0 dk |
| 8 | Task 9 kanıtı | 2,0 dk |
| 9 | QuixBugs altyapı kanıtı | 1,0 dk |
| 10 | Kayıtlı V4 bulguları | 2,0 dk |
| 11 | Neden verifier-confirmed repair yok | 1,5 dk |
| 12 | Protokol/bütçe/kanıt kontratı dersleri | 1,0 dk |
| 13 | QLoRA durumu | 1,5 dk |
| 14 | Sınırlamalar | 1,0 dk |
| 15 | Yol haritası | 1,0 dk |
| 16 | Demo geçişi (konuşma kısmı) | 0,5 dk |
| 17 | Kapanış | 1,0 dk |
| | Konuşma alt toplamı (1–15: 20,0 dk; 16–17: 1,5 dk) | 21,5 dk |
| | Deterministik demo + geçişler (Slayt 16 demo bloğu) | 3,0 dk |
| | **Toplam (Q&A hariç)** | **24,5 dk** |
| | Q&A | Ayrı — toplama dahil değil |

Zaman baskısı altında önce atlanacaklar: Slayt 5, 7, 9, 12 (OPSİYONEL) — toplamda 4,0 dk kazandırır; demo bloğu asla kısaltılmaz. Prova hedefi: konuşma bölümü 21,5 dk'nın üzerine çıkmamalıdır; bu matematiksel toplamdır, prova ile azaltma vaat edilmez.

---

## Kaynakların bütünlüğü notu

Bu deck, `docs/FRIDAY_PRESENTATION_PLAN_V1.md` §4'teki kanıt tablosunu, §6 (denilebilecekler) ve §7 (denilemeyecekler) listelerini ve §8 (QLoRA reçetesi) bölümünü uygular. Plan §9 (contingency), §10 (Q&A) ve §11 (checklist) bu deck'le birlikte çalışır. Durable iddia olarak `_ai-review/` veya `operator/` yollarına atıf yapılmaz.
