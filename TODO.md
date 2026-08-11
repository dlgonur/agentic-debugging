# Agentic Debugging Internship TODO

## Durum notu

**2026-08-11 final closeout:** Proje kabul edilen bounded-negative yol üzerinde
**COMPLETE** olarak kapanmıştır (S9 final closeout). Tek canonical final
status/handoff otoritesi: `Agentic_Debugging_Project_Closeout_2026-08-11.md`.
S8 deliverables tamamlandı: `docs/FINAL_TECHNICAL_REPORT_V2.md` ve
`diary/diary.md` (2026-08-11'e kadar, S9 subsection dahil). S7 literature
closeout DONE (20 çalışma, `677992f`). Professor TODO #23/#24/#25 =
**CLOSED — BOUNDED NEGATIVE** (engineering capability YES, positive real-model
success NO, bounded-negative evidence YES); S4 (fine-tuned+RAG) =
**CLOSED — PARTIAL / COMPUTE-CONSTRAINED** (primary correctness NOT_EVALUATED,
no RAG success/failure claim). Bu kapalı statüler yeni deney yetkilendirmesi
değildir. Aşağıdaki 2026-08-07 notları tarihsel statü kayıtlarıdır.

**2026-08-07 reconciliation:** Current status is reconciled against reachable
history at `1e680b1` in
`docs/REPOSITORY_STATUS_RECONCILIATION_2026-08-07.md`. This corrects stale
pre-merge claims about the literature syntheses (`3c23b6e`), post-mortem PDB
work (`f7ba129`..`e92634e`), and RAG/comparison/preference infrastructure
(`1e680b1`). External QLoRA state is recorded but not modified.

**2026-08-07 local completion follow-ups:** Tracker 7.1.2 now has a strict
root-cause explanation assessment/aggregation contract, and bounded
post-mortem PDB evidence now uses the existing `get_failure_trace` controller
action and canonical event/replay path. See
`docs/ROOT_CAUSE_EXPLANATION_METRIC_V1.md` and
`docs/POST_MORTEM_TRAJECTORY_INTEGRATION_V1.md`. These are infrastructure
completions, not live-model performance evidence.

**2026-08-07 full-suite follow-up:** The historical 32-node synthetic
OpenCode wrapper failure family is repaired. Its actual cause was a
test-only compiled-forwarder cache collision across distinct target scripts,
not generic OS resource pressure. The post-fix full suite completed with
3733 passed and 3 skipped; see
`docs/FULL_SUITE_FORWARDER_CACHE_REPAIR_V1.md`. Production provider and route
gates were not changed.

Aşağıdaki faz listesi, stajın orijinal geniş araştırma/ürün planını temsil eder.
Bu plan içinde bazı maddeler, fine-tuning veya RAG üzerinden değil,
deterministic tool'lar ve verifier-backed bir controller agent üzerinden
(Task 1-9), ardından Task 10A real-model evaluation harness, Task 10B-R1 live
protocol/accounting repair ve Task 10B-R3 invalid-directive retry feedback
repair'i ile tamamlanmış ve kabul edilmiştir. Task 10B-R5'in accepted source/merge commit'i
`63fa27cc4d30490b9770ead3ce14b4b6d3ddf222`, live wire protocol sürümü
`1.3`'tür.

### Routing ve iş sahipliği (2026-08-05 itibarıyla)

- **2026-08-05 — kampanya altyapısı main'de kabul edildi; V4 attempt kaydı; QLoRA implementasyonu.** Kampanya altyapısı ve paired-pilot v4 terminal kontratı `main` üzerinde `0abb588` commit'ine kadar kabul edildi (`eb63c76` kampanya bütçe/verifier yolunu sertleştirdi; `9f53df7` gerçek V4 interrupted budget terminalini ekledi; `0abb588` terminal, exact-identity validation ve fail-closed budget-exhaustion provenance altyapısını ekledi — run persistence, campaign-record validation ve attempt-package verification üzerinden). Kabul edilen kampanya doğrulaması: odaklı kampanya entegrasyon suite'i 389 test geçti; sınırlandırılmış tam suite 3394 passed, 3 skipped ve aynı altı önceden bilinen OpenCode wrapper/transport failure'ı üretti — yeni failure yok. Kayıtlı case kimliği düzeltmesi: `0abb588`'de kabul edilen sanitized attempt fixture ve replay assertion'ları iki gözlemlenen şekli yanlış frozen case'lere bağlamıştı; fixture/test identity eşlemesi korunmuş campaign record, private transport kanıtı, provider-reported cost toplamları ve frozen v4 case sırasına göre düzeltildi ve `main` üzerinde `fc7c85b` commit'inde kabul edildi — artık pending bir "Friday-readiness candidate" değildir. Production budgets, frozen manifest, route, provider, authorization ve controller davranışı değişmedi.
- Kayıtlı V4 attempt (`quixbugs-paired-pilot-v4-attempt-3b5d7488...`, ignored `operator/` altında korunuyor) kesin sınırlarıyla: **Case 1** `find-in-sorted` / `pdb-on-uncertainty` (order 1): 10 provider process, 9 logical call, 1 retry, 26.139 public-evidence byte, tüm patch denemeleri malformed unified diff (hunk header `old_count=7` ama gövde 6 satır) nedeniyle reddedildi, candidate uygulanmadı, verifier çalışmadı, cost `$0.007378`, terminal `INFRASTRUCTURE_ERROR`. **Case 2** `find-in-sorted` / `static-baseline` (order 2): 15 provider process, 14 logical call, 1 retry, 38.534 byte, patch uygulandı ve Validate ziyaret edildi, verifier çalışmadı, run interrupted, cost `$0.012323`; orijinal kampanya `ABORTED/BUDGET_EXCEEDED` ile abort etti, kabul edilen repair artık her iki şekli schema-valid terminal olarak materialize ediyor (`INFRASTRUCTURE_ERROR`, `ABORTED/INTERRUPTED`). Bu bir verifier-confirmed live repair, canlı PDB yararı veya post-repair provider kampanyası değildir; Authorized Six-Case Live Campaign açık ve yetkilendirilmemiş durumda.
- QLoRA deney implementasyonu (tracked `independent_ai` audit kontratı ve run-provenance dahil), unmerged `experiment/qlora-patch-pilot-v1` branch'inde `3f0d3e7` commit'inde kabul edildi (FirstMate implementation review geçti). Owner suite review: 3457 passed, 3 skipped, 36 ilişkisiz önceden var olan OpenCode transport/wrapper failure, QLoRA odaklı failure yok. Owner-delegated bağımsız FirstMate AI audit'i 75 frozen satır için dışarıda tamamlandı: 39 ACCEPT / 36 REJECT (insan audit'i değildir; insan review olarak tanımlanamaz); final corpus acceptance ve fail-closed audit/corpus-quality kararları bekliyor. Final QLoRA training 2026-08-05'te FirstMate tarafından dışarıdan yetkilendirildi; kabul edilmiş bir final-training artifact'i/result'ı henüz yok, sonuçlar FirstMate artifact review'ini bekliyor. Held-out generation ve base-versus-tuned karşılaştırması hâlâ yetkilendirilmemiş. `3f0d3e7`'deki tracked freeze_record'daki `final_training_authorized: false` tarihsel branch-bound freeze kaydıdır; güncel dış yetkilendirme kanıtı değildir. Bu not hiçbir instructor TODO maddesini tamamlanmış işaretlemez.



