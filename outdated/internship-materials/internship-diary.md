# Agentic Debugging Staj Defteri

Bu dosyada 13 Temmuz–13 Ağustos 2026 arasında yaptığım çalışmaları gün gün özetledim. Eski günlük dosyasında aynı günün altına çok fazla teknik ayrıntı ve sonradan eklenmiş farklı tarihli kayıtlar birikmişti. Burada kronolojiyi düzelttim ve yalnızca gerçekten önemli olan işleri bıraktım. Teknik terimleri gerektiği yerde kullandım ama anlatımı daha sade tuttum.

---

## 13 Temmuz 2026

Bugün staj projesinin kapsamını netleştirmeye çalıştım. Hocanın verdiği TODO listesini inceledim. Debugging, automated program repair, LLM tabanlı hata ayıklama, fine-tuning, RAG ve değerlendirme gibi başlıkların tek projede nasıl bir araya gelebileceğini çıkardım.

İlk olarak ChatDBG ve debug-gym çalışmalarına baktım. Buradan en önemli çıkarımım, modelin sadece kodu okuyup tahmin yürütmesi yerine gerçek çalışma zamanı bilgisine ulaşmasının değerli olabileceğiydi. Aynı zamanda debugger'ı her durumda açmanın da doğru olmadığını gördüm.

İlk mimari kararımı bu gün verdim: proje Python ve PDB ile başlayacak, model ham terminale doğrudan erişmeyecek ve üretilen patch mutlaka testlerle doğrulanacak.

---

## 14 Temmuz 2026

Bugün Agentless ve SWE-bench tarafını inceledim. Özellikle debugger kullanmayan güçlü bir statik baseline'ın gerekli olduğunu fark ettim. Aksi halde ileride PDB kullanan sistemin gerçekten fayda sağlayıp sağlamadığını adil şekilde karşılaştıramazdım.

SWE-bench'teki fail-to-pass ve pass-to-pass test ayrımını proje için örnek aldım. Bir patch'in yalnızca hedef testi geçirmesi yetmiyor; önceden çalışan davranışı da bozmaması gerekiyor.

Bu çalışmadan sonra ilk değerlendirme sınıflarını oluşturdum: RESOLVED, BREAKING_RESOLVED, PARTIALLY_RESOLVED, NO_OP, REGRESSION ve benzeri sonuçları ayrı ayrı tutmaya karar verdim.

---

## 15 Temmuz 2026

Bugün LDB, RepairAgent ve SWE-Agent çalışmalarını okudum. LDB'den runtime state kullanma fikrini, RepairAgent'tan kontrollü state-machine yaklaşımını, SWE-Agent'tan ise model ile bilgisayar arasındaki arayüzün önemini aldım.

Controller için ilk state yapısını çıkardım: reproduce, understand, runtime evidence, patch, validate ve terminal durumlar. Modelin her aşamada yalnızca izin verilen araçları kullanmasını istedim.

PDB tarafında stack, frame, locals ve source bilgilerini ayrı ve tipli araçlar üzerinden vermenin daha güvenli olacağına karar verdim. Ham `eval`, ham shell veya sınırsız PDB komutları ilk sürümün dışında kaldı.

---

## 16 Temmuz 2026

Bugün AutoCodeRover ve OpenHands çalışmalarını inceledim. Burada iki konu öne çıktı: kaynak kodu yapısal olarak aramak ve bütün agent hareketlerini bir event akışı halinde kaydetmek.

Proje mimarisini küçük, test edilebilir parçalara böldüm. Controller bir action üretecek, araç çalışacak, observation dönecek ve bütün süreç loglanacak. Böylece daha sonra gerçek model olmadan bile sistemin parçalarını test edebilecektim.

Aynı gün MVP implementation planını hazırladım. Paket adı, Python sürümü, task schema, safety sınırları, ilk benchmark görevleri ve geliştirme sırası netleşti. Araştırmayı daha fazla uzatmak yerine koda geçme kararı aldım.

---

## 17 Temmuz 2026

Bugün ilk gerçek kodlama task'ını yaptım. `agentic_debugger` paketinin foundation katmanını kurdum. Task schema, state-machine contract, action/observation/event kayıtları ve JSONL logger geliştirildi.

