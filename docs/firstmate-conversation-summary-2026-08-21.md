# FirstMate Görüşme ve Proje Süreci Özeti

**Tarih:** 21 Ağustos 2026  
**Proje:** `agentic-debugging-internship`  
**Hazırlanma amacı:** Bu konuşmada alınan kararları, yapılan deneyleri, karşılaşılan sorunları, elde edilen kanıtları ve bir sonraki doğru adımı tek bir yerde toplamak.

> Bu belge, konuşmanın karar ve kanıt odaklı özetidir. Eski raporların veya deney kayıtlarının yerine geçmez; onların nasıl okunması gerektiğini ve bugünkü ortak anlayışımızı açıklar.

## 1. FirstMate rolü ve çalışma biçimi

Konuşmanın başında Onur, `Onurs_Workflow_v5.4.md` dosyasını paylaşarak ChatGPT'yi projenin **FirstMate'i** olarak tanımladı. Başlangıçtaki beklenti kod yazmak değil, projeyi ve süreci yerel repository üzerinden doğru anlamak, teknik ajanların planlarını ve sonuçlarını eleştirel biçimde değerlendirmek ve sahibine karar desteği vermekti.

Bu rolün temel anlamı şuydu:

- Proje sahibi ve nihai ürün/Git karar vericisi Onur'dur.
- FirstMate, proje hedefini korur; planları, aday değişiklikleri ve kanıt paketlerini inceler.
- Bir ajanın “başardım” demesi kanıt değildir. Başarıyı bağımsız verifier, çalıştırma kayıtları, temizlik ve tekrar oynatma kanıtlar.
- Eski raporlar bağlam sağlar fakat teknik gerçeklik mevcut kaynak kod, testler ve gerçek çalışma davranışıdır.
- Model başarısı, altyapı başarısı ve bilimsel iddia birbirine karıştırılmaz.

Süreç ilerledikçe Onur, sürekli plan/ajan devirlerinden yorulduğunu açıkça belirtti ve FirstMate'in gerekli teknik teşhis ve düzeltmeleri doğrudan yapmasını istedi. Bundan sonra çalışma biçimi, güvenli sınırlar içerisinde “incele, düzelt, doğrula ve sonucu açıkla” yönüne döndü.

## 2. Başlangıçtaki temel sorun: Çok zaman, çok token, sıfır net ilerleme

Onur'un ilk büyük itirazı son derece haklıydı: Yaklaşık 2–3 gündür devam eden çalışmalar ilerliyormuş hissi vermiyordu; on görevlik testler saatler sürüyor, 2–3 saat beklendikten sonra sonuç sıfır çıkıyor ve toplam tüketim yaklaşık iki milyon token seviyesine ulaşıyordu.

Başlangıçta şu sorular soruldu:

- Bir debugger aracının testi neden saatler sürüyor?
- Proje başarılı olduğunda her on test yine yaklaşık 100 dakika mı sürecek?
- Asıl ürün mü test ediliyor, yoksa değerlendirme altyapısının sorunlarıyla mı uğraşılıyor?
- SWE-rebench üzerinde modele tam olarak ne yaptırılmaya çalışılıyor?
- Eski ve sınırlı bir modelden kapasitesinin çok üstünde işler mi bekleniyor?
- Debugger kullanımı neden gerçekten zorunlu ve gözlenebilir değil?

Bu sorgulama, projenin yönünü değiştiren en önemli nokta oldu. Çünkü sorun yalnızca “model görevi çözemedi” değildi; modelin başarısızlığı ile evaluation harness'in başarısızlığı birbirinden ayrılamıyordu.

## 3. Projenin gerçek amacı yeniden netleştirildi

Projenin amacı sadece bir SWE-bench skoruna ulaşmak veya herhangi bir modele çok sayıda GitHub issue çözdürmek değildir.

Projenin çekirdeği şudur:

> Bir yazılım onarım ajanının Python/PDB debugger'ını gerçek ve zorunlu biçimde kullanarak hatayı gözlemlemesi, gözleme dayalı teşhis üretmesi, kontrollü bir patch uygulaması ve bağımsız verifier tarafından doğrulanmış bir onarım gerçekleştirmesi.

İnşa edilen sistemin ana parçaları:

