# diary.md — Staj Defteri İçerik Taslağı

> Bu dosya, şu ana kadar yapılan araştırma ve proje hazırlık çalışmalarını staj defteri diline çevirmek için hazırlanmış iç taslaktır. Resmî deftere aktarılırken tarih dağılımı fiilen yapılan çalışmayı doğru yansıtacak şekilde düzenlenmelidir.

---

## Yazım Formatı

Her günlük kayıt şu yapıyla yazılabilir:

```text
Tarih:
Çalışmanın Konusu:
Yapılan Çalışmalar:
Öğrendiklerim:
Sonuç / Bir Sonraki Adım:
```

---

# Toplu Günlük İçerik Taslağı

## 1. Proje hazırlığı ve çalışma alanının oluşturulması

Bugün staj projem için düzenli bir çalışma alanı oluşturdum. Projenin amacı, hocanın verdiği yapılacaklar listesi ve araştırma sürecinde izlenecek adımlar daha takip edilebilir hale getirildi.

Repository içinde `docs`, `research`, `diary` ve `prompts` gibi temel klasörlerin bulunması gerektiğini belirledim. Araştırma notları, sentez dosyaları, günlük kayıtları ve ileride kullanılacak promptların birbirinden ayrı tutulması için dosya yapısı planlandı.

Ayrıca proje takibini kolaylaştırmak için `docs/PROJECT_TRACKER.md` dosyası oluşturuldu. Bu tracker içinde literatür taraması, paper notları, sentez dosyaları, veri seti araştırmaları, MVP planı ve ileride yapılacak prototip çalışmaları sıraya alındı.

Bu çalışmada öğrendiğim en önemli nokta, agentic debugging gibi geniş bir konunun doğrudan kod yazarak başlanamayacak kadar kapsamlı olduğuydu. Önce kavramların ayrılması, literatürün sınıflandırılması ve projenin küçük parçalara bölünmesi gerektiğini gördüm.

---

## 2. Hocanın TODO listesinin teknik plana dönüştürülmesi

Bugün staj kapsamında verilen yapılacaklar listesini detaylı biçimde inceledim. Bu listede debugging, automated debugging, fault localization, automated program repair, LLM-based debugging, agentic debugging, dataset seçimi, fine-tuning, RAG, tool-using agent geliştirme, debugger adapter, patch generation ve evaluation gibi birçok başlık bulunuyordu.

Bu listeyi doğrudan uygulama sırası gibi kullanmak yerine, daha yönetilebilir bir araştırma ve prototip roadmap’ine dönüştürdüm. Özellikle şu ayrımı netleştirdim:

- Önce literatür ve kavramsal temel kurulacak.
- Sonra mevcut sistemler karşılaştırılacak.
- Daha sonra küçük bir Python/PDB tabanlı debugging MVP planlanacak.
- Fine-tuning, RAG, DPO/RLHF ve büyük dataset çalışmaları daha sonraki aşamalara bırakılacak.

Bu aşamada öğrendiğim şey, hocanın listesinin çok geniş bir akademik alanı kapsadığıydı. Bu yüzden ilk prototipin tüm başlıkları aynı anda çözmeye çalışmaması, bunun yerine ölçülebilir ve dar bir araştırma sorusuna odaklanması gerektiğine karar verdim.

---

## 3. Agentic debugging alanı için ilk literatür çerçevesi

Bugün agentic debugging kavramını anlamak için debugging, automated program repair, fault localization, LLM-based repair ve tool-using agents konularını ayrı başlıklar halinde incelemeye başladım.

Bu aşamada geleneksel debugging ile LLM tabanlı debugging arasındaki farkı daha net gördüm. Geleneksel debugging çoğunlukla geliştiricinin runtime state, stack trace, değişkenler ve test sonuçları üzerinden manuel karar vermesine dayanıyor. LLM tabanlı yaklaşımlar ise genelde issue açıklaması, hata mesajı veya repository context üzerinden patch önermeye çalışıyor.

