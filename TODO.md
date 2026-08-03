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