1. **Tek controller ajanı:** Modelin durumunu, araç çağrılarını ve onarım akışını yönetir.
2. **Typed/deterministic araçlar:** Kaynak inceleme, test, PDB, patch ve doğrulama işlemlerini sınırları belli sözleşmelerle sunar.
3. **PDB oturumu:** Modelin gerçek çalışma zamanındaki stack, locals ve step/next gözlemlerini almasını sağlar.
4. **Proof gate:** Yeterli ve göreve bağlı PDB kanıtı oluşmadan teşhis veya patch aşamasına geçilmesini engeller.
5. **Disposable workspace:** Canonical fixture veya dış kaynak doğrudan değiştirilmez; aday onarım geçici çalışma alanında denenir.
6. **PatchManager ve unified diff:** Modelin değişikliği kontrollü ve izin verilen dosyalarda uygulanır.
7. **Bağımsız verifier:** Modelin veya controller'ın iddiasından bağımsız olarak fail-to-pass ve pass-to-pass testlerini çalıştırır.
8. **Event trajectory ve replay:** Ne olduğunun sonradan deterministik biçimde incelenmesini sağlar.
9. **Cleanup ve canonical immutability:** Başarının bir parçası olarak geçici alanların temizlendiğini ve kaynak fixture'ın değişmediğini kanıtlar.

Dolayısıyla projenin esas araştırma sorusu “model kod yazabiliyor mu?” değil, daha dar ve daha değerlidir:

> Debugger destekli, kanıta bağlı ve fail-closed bir ajan mimarisi gerçek yazılım onarımında kullanılabilir mi; farklı model kapasiteleri bu mimaride hangi görev seviyesine kadar başarılı olur?

## 4. Eski SWE-rebench DEVQUAL-10 kampanyasında ne olmuştu?

Eski kampanya, GPT-OSS 20B'nin Ollama Cloud üzerinden SWE-rebench'in sabit ilk on görevi üzerinde qualification almasını hedefliyordu. V2'den V12'ye uzanan çok sayıda treatment kimliği üretildi fakat başarılı bir qualification elde edilemedi.

Önceki yerel ajanın repository araştırmasına ve daha sonra yapılan doğrulamalara göre sorunların büyük bölümü model kalitesinden önce evaluation altyapısındaydı:

- V11, provider bağlantısının kararlı olduğunu gösterdi: 181 transport attempt ve 181 generation call gerçekleşti, provider-invalid oluşmadı.
- Buna rağmen on görevin sekizi `infrastructure_invalid`, ikisi başarısız ve hiçbiri resolved değildi.
- Beş satırda resmi evaluator dış watchdog'un 360 saniyelik süresinde rapor üretmedi.
- Bir satır Docker pull için sabit 300 saniyelik sınıra takıldı.
- Bir satır typed exit/stderr kanıtı olmadan public-runtime dependency failure olarak kaldı.
- Bir satır, modelin uzun reasoning stream'i nedeniyle adapter'ın toplam 8 MiB wire guard'ına takıldı.
- Resmi evaluator'ın aynı aday üzerinde sonradan yaklaşık 2–3 saniyede tamamlanabilmesi, saatler süren kısmın “testin doğal maliyeti” olmadığını gösterdi.
- Container lifecycle, child process ve rapor-before-cleanup telemetrisi eksik olduğu için “yanlış patch” ile “evaluator hiç doğru başlamadı” ayrılamıyordu.

Kampanyanın uzun sürmesinin birleşik nedeni şuydu:

- Tek tek görevlerde 13–27 model üretimi gerçekleşebiliyordu.
- Generation deadline, outer request, model phase, Docker pull ve evaluator watchdog gibi iç içe sabit bütçeler vardı.
- On görev sırayla çalıştırılıyordu.
- Fail-closed politika nedeniyle belirsiz her durum altyapı geçersizliği olarak sınıflandırılıyordu.
- Her tedavi değişikliği yeni bir campaign identity gerektiriyordu.
- Çok zor ve geniş gerçek proje görevleri, model kapasitesi ölçülmeden doğrudan deneniyordu.
- PDB kullanımı ana iddiayı kanıtlayacak kadar zorunlu ve göreve bağlı değildi.

Sonuç olarak saatler süren şey debugger'ın kendisi değildi. Saatler süren şey, büyük görevler üzerinde çok sayıda model turu, dış provider trafiği, Docker/evaluator beklemeleri ve yetersiz gözlenebilirliği olan on görevlik kampanyaydı.

## 5. Eski 32/100 görevin neden zor olduğu