Agentic debugging tarafında ise model yalnızca cevap üreten bir sistem değil, araçları kullanan ve adım adım gözlem toplayan bir controller gibi ele alınıyor. Bu nedenle proje açısından asıl önemli olan konu, LLM’in hangi araçlara hangi sınırlar içinde erişeceği oldu.

Bu çalışmadan çıkardığım ana sonuç şuydu: Debugger erişimi modele ham biçimde verilmemeli. Bunun yerine sınırlı, tipli ve güvenli araçlar üzerinden stack, locals, source window ve test sonucu gibi gözlemler toplanmalı.

---

## 4. ChatDBG çalışmasının incelenmesi

Bugün ChatDBG makalesi üzerinden LLM ile gerçek debugger entegrasyonu fikrini inceledim. ChatDBG, LLM’in GDB, LLDB ve Pdb gibi debugger’lar içinde kullanıcı adına bazı komutlar çalıştırarak runtime state toplamasını sağlıyor.

Bu çalışmada özellikle “LLM takes the wheel” fikri dikkat çekiciydi. Yani model, sadece hata mesajını yorumlamıyor; debugger üzerinden ek bilgi toplayarak root cause hakkında daha iyi açıklama yapmaya çalışıyor.

Bu makaleden proje için aldığım dersler:

- PDB entegrasyonu projenin merkezinde olabilir.
- Stack trace tek başına yeterli değildir; frame locals ve source context önemlidir.
- Modelin debugger komutlarını doğrudan ve sınırsız çalıştırması güvenli değildir.
- Debugger interaction sonrası patch önerisi ayrıca testlerle doğrulanmalıdır.

Bu çalışma sonunda MVP için ilk araç fikri oluştu: `get_stack`, `get_frame`, `get_locals`, `safe_eval_expression`, `get_source_window`, `run_tests` ve `apply_patch`.

---

## 5. debug-gym çalışmasının incelenmesi

Bugün debug-gym çalışmasını inceledim. Bu çalışma, LLM ajanlarının PDB gibi debugger ortamlarında interaktif biçimde hata ayıklamasını değerlendirdiği için proje açısından önemliydi.

Bu çalışmadan öğrendiğim en önemli nokta, PDB erişiminin her zaman ve baştan açılmasının doğru olmayabileceğiydi. Bazı durumlarda model önce statik analiz ve test çıktısı ile ilerlemeli, debugger’a ise yalnızca ihtiyaç duyduğunda girmelidir.

Bu nedenle proje kararını şu şekilde netleştirdim:

- PDB always-on bir baseline olarak test edilebilir.
- Ancak asıl hedef controller-gated PDB olmalıdır.
- Debugger erişimi, modelin confidence’ı düşük olduğunda veya bug runtime state’e bağlı olduğunda açılmalıdır.
- PDB gözlemleri patch kararının tek kaynağı değil, evidence kaynaklarından biri olmalıdır.

Bu çalışma, MVP’de statik baseline ile PDB-enabled varyantları karşılaştırmam gerektiğini gösterdi.

---

## 6. Agentless ve SWE-bench çalışmalarının incelenmesi

Bugün Agentless ve SWE-bench çalışmalarını birlikte değerlendirdim. Agentless, debugger kullanmadan güçlü bir localization-repair-validation pipeline kurulabileceğini gösterdi. Bu nedenle PDB destekli bir sistemin değerini ölçmek için önce güçlü bir static/test-feedback baseline gerektiğini gördüm.

SWE-bench ise gerçek GitHub issue’ları üzerinden patch doğrulamanın nasıl yapılabileceğini gösterdi. Bu çalışmada özellikle fail-to-pass ve pass-to-pass test ayrımı önemliydi:

- Fail-to-pass: Başta başarısız olan testlerin patch sonrası geçmesi.
- Pass-to-pass: Başta geçen testlerin patch sonrası hâlâ geçmesi.

