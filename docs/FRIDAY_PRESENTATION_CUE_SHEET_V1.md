# Friday Presentation Cue Sheet v1

**Version:** 1.2 — 2026-08-05
**Source baseline:** `456f0e9a6576aab912f5af5980d756ff4e1e9dc3` is the accepted presentation plan/deck/cue delivery commit and the source baseline for this task's final-delivery candidate; campaign infrastructure accepted through `0abb588`; V4 identity correction accepted through `fc7c85b`. Version 1.1 was prepared from `fc7c85b`; version 1.2 updates the baseline identity. On presentation day, run from clean `main` matching `origin/main`, containing the delivery bundle files and descending from `456f0e9`.
**Usage:** presenter-side quick reference only. Full content, evidence paths, and overclaim boundaries live in `docs/FRIDAY_PRESENTATION_DECK_V1.md`; runbook and Q&A in `docs/FRIDAY_PRESENTATION_PLAN_V1.md`.

---

## 1. Ana anlatım — Q&A hariç toplam 24,5 dk (≤ 25 dk)

| Slayt | Konu | Süre |
|---|---|---|
| 1 | Kapak | 0,5 dk |
| 2 | Kapsam: 27 madde, 7/10/3/7/0 | 1,5 dk |
| 3 | Dürüst durum özeti (3 cümle) | 1,0 dk |
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
| Konuşma alt toplamı (1–15: 20,0; 16–17: 1,5) | — | 21,5 dk |
| Deterministik demo + geçişler (Slayt 16 demo bloğu) | — | 3,0 dk |
| **Toplam (Q&A hariç)** | — | **24,5 dk** |
| Q&A | — | Ayrı — toplama dahil değil |

Prova hedefi: konuşma bölümü 21,5 dk'yı aşmamalı (matematiksel toplam; prova ile azaltma vaat edilmez). Zaman baskısında önce Slayt 5, 7, 9, 12 (OPSİYONEL) atlanır; demo bloğu asla kısaltılmaz.

## 2. Kısa anlatım — Q&A hariç toplam 11,5 dk (10–12 dk aralığı)

Sıra: 1 → 2 → 3 → 4 → 8 → 10 → 11 → 13 → 14 → 16 → 17

| Slayt | Süre | Not |
|---|---|---|
| 1 | 0,5 dk | Kapsam tek cümle |
| 2 | 1,0 dk | Sadece dağılım; per-item detay yok |
| 3 | 0,5 dk | Üç cümle; dataset tek satır |
| 4 | 1,0 dk | "Model önerir, verifier karar verir" |
| 8 | 1,0 dk | Sayı tablosu + scripted sınırı; "gerçek PDB mekanizması var, canlı PDB yok" |
| 10 | 1,5 dk | Case 1/Case 2 + sıfırlar (slayt 9 tek satır burada) |
| 11 | 1,0 dk | Hunk + bütçe + preregistered abort |
| 13 | 1,0 dk | Reçete cümlesi; detay yok |
| 14 | 1,0 dk | Sınır listesi hızlı |
| 16 | 0,5 dk | Demo geçişi (konuşma); demo ayrı satırda |
| 17 | 0,5 dk | Teşekkür + üç ölçülebilir adım tek cümle |
| Konuşma alt toplamı | — | 9,5 dk |
| Tek-task demo + geçişler (Slayt 16 demo bloğu) | — | 2,0 dk |
| **Toplam (Q&A hariç)** | — | **11,5 dk** |
| Q&A | — | Ayrı — toplama dahil değil |

Atlananlar: 5 (state machine demo'da görülür), 6 (mesaj Slayt 8/demo'da), 7 (dataset), 9 (QuixBugs altyapı), 12 (dersler Slayt 11'de), 15 (yol haritası tek cümle Slayt 17'de).

## 3. Demo geçiş cümlesi (birebir)

> "Şimdi size platformu canlı göstereyim: tek bir curated hata üzerinde, model yerine offline scripted stand-in ile controller, tool'lar, PDB-capable yol, patch ve bağımsız doğrulayıcı uçtan uca çalışacak."

## 4. Deterministic demo komutu (her koşuda taze çıktı dizini üretir)

```powershell
$demoOut = "demo-out-friday-" + (Get-Date -Format "yyyyMMdd-HHmmss")
python -m agentic_debugger.demo --output-dir $demoOut --task-id curated-off-by-one-002
```

- `--output-dir` her çağrıda zorunludur; `--strict` yalnızca önceden yapılan tam 10-case provası içindir, canlıda gerekmez. Öncesinde bir kez `python -m pip install -e .[test]` doğrulayın.
- Zaman damgalı dizin adı tazeliği garanti eder; mevcut bir çıktı dizini asla silinmez/üzerine yazılmaz.
- Koşu sonrası açılacak dizin: `$demoOut` (örn. `demo-out-friday-20260805-142530`).

## 5. Açılacaklar ve işaret edilecekler (sırayla)

1. Terminal çıktısı: `cases: 2`, exit code 0.
2. `$demoOut\results.json` → `aggregates`: 2 case; verifier `RESOLVED` 2/2; F2P 1/1 + P2P 2/2 per case; localization `CORRECT_TARGET_SYMBOL`.
3. `$demoOut\technical-evaluation-summary.md` → "Tested state" (HEAD, offline policy) ve §3 tablosu (Controller Done, Verifier COMPLETED/RESOLVED, Full suite PASS).
4. `$demoOut\trajectories\curated-off-by-one-002__pdb-on-uncertainty.events.jsonl` → state geçişleri (reproduce → understand → gate → patch → validate) ve typed direktifler (file-read / search / test-run / patch-apply).
5. Aynı case'in `.semantic.json` → stabil projeksiyon (replay-valid).
6. `$demoOut\technical-evaluation-summary.md` §4 "Offline enforcement": 0 provider, 0 network; "Workspace" temizlik alanı: `CLEANED`; fixture hash before/after eşit.