İlk testlerde her şey iyi görünüyordu fakat bağımsız inceleme sırasında bazı açıklar çıktı. Path traversal, unsafe write path, NaN/Infinity gibi standart JSON dışı değerler ve fazla gevşek schema kontrolleri bunlardan bazılarıydı.

Düzeltmelerden sonra toplam 171 unit test geçti. Bu gün öğrendiğim en önemli şey, “testler geçiyor” demenin tek başına yeterli olmadığıydı. Testlerin neyi kapsadığına ayrıca bakmak gerekiyor.

---

## 18 Temmuz 2026

Bugün workspace ve command/test runtime katmanını geliştirdim. Her görev için izole bir çalışma klasörü oluşturan yapı, subprocess çalıştırma, timeout, stdout/stderr sınırlandırma ve test runner tarafı tamamlandı.

Windows ve POSIX process yönetimi arasında beklediğimden daha fazla fark çıktı. Özellikle timeout sonrasında alt process'leri temizlemek ve uzun çıktıları head/tail biçiminde güvenli şekilde saklamak için birkaç düzeltme yaptım.

Son durumda 263 test geçti, 2 platforma özel test atlandı.

---

## 19 Temmuz 2026

Bugün kaynak kod tarama ve patch lifecycle tarafına geçtim. AST ile function/class bulma, bounded source window, literal code search ve strict unified-diff parser geliştirdim.

Patch uygulama kısmı tahmin ettiğimden daha zor çıktı. Multi-hunk diff, zero-count hunk, CRLF/LF, BOM, final newline, file permission, rollback ve malformed diff gibi birçok edge case vardı.

Bu task biraz fazla geniş olmuştu ve birkaç review/repair turu gerekti. Sonunda 454 test geçti, 2 test atlandı. Bundan sonra task'ları daha küçük tutmam gerektiğini net şekilde gördüm.

---

## 20 Temmuz 2026

Bugün önceki iki günün runtime ve patch foundation çalışmalarını kapattım. İlgili commit'leri main üzerine aldım ve proje takip dosyalarını güncelledim.

Yeni büyük bir özellik eklemedim. Daha çok kabul, merge ve ilerleme kaydını düzgün hale getirme işi yaptım.

Bir sonraki hedef gerçek PDB session altyapısına geçmek oldu.

---

## 21 Temmuz 2026

Bugün PDB session lifecycle ve worker protokolünü geliştirdim. PDB'yi doğrudan modele açmak yerine izole bir worker process ve strict JSON protokolü kullandım.

İlk aşamada `hello`, `ping` ve `shutdown` gibi session işlemlerini güvenli hale getirdim. Ardından programı ilk breakpoint'e kadar çalıştıran one-shot execution ve breakpoint'te gerçekten bekleyen persistent paused-target yapısını geliştirdim.

Bu kısımda process cleanup, protocol response ownership, timeout, thread lifecycle ve target I/O izolasyonu gibi çok sayıda detayla uğraştım. PDB'nin kendisinden çok, PDB'yi güvenilir biçimde yöneten altyapının zor olduğunu gördüm.

---

## 22 Temmuz 2026

Bugün persistent target üzerinde continue/resume davranışını tamamladım. Program artık breakpoint'te durabiliyor, açık bir continue komutuyla ilerliyor ve sonraki breakpoint'te tekrar durabiliyordu.

Aynı source line'a loop nedeniyle yeniden gelinebildiği için ilerlemeyi yalnızca satır numarasına bakarak ölçmenin yanlış olduğunu gördüm. Bunun yerine pause generation mantığı kullandım.

Daha sonra stack, frame ve locals inceleme tarafını geliştirdim. Inspection işlemlerinin programı ilerletmemesine ve sadece gerçekten paused durumda çalışmasına dikkat ettim. Safe evaluation tarafında da keyfi Python çalıştırmak yerine sınırlandırılmış bir yaklaşım kullandım.

---

## 23 Temmuz 2026

Bugün hardened controller state machine tarafını tamamladım. Amaç, modelin her durumda istediği aracı çağırmasını engellemek ve hangi state'te hangi action'ın geçerli olduğunu deterministik hale getirmekti.

Controller ile tool registry arasındaki sınırları netleştirdim. Geçersiz action, yanlış transition ve bütçe dışı isteklerin fail-closed biçimde reddedilmesini sağladım.

Bu gün kod tarafında yeni özellikten çok agent'ın davranış sınırlarını sağlamlaştırmaya odaklandım.