Bu ayrım, patch’in yalnızca hedef hatayı çözmesini değil, mevcut davranışı da bozmamasını sağlıyor. MVP evaluation tarafında bu yaklaşımı kullanmaya karar verdim.

Ancak SWE-bench’in ilk MVP için çok büyük ve maliyetli olduğunu gördüm. Bu yüzden ilk aşamada full SWE-bench yerine küçük, kontrollü ve Python/PDB için uygun curated bug seti ile başlamanın daha doğru olduğuna karar verdim.

---

## 7. Tier 1 synthesis hazırlanması

Bugün ChatDBG, debug-gym, Agentless ve SWE-bench notlarını birleştirerek ilk mimari sentezi hazırladım. Bu sentezde projenin ana yönünü netleştirdim.

Tier 1 sonunda alınan karar:

```text
Python/PDB-first
single-controller agent
deterministic tool wrappers
runtime-state inspection
patch generation
patch/test verifier
Agentless-style static baseline comparison
```

Bu aşamada ayrıca şunların MVP dışında kalmasına karar verdim:

- GDB/LLDB
- multi-agent sistemler
- fine-tuning
- DPO/RLHF
- full SWE-bench
- büyük local model training
- production RAG altyapısı

Bu sentez, projenin kapsamını ciddi şekilde daralttı ve daha gerçekçi hale getirdi.

---

## 8. LDB / Debug Like a Human çalışmasının incelenmesi

Bugün LDB makalesi üzerinden runtime execution information’ın LLM debugging için ne kadar önemli olabileceğini inceledim. LDB, programı basic block seviyesinde inceleyip değişken durumlarını kullanarak modelden local correctness verdict alıyor.

Bu çalışmadan öğrendiğim en önemli şey, modelin sadece kendi cevabını tekrar düşünmesinin yeterli olmadığıydı. Gerçek runtime state, özellikle semantic bug’larda daha sağlam evidence sağlıyor.

Ancak LDB’nin full CFG/basic-block instrumentation yaklaşımının ilk MVP için fazla ağır olduğunu düşündüm. Bu yüzden daha küçük bir adaptasyon belirledim:

```text
failing test
-> traceback
-> failing frame
-> locals
-> source window
-> caller frame
-> frame-level diagnosis
-> patch
-> tests
```

Bu çalışma, PDB frame/locals gözlemlerinin neden gerekli olduğunu daha iyi gerekçelendirdi.

---

## 9. RepairAgent çalışmasının incelenmesi

Bugün RepairAgent makalesini inceledim. Bu çalışma, autonomous repair agent için state-machine ve tool control açısından önemliydi. RepairAgent’ta LLM tamamen serbest bırakılmıyor; belirli state’ler, tool setleri ve middleware ile yönlendiriliyor.

Bu makaleden aldığım ana ders:

```text
Raw autonomy güvenli ve verimli değil.
Agent, state-machine ile sınırlandırılmalı.
```

MVP için önerilen state yapısı bu aşamada netleşti:

- Reproduce
- Understand
- RuntimeEvidence
- Patch
- Validate
- Done
- Failed

Ayrıca hypothesis lifecycle fikri de bu çalışmadan geldi. Model yalnızca patch yazmamalı; önce root-cause hypothesis oluşturmalı, runtime evidence ile bunu desteklemeli veya reddetmelidir.

---

## 10. SWE-Agent çalışmasının incelenmesi

Bugün SWE-Agent makalesini inceledim. Bu çalışma Agent-Computer Interface kavramı açısından çok önemliydi. LLM’in başarısının yalnızca model gücüne değil, modele verilen araçların şekline de bağlı olduğunu gösteriyor.

Bu çalışmadan çıkardığım temel dersler:

- Raw shell iyi bir interface değildir.
- Dosyaları sınırsız göstermek context’i bozar.
- Araç çıktıları kısa, net ve task’a uygun olmalıdır.
- Edit araçları guardrail içermelidir.
- History/context yönetimi performansı etkiler.

