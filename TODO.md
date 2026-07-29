# Agentic Debugging Internship TODO

## Durum notu

Aşağıdaki faz listesi, stajın orijinal geniş araştırma/ürün planını temsil eder.
Bu plan içinde bazı maddeler, fine-tuning veya RAG üzerinden değil,
deterministic tool'lar ve verifier-backed bir controller agent üzerinden
(Task 1-9), ardından Task 10A real-model evaluation harness, Task 10B-R1 live
protocol/accounting repair ve Task 10B-R3 invalid-directive retry feedback
repair'i ile tamamlanmış ve kabul edilmiştir. Task 10B-R3'ün accepted commit'i
`1bb1d5251cc732f331ce2f5fdd163d9e46309d29`, live wire protocol sürümü
`1.2`'dir.

Private operator runner üzerinden tek fixture, iki policy ve policy başına iki
repetition içeren dört-case descriptive matrix tamamlandı. Static policy 2/2
case'i çözdü; PDB-on-uncertainty 0/2 case çözdü ve iki case de PDB açılmadan
`invalid_model_response` ile sonlandı. Altı feedback episode'unun dördünde
legal-directive recovery gözlendi. Bu küçük, fixture-specific sonuç causal PDB
etkinliği, policy üstünlüğü veya genel model güvenilirliği kanıtı değildir.

Maddeler aşağıda `[x]` ile işaretlenmiş ve neye dayandığı not edilmiştir; tam
kanıt kaydı için `docs/PROJECT_TRACKER.md`'e bakınız. Mevcut aktif mühendislik
önceliği, PDB policy yolunun PDB'ye ulaşmadan neden illegal veya malformed
directive ürettiğini offline olarak incelemektir. Dataset expansion, broader
evaluation, fine-tuning, RAG genişletmesi, preference optimization (DPO/RLHF),
containment hardening ve final teknik rapor hâlâ future work olarak kalır; bu
not stajın veya daha geniş araştırma projesinin tamamlandığı anlamına gelmez.

## Daily requirement

- [ ] Her gün yapılanları 1 sayfa olacak şekilde staj defteri olarak yaz.

## Phase 1 — Literature Review

- [ ] Debugging, automated debugging, fault localization ve program repair konularında literatür taraması yap.
- [ ] LLM-based debugging çalışmalarını incele.
- [ ] Agentic debugging, tool-using agents ve multi-agent debugging çalışmalarını incele.
- [ ] Geleneksel debugging, LLM-based debugging ve agentic debugging yaklaşımlarını karşılaştır.
- [ ] SWE-Agent, OpenHands, AutoCodeRover, Agentless ve ChatDBG gibi sistemleri incele.

## Phase 2 — Dataset Research

- [ ] Hugging Face ve açık kaynak platformlarda debugging ve bug-fix veri setlerini araştır.
- [ ] SWE-bench, SWE-bench Lite, SWE-bench Verified, BugsInPy, Defects4J ve QuixBugs veri setlerini karşılaştır.
- [ ] Fine-tuning, RAG ve değerlendirme için uygun veri setlerini seç.
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

- [ ] Sonuçları başarı oranı, localization accuracy, test pass rate, maliyet ve çalışma süresi açısından değerlendir.
- [ ] Çalışan bir agentic debugging demosu ve teknik rapor hazırla.