- **2026-08-06 — Repair 1: RAG/comparison/preference contract hardening (offline).** Aynı branch üzerinde tek kapsamlı contract-hardening geçişi (yeni mimari yok): (1) candidate patch artık `raw_output`'a sıkı şekilde bağlı — `patch_extraction` exact/substring sözleşmesi, load'da yeniden inşa + substring/SHA-256 eşitliği + offset sınırları; ilgisiz geçen patch, bir-byte değişiklik ve raw-output dışı offset'ler reddedilir; (2) attempt `role` ayrımı — `evaluation` (birincil; (task,condition) başına en fazla bir) vs `preference-fixture` (yardımcı; primary aggregate/delta'lara asla girmez; eski sentetik `base 0.50 vs tuned 1.00` sonucu yapısal olarak imkânsız, regresyon testi ile); (3) RAG index/retrieval/RagContext artifact'ları build ve load'da her identity alanını yeniden hesaplayıp doğrular (index_id, corpus_digest, chunk id, query/retrieval id, selection bytes, document/chunk caps; tampering testleri); (4) free-form JSON payload'lar recursive bounds ile doğrulanır (nested NaN/Infinity schema load'da reddedilir); (5) imported-attempt: `EvaluationInputError` branch'inde `UnboundLocalError` imkânsız, kategori ayrımı (NO_PATCH/PATCH_INVALID/PATCH_NOT_APPLIED/SYNTAX_FAILED/VERIFIER_FAILED), response 64 KiB marker-inclusive bound (2/3/4-byte UTF-8 sınır testleri), artifact telemetry (runtime/memory/cost/tokens) ve external provider/network telemetry ayrı taşınır; `memory_bytes` normalize metrik/CSV'de; (6) preference: pair identity response/patch/verifier-evidence hash'lerini bağlar ve load'da yeniden hesaplanır; contamination tam yanıt üzerinde (storage cutoff öncesi) kontrol edilir; audit tam anahtar seti; (7) chunking: symbol dışı satırlar (docstring/import/constant/kod arası/trailing) deterministik gap chunk'larla korunur — tam satır kapsamı kanıtlandı; (8) demo/live boundary'leri yalnızca doğrulanmış `RagContext` kabul eder. Demo sonucu: 4 primary condition × 2 task; condition başına tam 2 primary attempt (base ek olarak 2 labeled auxiliary preference-fixture); resolved 2/2 rate 1.0 her condition'da (sentetik tuned üstünlüğü yok, delta 0); replay-valid 4/4; cleanup cleaned 10/10; canonical fixture değişmedi; local provider/network 0/0; external telemetry ayrı; 4 verifier-backed preference pair. Testler: yeni suite 188 passed (rag 92, comparison 40, live-request, preference, integration 4) + etkilenen yüzeyler 707 passed; compileall exit 0; `git diff --check` temiz. Karar kaydı `docs/RAG_COMPARISON_DECISION_V2.md` §5. Hiçbir provider, live campaign, WSL, BugsInPy, QLoRA, DPO/RLHF çalıştırılmadı; commit/merge/push yapılmadı.
- **2026-08-06 — Friday RAG/comparison/preference engineering sprint (offline, infrastructure-only).** `goal/friday-rag-comparison-v1` branch'inde baseline `e92634e3` üzerinde: (1) deterministik repository-native lexical RAG (`agentic_debugger/rag/`; fixture-scoped default + declared corpus-root modu; source/test/issue/failure dokümanları; oracle-projeksiyon dışlama testleri; explicit exclusion kuralları; `repository-index-v1` + `retrieval-result-v1` strict artifact'ları; revision binding; budget'lar; fail-closed); (2) opsiyonel RAG context enjeksiyonu (demo/model-adapter ve `LiveModelAdapter` additive `rag_context` seam'leri; default request/case byte-identity kanıtlı; 20 KB public-request bound pre-transport fail-closed; frozen QuixBugs runner değişmedi); (3) unified comparison harness (`agentic_debugger/comparison/`; strict `generation-artifact-v1` import + native agentic mod; normalized `comparison-v1` metrikler; JSON/CSV/Markdown + aggregates + baseline delta; CLI `build-index|retrieve|import-attempt|compare|export-preferences|demo`; unique output roots); (4) preference-pair exporter (`agentic_debugger/preference/`; ordered verifier-backed rules; `preference-pair-v1`; held-out + oracle-answer contamination + duplicate/same-response/no-evidence guard'ları; JSONL + audit; DPO/RLHF yok). Deterministik iki-task demo: index/retrieval/provenance, imported base+tuned (sentetik `offline-deterministic-demo` kimlikler) + non-repair fixture (verdict verifier-decided), native agentic + RAG-agentic same-patch parity, 4-condition rapor, preference pairs; replay-valid, CLEANED, canonical fixture değişmedi, 0 provider / 0 network; deterministik view iki koşu arası byte-identical (timing hariç). Testler: yeni rag/comparison/preference unit + integration suite'leri ve etkilenen yüzeyler (demo 289, live/verifier/patcher/workspace/search 418) geçti; compileall exit 0; `git diff --check` temiz. Karar kaydı: `docs/RAG_COMPARISON_DECISION_V2.md` — v1 decision gate'in RAG NO-GO'su yalnızca bu yetkilendirilmiş offline altyapı kapsamı için supersede edildi; fine-tuned+RAG performansı, gerçek base-versus-tuned karşılaştırması, production preference corpus ve DPO/RLHF hâlâ açık. Instructor TODO: RAG sistemi (Phase 4 RAG item, tracker 4.1) kanıtla kapatıldı; fine-tuned+RAG birleştirme, gerçek dört-condition karşılaştırma ve production preference corpusu partial/open kaldı (sentetik kimlikler model performansı iddiası değildir). Hiçbir provider, live campaign, WSL, BugsInPy, QLoRA, DPO/RLHF çalıştırılmadı; commit/merge/push yapılmadı.
- **2026-08-05 — Friday professor delivery bundle (offline, documentation + rehearsal only).** Kabul edilmiş kaynak baseline `456f0e9` (kabul edilmiş sunum plan/deck/cue delivery commit'i); kampanya altyapısı `0abb588` ile, V4 identity düzeltmesi `fc7c85b` ile kabul edildi. `docs/FRIDAY_DELIVERY_MANIFEST_V1.md` (manifest, evidence index, exact commands, fallbacks), `docs/FRIDAY_PREFLIGHT_CHECKLIST_V1.md` (final preflight/rehearsal checklist), `docs/FRIDAY_STATUS_HANDOFF_V1.md` (status handoff + post-Friday batches B1–B7); plan/deck/cue sheet v1.2; README, tracker (Last Updated), DEMO_TASK9 (`--list-tasks` `--output-dir` requirement), report section order, ve diary 2026-08-05 entry güncellendi. Bu bundle, `456f0e9` üzerine FirstMate review'i sırasında inşa edilen commit edilmemiş bir adaydır; entegrasyon commit'i henüz bilinmiyor. Taze single-task demo provası çalıştırıldı ve doğrulandı (exit 0; 2 case; RESOLVED 2/2; F2P 2/2; P2P 4/4; 0 provider/0 network; workspace CLEANED). Hiçbir instructor TODO maddesi işaretlenmedi; hiçbir provider/kampanya/WSL/eğitim/benchmark çalıştırılmadı; FirstMate entegrasyonundan önceki coding-agent build/prova fazında commit yapılmadı.

- Model kullanımı açıkça yetkilendirilmiş görevlerde varsayılan implementation
  route'u, operator'ün OpenCode Go aboneliği üzerinden DeepSeek V4 Flash'tır.
  Bu route'un görev/sıra/bütçe/qualification authority'si paired-pilot v2
  kontratından gelir; bir sonraki live deneme ise aynı kontratı koruyan
  `research/quixbugs/PAIRED_PILOT_V4.json` (canonical SHA-256
  `020dfc1f7b8f23aa96a4d7c7942429e306cc290906abfed5ce96cde22b90354d`) ile
  yürütülmelidir. v4, verifier-authoritative classification ve v3'ün
  terminalize edemediği post-apply/Validate-visited/verifier-executed
  public-evidence budget-exhaustion şekillerini ekler. v3,
  `VALIDATION_NOT_REACHED` ve candidate provenance ekler. Zen route,
  free-tier ikamesi, Ollama, alternatif provider,
  model substitution, metered/paid-overage/per-call billing fallback yoktur;
  ilk provider çağrısından önce abonelik entitlement ve billing-route kanıtı
  kurulamazsa kampanya o çağrıdan önce bloklanır. CLI v2 default'unu yalnızca
  compatibility için korur; v4 operator komutları manifest'i explicit verir.
  Eski OpenCode Zen
  free-model matrix'i yalnızca historical, descriptive kayıttır.
- 2026-08-04 v3 live attempt `fddf1e39...` BUDGET_EXCEEDED ile dürüstçe
  abort oldu: case, baseline reproduction'dan Validate'e ve verifier
  execution'a kadar ilerledi (12 logical call, 13 process attempt, 1 bounded
  retry, patch applied), ancak frozen v3 terminal matrix'i completed
  post-apply public-evidence exhaustion şeklini temsil edemedi. Bu sonuç
  v3'ün preregistered kontratının beklenen davranışıdır; v4 bu şekli
  preregister eder.
- Non-blocking follow-up TODO: campaign ledger `created_at`/`updated_at`
  alanları campaign-start `reference_time`'ını kullanır (runner
  `_utc_now(clock)`), bu yüzden ledger zaman damgaları gerçek campaign sonunu
  yansıtmaz; ayrı bir görevde düzeltilmelidir.
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

### Live-runner altyapısı (2026-08-02, runner-only)

Paired-pilot v2 live-runner altyapısı (yalnızca runner görevi) tamamlandı ve
doğrulandı: `scripts/quixbugs_live_runner_v2.py` üzerinden katı versioned
authorization kontratı, pre-provider route gate'i, frozen altı-case sıralı
orchestration, fail-closed stop/abort davranışı, deterministik versioned
çıktı paketi ve no-rerun enforced durable attempt ledger; mevcut paired-pilot
entry point'ine `preflight`, `template` ve `live` (--preflight-only) olarak
bağlandı. Gerçek live yürütme; ayrı operatör authorization artifact'i, kabul
edilen baseline, başarılı route gate'i ve açıkça yapılandırılmış provider
transport + case runner gerektirir; bu görevde hiçbiri yoktur, bu yüzden
yalnızca synthetic transport'lar, geçici fixture'lar ve deterministik test
double'ları kullanıldı, provider çağrı sayacı sıfır kanıtlandı. Dokümanlar:
`docs/QUIXBUGS_PAIRED_PILOT_V2_AUTHORIZATION_V1.md` ve
`docs/QUIXBUGS_PAIRED_PILOT_V2_LIVE_RUNNER_V1.md`; non-authorizing şema
referansı `research/quixbugs/PAIRED_PILOT_V2_AUTHORIZATION_TEMPLATE.json`
(validator tarafından reddedilir; gerçek authorization'lar tracked dışı
`operator/` dizininde yaşar). Ayrı ve gelecekteki görev: gerçek operator
authorization'ı + gerçek route evidence + açıkça yapılandırılmış
transport/case runner ile six-case live campaign yürütülmesi. Bu altyapı
görevi hiçbir live campaign, empirical evaluation, model-performance sonucu,
PDB etkinliği, RAG, SFT veya DPO çalıştırmadı ve bunları tamamlanmış
işaretlemez. Tarihsel OpenCode Zen kayıtları değişmeden historical kaldı.

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

