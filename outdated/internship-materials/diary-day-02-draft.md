# Staj Defteri — Gün 02

## Yapılan Çalışma

Bugün agentic debugging projesi için üç farklı yapay zeka sistemi tarafından oluşturulan derin literatür raporları toplandı ve karşılaştırmalı şekilde incelendi. Gemini, ChatGPT ve Claude tarafından hazırlanan raporların ortak noktaları, farklı kaynak keşifleri, çelişkili iddiaları ve proje açısından önerdikleri mimari kararlar analiz edildi.

Üç raporda da projenin temel ayrımı netleşti: mevcut repository-level yazılım ajanları genellikle kod dosyaları, hata metinleri, test çıktıları ve shell komutları üzerinden çalışırken, hedeflenen sistemin canlı debugger oturumu üzerinden runtime state, stack frame ve değişken değerlerini inceleyebilmesi gerekmektedir. Bu nedenle proje, sadece statik kod analizi veya test-feedback tabanlı patch üretimi olarak ele alınmamalıdır.

## Öğrenilen Kavramlar

Bugün özellikle fault localization ile root-cause analysis arasındaki fark netleştirildi. Fault localization çoğu zaman hatalı olabilecek satır, metod veya dosyaları sıralar; ancak bu, hatanın nedensel zincirini açıkladığı anlamına gelmez. Root-cause analysis ise hataya sebep olan veri akışı, kontrol akışı, değişken değeri veya çalışma zamanı kararının açıklanmasını gerektirir.

Ayrıca automated program repair literatüründeki plausible patch ve correct patch ayrımı tekrar vurgulandı. Bir patch testlerden geçse bile semantik olarak doğru olmayabilir. Bu nedenle gelecekteki sistemde bağımsız doğrulayıcı ve regresyon testleri önemli olacaktır.

## Projeye Katkısı

Karşılaştırmalı analiz sonucunda ilk prototip için Python ve PDB odaklı bir yaklaşımın en mantıklı başlangıç olduğu belirlendi. ChatDBG doğrudan önceki çalışma olarak, debug-gym ise PDB tabanlı etkileşimli debugging ortamı olarak öncelikli okunacak kaynaklar arasına alındı. Agentless, SWE-agent ve AutoCodeRover ise debugger kullanmayan güçlü baseline sistemler olarak konumlandırıldı.

## Sonraki Adım

Bir sonraki adımda öncelikli PDF indirme listesi tamamlanacak ve ilk olarak ChatDBG makalesi manuel okunarak ayrıntılı not çıkarılacaktır. Ardından debug-gym, Agentless ve SWE-bench makaleleri okunarak PDB tabanlı minimum uygulanabilir prototip için araştırma gerekçesi hazırlanacaktır.