---

## 24 Temmuz 2026

Bugün ilk küçük curated benchmark setini hazırladım. Birbirinden farklı beş Python bug'ı seçtim ve her görev için test, task metadata ve beklenen evaluator davranışını sabitledim.

Bu fixture'lar daha sonra none handling, off-by-one, wrong branch, mutation/alias ve caller-callee gibi farklı hata türlerini test etmek için kullandığım temel set oldu.

Amaç büyük bir benchmark yapmak değildi. Önce sistemin kontrollü ve tekrar üretilebilir küçük görevlerde gerçekten çalıştığını göstermek istedim.

---

## 25 Temmuz 2026

Bu tarih için ayrı bir teknik çalışma kaydı yok. Eski günlükte de bu güne ait doğrulanabilir ayrı bir kayıt bulunmadığı için sonradan iş uydurmadım.

---

## 26 Temmuz 2026

Bugün golden trajectory ve replay tarafını geliştirdim. Gerçek model olmadan, önceden belirlenmiş action/observation zincirlerini tekrar oynatıp controller ve tool davranışını doğrulayabildim.

Aynı gün ilk end-to-end demo tarafını da kapattım. Reproduction, source inspection, patch, verification ve event kayıtları tek akışta çalıştı.

Bu aşama önemliydi çünkü artık elimde sadece ayrı ayrı çalışan modüller değil, baştan sona ilerleyen deterministik bir debugging pipeline'ı vardı.

---

## 27 Temmuz 2026

Bugün gerçek model değerlendirmesi için harness geliştirdim. Burada önemli hedef, online/provider bağımlılığı olmasa bile aynı evaluation contract'ını çalıştırabilmekti.

Credential, route, timeout, usage, model identity ve attempt accounting gibi konuları ayrı kaydetmeye başladım. Böylece daha sonra bir run başarısız olduğunda bunun modelden mi, provider'dan mı yoksa harness'ten mi kaynaklandığını ayırmak mümkün olacaktı.

Aynı zamanda “başarısız model cevabı” ile “altyapı hatası”nı aynı kategoriye koymamak gerektiğini netleştirdim.

---

## 28 Temmuz 2026

Bugün live protocol contract'larını düzelttim. Controller'ın gerçekten kabul ettiği action'lar ile modele gösterilen action'ların aynı olması için state-specific contract'ları daha açık hale getirdim.

Controlled live baseline çalışmasında static policy başarıyla RESOLVED oldu. PDB policy ise modelin geçersiz action üretmesi nedeniyle debugger açılmadan sonlandı.

Bu yüzden o sonucu “PDB başarısız oldu” diye yorumlamadım. Asıl problem modelin PDB yoluna geçmeden önce protokolü doğru takip edememesiydi.

---

## 29 Temmuz 2026

Bugün invalid directive sonrasında modele bounded corrective feedback veren retry mekanizmasını ekledim. Önceden model hatalı action üretince aynı hatayı kör şekilde tekrarlayabiliyordu.

Küçük live diagnostic'te bazı denemelerde model feedback sonrasında legal bir action'a döndü, bazı denemelerde yine hatalı kaldı. Yani feedback faydalı olabiliyordu ama güvenilir bir çözüm değildi.

Aynı gün küçük bir dört-case matrix koştum. Static policy 2/2 RESOLVED olurken PDB policy 0/2 kaldı; ancak iki PDB case'i de debugger açılmadan directive validation aşamasında bitti. Bu nedenle sonucu PDB'nin kalitesi hakkında karşılaştırmalı kanıt olarak kullanmadım.

---

## 30 Temmuz 2026

Bugün PDB policy yolunu offline olarak detaylı biçimde denetledim. Modelin gördüğü live contract ile controller'ın gerçek kabul sınırları arasında bazı farklar buldum.

`decide_pdb_access`, state allowlist, tool registry, PDB lifecycle ve observation budget bilgilerini tek authoritative contract altında topladım. Protocol sürümünü 1.3'e çıkardım ve registry-less fallback gibi gevşek davranışları kaldırdım.

Final offline doğrulamada unit/golden ve integration testleri birlikte geçti. Aynı gün dataset ve evaluation kararlarını da toparladım. Büyük ve pahalı bir benchmark yerine QuixBugs'ı kontrollü fallback dataset olarak kullanma yönünü netleştirdim.