Demo sırasında söylenecek sınır: "Bu, scripted stand-in ile platform gösterimidir; model kalitesi kanıtı değildir; PDB yolu bir driver script'e pre-known breakpoint ile bağlıdır."

## 6. Demo'dan dönüş cümlesi (birebir)

> "Gördüğünüz deterministik hat, canlı model bulgularından ayrıdır: V4 bulguları kaydedilmiş deneylerdir, canlı gösterim değil. Şimdi yol haritasıyla bitirelim."

## 7. Reçete edilmiş QLoRA durum cümlesi (birebir)

> "Frozen corpus'un owner-delegated bağımsız FirstMate AI audit'i dışarıda tamamlandı (75 satır: 39 ACCEPT / 36 REJECT; bu bir AI audit'tir, insan review değildir) ve QLoRA implementasyonu FirstMate implementation review'ini geçti. Final training 2026-08-05'te FirstMate tarafından dışarıdan yetkilendirildi; kabul edilmiş bir final-training artifact'i henüz yok ve sonuçları FirstMate artifact review'i bekliyor. Kontrat doğrulaması, corpus acceptance kararı ve frozen base-versus-tuned karşılaştırması hâlâ beklemede; held-out generation yetkisiz. Mevcut kanıt dondurulmuş metodoloji, sızıntı kontrollü gerçek corpus ve teknik olarak başarılı bir one-step CUDA QLoRA smoke güncellemesi ile adapter save/reload'udur — bu, şu anda çalışan final training'in sonucu değildir ve kabul edilmiş bir final checkpoint'i ima etmez."

## 8. "Söylemeyin" listesi (en fazla 10)

1. "Model canlı bir hatayı onardı / X görevini çözdü" — verifier-confirmed live repair yok.
2. "PDB, performansı artırdı" — geçerli static-versus-PDB karşılaştırması yok.
3. "QuixBugs 8/8 model başarısı" — adaylar literal upstream diff, model yok.
4. "Fine-tuning modeli iyileştirdi" — kabul edilmiş sonuç yok, karşılaştırma yok.
5. Herhangi bir uydurulmuş/öngörülmüş eğitim metriği (loss, RESOLVED, delta).
6. "BugsInPy üzerinde değerlendirme yaptık" — license-blocked, preflight-only.
7. "RAG/DPO implemente edildi" — NO-GO-FOR-NOW kayıtlı kararlar.
8. "Proje bitmiş genel amaçlı onarım sistemi" — altyapı + metodoloji katkısıdır.
9. "Önceki denemeler model-performans sonucuydu" — protokol/bütçe/infra terminalleri.
10. "Audit insan review'dur" veya "freeze flag'i bugünkü yetkidir" — AI audit; tarihsel kayıt.

## 9. Hata/koşul tablosu

| Durum | Fallback |
|---|---|
| Demo komutu fail (exit 1) | `pip install -e .[test]` tekrar; taze `$demoOut` ile tek-task formu tekrar; olmazsa kayıtlı çıktıları göster — `docs/DEMO_GUIDE_V1.md` §2'nin kabul edilmiş sonuçları ve yerel korunmuş rehearsal çıktısı (yalnızca **yerel operasyonel fallback**; durable iddia kaynağı değildir). Verifier/bütçe kapısı gevşetilmez. |
| `--strict` fail | Regresyon sinyalidir (DEMO_GUIDE §6); canlıda zaten kullanılmaz; kayıtlı sonuçlara dön. |
| İnternet/provider yok | Demo bağımsızdır; sorun yok. V4 kayıtları yerel tracked kaynaklardır. |
| QLoRA sonucu sorulur | Reçete cümlesi (§7); tahmin yok. |
| Kayıtlı V4 dosyası yoksa | Tracked kaynaklardan anlat: `research/quixbugs/PAIRED_PILOT_V4.json`, status map §5, `PROJECT_TRACKER.md` 2026-08-05. |
| Süre kesilirse | Kısa anlatım sırasına geç (§2); demo tek-task. |

## 10. Sunum öncesi kontrol listesi

- [ ] Git kontrolü: sunum günü temiz `main` üzerinden, `origin/main` ile aynı, delivery bundle dosyalarını içeren ve kabul edilmiş kaynak baseline `456f0e9`'dan türeyen bir ağaçtan çalışılır; tracked working tree temiz. (Ignored `.opencode/` veya `_ai-review/` dosyalarının bulunmaması gerekmez.)
- [ ] `python -m pip install -e .[test]` başarılı; `python --version` 3.11+.
- [ ] Tek-task provası bir kez çalıştırıldı: exit 0, 2 case, 0 provider/network; çıktılar yerel operasyonel fallback olarak saklandı (durable iddia kaynağı değil).
- [ ] QLoRA alanları boş; reçete cümlesi (§7) hazır; smoke cümlesi "one-step CUDA QLoRA smoke update + adapter save/reload" olarak doğru.
- [ ] Audit cümlesi "AI audit, insan review değil" şeklinde doğru.
- [ ] "Söylemeyin" listesi (§8) gözden geçirildi.
- [ ] Slayt 16 geçiş/dönüş cümleleri prova edildi; ana anlatım 24,5 dk (Q&A hariç) ve kısa anlatım 11,5 dk (Q&A hariç) prova edildi.
- [ ] Tüm slayt kaynak yolları erişilebilir; sunum ve demo internetsiz çalışır.
