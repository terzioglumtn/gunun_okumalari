# Günün Okumaları

Her sabah belirlediğim kaynakları tarayan, en değerli 10-15 maddeyi seçip **Türkçe** özetleyen
ve statik bir sayfa olarak yayınlayan kişisel okuma sistemi. Her maddede orijinal başlık ve
kaynak bağlantısı bulunur.

Yayın adresi: https://terzioglumtn.github.io/gunun_okumalari/

## Nasıl çalışır

```
Actions (07:00 İstanbul) → akışları topla → tekilleştir → konu başına eleme (LLM, ucuz model)
→ seçilenlerin metnini çek → Türkçe özet (LLM) → data/YYYY-MM-DD.json → statik site → Pages
```

- **Veri asıl üründür:** `data/*.json`. HTML bundan üretilir; şablonu değiştirip tüm arşivi yeniden çizebilirsiniz.
- **Sayfalar önceden çizilir:** JavaScript gerekmez; RSS (`feed.xml`) ve paylaşım önizlemeleri çalışır.
- **Kaçırılan gün kaybolmaz:** Toplama 48 saat geriye bakar, son 7 günde yayınlananlar tekrar seçilmez.
- **Sessiz bozulma yok:** Akışların yarısından fazlası okunamazsa iş başarısız olur ve GitHub uyarı e-postası gönderir. Sayfa altında "Son üretim" damgası vardır.

## Kurulum (bir kez)

1. **Pages:** Settings → Pages → Source: **GitHub Actions**.
2. **LLM kimlik bilgisi** (birini seçin):
   - Abonelik: kendi bilgisayarınızda `claude setup-token` çalıştırın, çıkan token'ı
     Settings → Secrets and variables → Actions → **Secret** olarak `CLAUDE_CODE_OAUTH_TOKEN` adıyla ekleyin.
   - API: Console'dan anahtar alın, **Secret** olarak `ANTHROPIC_API_KEY` ekleyin ve
     **Variable** olarak `LLM_BACKEND` = `anthropic_api` tanımlayın.
3. **İlk çalıştırma:** Actions → "Günlük sayı" → Run workflow. İlk sınama için önce
   `LLM_BACKEND` = `mock` verebilirsiniz (LLM çağırmaz, hattı sınar).

Anahtarları ve token'ları depoya yazmayın; yalnızca Actions secret'ında durmalıdır.

## Yerelde deneme

```bash
pip install -r requirements.txt
pytest -q                          # testler (ağ gerekmez)
LLM_BACKEND=mock python -m src.run # sahte LLM ile örnek sayı, site/ içine yazılır
```

## Düzenlenecek dosyalar

| Dosya | Ne için |
|---|---|
| `config/config.yaml` | Konular, akışlar, limitler, modeller |
| `config/interests.md` | LLM'in neyi seçeceğini belirleyen ilgi profili (genel tutun, halka açıktır) |
| `templates/` | Sayfa görünümü |

Yeni konu eklemek: `topics` altına blok açın, `feeds` altına akışlarını `topic:` ile yazın.

## Bilinen belirsizlikler (ilk çalıştırmada doğrulayın)

- `claude` CLI bayrakları (`-p`, `--model`, `--output-format text`) bu depoda canlı sınanmadı.
- Stack Overflow etiket akışı adresleri (`/feeds/tag/<etiket>`) canlı doğrulanmadı.
- Abonelik token'ının zamanlanmış, insansız kullanımının koşullarını kendiniz kontrol edin;
  uygun değilse `LLM_BACKEND=anthropic_api` kullanın.
- Reddit akışları bulut sunucu adreslerini sık engeller; bu yüzden ilk dalgada yoktur.