---

## 31 Temmuz 2026

Bugün model/RAG/SFT/DPO kararlarını mevcut kanıta göre topladım ve ilk kapsamlı teknik rapor ile demo paketini hazırladım.

Bu aşamada önemli bir ayrımı açıkça yazdım: o tarihe kadar QuixBugs tarafındaki bazı başarılı sonuçlar gerçek model başarısı değil, gold patch ve deterministik evaluator doğrulamasıydı. Gerçek-model kanıtı ile altyapı kanıtını birbirine karıştırmamaya dikkat ettim.

RAG için şimdilik bekleme, SFT için erteleme ve DPO için NO-GO kararı verdim. Bir sonraki adımın daha küçük ve kontrollü gerçek-model deneyleri olması gerektiğini belirledim.

---

## 1 Ağustos 2026

Bu tarih için ayrı bir teknik çalışma kaydı yok. Sonraki çalışmalar 2 Ağustos'ta ayrı bir campaign olarak başladı.

---

## 2 Ağustos 2026

Bugün QuixBugs paired-pilot v2 live runner üzerinde çalıştım. İlk sürüm yalnızca runner kapsamındaydı fakat gerçek koşularda bazı materyal problemleri ortaya çıktı.

Aynı gece birkaç dar repair yaptım. Single-winner sonucu, occupied workspace root'ları, case sonrasında hangi sonucun authoritative olduğu, strict JSON kayıtları ve crash-safe terminal commitment gibi konuları düzelttim.

Bu günün büyük kısmı “model ne kadar iyi?” sorusundan çok, deney koşusu yarıda kalsa bile geride doğru ve yorumlanabilir evidence bırakmakla geçti.

---

## 3 Ağustos 2026

Bugün OpenCode Go execution adapter ve gerçek route preflight tarafıyla yoğun şekilde uğraştım. Adapter'ın doğru executable'ı bulması, model/provider route'unu gerçekten kilitlemesi ve run sırasında hangi route'un kullanıldığını kanıtlayabilmesi gerekiyordu.

İlk çözüm birkaç kez yetersiz kaldı. Wrapper, catalog provider selection, isolated route capture, directive transport ve native executable yolu için art arda bounded repair'ler yaptım.

Bu gün biraz yorucu oldu çünkü sorunların çoğu model kalitesinden değil taşıma katmanından çıkıyordu. Sonunda live model deneyinden önce route ve transport tarafını daha güvenilir hale getirdim.

---

## 4 Ağustos 2026

Bugün live campaign için case-level public-evidence budget ve terminal davranışını kapattım.

Amaç, tek bir case'in sınırsız model çağrısı veya evidence üretmesi yerine belirlenen bütçe içinde tamamlanması ve durduğunda neden durduğunun açıkça kaydedilmesiydi.

Yeni büyük bir model deneyi yapmaktan çok, önceki günün transport ve campaign altyapısını son bir kez sağlamlaştırdım.

---

## 5 Ağustos 2026

Bugün campaign altyapısını main üzerinde kabul ettim ve yeni attempt kayıtlarını oluşturdum. Aynı dönemde QLoRA training pipeline'ının uygulanabilir taraflarını geliştirmeye başladım.

Ayrıca hocaya sunulabilecek Friday delivery bundle üzerinde çalıştım. Bu paket daha çok offline dokümantasyon, rehearsal ve mevcut kanıtın düzenlenmesine yönelikti.

Bu gün kod, eğitim hazırlığı ve sunum tarafı aynı anda ilerledi.

---

## 6 Ağustos 2026

Bugün ana repository üzerinde completion hardening yaptım. Ledger zaman bilgileri, transport teardown race, bilinen test failure'ları ve post-mortem PDB tarafındaki açıkları gözden geçirdim.

Aynı gün RAG/comparison/preference engineering için ayrı bir sprint yaptım. RAG, karşılaştırmalı değerlendirme ve preference/DPO tarafındaki contract'ları daha net hale getirdim.

İlk sürümde bazı sınırlar gevşek kaldığı için dar bir repair turu daha yaptım. Bu çalışma sonunda hangi parçaların gerçekten araştırma değeri taşıdığı, hangilerinin sadece engineering overhead olduğu daha netleşti.

---

## 7 Ağustos 2026

