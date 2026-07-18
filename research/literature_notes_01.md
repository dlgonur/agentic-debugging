# Literature Notes 01 — Debugging, Automated Debugging, Fault Localization, Program Repair

## Goal

Bu dosyanın amacı, agentic debugging projesinin temel akademik kavramlarını netleştirmektir.

## Core Concepts

### Debugging

Programın beklenen davranışı göstermemesine sebep olan hatayı bulma, anlama ve düzeltme sürecidir.

### Automated Debugging

Debugging sürecindeki bazı adımların otomatikleştirilmesidir. Örneğin hata konumunu bulma, test sonuçlarını analiz etme, olası root cause çıkarma veya patch önerme.

### Fault Localization

Hatanın programın hangi dosya, fonksiyon, satır veya değişken akışından kaynaklandığını tahmin etmeye çalışan alandır.

### Program Repair

Tespit edilen hataya otomatik veya yarı otomatik şekilde patch üretmeye çalışan alandır.

## Relation to Our Project

Bizim proje bu alanların üstüne LLM ve agent katmanı ekliyor.

Temel hedef:

Debugger runtime bilgisi + kod + test/hata mesajı + agent araçları
→ root cause
→ patch
→ test doğrulaması

## Initial Project Interpretation

Geleneksel debugging insan merkezlidir.
Automated debugging bazı analiz adımlarını otomatikleştirir.
LLM-based debugging modele hata açıklama ve patch üretme yeteneği verir.
Agentic debugging ise modele araç kullandırır: dosya okuma, kod arama, test çalıştırma, debugger komutu üretme ve patch uygulama.

## Open Questions

- Fault localization için hangi metrikler kullanılmalı?
- Program repair başarısı nasıl ölçülmeli?
- LLM debugging sistemleri statik kod analizinden nasıl ayrılır?
- Runtime debugger context modele nasıl verilmeli?
- Fine-tuning mi, RAG mi, agentic tool use mu daha kritik?