Konuşmada eski SWE-rebench görevlerinden birinin niteliği özellikle sorgulandı. Daha sonra yeniden kullanılan görev `audreyr__cookiecutter-967` oldu. Görev, Cookiecutter'ın YAML config davranışındaki `gh:` abbreviation uyumluluğunu düzeltmeyi gerektiriyordu.

Yüzeyde küçük görünen bir config hatası olsa da gerçek onarım şu yetenekleri gerektiriyordu:

- Dış ve gerçek bir Python paketinin kod yolunu anlamak.
- Config'in birden fazla katmanda nasıl yüklendiğini takip etmek.
- Varsayılan abbreviation'lar ile kullanıcı konfigürasyonunun precedence ve nested-merge semantiğini doğru kurmak.
- Sadece görünür örneği değil farklı konfigürasyon varyantlarını da doğru ele almak.
- Mevcut davranışlarda regression oluşturmamak.
- Paket içi relative import'larla PDB oturumunu doğru başlatmak.

Bu nedenle görev, küçük curated fixture'lardan belirgin biçimde daha zordu. “32/100” bir benchmark yüzdesi veya evrensel zorluk ölçümü değildir; bizim capability ladder'ımızdaki göreli ve sıralı bir etikettir.

## 6. Onur'un yaklaşımı değiştiren ana fikirleri

Onur birkaç temel düzeltme önerdi ve sonraki deney tasarımı bunlara göre kuruldu.

### 6.1 Tek görevle ilerlemek

Beş, sekiz veya on görevle yeniden başlamak reddedildi. İlk amaç tek bir görevin tüm ürün yolundan geçtiğini kanıtlamak oldu. Bir görev başarıyla tamamlandıktan sonra görev zorluğu kademeli olarak artırılacaktı.

Bu yaklaşımın avantajları:

- Bir başarısızlığın kök nedeni daha hızlı bulunur.
- Provider maliyeti ve süre ciddi ölçüde azalır.
- Altyapı ile model kapasitesi birbirinden ayrılır.
- Her seviyede hangi yeteneğin sınandığı açık olur.
- Modelin gerçek sınırı gözlenebilir hale gelir.

### 6.2 Debugger kullanımını zorunlu kılmak

Bir patch'in testleri geçmesi tek başına projenin ana iddiasını kanıtlamaz. Modelin:

- PDB oturumunu başlatması,
- stack görmesi,
- göreve bağlı local değişkeni incelemesi,
- step veya next ile yürütmeyi ilerletmesi,
- teşhisini bu gözlemlere bağlaması

zorunlu hale getirildi. Yeterli PDB kanıtı olmadan patch aksiyonu açılamadı.

### 6.3 Düşünce çıktısını aksiyondan ayırmak

Onur'un önemli itirazlarından biri, modelin düşünürken ürettiği uzun çıktının neden otomatik olarak kabul edilemez sayıldığıydı. Mouse kontrolü örneğiyle anlatılan ayrım şuydu:

- Modelin “şuraya tıklamayı düşünüyorum” diye yazması bir **düşünce/telemetri** olayıdır.
- Mouse'u gerçekten hareket ettirip tıklaması bir **aksiyon** olayıdır.
- Başarı, düşüncenin kısa olmasına değil, doğru aksiyonun gerçekleştirilmesine göre değerlendirilmelidir.

Bu nedenle yeni yaklaşımda reasoning/thinking stream:

- Yetkili bir tool action olarak kabul edilmez.
- Protokol komutu olarak yanlış yorumlanmaz.
- Tek başına başarısızlık nedeni yapılmaz.
- İlerleme/aktivite telemetrisi olarak izlenebilir.
- Canonical request bütçesi ve provider protokolü açısından güvenli biçimde taşınır.

### 6.4 Sabit duvar süresi yerine aktivite-aware koruma

Onur, bir ajanın gerçek iş yaparken 15–20 dakika çalışabilmesinin normal olduğunu; asıl sorun hiçbir çıktı, düşünme, tool çağrısı veya ilerleme belirtisi olmadan takılı kalması olduğunu belirtti.

Yeni ilke:

> Aktif biçimde ilerleyen ajanı sırf toplam süre doldu diye erken kesme; fakat belirli bir süre hiçbir ilerleme veya heartbeat yoksa takılmayı fail-closed biçimde durdur.

Bu, limitsiz çalışma anlamına gelmez. Provider ve sistem güvenliği için üst sınırlar korunur; ancak ana karar yalnızca kör bir kısa wall-clock limitine dayanmaz.

### 6.5 Model capability ladder