Bugün main repository için genel reconciliation ve completion pass yaptım. Eski dokümanlar, TODO durumları, araştırma kararları ve kabul edilmiş deney sonuçları arasındaki tutarsızlıkları topladım.

Model seçimi, dataset seçimi, RAW baseline, fine-tuning ve RAG/DPO başlıklarını sonraki deneylere temel olacak şekilde daha düzenli hale getirdim.

Bu gün daha çok “yeni özellik ekleme” yerine, şimdiye kadar yaptığım şeylerin birbirini doğru anlatmasını sağlama günüydü.

---

## 8 Ağustos 2026

Bu tarih için ayrı bir teknik çalışma kaydı yok. Eski günlükte de bu güne ait bağımsız ve doğrulanabilir bir entry bulunmadığı için kayıt eklemedim.

---

## 9 Ağustos 2026

Bugün tuned interactive debugger pilotu için hazırlık yaptım. Hedef, daha önce SWE-rebench üzerinde fine-tune edilmiş cp118 modelini gerçek debugger interaction içinde sınamaktı.

Modelin static patch üretme davranışı ile debugger action üretme davranışının aynı şey olmadığını burada daha net görmeye başladım.

Pilot için route, model identity, action contract ve evidence kayıtlarının hazır olduğundan emin oldum.

---

## 10 Ağustos 2026

Bugün RAW base control, master execution plan ve debugger interaction v2 tarafında birkaç önemli işi kapattım.

RAW model ile gerçek debugger yolu üzerinde D1 denemesi yaptım. İlk gerçek-model denemeleri kullanılabilir runtime evidence üretemedi; bazı action'lar tool error veya invalid path ile sonlandı. Bu sonuçları başarısızlığı gizlemeden bounded-negative evidence olarak tuttum.

Aynı gün cp118 + RAG tarafındaki S4 çalışmasını da değerlendirdim. cp118'in 40 görevde 0 RESOLVED kaldığı, 19/40 truncation ve çok yüksek scope violation gösterdiği netleşti. İlk fine-tune'un gerçek repair yeteneğini iyileştirmediğini burada açık biçimde gördüm.

---

## 11 Ağustos 2026

Bugün önceki deneyleri tek bir controlled comparison altında topladım. RAW, cp118, DPO, RAG, debugger interaction, model-generated test ve static verifier sonuçlarını aynı skora sıkıştırmadan ayrı eksenlerde karşılaştırdım.

Hocaya gösterilebilecek S6 evidence presentation'ı hazırladım. O tarihte gerçek model ile başarılı dinamik debugger demosu henüz yoktu; bunu açıkça bounded-negative olarak sundum.

Ayrıca literatür closeout yaptım ve debugger-aware sistemler, runtime evidence ve tool-use training çalışmalarını yeniden değerlendirdim. Sonuç, bizim yaşadığımız problemlerle uyumluydu: sadece modele PDB vermek yeterli değildi; arayüz ve öğrenilmiş interaction competence önemliydi.

Final teknik raporun yeni sürümünü ve günlük kayıtlarını da bu gün toparladım.

---

## 12 Ağustos 2026

Bugün projenin en yoğun günlerinden biri oldu.

Önce R5 generalized debugger matrix üzerinde çok sayıda küçük repair yaptım. 14B base modele geçtikten sonra sistem 5/5 RESOLVED sonucuna ulaştı. Fakat prompt'ları tekrar incelediğimde PATCH aşamasında hidden-test leakage olduğunu fark ettim. Bu nedenle 5/5 sonucu kabul etmedim ve diskalifiye ettim.

Sonra sanitizer, gerçek production exception yolu, region-filtered observations ve exact prompt leakage audit ekledim. Temiz R5.9 koşusunda base Qwen2.5-Coder-14B beş görevin tamamını yeniden çözdü:

- 5/5 RESOLVED
- 41 prompt denetlendi
- 0 leakage finding

Bu sonuç fine-tuning sonucu değildi; kullanılan model base 14B idi.

Aynı gün R6 için debugger-oriented yeni SFT pipeline'ını hazırladım. QuixBugs'tan 29 kullanılabilir fixture seçtim, 21 TRAIN ve 8 VALIDATION olarak ayırdım. Toplam 164 train ve 61 validation pair ürettim. Bu kez eğitim verisi sadece bug → patch değildi; debugger action, observation, diagnosis, patch ve verifier adımlarını içeriyordu.

