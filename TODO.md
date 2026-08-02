# Agentic Debugging Internship TODO

## Durum notu

Aşağıdaki faz listesi, stajın orijinal geniş araştırma/ürün planını temsil eder.
Bu plan içinde bazı maddeler, fine-tuning veya RAG üzerinden değil,
deterministic tool'lar ve verifier-backed bir controller agent üzerinden
(Task 1-9), ardından Task 10A real-model evaluation harness, Task 10B-R1 live
protocol/accounting repair ve Task 10B-R3 invalid-directive retry feedback
repair'i ile tamamlanmış ve kabul edilmiştir. Task 10B-R5'in accepted source/merge commit'i
`63fa27cc4d30490b9770ead3ce14b4b6d3ddf222`, live wire protocol sürümü
`1.3`'tür.

### Routing ve iş sahipliği (2026-08-02 itibarıyla)

- Model kullanımı açıkça yetkilendirilmiş görevlerde varsayılan implementation
  route'u, operator'ün OpenCode Go aboneliği üzerinden DeepSeek V4 Flash'tır.
  Bu route, paired-pilot v2 kontratında (`docs/QUIXBUGS_PAIRED_PILOT_V2.md`,
  `research/quixbugs/PAIRED_PILOT_V2.json`) fail-closed abonelik koşullarıyla
  dondurulmuştur: Zen route, free-tier ikamesi, Ollama, alternatif provider,
  model substitution, metered/paid-overage/per-call billing fallback yoktur;
  ilk provider çağrısından önce abonelik entitlement ve billing-route kanıtı
  kurulamazsa kampanya o çağrıdan önce bloklanır. Eski OpenCode Zen
  free-model matrix'i yalnızca historical, descriptive kayıttır.
- Literatür taraması, deep research, kaynak doğrulama ve geniş karşılaştırmalı
  araştırma; coding-agent oturumları dışında, ayrı bir ChatGPT
  konuşmasındaki GPT-5.6 High tarafından yürütülür. Coding agent'lar yalnızca
  review edilmiş repository araştırma artifact'lerini tüketebilir; görevleri
  açık uçlu araştırma kampanyalarına genişletemez.
- Araştırma çıktıları, tracked project artifact'lerine review edilip
  işlenmeden authoritative değildir. Her görev, provider/model çalıştırmak
  için hâlâ ayrı ve açık yetkilendirme gerektirir. Coding agent'lar, görev
  açıkça yetkilendirmedikçe ek model, araştırma agent'ı, MCP, benchmark veya
  paid servis başlatamaz. Bu sorumlulukların operasyonel sahibi
  `CURRENT_AGENT_ROSTER.md` dosyasıdır.
- Bu routing güncellemesi, aşağıdaki faz listesindeki hiçbir literatür, SFT,
  RAG, DPO veya empirical-evaluation maddesini tamamlanmış işaretlemez.

Private operator runner üzerinden tek fixture, iki policy ve policy başına iki
repetition içeren dört-case descriptive matrix tamamlandı. Static policy 2/2
case'i çözdü; PDB-on-uncertainty 0/2 case çözdü ve iki case de PDB açılmadan
`invalid_model_response` ile sonlandı. Altı feedback episode'unun dördünde
legal-directive recovery gözlendi. Bu küçük, fixture-specific sonuç causal PDB
etkinliği, policy üstünlüğü veya genel model güvenilirliği kanıtı değildir.

Maddeler aşağıda `[x]` ile işaretlenmiş ve neye dayandığı not edilmiştir; tam
kanıt kaydı için `docs/PROJECT_TRACKER.md`'e bakınız. BugsInPy execution hâlâ
license gate nedeniyle bloke; bu blok kaldırılmadı, sadece bekletiliyor.
Bu blok nedeniyle dar kapsamlı bir QuixBugs (Python `gcd`) fallback
resource-limited real no-model smoke tamamlandı ve kabul edildi:
pinned revision `4257f44b0ff1181dedaedee6a447e133219fcebf`, verdict
`ACCEPT CANDIDATE — REAL SMOKE PASSED`; bkz. `docs/QUIXBUGS_SMOKE_USAGE_V1.md`.
Bu tek-task smoke, aynı pinned revision üzerinde sekiz-task no-model gold
baseline'a genişletildi (`gcd`, `bucketsort`, `find_in_sorted`, `flatten`,
`kth`, `hanoi`, `is_valid_parenthesization`, `kheapsort`): 8/8 seçilen task
çözüldü (gold patch uçtan uca doğrulandı), verdict
`ACCEPT CANDIDATE — EIGHT-TASK BASELINE COMPLETE`; bkz.
`docs/QUIXBUGS_EIGHT_TASK_BASELINE_V1.md`. İkisi de sadece altyapıyı
doğrular; model, PDB veya geniş benchmark kampanyası
çalıştırılmadı.