Tek bir modelin birden çok artan zorluk seviyesinde denenmesi, sınırına gelince daha güçlü modele geçilmesi kararlaştırıldı. Bilimsel olarak hedeflenen anlatı şudur:

> Fine-tuning önemli olabilir; fakat temel model seçimi de debugger destekli onarım başarısını ciddi biçimde belirler. Aynı frozen sistem ve görev üzerinde daha güçlü modelin sınırı ileri taşıyıp taşımadığı ölçülebilir.

## 7. 6/100: İlk tek görevli PDB-required proof

İlk kontrollü deney `codex/single-task-pdb-required-proof-v1` branch'inde oluşturuldu. Çok küçük, sentetik ve net bir boundary hatası kullanıldı.

Kurulan kanıt şunları kapsadı:

- Exact pytest failure üretimi.
- PDB üzerinden göreve bağlı gözlemler.
- Kanıta bağlı teşhis.
- Proof gate tamamlanmadan Patch aracının kapalı olması.
- PatchManager üzerinden unified diff uygulanması.
- Bağımsız verifier tarafından `RESOLVED` kararı.
- Fail-to-pass ve pass-to-pass kontrolleri.
- Workspace cleanup.
- Canonical fixture immutability.
- Event replay.

Sentetik preflight yolunda 22 logical call / 22 transport attempt görüldü. Gerçek accepted live koşuda kayıt altına alınan temel sonuçlar:

- 21 model call / 21 transport attempt.
- 0 retry ve 0 provider error.
- 3 başarılı, 0 başarısız PDB observation.
- F2P: 1/1.
- P2P: 1/1.
- Verifier: `RESOLVED`.
- Cleanup ve canonical immutability: başarılı.
- Replay: `Done`.
- Toplam süre: yaklaşık 68.233 saniye.

Bu deney, projenin temel ürün yolunun küçük bir görev üzerinde gerçekten çalışabileceğini kanıtladı.

## 8. İlk live proof sırasında ortaya çıkan sorunlar ve onarımlar

Başarı düz bir çizgide gelmedi. Birkaç önemli altyapı problemi gerçek live çalıştırmalarda görünür oldu.

### 8.1 Yanlış acceptance script davranışı

İlk PowerShell kontrolünde acceptance koşulu `throw` üretmesine rağmen sonraki satır koşulsuz olarak `LIVE SINGLE-TASK PDB PROOF: ACCEPTED` yazdı. Bu, başarı mesajının verifier kanıtına bağlı olmadığını gösterdi. Script fail-closed hale getirildi; rejected durumda success mesajı yazmaması sağlandı.

### 8.2 Ollama sürüm eşleşmesi

Script `0.32.14` beklerken yerel sürüm `0.32.15` idi ve provider'a temas edilmeden live execution reddedildi. Pin güncellendi fakat bu tek başına yetmedi.

### 8.3 UTF-8 BOM/config sorunu

PowerShell'in UTF-8 yazımı config dosyasına BOM ekledi ve strict config parser bunu reddetti. Yazım encoding'i ASCII olarak değiştirildi.

### 8.4 Provider error'ın tip bilgisini kaybetmesi

Sonraki koşuda üç model call sonrası `PROVIDER_ERROR` oluştu; ilk rapor yeterli typed ayrıntıyı korumuyordu. Adapter ve live runner, provider failure'ın türünü, stderr/exit bilgisini ve gerçek nedeni kaybetmeden raporlayacak şekilde onarıldı.

### 8.5 Exact PDB session ve proof readiness boşlukları

`get_failure_trace` her durumda exact-proof PDB session factory'sini kullanmıyordu. Ayrıca structured pytest bootstrap failure ve proof-only diagnosis readiness yeterince sıkı değildi. Bunlar fail-closed biçimde düzeltildi; custom factory desteği korundu.

### 8.6 Request/history uyumluluğu

Proof history normal geçmişten ayrıldı, sentetik transport'un yalnızca mevcut payload'dan teşhis kanıtı türetmesi sağlandı ve canonical request büyüklüğü Ollama adapter sınırına uygun hale getirildi.

Bu süreçte Onur'un gerçekleştirdiği başlıca commit'ler:

| Commit | Açıklama |
|---|---|
| `3c50c27` | `Add single-task PDB-required repair proof` |
| `525e612` | `Repair exact PDB proof workflow` |
| `d6d4dd1` | `Preserve typed live adapter failures` |
| `7d2553b` | `Constrain exact PDB proof lifecycle` |
| `1b601f8` | `Complete single-task exact PDB live proof` |