Qwen2.5-Coder-7B modelini QLoRA ile eğittim. Eğitim 3 epoch ve toplam 48 optimization step sürdü. Checkpoint-30 validation açısından en iyi aday oldu. Gün içinde uzun GPU yükü sırasında hard power-off problemi de tekrar yaşandı, bu yüzden donanım tarafında daha dikkatli ilerlemeye karar verdim.

---

## 13 Ağustos 2026

Bugün R6'nın asıl validation değerlendirmesini tamamladım. Checkpoint-30 yalnızca ayrı tutulan QuixBugs validation görevlerine göre seçildi; final holdout seçimde kullanılmadı.

Fine-tuned Qwen2.5-Coder-7B modeli eğitimde görmediği 8 validation görevinin tamamını gerçek debugger/tool execution ve bağımsız verifier ile çözdü:

- 8/8 RESOLVED
- 97 model çağrısı
- 64.783 token
- yaklaşık 841,7 saniye toplam yürütüm
- 0 row error

Bundan sonra beş görevlik final holdout'a geçtim. None-handling görevi strict PASS ile RESOLVED oldu. Off-by-one verifier tarafında BREAKING_RESOLVED oldu ancak strict kriteri geçemedi. Üçüncü görev sırasında laptop yeniden hard power-off yaşadı. Wrong-branch yarıda kaldı; mutation-alias ve caller-callee hiç başlamadı. Bu nedenle final holdout için tam başarı oranı vermedim. Durumu `INCOMPLETE_HARDWARE_STOP` olarak kapattım.

Aynı gün bütün gerçek koşulardan professor-facing JSON trace paketi çıkardım. Toplam 10 trace hazırlandı: 8 validation ve 2 tamamlanabilen final-holdout koşusu. Şema, leakage ve yeniden üretim kontrolleri geçti; temiz checkout'tan üretilen dosyalar byte-identical kaldı.

Sonrasında proje dokümantasyonunu R1–R6 ile senkronize ettim. Eski raporları archive altına taşıdım, güncel final report ve project closeout dosyalarını yeniledim, README/TODO/project tracker kayıtlarını düzelttim. Son regression testleri main'e aldım ve repository'yi temiz duruma getirdim.

Günün sonunda staj sürecini anlatan bir Word dokümanı ve kısa, görsel bir HTML özet hazırladım. Bunları `docs/presentation/` altında proje sunum dosyaları olarak ekledim. Yerel çalışma alanında kalan `.pytest_cache`, boş output klasörleri ve geçici `tmp` içeriğini de temizledim; `tmp` içeriğini silmeden önce harici proje arşivine taşıdım.

---

# Genel Değerlendirme

Bu süreçte ilk başta düşündüğümden çok daha fazla sistem mühendisliği yaptım. Başlangıçta hedefim “LLM debugger kullansın ve bug düzeltsin” kadar basit görünüyordu. Gerçekte ise güvenilir bir sonuç için controller, tool contract, PDB lifecycle, patch manager, verifier, dataset ayrımı, leakage kontrolü ve reproducibility gibi parçaların hepsinin doğru çalışması gerektiğini gördüm.

En önemli deneysel sonuçlardan biri ilk fine-tune'un başarısız olmasıydı. RAW Qwen2.5-Coder-7B 40 görevde 5 RESOLVED üretirken SWE-rebench cp118 0/40 kaldı. Bu sonuç bana sadece bug → patch verisiyle fine-tuning yapmanın modeli otomatik olarak daha iyi bir debugger yapmadığını gösterdi.

Daha sonra eğitim hedefini debugger trajectory'lerine göre değiştirdim. Yeni fine-tuned 7B modelin ayrı validation setinde 8/8 RESOLVED alması projenin en güçlü olumlu sonucu oldu. Yine de matched-base ablation tamamlanmadığı için bu farkı yalnızca fine-tuning'in nedensel etkisi olarak sunmuyorum.

Proje sonunda elimde gerçek PDB ile çalışan bir agentic debugger, bağımsız verifier, temiz holdout değerlendirmeleri, debugger-oriented fine-tuned model, yeniden üretilebilir JSON trace'ler ve bütün süreci açıklayan teknik dokümantasyon oluştu. Final holdout donanım problemi nedeniyle tamamlanamadı; bunu da sonuçların sınırı olarak açık biçimde bıraktım.