- [x] Her gün yapılanları 1 sayfa olacak şekilde staj defteri olarak yaz. (Tamamlandı 2026-08-11 — consolidated `diary/diary.md` 2026-07-13 → 2026-08-11 chronology, S9 closeout subsection dahil; tarih kaynakları Git commit/frozen run timestamp'leri.)

## Phase 1 — Literature Review

- [x] Debugging, automated debugging, fault localization ve program repair konularında literatür taraması yap. (Tamamlandı 2026-08-05 — `docs/AUTOMATED_DEBUGGING_LITERATURE_SURVEY_V1.md`, commit `3c23b6e`; doğrulanmamış iddialar kapsam dışı bırakıldı.)
- [x] LLM-based debugging çalışmalarını incele. (Tamamlandı 2026-08-05 — `docs/LLM_BASED_DEBUGGING_LITERATURE_REVIEW_V1.md`, commit `3c23b6e`; ek frontier okuma gelecek iş olabilir fakat tamamlanmış bounded review'ı geçersiz kılmaz.)
- [x] Agentic debugging, tool-using agents ve multi-agent debugging çalışmalarını incele. (Tamamlandı 2026-08-11 — S7 focused literature closeout, 20 çalışma, `677992f`; evidence tier'ları korundu; bkz. `research/literature/agentic_debugging_literature_closeout_2026-08-11.md`.)
- [x] Geleneksel debugging, LLM-based debugging ve agentic debugging yaklaşımlarını karşılaştır. (Tamamlandı 2026-08-05 — `docs/DEBUGGING_APPROACH_COMPARISON_V1.md`, commit `3c23b6e`.)
- [x] SWE-Agent, OpenHands, AutoCodeRover, Agentless ve ChatDBG gibi sistemleri incele. (Tamamlandı — reviewed notes + system capability matrix.)

## Phase 2 — Dataset Research

- [x] Hugging Face ve açık kaynak platformlarda debugging ve bug-fix veri setlerini araştır. (Dataset and Evaluation Decision v1.)
- [x] SWE-bench, SWE-bench Lite, SWE-bench Verified, BugsInPy, Defects4J ve QuixBugs veri setlerini karşılaştır. (Dataset and Evaluation Decision v1.)
- [x] Fine-tuning, RAG ve değerlendirme için uygun veri setlerini seç. (Final karar: **SWE-rebench V2 = birincil authentic SFT / post-training veri seti**; **QuixBugs = kontrollü değerlendirme kohortu ve SFT eğitiminin tamamen dışında**; **BugsInPy = dataset araştırması sırasında değerlendirilen tarihsel aday, final birincil SFT kaynağı değil**. Sequencing decisions recorded in Dataset and Evaluation Decision v1; BugsInPy execution license-gated; QuixBugs gcd resource-limited real no-model smoke completed and accepted — bkz. docs/QUIXBUGS_SMOKE_USAGE_V1.md.)
- [x] Veri setlerini analiz et ve eğitim/test ayrımını hazırla. (Tamamlandı — SWE-rebench V2: 1,594 görev / 347 repo; frozen split 1,000 train / 150 validation / 444 unused; repo-overlap 0; seed `20260808`; ≤32K no-truncation view 940/135. QuixBugs SFT dışında tutuldu.)

## Phase 3 — Model and Fine-tuning

- [x] Seçilen açık kaynak kod modelini belirle. (Tamamlandı, external/branch-bound karar — `Qwen/Qwen2.5-Coder-7B-Instruct`, pinned revision; ayrı QLoRA repository/branch bu reconciliation'da değiştirilmedi.)
- [x] Veri seti modele uygun değilse instruction-response formatına dönüştür. (Tamamlandı — SFT formulation: input problem statement + oracle-file-localized exact pre-fix source; target `PATCH` + stored gold repair diff; localized-repair / repair-after-localization SFT.)
- [x] LoRA veya QLoRA ile supervised fine-tuning yap. (Tamamlandı — QLoRA SFT `Qwen/Qwen2.5-Coder-7B-Instruct` @ `c03e6d358207e414f1eca0bb1891e29f1db0e242`; definitive surviving checkpoint cp118.)
- [x] Fine-tuning öncesi ve sonrası modeli karşılaştır. (Tamamlandı — RAW vs cp118: negative executable-repair transfer; cp118 0/40 apply, 0/40 RESOLVED vs RAW 20/40 apply, 5/40 RESOLVED. "Fine-tuning kötüdür" iddiası değildir.)

## Phase 4 — RAG and Agent Tools

- [x] Repository kodları, testler, issue açıklamaları ve hata mesajları için RAG sistemi kur. (Tamamlandı 2026-08-06 — deterministik repository-native lexical RAG v1: fixture-scoped default + declared corpus-root modu; source/test/issue/failure dokümanları; oracle-projeksiyon dışlama; explicit exclusion kuralları; `repository-index-v1`/`retrieval-result-v1` strict artifact'ları; revision binding; budget'lar; fail-closed. Bu bir altyapı tamamlamasıdır; RAG'ın model performansına katkısı iddia edilmez. bkz. docs/REPOSITORY_RAG_V1.md, docs/RAG_COMPARISON_DECISION_V2.md.)
- [ ] Fine-tuned modeli RAG sistemiyle birleştir. (**CLOSED — PARTIAL / COMPUTE-CONSTRAINED** 2026-08-11 — frozen cp118+RAG treatment 10/40 geçerli pair üretti (frozen manifest sırasının ilk 10'u), kampanya compute fezibility nedeniyle durduruldu; primary correctness **NOT_EVALUATED**; RAG success/failure iddiası yok. Aktif gelecek görevi değildir.)
- [x] Modelin kullanacağı dosya okuma, kod arama, test çalıştırma ve patch uygulama araçlarını geliştir. (Tamamlandı — deterministic file-read/code-search/test-run/patch-apply tools; bkz. docs/PROJECT_TRACKER.md Task 2-3.)
- [x] Debugging agentini oluştur. (Tamamlandı — controller state machine ve Task 9 uçtan uca demonstration; bkz. docs/PROJECT_TRACKER.md.)
- [ ] Modelin hata konumunu bulmasını, root cause belirlemesini ve patch üretmesini sağla. (PARTIAL — static real-provider model→patch→verifier QuixBugs gcd'de verifier RESOLVED'a ulaştı (F2P 5/5, P2P 1/1); full dynamic debugger-informed chain gerçek modelle elde edilmedi — bounded negative.)

## Phase 5 — Preference Optimization

- [x] Başarılı ve başarısız debugging çıktılarından preference veri seti oluştur. (Tamamlandı bounded historical controlled kapsamda — preference-pair exporter v1 (2026-08-06) + historical controlled preference data; authentic production preference corpusu **CLOSED / NOT JUSTIFIED** (yetersiz temiz homojen veri).)
- [x] DPO veya uygun bir RLHF yöntemi uygula. (Tamamlandı bounded historical controlled investigation olarak — B1 27/30, matched SFT 27/30, DPO 21/30; **negatif sonuç, kampanya CLOSED / NOT JUSTIFIED**.)
- [x] Base model, fine-tuned model, RAG destekli model ve agentic sistemi karşılaştır. (Tamamlandı S5 üzerinden explicit missingness ile — 8 eksenli canonical ledger, `NOT_RECORDED` / `NOT_EVALUATED`; dört-yönlü tamamlanmış bir correctness matrix olarak sunulmaz.)

## Phase 6 — Debugger Adapter

- [x] PDB, GDB veya LLDB için bir debugger adapter geliştir. (Tamamlandı — instructor maddesindeki “veya” seçeneği PDB ile karşılandı; accepted Python/PDB-first scope, Task 4A-4D ve post-mortem PDB `e92634e`. GDB/LLDB kapsam dışıdır.)
- [ ] Fine-tuned modelin debugger komutları üretmesini ve çıktıları yorumlamasını sağla. (**CLOSED — BOUNDED NEGATIVE** 2026-08-11 — engineering capability YES; deterministic engineering evidence YES; positive real-model success NO (D1 `break 20` → tool error, S2 `continue` → rejected, 0 successful observations); bounded-negative evidence YES. Yeni deney yetkilendirmesi değildir; bkz. S5 coverage matrix.)
- [ ] Modelin breakpoint koymasını, değişkenleri incelemesini, stack trace okumasını ve adım adım debug yapmasını sağla. (**CLOSED — BOUNDED NEGATIVE** 2026-08-11 — engineering/deterministic capability YES; positive real-model sequence NO; bounded-negative evidence YES. İki model koşulunda da başarılı breakpoint→observation→step/locals dizisi yoktur.)
- [ ] Modelin debugger etkileşiminden sonra patch üretmesini ve testlerle doğrulamasını sağla. (**CLOSED — BOUNDED NEGATIVE** 2026-08-11 — static model→patch→verifier YES (gcd, non-debugger); debugger-informed real-model patch→verifier NO; bounded-negative evidence YES.)

## Phase 7 — Evaluation and Final Report

- [x] Sonuçları başarı oranı, localization accuracy, root-cause explanation, test pass rate, maliyet ve çalışma süresi açısından değerlendir. (Tamamlandı S5 üzerinden — 8 eksenli canonical comparison ledger; eksik değerler `NOT_RECORDED` / `NOT_EVALUATED` olarak explicit; root-cause assessment contract + comparison harness; hiçbir eksik değer sıfıra çevrilmedi.)
- [x] Çalışan bir agentic debugging demosu ve teknik rapor hazırla. (Tamamlandı — Final Technical Report **V2** (`docs/FINAL_TECHNICAL_REPORT_V2.md`, S8) + V1 (2026-07-31) historical; deterministic offline demo + S6 professor-facing bounded-negative evidence presentation (`presentation/s6-real-debugging-evidence/`). Bu bir altyapı/evaluation-platform demosudur, model debugging performance demosu değildir.)

### Live-runner material repair (2026-08-02, runner-only)

Live-runner siniri bounded material repair ile sertlestirildi: (1)
execution-commit baglama - `accepted_campaign_commit` artik kampanyayi
calistiracak tam commit'tir; gercek Git HEAD bununla esit olmali, commit
mevcut olmali ve accepted baseline'dan turemeli, tracked working tree ve
Git index temiz olmali; ledger claim/preflight/transport oncesinde ve her
case oncesinde dogrulanir; post-preflight drift `TRACKED_SOURCE_CHANGED`
authority kaniti ile kampanyayi durdurur; dogrulanmis commit campaign, case,
authority, route-binding ve ledger kanitlarina islenir. (2) Strict raw route
evidence - her acceptance-critical alan acikca tiplenmis olmali; eksik
alanlar manifest/authorization'dan doldurulmaz, eksik denial/fiyat kaniti
False/zero olarak uretilmez; account status authorization ile birebir
eslesmeli; timestamp parse edilebilir, gelecekte ve stale olmamali. (3)
Immutable output - bir output root yalnizca bir attempt identity'ye aittir
(atomik `.attempt-owner`); authoritative artifact'lar create-once
semantigiyle asla uzerine yazilmaz; rejection'lar non-authoritative
`rejections/` dizinine yazilir; yeni authorization yeni root ister. (4)
Atomic ledger lifecycle - cross-process exclusive claim (es zamanli iki
claim'den yalnizca biri kazanir); eksik transport/runner authorization
tuketmeden reddeder; terminal ledger state campaign.json'dan once
finalize edilir (campaign.json en son yazilir); ledger-finalization hatasi
tamamlanmis gorunumlu artifact birakmaz; lifecycle sayilari frozen alti
case ile birebir dengelenir. Authorization strictness: account observation
tam alan seti, gelecek creation timestamp yok, validity creation ve
execution sonrasi. Tum senaryolar icin adversarial testler eklendi
(iki-process concurrent claim, forged-commit reddi ve onceki kanitin
degismemesi dahil). Repair sonrasi: live-runner suite 222 passed;
paired-pilot suite'leri 267 passed. Bu gorevde hicbir live campaign,
benchmark, model veya paid endpoint calistirilmadi.

### Live-runner material repair 2 (2026-08-02, runner-only)

Ikinci bounded material repair: (1) single-winner attempt claim - exclusive
`.attempt-owner` (O_EXCL) kapisi hicbir ikinci process'in gecmesine izin
vermez (identity/authorization hash eslesmesi bile); ayni-identity duplicate
(`DUPLICATE_ATTEMPT`) ve farkli-owner conflict (`OUTPUT_ROOT_OWNED`) typed
error'lar ile ayristirilir; deterministik barrier'li iki-process testi tam
bir kazanan oldugunu kanitlar. (2) Occupied output root - claim oncesinde
authoritative root yok veya yapisal olarak bos olmali; onceden var olan
campaign/ledger/case/private/temp/unknown dosyalar, dizinler, symlink'ler
veya celiskili owner verisi `OUTPUT_ROOT_OCCUPIED` ile reddedilir (sifir case
execution, sifir provider aktivitesi); rejection ve preflight kayitlari
parent-level non-authoritative konuma tasindi. (3) Post-case ve pre-terminal
authority dogrulamasi - her case sonrasi ve terminal ledger finalization'den
hemen once repository state ve tracked authority'ler yeniden dogrulanir;
drift typed `TRACKED_SOURCE_CHANGED` authority kanitiyla kampanyayi durdurur
ve kampanya asla `COMPLETED` donebilir/persist edemez. (4) Non-finite numeric
evidance ve strict JSON - `NaN`/`±Infinity` her yerde `math.isfinite()` ile
reddedilir; tum persisted JSON `allow_nan=False`; serialization hatasi
kismi dosya birakmadan fail-closed. Terminalization iki fazli oldu
(campaign.json once, ledger sonra): `COMPLETED` ledger her zaman eslesen
dogrulanmis terminal campaign.json'a sahiptir; artifact olusturma hatasi
`ABORTED`/`OUTPUT_INTEGRITY_FAILURE` terminali uretir. Repair sonrasi:
live-runner suite 251 passed; paired-pilot suite'leri 267 passed. Hicbir
live campaign, benchmark, model veya paid endpoint calistirilmadi.

### Live-runner final material repair (2026-08-02, runner-only)

Son bounded material repair: (1) crash-safe terminal package commitment -
terminalization artik uc asamali durable protokol: campaign.json (PREPARED
isaretli, non-authoritative), ledger terminalization, en sonda create-once
`terminal-commit.json` (attempt identity, authorization hash, execution
commit, status, campaign.json SHA-256, terminal ledger entry SHA-256, manifest
hash ve case inventory baglar). Standalone campaign.json yalnizca commitment
varken kabul edilir; verify_attempt_package ve tum loader'lar
uncommitted/interrupted paketleri `TERMINAL_COMMIT_MISSING` ile reddeder; her
terminalization adiminda fault injection (BaseException process-death dahil)
test edilir; kesintiye ugrayan attempt asla sessizce resume edilmez. (2)
Authority-invalidated cases - post-case drift tespit edilen case artik
completed sayilmaz: lifecycle `authority-invalidated`, completed_case_count
haric, invalidated_case_count icinde, yalnizca quarantined evidence olarak
korunur (authority record hash ve provider-contact flag ile); dengeleme
completed + blocked + aborted + invalidated + unstarted == 6; final-case
drift PARTIAL + completed 5 / invalidated 1 / unstarted 0 uretir; pre-terminal
drift ayri campaign-level failure'dir (affected case ID null). Repair sonrasi:
live-runner suite 266 passed; paired-pilot suite'leri 267 passed. Hicbir live
campaign, benchmark, model veya paid endpoint calistirilmadi.

### OpenCode Go execution adapter v1 (2026-08-03, adapter-only)

Paired-pilot v2 live runner icin OpenCode Go execution-adapter wiring'i
tamamlandi ve dogrulandi (yalnizca adapter; hicbir provider temas yok):
`scripts/quixbugs_opencode_go_adapter.py` - strict versioned adapter
configuration kontrati (`quixbugs-opencode-go-execution-adapter-v1`; tracked
non-executable template `research/quixbugs/OPENCODE_GO_EXECUTION_ADAPTER_TEMPLATE.json`
aktif config olarak reddedilir; gercek konfigurasyonlar tracked disi `operator/`
dizininde yasar), runtime identity binding (runtime model kimligi yalnizca
dogrulanmis authorization + route evidence'dan gelir; tarihsel
`opencode/deepseek-v4-flash-free` Zen kimligi execution identity olarak
reddedilir; alias/catalog/version/variant/route-class drift ve Zen/free-tier/
Ollama/alternate-provider/fallback durumlari typed `RouteDriftError` ile
reddedilir), explicit transport factory (accepted protocol transport'un
structured argv + explicit cwd + bounded env allowlist + bounded
stdout/stderr/diagnostics + process-group-aware timeout/cleanup + sifir
otomatik retry/fallback/catalog sorgusu ile adaptasyonu; her provider process
attempt oncesi binding ve output/attempt ownership gate'leri yeniden
dogrulanir), case-runner binding (accepted QuixBugs live path
`run_live_quixbugs_case` uzerinden bir frozen case basina bir fresh
transport/session/workspace; static-baseline PDB yasagi; PDB-on-uncertainty
yalnizca accepted controller gate ve budget'lar ile, runtime identity
`pdb_identity_binding` ile acikca baglanir; ledger/terminal commitment/
authority checks/stop rules/result validator asla bypass edilmez; route drift,
transport failure, malformed-response exhaustion, budget exhaustion,
containment/verifier/cleanup failure ve public/private boundary ihlalleri
accepted typed stop/result kontratlarina map'lenir) ve CLI
(`adapter-template`, `adapter-validate`, `route-preflight-only` - sifir
provider process, `selftest` - yalnizca synthetic,
`live-wire` - aktif validate edilmis config + explicit operator artifact'lari
olmadan kullanilamaz). Deterministik network-incapable synthetic executable
(`scripts/opencode_go_synthetic_executable.py`) ile valid response, malformed
+ recovery, exhaustion, startup failure, timeout, oversized output, non-zero
exit, identity/model/route drift, missing/non-finite usage, credential
sanitization ve child-process cleanup senaryolari kanitlandi; sifir gercek
OpenCode/provider/catalog/account cagrisi, network-enabled komut yok, Zen/
free-tier route yok, fallback yok, exact process-attempt/logical-call
muhasebesi ve her case icin fresh process/session boundary + dogru cleanup
kanitlandi. Testler: yeni unit suite 76 passed (configuration 40 + transport
24 + case-runner 12), yeni integration suite 10 passed; mevcut live-runner
266, paired-pilot 267, live-quixbugs/transport/live/controller/verifier
suites ve tam unit suite 2783 passed (3 skipped), integration 357, golden
trajectories 11 passed; v1/v2 validators gecerli; py_compile ve git diff
--check temiz. Gercek kampanya oncesi hala gerekli: gercek operator
authorization artifact'i, preflight'tan gecen gercek route evidence, adapter
commit'inin authorization'a baglanmasi, operator saglanan QuixBugs execution
environment'i ve operator'un gercek kampanya icin acik yetkisi. Tamamlanmis
isaretlenmez: operator authorization, gercek route preflight, gercek OpenCode
Go execution, six-case live campaign, empirical evaluation, model
performance, PDB effectiveness, RAG, SFT, DPO. Tarihsel OpenCode Zen
kayitlari degismeden historical kaldi. Dokuman:
`docs/QUIXBUGS_OPENCODE_GO_EXECUTION_ADAPTER_V1.md`.

### OpenCode Go execution adapter v1 - wrapper repair (2026-08-03, adapter-only)

Bounded surgical repair: (1) adapter command artik dogrudan OpenCode CLI
komutu degil, accepted protocol wrapper'i
(`scripts/opencode_protocol_transport.py`) acikca baslatiyor - `[python,
wrapper, --model <runtime id>, --variant <v>, --route-mode opencode-go,
--expected-opencode-version <v>, --expected-catalog-fingerprint <hex>,
--expected-runtime-model-id <id>, --expected-account-status <status>,
--expected-billing-route SUBSCRIPTION]`; `--evidence-file` yalnizca wrapper'in
sahip oldugu arguman oldugu icin adapter tarafindan eklenir. Wrapper'i bypass
eden direct OpenCode CLI komutlari `DIRECT_OPENCODE_COMMAND_REJECTED` /
`WRAPPER_NOT_BOUND` ile reddedilir; `--route-mode opencode-go` ve tum
route-binding flag'lari config degerlerine baglanir (`ROUTE_MODE_NOT_BOUND`,
`ROUTE_BINDING_FLAGS_MISSING`). (2) Wrapper'a minimal route mode eklendi:
`legacy` (varsayilan; tarihsel OpenCode Zen zero-price davranisi degismeden
korunur) ve `opencode-go` (catalog fiyatlari oldugu gibi korunur, sifir
gerektirmez; launcher version `--expected-opencode-version` ile birebir
eslesmeli; model/fingerprint/account/billing-route kaniti dis authorization/
preflight kontrati tarafindan dogrulanmis olarak zorunlu tutulur ve evidence'da
kaydedilir; gizli fallback, model secimi, catalog/account yeniden sorgusu ve
Zen/free-tier inference yok). (3) Case execution cost artik her provider
response'un acikca bildirdigi sonlu monetary cost'larin toplamidir
(`provider_telemetry.cost`): absent cost fabricated edilmez (schema sifir
yokluk temsili), acik sifir sifir kalir, abonelik erisimi sifir cost ima
etmez, preflight route-observation cost case cost olarak kullanilmaz; frozen
v2 case validator'unun cost esitligi kontratu buna gore gevsetildi (dogrudan
etkilenen compatibility fix). (4) Synthetic validation artik fake OpenCode
CLI'yi GERCEK wrapper uzerinden calistiriyor (stdin uzerinden request, bounded
`opencode run` komutu kurulumu, response model-adapter sinirina ulasiyor);
absent/zero/positive cost ayrimi kanitlandi; sifir gercek provider cagrisi.
Focused checks: yeni wrapper repair suite 12, configuration 45, transport 24,
case-runner 13, CLI integration 10, wrapper transport 30, paired-pilot v2 88
passed; live-runner ve paired-pilot cost odakli testler 7 passed; py_compile
ve git diff --check temiz. Gercek kampanya oncesi gerekenler degismedi
(gercek authorization, gercek route evidence, adapter commit baglama,
operator QuixBugs environment'i, operator yetkisi); operator authorization,
gercek route preflight, gercek OpenCode Go execution, six-case live campaign,
empirical evaluation, model performance, PDB effectiveness, RAG, SFT ve DPO
tamamlanmis isaretlenmedi; tarihsel OpenCode Zen kayitlari degismeden
historical kaldi.

### Operator Authorization and Real Route Preflight v1 (2026-08-03, operator preparation; OPEN)

- [ ] **Gerçek operator preflight (ACIK / OPEN).** Gerçek `route-capture` ve
  `operator-bundle` komutlarinin operator tarafindan calistirilmasi hala
  bekliyor: FirstMate review'i ve Onur'un manuel yurutmesi gerekiyor. Bu
  maddede uygulama agent'i hicbir gercek OpenCode inspection komutunu
  calistirmadi; yalnizca operator-akisi implementasyonu ve paketi hazir.
  Tamamlanmis isaretlenmez: operator authorization yurutmesi, gercek route
  preflight, gercek OpenCode Go execution, six-case live campaign, empirical
  evaluation, model performance, PDB effectiveness, RAG, SFT, DPO.

Operator hazirlik akisi implemente edildi ve paketlendi (yalnizca operator;
hicbir gercek OpenCode inspection komutu calistirilmadi):
`scripts/quixbugs_opencode_go_adapter.py` uzerinde iki odakli operator modu:
(1) `route-capture` - salt-okunur komut; yalnizca yerel/non-model OpenCode
inspection komutlari (`opencode.cmd --version` ve
`opencode.cmd models opencode-go --verbose --pure`), asla `opencode run`
degil; exact operator-secimli runtime model ID (tarihsel
`opencode/deepseek-v4-flash-free` Zen kimligi ve `opencode-go/` disindaki
tum provider'lar reddedilir) ve variant
gerektirir; tam olarak bir aktif catalog entry'si bulur; gozlemlenen status,
variant availability ve sonlu pricing metadata'sini kaydeder; operator
tarafindan acikca saglanan account status, subscription entitlement
confirmation/reference ve billing-route assertion'i zorunlu tutar (tahmin
etmez); tum denial/fallback gozlemlerini acikca kaydeder; create-once
semantigiyle ignored `operator/` storage'a strict `quixbugs-route-evidence-v1`
JSON yazar (mevcut live-runner validator'u tarafindan kabul edilir);
credential/token/cookie/raw private account verisi icermez. (2)
`operator-bundle` - accepted route-evidence dosyasini tuketir ve gercek
`quixbugs-paired-pilot-authorization-v1` artifact'i ile gercek
`quixbugs-opencode-go-execution-adapter-v1` config'ini uretir; ikisi de
**operator komutu calistirdigi anda salt-okunur Git incelemesiyle gozlemlenen
gercek temiz Git HEAD'ine** baglanir (task kabul edilip merge edildikten sonra;
asla caller-supplied bir commit'e ve asla task baseline'a degil - task
baseline `618c33ff186493892665ca1233c3edd8b2eec13f` yalnizca minimum lineage
onkosulu olarak tutulur). Gozlemlenen HEAD gecerli mevcut bir commit olmali,
accepted project baseline'dan ve task baseline'dan turemeli, temiz tracked
working tree, temiz gercek index ve non-ignored untracked dosyasiz olmali;
artifact'ler yazilmadan hemen once HEAD ve repository temizligi yeniden
kontrol edilir ve gozlem ile materialization arasinda herhangi bir drift
hicbir aktif artifact uretilmeden fail-closed olur. Ayni bagimsizca gozlemlenen
HEAD authorization `accepted_campaign_commit`'inde, adapter configuration
`execution_commit`'inde, route-preflight execution binding'inde, runtime
identity binding'inde ve dondurulen record'da tutarli sekilde kullanilir.
Ayrica frozen manifest hash `bc3df3129f1e7d184f26de5b7b8c4953a497d463b30934aaae21865b809f3171`'e,
exact alti frozen case ID ve sirasina, protocol `1.3`'e, exact gozlemlenen
OpenCode version/runtime model ID/variant/catalog fingerprint'e, account
status ve subscription billing route'a, bir operator authorization ID'ye, bir
fresh attempt identity ve output root'a, acik sinirli gecerlilik suresine ve
operator-cozumlu Python executable/repository wrapper path/working
directory/operator boundary root'a baglanir. Dirty/staged source, drift,
occupied target, template value, route drift, unknown field, malformed
path ve celiskili subscription/fallback assertion'lari reddedilir; aktif
operator artifact'lari commit edilmez. Deterministik catalog-entry fingerprint
kontrati `scripts/opencode_protocol_transport.py` icinde bir kez uygulanir
(exact selected entry parse et, projenin canonical JSON kurallariyla seri hale
getir, SHA-256) ve route evidence, authorization, adapter configuration ve
wrapper verification'da aynen kullanilir; wrapper'in OpenCode Go preflight'i
secili entry fingerprint'ini bagimsizca yeniden hesaplar ve herhangi bir model
process calismadan once authorization-bound expected fingerprint ile
karsilastirir. Uretilen artifact'ler mevcut sifir-provider-process
`route-preflight-only` komutuyla calisir (PowerShell ornegi:
`docs/QUIXBUGS_OPENCODE_GO_EXECUTION_ADAPTER_V1.md`). Testler eklendi:
deterministik fingerprinting, exact selected-entry matching, malformed/
duplicate/inactive/missing-variant/historical-free-route reddi, route evidence
schema uretimi, authorization/config cross-binding, dirty-Git ve
occupied-target reddi, task baseline'dan farkli temiz descendant HEAD'in kabul
edilip exact generated execution commit olmasi, nonexistent/non-descendant/
dirty/staged/drifting HEAD reddi, wrapper fingerprint mismatch reddi ve
capture'in `opencode run`'u asla insa etmedigi/calistirmadigi kaniti
(komut envanteri uzerinden). Dogrulama bilincli olarak calistirilmadi
(FirstMate'e aittir). Gercek operator preflight FirstMate review'i ve
Onur'un manuel yurutmesini bekliyor; `operator-bundle` artifact'leri Git
closeout'undan sonra mevcut temiz HEAD'e baglar.

### OpenCode Go catalog provider selection repair (2026-08-03, adapter-only)

Gercek Windows incelemesi, Go modunun `opencode.cmd models opencode --verbose
--pure` sorguladigini ve bu yuzden tarihsel Zen/free kimligini
(`opencode/deepseek-v4-flash-free`) gordugunu kanitladi. Route-capture ve
protocol-wrapper yollari onarildi:

- OpenCode Go modu artik tam olarak `models opencode-go --verbose --pure`
  sorguluyor; `scripts/opencode_protocol_transport.py` catalog komutunu route
  mode'a gore seciyor (legacy mod `models opencode`'u degismeden koruyor) ve
  operator `route-capture` (`_resolve_catalog_command`) yalnizca Go provider
  komutunu kullaniyor.
- Go runtime kimlikleri `opencode-go/` provider prefix'ini kullanmak
  zorunda (adapter'da `GO_RUNTIME_ID_PREFIX`, wrapper'da
  `OPENCODE_GO_RUNTIME_ID_PREFIX`): `opencode/`, tarihsel
  `opencode/deepseek-v4-flash-free` kimligi ve diger tum provider'lar model
  calistirilmadan once reddediliyor — wrapper OpenCode Go preflight'i
  (`_require_go_runtime_identity`; catalog sorgusundan ve `opencode run`
  oncesinde), operator `route-capture`, `operator-bundle` route-evidence
  kapisi ve strict adapter-configuration validator'u (`PROVIDER_MISMATCH`).
- Secilen `opencode-go/<model>` catalog entry'si deterministik kontratla
  fingerprintlenmeye devam ediyor ve wrapper'in OpenCode Go preflight'i
  fingerprint'i authorization-bound expected fingerprint ile karsilastiriyor;
  wrapper evidence'i sorgulanan `catalog_provider`'i kaydediyor.
- Route capture `opencode run`'u asla insa etmiyor/calistirmiyor (komut
  envanteri kaniti korundu). Operator PowerShell ornegi artik
  `--runtime-model-id opencode-go/deepseek-v4-flash` kullaniyor; gercek Go
  catalog'i incelenmeden hicbir model variant uydurulmadi.
- Dogrudan etkilenen testler guncellendi (route-capture, operator-bundle,
  wrapper-repair, operator route-preflight CLI integration) ve odakli bir
  Go-mode provider-reddi wrapper testi eklendi; synthetic executable kontrati
  artik `models opencode-go --verbose --pure` belgeliyor.

Gercek operator preflight TODO maddesi acik tutuluyor (tekrarlanan Windows
route capture bekleniyor). Hicbir test/build/lint/compile/dogrulama
calistirilmadi (FirstMate'e aittir); gercek OpenCode komutu, catalog,
provider veya paid endpoint calistirilmadi; commit/stage/push yapilmadi.

### QuixBugs multi-task PDB live-wire repair (2026-08-03, adapter + live path)

Frozen alti-case kampanyanin ilk case'i `pdb-on-uncertainty` oldugu halde
live yolu PDB'yi yalnizca `QUIXBUGS_PDB_TASK_ID` (`quixbugs-gcd-smoke-v1`)
icinde tutuyor, her PDB case'i icin `prepare_quixbugs_gcd_pdb_probe`
kullaniyor, `PAIRED_PILOT_V2.json`'da dondurulmus task-local probe'larini
calistiramiyor ve her task icin sifir-argumanli generic bir facts provider
cagiriyordu (QuixBugs dependency gate'i ise `DependencyPreparation`'in exact
task manifest/fingerprint/algorithm/revision'a baglanmasini gerektiriyor).
Bu yuzden `live-wire` alti-case karsilastirmasini uretemeden abort ediyor.
Bounded live-path repair (paralel campaign runner yok):

- **Task-local PDB probe.** `run_live_quixbugs_case` explicit task-local
  `RuntimeProbe` girdisi alir: static-baseline probe kabul etmez ve sifir
  PDB erisimi korur; PDB-on-uncertainty secili task icin explicit reviewed
  probe gerektirir; probe secili task ID'sine (varsayilan gcd probe'unun gcd
  kilidi korunur), buggy modul path'ine, corrected-source/test/support
  dislamasina, reviewed target symbol'a, kaynak containment'ina ve cozulebilir
  breakpoint anchor'una karsi dogrulanir (`validate_quixbugs_runtime_probe_identity`,
  artik public). Probe hazirligi `prepare_quixbugs_pdb_probe` ile yapilir;
  tarihsel standalone gcd API'leri (`prepare_quixbugs_gcd_pdb_probe`,
  `run_live_quixbugs_evaluation`'un gcd PDB kilidi, default GCD probe)
  degismeden korunur; contained-PDB/resource/cleanup/identity gate'leri
  zayiflatilmadi.
- **Adapter case binding.** `OpenCodeGoCaseRunner` her frozen case icin exact
  inventory entry'sini cozer (eksik/duplikat entry reddedilir), PDB case'lere
  probe'u yalnizca o entry'nin frozen `runtime_probe` alanlarindan uretir
  (corrected source/test/model ciktisi/runtime tahmininden asla turetmez),
  missing/malformed/mismatched/duplicate probe metadata'sini provider
  etkilesiminden once reddeder ve probe'u yalnizca `pdb-on-uncertainty`
  icin gecirir. Uc secili PDB task'i: `quixbugs-find-in-sorted-smoke-v1`,
  `quixbugs-is-valid-parenthesization-smoke-v1`, `quixbugs-hanoi-smoke-v1`.
- **Task-bound facts provider.** Facts-provider kontrati
  `provide(manifest_path: str) -> QuixBugsPreflightFacts` oldu: case runner
  her frozen case icin ayri ayri exact manifest path'i ile cagirir, exact
  `QuixBugsPreflightFacts` sonucu ister, dependency preparation'inin secili
  task manifest'iyle eslesmesini zorunlu tutar; sifir-argumanli generic
  facts, wrong-task facts ve malformed sonuclar provider oncesi reddedilir;
  `--facts-provider module:callable` operator secimi korundu. Yeni operator
  modulu `scripts/quixbugs_live_wire_environment.py`: accepted read-only
  WSL/Bubblewrap readiness'ini yeniden kullanir (install/clone/reset/clean/
  download yok), secili manifest'ten task-bound verified facts uretir ve
  `quixbugs-environment.json` icin gereken repository root + sources parent'i
  donduren `describe_environment()` aciklar. WSL execution mimarisi
  kopyalanmadi.
- **Testler.** Uc secili PDB case'inin her birine kendi exact reviewed
  probe'unun gittigi, static case'lerin probe almadigi ve sifir PDB
  erisimini korudugu, non-GCD PDB case'lerin yalnizca non-GCD olduklari icin
  artik reddedilmedigi, missing/mismatched probe metadata'sinin provider
  yurutmesinden once dustugu, GCD-only legacy/default API'lerin degismedigi,
  facts'in her case icin exact manifest path'i ile ayri ayri istendigi,
  wrong-task dependency facts'in reddedildigi ve alti-case runner'in gercek
  provider olmadan synthetic transport ile tum alti binding'e girdigi
  kanitlari eklendi (focused unit + integration). Baska kapsam genisletmesi
  yapilmadi.

Dogrulama bilincli olarak calistirilmadi (FirstMate'e aittir); gercek
OpenCode komutu, catalog, provider veya paid endpoint calistirilmadi;
commit/stage/push yapilmadi. Live kampanya TODO maddesi FirstMate review'i
ve gercek operator yurutmesi bekledigi icin acik tutuldu: tamamlanmis
isaretlenmez — operator authorization yurutmesi, gercek route preflight,
gercek OpenCode Go execution, six-case live campaign, empirical evaluation,
model performance, PDB effectiveness, RAG, SFT, DPO.

### OpenCode Go isolation provider selection repair (2026-08-03, wrapper-only)

Ilk gercek alti-case denemesi (`quixbugs-paired-pilot-v2-attempt-81f2e5d859cb401681c701f19a25a4f6`)
tum alti case binding'ine girdi ama her case `PROVIDER_ERROR` /
`process_error` ile bitti; 18 transport denemesinin tamami `opencode run`
oncesinde `RuntimeError: OpenCode model catalog failed with exit code 1` ile
dustu. Kok neden: Go modu catalog'i dogru sekilde
(`models opencode-go --verbose --pure`) sorguluyordu, ancak izole OpenCode
konfigurasyonu hala `enabled_providers: ["opencode"]` yaziyordu ve
effective-config validator'u da `["opencode"]`'u hardcode ediyordu. Yerlesik
wrapper yolunun yalitim provider secimi route-mode-aware hale getirildi
(sinirli, wrapper-only repair):

- `_isolation_config(route_mode)` artik route'a gore tam allowlist yaziyor:
  `opencode-go` modu `enabled_providers: ["opencode-go"]`, `legacy` modu
  `enabled_providers: ["opencode"]`; provider ambient konfigurasyondan asla
  infer edilmiyor. Explicit route mode; isolation-config olusturma,
  isolation hazirligi (`_prepare_isolation(root, route_mode)`), effective-
  config dogrulamasi (`_validate_effective_config(config, route_mode)` /
  `verify_opencode_effective_config(..., route_mode)`), wrapper preflight ve
  gercek wrapper yurutmesi boyunca thread edildi.
- Effective-config kapisi artik aktif route icin tam beklenen provider'i
  zorunlu tutuyor: Go icin tam `["opencode-go"]`, legacy icin tam
  `["opencode"]`; karisik, eksik veya ek provider'lar reddediliyor. Mevcut
  permission, MCP, plugin, instruction, sharing ve autoupdate denial'lari
  korundu.
- Tanisal sertlestirme: yerel catalog inceleme komutu sifir-disi dondugunde
  wrapper artik typed `CatalogFailureError` (`catalog_command_failed`
  classification) ile birlikte sinirli (4096 karakter), sanitize edilmis ve
  ANSI-temizlenmis stdout/stderr orneklerini (catalog komutu ve exit code
  ile) error/evidence'a ekliyor; credential/auth icerigi ve kisitlanmamis
  ortam degerleri kayitlanmiyor. Failure kayitlari
  `failure_classification` / `failure_detail` alanlarini tasiyor.
- Odakli testler eklendi/guncellendi: Go isolation config tam
  `["opencode-go"]`, legacy tam `["opencode"]`; Go effective-config yalnizca
  `opencode-go` kabul eder; legacy yalnizca `opencode` kabul eder; karisik
  ve cross-route listeler reddedilir; wrapper Go preflight'i sentetik basarili
  `models opencode-go` yaniti altinda catalog parsing'e ulasir ve `opencode
  run`'u asla calistirmaz (mocked ve gercek-subprocess preflight kanitlari);
  catalog-failure evidence'i sinirli sanitize diagnostic detay icerir;
  legacy wrapper davranisi degismeden korunur.

Gercek operator preflight TODO maddesi ve Authorized Six-Case Live Campaign
TODO maddesi bu repair'in ardindan taze bir deneme bekledigi icin acik
tutuluyor. Hicbir test/build/lint/compile/dogrulama calistirilmadi
(FirstMate'e aittir); gercek OpenCode komutu, catalog, provider veya paid
endpoint calistirilmadi; commit/stage/push yapilmadi. Basarisiz kampanya
gecerli bir deney olarak yeniden yorumlanmadi; task-local PDB, facts-
provider, authorization, manifest, campaign schema ve case-runner tasarimi
degistirilmedi.

---

## OpenCode Go isolated route-capture environment repair (sekizinci islem, 2026-08-03)

Taze alti-case denemesi (`quixbugs-paired-pilot-v2-attempt-4c7fc4445de54c8d9a33f8ab9a23fd97`) tum alti case binding'ine ulasti ancak 18 transport denemesinin tamami model inference'ten once `catalog fingerprint drift` ile dustu: wrapper'in izole ortamda bagimsizca yeniden hesapladigi fingerprint, operator route-capture'in ambient kullanici OpenCode konfigurasyonu altinda kaydettigi authorization-bound fingerprint'e esit degildi (route/provider var; exact catalog-entry fingerprint kontrati iki farkli konfigurasyon ortaminda farkli exact entry uretiyor). Exact karsilastirma korunarak route capture; wrapper ile AYNI deterministik izolasyon ortamini ve effective-configuration kontratini kullanacak sekilde onarildi:

- `scripts/opencode_protocol_transport.py` — `observe_isolated_catalog(...)`: route capture ve wrapper catalog dogrulamasinin ortak tek izole catalog-observation yolu. Gecici deterministik izolasyon koku (isolation_root verilmezse), `route_mode="opencode-go"` ile `_prepare_isolation`, exact effective configuration zorunlulugu (permission/MCP/plugin/instruction/sharing/autoupdate denial'lari + tam `["opencode-go"]` allowlist), izole env altinda `opencode.cmd --version` ve `opencode.cmd models opencode-go --verbose --pure`, exact `opencode-go/deepseek-v4-flash` entry'sinin shared select/facts/fingerprint path'inden secimi ve canonical JSON SHA-256 fingerprint hesabi; helper-sahibi kok success/failure'da her zaman temizlenir; `opencode run` asla insa edilmez/calistirilmaz. Wrapper (`_preflight`/`main`) ayni paylasilan yolu kendi kokuyla kullanir ve authorization-bound expected fingerprint'i bagimsizca karsilastirir; route capture fabrikasyon expected fingerprint olmadan saf gozlem yapar. Catalog komutu/parse/route check'leri `_catalog_entry_observation` + `_enforce_catalog_route_checks` altinda toplandi (legacy sifir-fiyat kapisi ve Go drift mesajlari birebir korundu).
- `scripts/quixbugs_opencode_go_adapter.py` — `run_route_capture` ambient inceleme yerine paylasilan izole gozlem yolunu kullaniyor; strict `quixbugs-route-evidence-v1` schema'si ve create-once/boundary semantigi degismedi; companion capture record'u sinirli `observation_mode` blogu tasiyor (mode `isolated-opencode-go`, effective provider allowlist, isolation/config validation passed, temporary isolation cleaned, run_invoked false, model_requests 0); auth icerigi/credential/environment dump/sinirsiz catalog ciktisi kayitlanmiyor. Ambient `_run_catalog_inspection`/`_resolve_catalog_command` kaldirildi.
- Legacy davranis degismedi: wrapper legacy provider'i `opencode`, historical zero-cost check'leri ve eski route-capture davranisi ayni; yeni legacy route-capture davranisi yok.

Odakli testler: ambient ve izole entry'lerin farkli olabilecegi + route capture'in izole entry'yi fingerprintledigi; capture fingerprint'inin wrapper'in bagimsiz izole yeniden hesaplamasiyla birebir esitligi (wrapper preflight captured fingerprint'a baglaninca gecer); Go capture effective config'inin tam `["opencode-go"]` gerektirdigi; capture record observation-mode alanlari; gecici izolasyon temizliginin success/failure'da gerceklestigi; catalog/version failure'larinin typed ve sinirli kaldigi; route capture komut envanterinde `opencode run`'un hic bulunmadigi; shared helper'in caller-sahipli kokta temizligi caller'a biraktigi ve expected fingerprint verildiginde drift karsilastirmasi yaptigi; legacy sifir-fiyat kapisinin korundugu. CLI integration fake shim'i `debug config` hizmeti verecek sekilde guncellendi.

Gercek operator preflight ve Authorized Six-Case Live Campaign TODO maddeleri bu repair'in ardindan taze bir deneme bekledigi icin acik tutuldu; onceki iki deneme altyapi-basarisiz denemeler olarak siniflandirilmaya devam edildi (gecerli deney degil); campaign, authorization, task-local PDB, facts-provider, case-runner ve verifier mantigi degistirilmedi. Hicbir test/build/lint/compile/dogrulama calistirilmadi (FirstMate'e aittir); gercek OpenCode komutu, catalog, provider veya paid endpoint calistirilmadi; commit/stage/push yapilmadi.

### OpenCode Go directive transport repair v1 (2026-08-03, transport-only)

Ilk provider-bagli alti-case denemesi (`quixbugs-paired-pilot-v2-attempt-705aa04741064933b84767e095cd95bf`) gercek OpenCode Go modeline ulasti (16 logical model call, 10 accepted directive, $0.008036 provider-reported cost) ancak alti case de sifir hypothesis/PDB session/patch/verifier ile sonlandi. Iki iliskili protocol tasima hatasi kanitlandi: (A) model `--file <root>\public-request.json` icerigini Read/Bash/PowerShell ile okumaya calisiyordu (DSML tool-call metni, direktif yerine); (B) model dogrudan yanit verdiginde siklikla `{"action":"find_function","name":"hanoi","path":"..."}` gibi yapisal olarak gecersiz nesneler donduruyordu ve mevcut extractor birden fazla JSON nesnesi iceren ciktiyi, bunlardan tam olarak biri gecerli bir direktif olsa bile `ambiguous_json_output` ile reddediyordu. Sinirli transport-only repair (campaign, controller, case runner, PDB gates, facts provider, verifier, authorization ve route identity degismedi):

1. **Inline public request.** Sanitized public request artik model-readable bir `--file` yerine, OpenCode user message'inin icinde canonical compact JSON olarak explicit delimiter'lar arasinda (`=== BEGIN PUBLIC REQUEST ===` / `=== END PUBLIC REQUEST ===`) gonderiliyor: tek argv degeri (shell interpolation yok), evidence'da request icerigi degil yalnizca `request_sha256` + `request_byte_count`; `MAX_PUBLIC_EVIDENCE_BYTES = 20000` (frozen public-evidence budget) asilirsa fail-closed; model yurutmesi native `opencode.exe` ile (batch shim bypass; launcher baglantili native cozumu ve ayni version kaniti - v3'te trusted npm-package-root cozumune guncellendi -, `MAX_NATIVE_COMMAND_LINE_CHARS = 30000` native komut satiri siniri). Message ayrica kisa protocol talimati, compact exact output-shape ornekleri (action, transition, add_hypothesis, revise_hypothesis) ve explicit yasaklar tasiyor (code fence yok, aciklama yok, tool call yok, protocol/version wrapper yok, alternate envelope yok; embedded request'teki allowed actions ve argument contract'lari authoritative). Gercek `opencode run` komutundan `--file` kaldirildi; izole `--dir` korundu; her OpenCode permission denial'i aynen korundu (Read/Bash hala deny).

2. **Schema-aware extraction.** `_extract_directive` model metnindeki her JSON nesnesini, request'e gomulu `directive_schema` + `action_contracts` + `controller` (state, allowed_actions, legal_transition_targets) baglamina karsi strict protocol-1.3 parser ile dogruluyor; tam olarak bir gecerli direktif varsa kabul, sifir varsa `no_valid_directive` reddi, birden fazla varsa `ambiguous_json_output` reddi; kopyalanmis request/config nesneleri yalnizca direktif dogrulamasini gecemedikleri icin yok sayiliyor (heuristic key stripping yok). `action` -> `kind`, `params`/`payload` -> `arguments`, tahmini target state, yanlis path ve eksik required field'lar asla normalize edilmiyor; duzeltme mevcut bounded directive-feedback cycle uzerinden yapiliyor. `directive_schema` tasimayan legacy/minimal request'ler icin tarihsel tek-nesne extraction davranisi degismeden korundu.

3. **Correction feedback.** Protocol yolunda direktif reddedildiginde wrapper provider-completed `directive_error` response'u donduruyor (usage/cost dogru kaliyor): tek compact machine-generated correction mesaji — precise validation failure, current allowed directive kinds icin required top-level envelope (`kind in [action|transition|...]`), "return one JSON object only", tools/code fence/explanation yok; onceki model response'u asla dahil edilmiyor ve mesaj accepted `MAX_REJECTION_DETAIL_CHARS` (200) sinirina sigacak sekilde compact. Adapter (`OpenCodeGoTransport._parse_response`) bu envelope'i accepted `LiveModelAdapterError` rejection'ina (MALFORMED_DIRECTIVE, detail = correction message) ceviriyor; boylece mevcut bounded directive-feedback cycle (retry + `directive_feedback`) tam correction'i modele tasiyor ve rejection'lar `malformed_directive_rejections` / `bounded_directive_feedback_events` icinde sayiliyor (retry/directive-feedback/PDB/patch butceleri degistirilmedi).

4. **Command/audit contract.** Preflight ve effective command validation yeni inline kontrata gore guncellendi: message tek nonempty positional argv degeri; trailing positional yok; `--file` yok; shell yok; repository working directory yok; read/bash/edit/write tool'lari kapali. Synthetic executable (`opencode_go_synthetic_executable.py`) request'i artik `--file` yerine inline message'dan kurtariyor; `state-legal`, `copied-request-plus-valid` ve `tool-call-text` senaryolari eklendi.

Odakli testler: inline message'da canonical public request'in delimiter'lar arasinda bulunmasi; gercek komutta `--file` olmamasi ve tek positional message; Read/Bash deny'larinin korunmasi; prose + kopyalanmis non-directive JSON ile cevrili tek gecerli direktifin kabulu; iki gecerli direktifin ambiguous reddi; sifir gecerli direktifin reddi; alternate envelope'larin (`action`/`params`/`payload`/protocol-version wrapper) reddi; malformed action argument'larinin reddi; bounded correction feedback'in exact failure'i icermesi ve onceki response'u icermemesi (<=200 karakter); her frozen controller state'in kendi legal action/transition/hypothesis direktifini real wrapper + synthetic provider uzerinden almasi (`Reproduce` action, `Understand` add_hypothesis, `RuntimeEvidence` revise_hypothesis); wrapper preflight'inin hala sifir provider inference uretmesi; legacy davranisin degismemesi. `705aa047...` denemesi gecerli bir static-versus-PDB deneyi olarak degil, provider-connected ama protocol-invalid bir deneme olarak siniflandirildi; Authorized Six-Case Live Campaign TODO maddesi acik tutuldu. Hicbir test/build/lint/compile/dogrulama calistirilmadi (FirstMate'e aittir); gercek OpenCode komutu, catalog, provider veya paid endpoint calistirilmadi; commit/stage/push yapilmadi.

### OpenCode Go native-executable directive transport repair v2 (2026-08-03, transport-only)

Replay against the provider-connected attempt `705aa047...` proved that the
previous inline-message design still blocked the campaign: 27 unique public
requests were observed (canonical 4515-8661 bytes), only 14 fit the 7800-byte
message ceiling, 13 failed closed before provider execution, and EVERY frozen
case's Understand-stage request was too large (complete inline messages
9189-9752 bytes). The public-evidence contract permits 20000 bytes; the
cmd.exe batch-shim line limit (~8191 characters), not the protocol budget,
was the blocker. Bounded transport-only repair (campaign, controller, case
runner, PDB gates, facts provider, verifier, authorization, route identity
degismedi):

1. **Native executable execution.** Model execution now invokes the native
   `opencode.exe` directly, bypassing the `.cmd` batch shim: the wrapper
   begins from the independently verified `opencode.cmd` launcher path,
   resolves the native `opencode.exe` through the trusted npm package root
   (`<launcher-dir>\node_modules\opencode-ai`; explicit allowlist:
   `node_modules\opencode-windows-x64\bin\opencode.exe`, the baseline x64
   platform package, and the direct package `bin`; hard-linked copies of the
   single platform binary count as one; exactly one unique native binary
   must remain; root containment + regular file + launcher/authorization
   version equality required; zero, multiple distinct and path-escape
   candidates fail closed), uses the absolute native path as argv[0] with
   `shell=False`, keeps the isolated `--dir` and every permission denial,
   retains the exact model/variant/route binding, and never falls back
   silently to the batch shim, PATH lookup, environment-supplied executable
   paths, PowerShell, shell interpolation, or another executable. Short
   non-model inspection commands (`--version`, `models ...`, `debug config
   --pure`) may continue through the launcher. Only bounded resolution
   evidence is recorded (strategy `npm-package-layout`, package-relative
   native path, regular-file/root-containment/version-match flags) - never
   executable bytes or unrestricted environment data.
2. **Restored public-evidence budget.** The artificial 7800-byte message
   ceiling was removed: the 20,000-byte public-evidence limit applies to the
   canonical public request serialization, not to the complete user message
   (a canonical request up to and including `MAX_PUBLIC_EVIDENCE_BYTES =
   20000` bytes is accepted and its complete message is constructed
   unchanged), and the fully constructed native command is independently
   checked against a conservative Windows command-line bound
   (`MAX_NATIVE_COMMAND_LINE_CHARS = 30000` via `subprocess.list2cmdline`,
   below the CreateProcess maximum of 32767) and fails closed before process
   creation. No batch shim, response file, shell, or model-readable
   attachment is used. The six frozen cases' actual request shapes - the
   8661-byte canonical Understand request and its complete inline scaffolding
   (9752 bytes) - construct successfully.
3. **Strict top-level directive fields.** The schema-aware validator rejects
   unknown top-level fields for every kind (action: kind/name/arguments;
   transition: kind/target_state/reason; add_hypothesis/revise_hypothesis:
   kind/hypothesis_id/statement/confidence/evidence_refs/
   requires_runtime_evidence; set_hypothesis_status: kind/hypothesis_id/
   status); missing and additional fields are rejected, never normalized or
   stripped; action-argument contract validation is unchanged.
4. **Precise bounded correction feedback.** The correction message now
   carries the actual candidate-validation reason (e.g. `unknown argument
   field 'extra'`, `missing required argument 'path'`, `action 'x' is not
   allowed in state 'Understand'`) instead of only "no valid directive":
   exactly one invalid candidate -> its exact bounded reason; multiple
   candidates with none valid -> a deterministic bounded reason without the
   full model output; more than one valid candidate -> the ambiguous reason.
   The message remains <= 200 characters and includes the precise reason,
   the legal `kind: [...]` envelope, "one JSON object only", and no
   tools/code fence/explanation; the prior provider response is never
   included and malformed alternate envelopes are never converted.
5. **Preserved diagnostic classifications.** Empty output, extracted text
   without a protocol directive, no JSON object, zero valid directives, and
   multiple valid directives remain distinct evidence classifications; only
   directly affected stale test expectations were updated.

Odakli testler: frozen request-size range (>= 8661-byte canonical, > 9000-byte
message, native command construction, no `.cmd`/`--file`/shell/truncation);
> 20000-byte requests fail closed; native command-line bound enforced;
native `opencode.exe` resolution same-directory/version-bound/fail-closed;
extra top-level fields rejected per kind; precise candidate reason reaches
bounded correction feedback; one valid directive among copied non-directive
JSON accepted; two valid directives ambiguous; Read/Bash/edit/write denied;
wrapper preflight zero provider inference; legacy unchanged. Deterministic
synthetic fixtures only: a compiled fake native `opencode.exe` forwarder
(test-only) plus the fake launcher shim; no real OpenCode or provider call.
`705aa047...` denemesi provider-connected ama protocol-invalid olarak
siniflandirilmaya devam ediyor; Authorized Six-Case Live Campaign TODO
maddesi FirstMate review'i ve taze bir gercek deneme bekledigi icin acik
tutuldu. Hicbir test/build/lint/compile/dogrulama calistirilmadi (FirstMate'e
aittir); gercek OpenCode komutu, catalog, provider veya paid endpoint
calistirilmadi; commit/stage/push yapilmadi.

### OpenCode Go npm-native + full public-evidence budget repair v3 (2026-08-03, transport-only)

FirstMate material review found two remaining transport-contract gaps plus
three stale focused-test assertions. Sinirli transport-only repair (campaign,
controller, case runner, PDB gates, facts provider, verifier, authorization,
route identity, isolation degismedi):

1. **Trusted npm-native resolution.** The same-directory-only assumption was
   replaced with a deterministic, fail-closed npm-installation resolution
   contract: the wrapper begins only from the independently verified
   `opencode.cmd` launcher path, defines the trusted npm package root as
   `<launcher-dir>\node_modules\opencode-ai`, and resolves the native
   executable exclusively from an explicit allowlist of package-managed
   relative locations under that root — the established Windows x64
   platform-package path `node_modules\opencode-windows-x64\bin\opencode.exe`,
   the baseline x64 platform package
   `node_modules\opencode-windows-x64-baseline\bin\opencode.exe`, and the
   direct package `bin\opencode.exe` (the npm shim's own invocation target).
   The genuine npm layout hard-links the single platform binary into these
   locations, so candidates sharing one file identity count as one; exactly
   one unique native binary must remain. Every candidate must resolve to an
   absolute path inside the trusted root (no symlink/reparse escape) and
   exist as a regular executable file; zero candidates, multiple distinct
   candidates, and path-escape candidates fail closed. The resolved native
   must report the exact same version as the launcher (and, in Go mode, the
   exact authorization-bound version) and is used as argv[0] with
   `shell=False`; arbitrary recursive searches, PATH lookup,
   environment-supplied executable paths, shell interpolation, PowerShell
   execution, parsing an unrestricted command from the batch file, and
   fallback to `opencode.cmd` are rejected by construction. Evidence records
   only the resolution strategy (`npm-package-layout`), the bounded
   package-relative native path, and the regular-file/root-containment/
   version-match flags. Real machine inspection confirmed the established
   npm layout (launcher
   `C:\Users\benya\AppData\Roaming\npm\opencode.cmd`; native
   `...\node_modules\opencode-ai\bin\opencode.exe` plus the two platform
   packages, all hard-links of the single 174 MB binary; no sibling exe).
   All synthetic fixtures now mirror the production layout (native under
   `node_modules\opencode-ai\node_modules\opencode-windows-x64\bin\`); a
   sibling-only `opencode.exe` is never trusted.
2. **Full 20 KB public-evidence support.** The 20,000-byte public-evidence
   limit applies to `canonical_public_request(request).encode("utf-8")`, not
   to the complete user message: canonical requests up to and including
   20000 bytes are accepted (FirstMate reproduced: canonical 18914 bytes,
   complete message 20005 bytes — previously rejected), canonical requests
   above 20000 bytes fail closed, the canonical request is never truncated,
   reduced, summarized, split, or mutated, the complete message is
   constructed unchanged, and the fully constructed native command remains
   independently bounded by `MAX_NATIVE_COMMAND_LINE_CHARS = 30000`
   (`subprocess.list2cmdline`) failing before process creation. Focused
   boundary tests: canonical exactly 20000, canonical 20001, canonical below
   20000 whose full message exceeds 20000, and a complete command exceeding
   the native command-line bound.
3. **Stale focused-test corrections** (no runtime weakening): the inline
   message assertion compares lowercase to lowercase; pure prose preserves
   the established `no_json_object` classification (not
   `no_valid_directive`); the route-capture inspection inventory includes the
   native executable's `--version` proof while still proving no command uses
   the `run` subcommand.

Odakli testler: nested npm x64 native binary resolves; resolved native
remains under the trusted `opencode-ai` root; zero/multiple-distinct/
path-escape candidates fail closed; sibling `opencode.exe` not implicitly
trusted; native version bound to launcher and authorization; route capture
and wrapper share the same resolved native identity; route capture never
invokes `opencode run`; real model execution uses the nested native
executable directly (no `.cmd`, shell, PowerShell, response file, or
`--file`); canonical 20000-byte boundary; frozen 8661-byte request and
>9000-byte message still construct; Read/Bash/edit/write and all isolation
denials intact; strict top-level fields and precise bounded correction
feedback unchanged. `705aa047...` denemesi provider-connected ama
protocol-invalid olarak siniflandirilmaya devam ediyor; Authorized Six-Case
Live Campaign TODO maddesi FirstMate review'i ve taze bir gercek deneme
bekledigi icin acik tutuldu. Hicbir test/build/lint/compile/dogrulama
calistirilmadi (FirstMate'e aittir); gercek OpenCode komutu, catalog,
provider veya paid endpoint calistirilmadi; commit/stage/push yapilmadi.

### Case-level public-evidence budget terminal v1 (2026-08-04, runner-only)

Ilk tam OpenCode Go iletim kanitli deneme (`quixbugs-paired-pilot-v2-attempt-8890ed932cca43ba9f9afaf77971d6c6`) 9 provider process'i exit 0 ile tamamladi, baseline reproduction calisti, controller Understand'a girdi, function/source window'lari alindi, bir dogru high-confidence kok-neden hipotezi kaydedildi, controller Patch'e girdi ve iki patch directive uretti; provider-reported cost yaklasik 0.0066370976 USD idi. Deneme yalnizca bir sonraki bounded public request'in frozen case limitini asacagi icin durdu: `public_evidence_bytes = 21949 > 20000`. Kampanya bunu campaign-level `BUDGET_EXCEEDED` abort olarak siniflandirdi: hicbir case result materialize edilmedi, tamamlanmis provider muhasebesi campaign aggregatelerinden dusuruldu ve bes case baslatilmadan kaldi. Sinirli runner-only repair (manifest, campaign identity, transport, inline request, native executable resolution, prompt, directive extraction, controller action kontratlari, patch/PDB/test/retry budget'lari, authorization, route identity ve verifier degismedi; frozen 20000-bayt public-evidence limiti degistirilmedi):

1. **Beklenen case-budget tukenmesi case-level terminaldir, campaign abort degildir.** `enforce_case_budgets`, gecerli non-negative bir `public_evidence_bytes` sayacinin frozen `max_public_evidence_bytes` limitini asmasini yeni typed `PublicEvidenceBudgetExhausted` ile raporluyor (negatif, non-integer veya eksik sayac ile diger tum frozen budget ihlalleri `BudgetViolationError` olarak campaign abort etmeye devam ediyor). Kampanya dongusu bu istisnayi yakaliyor, case'i yeni bir provider process baslatmadan durduruyor ve outcome'i mevcut frozen terminal temsiline ceviriyor; case `completed` lifecycle ile `campaign.cases`'e yaziliyor, tum tamamlanmis muhasebe (logical model calls, provider process attempts, accepted directives, malformed-directive rejections, token usage, provider-reported cost, controller states, hypotheses, patch attempts/submissions, PDB actions, observations, timing) korunuyor ve kampanya kalan frozen case'lerle devam ediyor. `public_evidence_bytes` frozen limitte raporlaniyor; precise gozlenen deger (21949) termination detail'inde korunuyor.

2. **Mevcut frozen terminal temsilleri.** (a) Pre-PDB completed-response sekli (pdb-on-uncertainty, baseline reproduced, >=1 logical call ve accepted directive, sifir PDB aktivitesi, candidate yok) `PDB_NOT_REACHED` / `PDB_NOT_REACHED_NO_GATE` ile materialize ediliyor (repair outcome `NO_CANDIDATE`): case PDB'ye ulasmadan terminal - provider failure, timeout, malformed response veya pre-provider infrastructure failure olarak siniflandirilmiyor; terminal transport evidence son tamamlanmis provider response'a (exit 0) baglaniyor. (b) Herhangi bir provider cagrisi oncesi tukenme (sifir logical call ve provider attempt) frozen semanin tek zero-activity LIVE_CASE terminali olan pre-provider `INFRASTRUCTURE_ERROR` (`WORKSPACE_FAILURE`, prior lifecycle false) ile no-contact schema-valid case terminali uretiyor. (c) Frozen semada gecerli temsili olmayan sekiller (ornegin provider temas sonrasi static-baseline case, PDB aktivitesi veya submitted candidate) `None` dondurup kampanyayi `BUDGET_EXCEEDED` ile durust sekilde abort ediyor - schema genisletilmedi/zayiflatilmadi.

3. **Muhasebe ayrimi.** Yalnizca bir sonraki request'in frozen case limitini asacagi beklenen public-evidence tukenmesi case-level terminal oluyor; negatif/non-integer counter ve gercek campaign invariant ihlalleri (ornegin transport counter uyumsuzlugu, diger budget tasmalari) kampanyayi abort etmeye devam ediyor. Provider errors, timeout, controller infrastructure mapping ve basarili case'ler degismedi.

Odakli testler: production-sekli regression (9 response, 9 directive, hipotez, Patch, 21949 > 20000, cost 0.0066370976) - ilk case `campaign.cases`'e yaziliyor, measurements ve cost korunuyor, aggregate count'lar case'i iceriyor, kampanya ikinci case'e geciyor, terminal commit + package verification basarili, `ABORTED` degil; provider cagrisi oncesi tukenme no-contact schema-valid terminal; negatif counter ve desteklenmeyen sekil hala abort; `enforce_case_budgets` ayrimi. `8890ed...` ve `320550...` denemeleri non-pilot diagnostic attempt olarak korundu; Authorized Six-Case Live Campaign TODO maddesi taze bir gercek kampanya tamamlanana kadar acik tutuldu. Hicbir test/build/lint/compile/dogrulama calistirilmadi (FirstMate'e aittir); gercek OpenCode komutu, catalog, provider veya paid endpoint calistirilmadi; commit/stage/push yapilmadi.

### Paired-pilot v3 ve tamamlanmış case-budget terminalleri (2026-08-04)

Takip eden live-proven şekiller için mevcut case terminal sözleşmesi tamamlandı:
completed `UNRESOLVED` (`ddc26502...`), completed `RESOLVED` (`238f25ed...`)
ve static-baseline aday uygulanmış fakat Validate'a geçmeden bütçesi tükenmiş
`e974af4...`. İlk iki şekil mevcut v2 sonuçlarını korur. Son şekil için v3,
`VALIDATION_NOT_REACHED/VALIDATION_NOT_REACHED_PRE_VALIDATE` ile
`candidate_provenance=applied_patch_event` ekler; verifier çalışmadığı için
başarı üretilmez. Desteklenen terminal sonuçları completed lifecycle, provider
muhasebesi, token/cost, case artifact'i ve sonraki case'e continuation'ı
korur. Contradictory veya temsil edilemeyen şekiller honest campaign abort
olarak kalır. PDB gate kararları gerçek logical gate tüketimi başına tek kayıt
olacak şekilde deduplicate edilmiştir.

Bir sonraki authorized six-case attempt yalnızca explicit
`research/quixbugs/PAIRED_PILOT_V4.json` ile yürütülebilir. Nominal denominator
6 case / policy başına 3 case'tir; budget-terminal completed case'ler
denominator'dan düşmez, authority-invalidated case'ler evaluation dışı kalıp
resource accounting'de korunur, blocked/aborted/unstarted case'ler açıkça
raporlanır. Gerçek route capture, authorization, preflight ve live campaign
hâlâ açık TODO'dur; fresh v4 operator artifact'leri ve fresh output root
gerektirir.

### Paired-pilot v4 verifier-authoritative terminal sözleşmesi (2026-08-04)

v3 live attempt `fddf1e39...` (case 1, find-in-sorted / pdb-on-uncertainty)
12 logical call, 13 provider process attempt (12 tamamlanmış response + 1
bounded retry), baseline reproduction, source inspection, hipotez, patch
uygulaması, Validate ve Done transition'ı tamamladı; verifier çalıştı; ancak
frozen v3 terminal matrix'i completed post-apply public-evidence exhaustion
(33,685 > 20,000) şeklini temsil edemediği için campaign `BUDGET_EXCEEDED`
ile dürüstçe abort etti. v4 bu şekli preregister eder:

1. **Verifier-authoritative classification (yalnızca v4).** Verifier'ı
   çalışmış bir case, PDB_NOT_REACHED kuralından önce verifier semantic
   outcome'una göre sınıflandırılır (`RESOLVED` / `UNRESOLVED`; verifier
   tamamlanmadıysa `VERIFIER_FAILED`). PDB_NOT_REACHED yalnızca authoritative
   verifier sonucu yokken uygulanır. v2/v3 classification davranışı
   aynen korunur (`_finalize_live_case(campaign_version=...)`; varsayılan 2).
2. **v4 budget-terminal matrix.** Completed lifecycle sonrası
   public-evidence exhaustion (candidate applied, provenance
   `verifier_record`, Validate visited, verifier executed, zero PDB)
   `RESOLVED`/`UNRESOLVED` olarak materialize edilir; tüm muhasebe (12/13/1/12/1,
   token, cost, timing) korunur, `public_evidence_bytes` frozen 20,000'e
   clamp edilir ve exact 33,685 termination detail'inde korunur.
   `VALIDATION_NOT_REACHED` v4'te pdb-on-uncertainty ve Validate-visited
   stop'ları da kapsar (verifier hâlâ NOT_RUN). Post-contact
   controller/cleanup/evidence-packaging `INFRASTRUCTURE_ERROR` şekli de
   v4'te terminalize edilir. Contradictory/temsil edilemeyen şekiller
   (örneğin PDB aktivitesi ile RESOLVED, verifier record ile
   VALIDATION_NOT_REACHED) honest abort olarak kalır; 20,000-bayt bütçe
   artırılmadı.
3. **Kampanya sayacı okuması.** Abort eden bir case'de campaign-level
   `counts` yalnızca `provider_process_attempts`'i içerir; "accepted
   directives: 0" gibi değerler case-level gerçeği değil, materialize
   edilmemiş case kaydının agregasyon artefaktıdır. Case-level gerçek,
   private transport evidence'dan okunur.

Odaklı testler: v4 manifest/authorization validate (hash
`020dfc1f7b8f23aa96a4d7c7942429e306cc290906abfed5ce96cde22b90354d`),
observed-shape RESOLVED/UNRESOLVED rewrite (13/12/1/12/1, 33685 detail),
v4 VALIDATION_NOT_REACHED pdb+Validate-visited, post-contact infra
terminalization, v3 aynı şekillerde hâlâ `None`/abort, v2 davranışı
değişmedi, verifier-authoritative classifier (v3 → PDB_NOT_REACHED,
v4 → RESOLVED), budget 20,000'de sabit, private transport evidence public
byte sayacına girmez. Sanitized replay fixture
(`tests/fixtures/quixbugs_v4_replay_fixture.json`) preserved attempt
evidence'ından yalnızca public protocol material + aggregate sayıları içerir
ve attempt-1 extraction/parse/acceptance, attempt-10 no_text_event, retry
accounting ve observed-shape terminalization'ı deterministik olarak tekrar
oynatır.