Bu commit zinciri, 6/100 temel kanıtı ve live taşıma yolunun güvenilir hale gelmesini sağladı.

## 9. 12/100: Caller–callee görevi

Bir sonraki seviye `pdb-required-caller-callee-007` oldu. Bu görev tek fonksiyonlu boundary hatasından daha zordu; hatayı anlamak için çağıran ve çağrılan fonksiyon arasındaki değer akışını görmek gerekiyordu.

Accepted sonuç:

- 22 model call / 22 transport attempt.
- 0 retry ve 0 provider error.
- 3 başarılı, 0 başarısız PDB observation.
- F2P: 1/1.
- P2P: 2/2.
- Private check: başarılı.
- Verifier: `RESOLVED`.
- Cleanup ve canonical immutability: başarılı.
- Replay: `Done`.
- Toplam süre: yaklaşık 391.289 saniye.

Modelin uzun reasoning çıktıları görüldü fakat bunlar aksiyon yerine activity telemetrisi olarak ele alındı. Sistem, model düşünürken onu hatalı protokol üretmiş saymadı.

## 10. 18/100: Çok aşamalı birim dönüşümü ve retry görevi

Sonraki deney `pdb-required-multistage-units-008` oldu. Görev üç aşamalı bir akış içeriyordu: normalize, convert ve retry. Modelin tek bir boundary değerinden ziyade birkaç fonksiyon boyunca değer dönüşümünü takip etmesi gerekiyordu.

PDB gözlemlerinde özellikle `value`, `base_delay` ve `retry_count` gibi göreve bağlı değişkenler kullanıldı.

Accepted sonuç:

- 21 model call / 21 transport attempt.
- 0 retry ve 0 provider error.
- 3 başarılı, 0 başarısız PDB observation.
- F2P: 1/1.
- P2P: 2/2.
- Private check: başarılı.
- Verifier: `RESOLVED`.
- Cleanup ve canonical immutability: başarılı.
- Replay: `Done`.
- Yaklaşık 54 event.
- Toplam süre: yaklaşık 165.463 saniye.

Bu sonuç, GPT-OSS 20B'nin yalnızca oyuncak bir off-by-one hatasını değil, birden fazla fonksiyon arasında runtime state izlemeyi gerektiren daha karmaşık curated görevi de sistem içerisinde çözebildiğini gösterdi.

## 11. Model seçenekleri ve GLM 5.2 tartışması

Onur, Ollama üzerinden erişilebilen daha güçlü modellerden biri olarak GLM 5.2'yi gündeme getirdi. Amaç, güçlü bir model 32/100 görevi başarırsa şu hipotezi desteklemekti:

> Model kapasitesi arttıkça debugger destekli ajan mimarisinden gerçek yazılım onarımında daha fazla fayda elde edilebilir; sadece fine-tuning değil, doğru temel model seçimi de belirleyicidir.

Ancak önce mevcut modelin sınırını temiz biçimde ölçme kararı alındı. Son kontrol sırasında yerel Ollama listesinde erişilebilir görünen cloud modeller:

- `gpt-oss:20b-cloud`
- `nemotron-3-nano:30b-cloud`

GLM 5.2 o anki listede görünmediği için henüz frozen deney rotasına alınmadı. İlke olarak model değiştirmek bütün sistemi yeniden yazmayı gerektirmemelidir; route uyumluysa çoğunlukla model adı, capability/config kimliği ve modele özgü güvenli transport parametreleri değişir. Fakat her model yeni bir treatment identity ile ve aynı görev sözleşmesi altında denenmelidir.

## 12. 32/100 dış gerçek proje görevi için hazırlık

18/100 başarısından sonra daha önce başarısız olunan `audreyr__cookiecutter-967` görevine dönüldü. Bu kez amaç eski on görevlik kampanyayı tekrarlamak değil, yalnızca tek ve frozen bir dış görevde ürün yolunu temiz biçimde sınamaktı.

Görev sözleşmesi:

- Public issue: Cookiecutter'da `gh:` prefix/abbreviation davranışını onarmak.
- Model yalnızca public task statement, izin verilen kaynak, public test ve kendi PDB gözlemlerini görebilir.
- Gold patch ve official hidden test ayrıntıları modele gösterilmez.
- Public sözleşmede 1 F2P ve 1 P2P kontrolü vardır.
- Official evaluator'da 5 F2P ve 9 P2P kontrolü vardır.
- Debugger kullanımı zorunludur.
- Retry sayısı sıfırdır.
- Önceki hidden evaluator sonucu üzerinden görevi veya prompt'u ayarlamak yasaktır.