2026-07-31 tarihinde, Model/RAG/Fine-Tuning/DPO Decision Gate v1
(`docs/MODEL_RAG_SFT_DPO_DECISION_GATE_V1.md`) ve Final Technical Report
and Demo Package v1 (`docs/FINAL_TECHNICAL_REPORT_V1.md`,
`docs/DEMO_GUIDE_V1.md`) documentation-only olarak tamamlandı ve kabul
edildi (baseline `2236775`). Decision Gate; future model-access strategy
için PROCEED (dar kapsamlı — mevcut free-tier route üzerinde tek QuixBugs
task'ı, sadece static-baseline policy), repository RAG için NO-GO-FOR-NOW,
SFT için DEFER, DPO/preference optimization için NO-GO-FOR-NOW kararlarını
kaydediyor; sekiz-task QuixBugs baseline'ın yalnız altyapı doğrulaması için
yeterli olduğunu, model seçimi, training veya generalization iddiası için
yeterli olmadığını açıkça belirtiyor. Final rapor ve demo guide mevcut
altyapıyı (Task 9 offline demo, QuixBugs tek-task ve sekiz-task WSL entry
point'leri) yeniden kullanıyor; paralel bir demo framework'ü oluşturulmadı.
Bu çalışma sırasında da hiçbir model, provider, RAG, training, PDB veya paid
API çalıştırılmadı ve kabul edilen benchmark kampanyaları yeniden
çalıştırılmadı. Dataset execution genişlemesi (BugsInPy'nin license block'u
hâlâ açık), fine-tuning, RAG genişletmesi ve preference optimization
(DPO/RLHF) hâlâ future work olarak kalır; bu not stajın veya daha geniş
araştırma projesinin tamamlandığı anlamına gelmez.

## Daily requirement

- [ ] Her gün yapılanları 1 sayfa olacak şekilde staj defteri olarak yaz.

## Phase 1 — Literature Review

- [ ] Debugging, automated debugging, fault localization ve program repair konularında literatür taraması yap.
- [ ] LLM-based debugging çalışmalarını incele.
- [ ] Agentic debugging, tool-using agents ve multi-agent debugging çalışmalarını incele.
- [ ] Geleneksel debugging, LLM-based debugging ve agentic debugging yaklaşımlarını karşılaştır.
- [ ] SWE-Agent, OpenHands, AutoCodeRover, Agentless ve ChatDBG gibi sistemleri incele.

## Phase 2 — Dataset Research

- [x] Hugging Face ve açık kaynak platformlarda debugging ve bug-fix veri setlerini araştır. (Dataset and Evaluation Decision v1.)
- [x] SWE-bench, SWE-bench Lite, SWE-bench Verified, BugsInPy, Defects4J ve QuixBugs veri setlerini karşılaştır. (Dataset and Evaluation Decision v1.)
- [x] Fine-tuning, RAG ve değerlendirme için uygun veri setlerini seç. (BugsInPy primary, QuixBugs fallback; sequencing decisions recorded in the decision document. BugsInPy execution license-gated; QuixBugs gcd resource-limited real no-model smoke completed and accepted — bkz. docs/QUIXBUGS_SMOKE_USAGE_V1.md.)
- [ ] Veri setlerini analiz et ve eğitim/test ayrımını hazırla.

## Phase 3 — Model and Fine-tuning

- [ ] Seçilen açık kaynak kod modelini belirle.
- [ ] Veri seti modele uygun değilse instruction-response formatına dönüştür.
- [ ] LoRA veya QLoRA ile supervised fine-tuning yap.
- [ ] Fine-tuning öncesi ve sonrası modeli karşılaştır.

## Phase 4 — RAG and Agent Tools

- [ ] Repository kodları, testler, issue açıklamaları ve hata mesajları için RAG sistemi kur.
- [ ] Fine-tuned modeli RAG sistemiyle birleştir.
- [x] Modelin kullanacağı dosya okuma, kod arama, test çalıştırma ve patch uygulama araçlarını geliştir. (Tamamlandı — deterministic file-read/code-search/test-run/patch-apply tools; bkz. docs/PROJECT_TRACKER.md Task 2-3.)
- [x] Debugging agentini oluştur. (Tamamlandı — controller state machine ve Task 9 uçtan uca demonstration; bkz. docs/PROJECT_TRACKER.md.)
- [ ] Modelin hata konumunu bulmasını, root cause belirlemesini ve patch üretmesini sağla.

## Phase 5 — Preference Optimization

- [ ] Başarılı ve başarısız debugging çıktılarından preference veri seti oluştur.
- [ ] DPO veya uygun bir RLHF yöntemi uygula.
- [ ] Base model, fine-tuned model, RAG destekli model ve agentic sistemi karşılaştır.

## Phase 6 — Debugger Adapter

- [x] PDB, GDB veya LLDB için bir debugger adapter geliştir. (Tamamlandı — yalnızca PDB için; GDB/LLDB henüz geliştirilmedi. bkz. docs/PROJECT_TRACKER.md Task 4A-4D.)
- [ ] Fine-tuned modelin debugger komutları üretmesini ve çıktıları yorumlamasını sağla. (Fine-tuning henüz başlamadı; typed-action eşdeğeri fine-tuning olmadan mevcut controller/harness üzerinden çalışıyor.)
- [x] Modelin breakpoint koymasını, değişkenleri incelemesini, stack trace okumasını ve adım adım debug yapmasını sağla. (Tamamlandı — bkz. docs/PROJECT_TRACKER.md Task 4B-4D.)
- [x] Modelin debugger etkileşiminden sonra patch üretmesini ve testlerle doğrulamasını sağla. (Tamamlandı — verifier-backed patch workflow, Task 7 ve Task 9 entegrasyonu; bkz. docs/PROJECT_TRACKER.md.)

## Phase 7 — Evaluation and Final Report

- [ ] Sonuçları başarı oranı, localization accuracy, test pass rate, maliyet ve çalışma süresi açısından değerlendir. (Metrikler tanımlı; ancak henüz hiçbir external dataset üzerinde gerçek bir model çalıştırılmadığı için bu metriklere karşı raporlanacak bir model sonucu yok.)
- [x] Çalışan bir agentic debugging demosu ve teknik rapor hazırla. (Tamamlandı 2026-07-31 — Final Technical Report v1 ve Demo Guide v1; bu bir altyapı/evaluation-platform demosu ve raporudur, model debugging performance demosu değildir. Bkz. `docs/FINAL_TECHNICAL_REPORT_V1.md`, `docs/DEMO_GUIDE_V1.md`, `docs/MODEL_RAG_SFT_DPO_DECISION_GATE_V1.md`.)