MVP açısından en kritik karar şuydu:

```text
Raw PDB terminal yok.
Typed PDB ACI var.
```

Yani model doğrudan PDB promptuna komut yazmayacak. Bunun yerine `get_stack_summary`, `get_frame_locals`, `safe_eval_expression`, `get_source_window`, `apply_patch`, `run_tests` ve `revert_patch` gibi araçlar kullanılacak.

---

## 11. AutoCodeRover çalışmasının incelenmesi

Bugün AutoCodeRover makalesini inceledim. Bu çalışma, repository’nin yalnızca dosya koleksiyonu olarak değil, program structure olarak ele alınması gerektiğini gösterdi. AutoCodeRover AST tabanlı class/method/snippet retrieval yaklaşımını kullanıyor.

Bu makaleden aldığım en önemli ders:

```text
PDB runtime evidence tek başına yeterli değildir.
Önce structural retrieval gerekir.
```

Yani doğru yaklaşım şu olmalı:

```text
issue/failure
-> AST/symbol/method-level retrieval
-> PDB runtime evidence
-> root-cause hypothesis
-> patch
-> verifier
```

AutoCodeRover’daki stratified search fikri de önemliydi. İlk arama sonucu yeni class/method isimleri ortaya çıkarabilir ve sonraki aramaların argümanı olabilir. MVP’de bunun basit versiyonu `find_function`, `find_class`, `search_code` ve `get_function_source` araçlarıyla kurulabilir.

---

## 12. OpenHands çalışmasının incelenmesi

Bugün OpenHands makalesini inceledim. OpenHands debugging’den çok agent platform mimarisi açısından önemliydi. Bu çalışma event stream, action-observation runtime, sandboxed execution ve AgentSkills library gibi kavramları bir arada sunuyor.

Bu makaleden proje için aldığım ders:

```text
PDB agent tek parça script gibi yazılmamalı.
Küçük bir action-observation runtime olarak tasarlanmalı.
```

Bu yüzden MVP mimarisinde şu yapıyı kullanmaya karar verdim:

```text
agent/controller
-> typed action
-> runtime/tool execution
-> structured observation
-> event log
-> next action
```

OpenHands ayrıca agent sistemlerinde integration testlerin önemini gösterdi. Bu yüzden MVP’de mocked LLM / golden trajectory testleri ileride önemli olacak. İlk implementation task’ı bile schema, event logger ve state-machine contract gibi deterministik parçalardan başlamalı.

---

## 13. Tier 2 synthesis hazırlanması

Bugün LDB, RepairAgent, SWE-Agent, AutoCodeRover ve OpenHands notlarını birleştirerek Tier 2 MVP architecture synthesis dosyasını hazırladım.

Bu sentez sonucunda MVP şu şekilde güncellendi:

```text
small PDB-debugging agent platform
typed actions
sandbox/runtime boundary
event stream
skills library
verifier
integration tests
state-machine controller
```

Bu noktada artık yalnızca research notları değil, implementation’a geçiş için mimari kararlar da ortaya çıktı.

Sıradaki ana artifact olarak `docs/MVP_IMPLEMENTATION_PLAN.md` belirlendi. Bu dosyanın package adı, task schema, ilk beş curated bug, controller states, PDB skills, test commands ve Implementation Task 1 acceptance criteria gibi kararları kilitlemesi planlandı.

---

## 14. MVP implementation plan hazırlığı

Bugün araştırma ve mimari sentezlerden sonra MVP implementation plan için hazırlık yaptım. Bu aşamada planın yalnızca dokümantasyon seviyesinde olması gerektiğine karar verdim. Henüz source code, prototype veya PDB runtime yazılmamalı.

Plan için şu kararlar netleştirildi:

- Python import package adı: `agentic_debugger`
- Distribution adı: `agentic-debugger`
- Minimum Python: `>=3.11`
- İlk dependency politikası: runtime dependency yok, yalnız pytest dev/test dependency
- İlk dataset: beş küçük curated Python bug
- İlk implementation task: package/schema/event skeleton
- İlk controller modeli: state-machine ve typed action/observation
- İlk PDB policy: controller-gated PDB
- Tier 3/supporting papers: ertelendi

Bu hazırlık, araştırma aşamasından implementation aşamasına geçişte bir köprü görevi gördü.

---

# Resmî Deftere Aktarılabilecek Kısa Günlük Bloklar

Aşağıdaki bloklar, resmî defter formatına aktarılırken sadeleştirilerek kullanılabilir.

## Günlük Blok A — Proje hazırlığı ve TODO analizi

Bugün staj projemin kapsamını ve hocamın verdiği yapılacaklar listesini inceledim. Debugging, automated program repair, fault localization, LLM-based debugging, agentic debugging, veri setleri, model eğitimi, RAG, debugger adapter ve evaluation başlıklarını ayrı ayrı not aldım. Proje sürecinin doğrudan kodlama ile başlamaması gerektiğini, önce literatür ve teknik kapsamın netleştirilmesi gerektiğini gördüm. Bu nedenle repository içinde araştırma, dokümantasyon ve günlük çalışmaları için ayrı klasörler oluşturdum ve proje takip dosyası hazırladım.

## Günlük Blok B — LLM tabanlı debugging ve ChatDBG/debug-gym incelemesi

Bugün LLM tabanlı debugging yaklaşımlarını inceledim. Özellikle ChatDBG ve debug-gym çalışmalarında LLM’in debugger ile nasıl etkileşime girdiğini analiz ettim. PDB gibi debugger’ların stack frame, local variable ve runtime state bilgisi sağlayarak hata sebebini daha iyi açıklamaya yardımcı olabileceğini öğrendim. Ancak debugger erişiminin ham ve sınırsız verilmemesi gerektiğini, güvenli ve tipli araçlar üzerinden sınırlandırılması gerektiğini gördüm.

## Günlük Blok C — Static baseline, SWE-bench ve evaluation yaklaşımı

Bugün Agentless ve SWE-bench çalışmalarını inceledim. Agentless, debugger kullanmadan güçlü bir localization-repair-validation pipeline kurulabileceğini gösterdi. SWE-bench ise gerçek GitHub issue’ları üzerinden patch doğrulamanın nasıl yapılabileceğini gösterdi. Fail-to-pass ve pass-to-pass test ayrımı benim için önemliydi. Bu ayrım sayesinde üretilen patch’in hem hedef hatayı çözüp çözmediği hem de mevcut davranışı bozup bozmadığı ölçülebiliyor.

## Günlük Blok D — Tier 2 mimari çalışmaları

Bugün LDB, RepairAgent, SWE-Agent, AutoCodeRover ve OpenHands çalışmalarını karşılaştırdım. LDB runtime state’in önemini, RepairAgent state-machine kontrollü agent yapısını, SWE-Agent iyi tasarlanmış tool interface ihtiyacını, AutoCodeRover structural code retrieval yaklaşımını, OpenHands ise action-observation event stream mimarisini gösterdi. Bu çalışmalar sonucunda MVP’nin tek parça script olarak değil, küçük ve test edilebilir bir debugging agent platformu olarak tasarlanmasına karar verdim.

## Günlük Blok E — MVP implementation plan hazırlığı

Bugün araştırma sonuçlarını implementation plan’a dönüştürdüm. İlk MVP’nin Python/PDB-first olmasına, package adının `agentic_debugger` olarak belirlenmesine, ilk veri setinin beş küçük curated Python bug’dan oluşmasına ve ilk implementation task’ın schema/event/logger foundation olmasına karar verdim. Fine-tuning, RAG, multi-agent, GDB/LLDB ve full SWE-bench gibi konuları ilk MVP dışına aldım. Böylece araştırmadan prototip geliştirmeye geçiş için net ve uygulanabilir bir yol haritası oluştu.