Paket hedeflerinde PDB'nin relative import'ları doğru çözebilmesi için established runtime yolu onarıldı:

- Workspace path, PDB worker'ın import ortamına doğru eklendi.
- `__main__` altında çalışırken paket için conventional `__package__` semantiği sağlandı.
- Senaryo bazlı breakpoint, inspect expression ve proof source line desteği eklendi.
- Operator script oluşturuldu.

Gerçek provider kullanılmadan yapılan smoke testte `get_config` içindeki ilgili satırda duruldu, stack/locals gözlendi ve next ile yürütme ilerletildi. Bu, PDB'nin gerçek Cookiecutter paketi içinde çalıştığını kanıtladı.

Hazırlık doğrulamaları arasında:

- Geniş ilgili test yüzeyinde 619 testin geçmesi.
- Provider-free dış görev replay'inde 21 call ile local `RESOLVED` elde edilmesi.
- Canonical request'in 25.000 byte sınırı altında kalması.
- Compile ve diff kontrollerinin geçmesi

yer aldı.

## 13. 32/100 live denemeleri

### 13.1 V1 — Request büyüklüğü hatası

İlk live denemede:

- 9 logical call,
- 8 provider response,
- 2 başarılı PDB observation

sonrasında 9. çağrı `request_too_large` ile reddedildi. Local değişken gözlemi hem geçmişte hem de current observation içerisinde tekrarlandığı için canonical request yaklaşık 33.224 byte'a çıktı.

Bu model başarısızlığı değildi; provider request projection hatasıydı. Authoritative event history korunurken provider'a gönderilen history daraltıldı ve mevcut PDB observation'ın tekrar taşınması engellendi.

### 13.2 V2 — Yanlış local binding seçimi

İkinci denemede:

- 15 logical call,
- 14 provider response,
- 3 başarılı PDB observation

elde edildi. Ancak generic politika ilk uygun local olarak `config_dict` seçti. Bu nesne Windows path'leri içerdiği için model bunları yeniden aktarırken kaçışlar bozuldu ve diagnosis gate `invalid_directive` üretti.

Bu da modelin asıl semantic onarım kalitesini ölçmeyen bir senaryo bağlama hatasıydı. Görev sözleşmesine explicit `inspect_expressions` eklendi ve doğru runtime binding olan `yaml_dict` seçildi.

### 13.3 V3 — Ürün yolu tamamlandı, official doğruluk başarısız oldu

Üçüncü deneme altyapı açısından başarılı biçimde tamamlandı:

- 24 model call / 24 transport attempt.
- 0 retry.
- 0 provider error.
- 3 başarılı, 0 başarısız PDB observation.
- Yaklaşık 209.016 token.
- Yaklaşık 391.047 thinking byte.
- Yaklaşık 86.520 streamed frame.
- Toplam model/controller süresi yaklaşık 704.710 saniye, yani 11 dakika 45 saniye.
- Controller tamamlandı.
- Local verifier yolu `RESOLVED` oldu.
- Public F2P: 1/1.
- Public P2P: 1/1.
- Cleanup ve canonical immutability başarılı oldu.
- Replay, provider'a yeniden temas edilmeden `Done` olarak kurtarıldı.

Bu koşu, modelin uzun düşünmesine izin veren activity-aware yaklaşımın çalıştığını gösterdi: on binlerce frame ve yüz binlerce thinking byte sistem tarafından protokol aksiyonu olarak yanlış yorumlanmadı.

### 13.4 Official evaluator entegrasyonunda bulunan iki son sorun

İlk olarak, official evaluation sonrası rapor yazımı sırasında replay state zaten string olduğu halde `.value` erişimi yapıldı ve `AttributeError` oluştu. Provider koşusu tekrarlanmadan, kaydedilmiş event'lerden rapor provider-free olarak yeniden üretildi.

İkinci olarak, modelin ham candidate diff'i terminal newline içermediği için official `git apply` exit 128 ile `corrupt patch` hatası verdi. Bu bir semantic model sonucu değildi. Ham patch ve hash'i korunarak yalnızca terminal newline deterministik biçimde normalize edildi ve official evaluator yeniden çalıştırıldı.

### 13.5 Official sonuç: Gerçek model sınırı

Normalize edilmiş aynı patch official evaluator'a verildiğinde altyapı çalıştı fakat onarım kabul edilmedi:

- Official F2P: 0/5.
- Official P2P: 9/9 geçmedi.
- Container exit code: 1.
- Sonuç: accepted değil.

Modelin ürettiği değişiklik özünde şuydu:

```python
config_dict.update(yaml_dict)
config_dict['abbreviations'].update(DEFAULT_CONFIG['abbreviations'])
```

Bu patch görünür `gh` örneğini geri getiriyordu fakat varsayılanların kullanıcı konfigürasyonunu ezmesine yol açan yanlış precedence kuruyordu ve genel nested-merge semantiğini çözmüyordu. Başka bir deyişle patch public/local testi geçti fakat hidden varyantlara genellenemedi.

Bu, sonunda aradığımız temiz ayrımı sağladı:

- Provider çalıştı.
- PDB gerçekten ve zorunlu olarak kullanıldı.
- Controller 24 gerçek karar turunu tamamladı.
- Patch uygulandı.
- Local verifier çalıştı.
- Cleanup ve replay çalıştı.
- Official evaluator çalıştı.
- Başarısızlığın nedeni artık harness belirsizliği değil, aday patch'in semantic olarak yetersiz olmasıydı.

Hidden sonuç görüldükten sonra aynı public task veya prompt üzerinde model lehine yeni ipucu eklenmedi. Bu nedenle V4 yapılmadı; aksi davranış deney kontaminasyonu olurdu.

## 14. Bugünkü bilimsel sonuç

Mevcut kanıtın dürüst yorumu şöyledir:

> Aynı GPT-OSS 20B treatment'ı, debugger kullanımı zorunlu ve verifier kontrollü capability ladder'da 6/100, 12/100 ve 18/100 görevleri çözdü; 32/100 gerçek Cookiecutter görevinde tüm altyapı ve PDB ürün yolunu tamamladı fakat official semantic doğruluğa ulaşamadı.

Bu sonuç şunları **kanıtlar**:

- PDB-first ajan mimarisi yalnızca sentetik test double'ı değildir.
- Model gerçek dış paket içinde PDB kullanabilir.
- Thinking stream ile aksiyon protokolü ayrılabilir.
- Tek görevli live deneyler saatler yerine dakikalar ölçeğinde çalıştırılabilir.
- Model başarısızlığı ile altyapı başarısızlığı artık ayrılabilir.
- Local test geçişi ile official/genel doğruluk arasındaki fark ölçülebilir.
- Mevcut treatment için gözlenen sınır 18 ile 32 seviyesi arasındadır.

Bu sonuç şunları **kanıtlamaz**:

- GPT-OSS 20B'nin genel başarı oranını.
- 18/100 veya 32/100 puanlarının evrensel/nesnel zorluk değerleri olduğunu.
- PDB'nin nedensel olarak PDB'siz ajandan daha iyi olduğunu; bunun için kontrollü ablation gerekir.
- Her daha güçlü modelin 32/100 görevi çözeceğini.
- Fine-tuning'in gereksiz olduğunu.
- Bir model sıralaması veya benchmark liderliği.

Bu bir capability boundary gözlemidir; geniş istatistiksel sonuç değildir.

## 15. Eski ve yeni yaklaşım arasındaki fark

| Eski yaklaşım | Yeni yaklaşım |
|---|---|
| Doğrudan 10 büyük görev | Bir seferde tek frozen görev |
| Model sınırı bilinmeden SWE-rebench | 6 → 12 → 18 → 32 capability ladder |
| PDB kullanımı belirsiz veya opsiyonel | Göreve bağlı exact PDB proof zorunlu |
| Uzun thinking stream şüpheli/ret nedeni | Thinking activity telemetrisi, tool action ayrı |
| Kör sabit süre sınırları | Aktivite-aware ilerleme ve güvenlik sınırları |
| Harness ile model hatası iç içe | Typed failure ve independent authority ayrımı |
| Local geçişe fazla güven | Public/local ve official sonuç ayrı raporlanıyor |
| Belirsizlikte yeniden kampanya | Tek koşu, kök neden, dar onarım, yeni treatment |
| Hidden sonuca göre ayarlama riski | Hidden/gold modele kapalı, post-result tuning yasak |
| Saatler sonra yorumlanamaz sonuç | Dakikalar içinde sınıflandırılabilir tek sonuç |

## 16. Repository'de tutulan kanıtlar

Capability ladder ve sonuçları repository içerisinde uygun araştırma/deney yüzeyinde kalıcı olarak tutulmaktadır. Başlıca yollar:

- `experiments/pdb_capability_ladder/README.md`
- 6/100, 12/100, 18/100 ve 32/100 seviye sözleşmeleri ve sonuçları
- 32/100 `result.json` ve operator script'i
- `docs/evaluation/real-model-eval.md`
- `docs/architecture/ollama-cloud-command-adapter-v1.md`
- `docs/project-tracker.md`
- `README.md`
- `TODO.md`

FirstMate inceleme paketi:

- `_ai-review/pdb-capability-ladder-through-level32-v1-FIRSTMATE.zip`
- SHA-256: `10A8A603E0539D54E2AB2B19AC80ADCB5F463C66B4FC5442DD45C52E8A66BC85`

Son geniş ilgili doğrulama:

- 100 test geçti, yaklaşık 216.71 saniye.
- Daha önceki geniş preflight yüzeyinde 619 test geçti, yaklaşık 243.72 saniye.
- Compile kontrolleri geçti.
- `git diff --check` temizdi.

Son kayıtlı branch/HEAD bağlamı:

- Branch: `codex/single-task-pdb-required-proof-v1`
- HEAD: `1b601f849fa2b2ef5a3bab7e02f4b5144d3c6eda`
- 12/18/32 ladder çalışmaları ve ilgili dokümantasyon bu HEAD sonrasında review adayı olarak bırakıldı; otomatik commit, merge veya push yapılmadı.

## 17. Bundan sonraki doğru deney

En anlamlı sonraki adım, **aynı frozen 32/100 görevini**, public sözleşmeyi veya hidden sonuçlara göre prompt'u değiştirmeden, daha güçlü bir modelle yeni bir treatment identity altında çalıştırmaktır.

Deney kuralları:

1. Görev aynı kalmalı: `audreyr__cookiecutter-967`.
2. Public task/prompt ve tool contract aynı kalmalı.
3. Debugger proof şartları aynı kalmalı.
4. Retry sıfır kalmalı veya treatment'ta açıkça önceden dondurulmalı.
5. Gold patch ve hidden testler modele görünmemeli.
6. Model adı, provider/adapter kimliği ve modele özgü zorunlu transport parametreleri yeni identity'de kaydedilmeli.
7. Başarı yalnızca official evaluator ile kabul edilmeli.
8. Başarısızlıkta aynı görevi hidden sonucu kullanarak modele kolaylaştırmamalıyız.

Eğer daha güçlü model aynı görevi çözerse, “model seçimi bu mimarinin ulaşabildiği görev karmaşıklığını ileri taşıyor” yönünde güçlü fakat yine sınırlı/descriptive kanıt elde edilir. Çözemezse bir sonraki model denenebilir. Ancak tek görev yolu güvenilirken tekrar on görevlik saatler süren kampanyaya dönmek için henüz neden yoktur.

## 18. Konuşmanın en kısa özeti

Başlangıçta proje, çok zor görevleri zayıf bir modelle, onlu kampanyalar ve gözlenebilirliği eksik bir harness üzerinden denediği için saatler harcıyor fakat model ile altyapı başarısızlığını ayıramıyordu.

Onur'un yönlendirmesiyle yaklaşım değişti:

- Tek görev seçildi.
- Debugger kullanımı zorunlu hale getirildi.
- Düşünce çıktısı ile aksiyon ayrıldı.
- Kör zaman sınırı yerine ilerleme gözlendi.
- Görevler 6 → 12 → 18 → 32 şeklinde zorlaştırıldı.
- Her aşama bağımsız verifier, cleanup, immutability ve replay ile kanıtlandı.

Sonuçta mevcut model 6, 12 ve 18 seviyelerini çözdü. 32 seviyesinde sistem baştan sona doğru çalıştı fakat modelin patch'i local örneğe fazla özelleşti ve official testlerde başarısız oldu.

Bugün elimizde artık “neden olmadığını bilmediğimiz sıfır sonuç” yok. Elimizde çalışan bir debugger-assisted repair harness'i, tekrar edilebilir bir capability ladder ve mevcut modelin gözlenen sınırını gösteren temiz bir başarısızlık var. Bir sonraki araştırma sorusu nettir: Aynı 32/100 frozen görev, daha güçlü bir modelle çözülebilecek mi?
