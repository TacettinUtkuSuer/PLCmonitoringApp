# Bench Monitor: AI devir notu

Bu dosya, projeye yeni katılan bir yapay zekâ asistanı (Claude Code vb.) ya da geliştirici için yazıldı. Önce bunu oku.
Kullanıcı Türkçe konuşuyor ve kısa, net cevap istiyor. Formülleri LaTeX ile gösterin.

## Proje ne?

Endüstriyel test tezgâhı için canlı izleme paneli. Tek dosya: `main.py` (~4950 satır).

- Backend: Python 3.11, FastAPI + uvicorn, port **8989** (`BENCHMONITOR_PORT` ortam değişkeniyle değişir, `get_port()`), `python main.py` ile başlar.
- Veri kaynağı: InfluxDB **1.x**, InfluxQL, HTTP `GET {url}/query?db=..&q=..`. Uygulama veri yazmaz, sadece okur.
- Frontend: HTML/CSS/JS `main.py` içinde `INDEX_HTML` string'i olarak gömülü. uPlot API'si ile uyumlu kendi çizim motoru var, CDN gerekmez. `vendor/` içine gerçek `uPlot.iife.min.js` + `uPlot.min.css` konursa onlar tercih edilir.
- Ekranlar arası canlı senkron: WebSocket `/ws` (sensör havuzu değişince tüm ekranlar F5'siz güncellenir).
- Bağımlılıklar: `requirements.txt`.

## main.py bölümleri

| # | Bölüm | Satır (yaklaşık) |
|---|---|---|
| 1 | CONFIGURATION: `config.json` okuma/yazma, `DEFAULT_CONFIG` | 35 |
| 2 | IN-MEMORY STATE: sensör havuzu (`state["pool_sensors"]`) | 86 |
| 3 | DYNAMIC DOWNSAMPLING: aralığa göre `GROUP BY time()` seçimi | 176 |
| 4 | ISO 8601 → epoch | 236 |
| 5 | INFLUXQL CLIENT: `build_influxql`, `influx_query`, `sensor_source`, `group_by_source` | 279 |
| 5b | GLOBAL PRESET deposu (`presets.json`) | 352 |
| 6 | WEBSOCKET HUB | 388 |
| 7 | EMBEDDED FRONTEND (`INDEX_HTML`) | 434 |
| 8 | FASTAPI APP: tüm uç noktalar | 4211 |
| 9 | ENTRYPOINT: dosya loglama (donmuş mod) + `uvicorn.run(..., port=get_port())` | 4960 |

## Ayarlar (`config.json`, `main.py` yanında)

`influx_url` (varsayılan `http://10.10.2.6:8086`), `db` (`lc_100`), `measurement` (`telemetry`), `username`, `password`, `refresh_ms` (min 250).
Arayüzde "InfluxDB Bağlantı Ayarları" penceresinden değiştirilir. Bunlar **varsayılan** değerlerdir: her sensör kendi `influx_url` / `db` / `measurement` değerini taşıyabilir (üçü birbirinden bağımsız; boş olan genel ayardan gelir).
`config.json`, `presets.json` ve `materials.json` **asla repoya girmemeli** (şifre içerebilir) → `.gitignore`'da.

## Sorgular

- Grafik `/api/data`: `SELECT last("P1") AS "P1", ... FROM "<meas>" WHERE time > now() - <range> GROUP BY time(<ivl>) fill(none)`
- Canlı/alarm `/api/last`: `SELECT last("P1") AS "P1" FROM "<meas>" WHERE time > now() - 10m`
- Aynı (url, db, measurement) üçlüsünü paylaşan sensörler tek sorguda; farklı kaynaklar ayrı sorgu, ortak zaman ekseninde birleştirilir. Bir kaynak düşerse diğerleri çalışır, hata `error` alanında döner.
- `MOCK_*` sensörler InfluxDB'ye gitmez, sentetik dalga üretir.

## Önemli uç noktalar

- `GET /api/webhook/sensor?influx_name=P1&name=Pompa&unit=Bar&min=0&max=250[&db=..&measurement=..&influx_url=..&expr=..]` (gövdesiz, tarayıcıdan yazılabilir; parametre sırası önemsiz). Boş değer (`db=`) o geçersiz kılmayı kaldırır.
- `GET /api/webhook/sensors?s=P1|Pompa|Bar|0|250|db|meas|url,...` (pipe formatı, **sıra zorunlu**: `influx_name|name|unit|min|max|db|measurement|influx_url`).
- `/api/sensors/{id}/limits`, `/source`, `/expr`, `/rename` (POST JSON `{to,name,unit,expr,exprs}`: kimliği yerinde değiştirir, sıra/eşik/kaynak korunur; `exprs` = aynı istekte güncellenecek diğer formüller); `/api/sensors/order?ids=A,B,C` (sol panel sırası); `/api/sensors/bulk`, `/clear`; `/api/presets*`; `/api/config`; `/api/health`; `/ws`.

## Sol panel (kart havuzu)

- Sıra sunucudaki `pool_sensors` listesinin sırasıdır (tüm ekranlar aynı sırayı görür). HMI webhook'u ve preset yüklemesi sona ekler; arayüzden (`manual=1`) eklenen **yeni** kart en başa eklenir. Taslak kart da en üstte açılır.
- Kartlar sürükle-bırak ile sıralanır (`initPoolReorder`, `movePoolCard` → `/api/sensors/order`). Grafiğe bırakmak hâlâ seri ekler. Preset havuzu kartları yalnızca o ekranda sıralanır; bölümler arası taşıma yok.
- Preset kaydı ekrandaki sırayı saklar; yüklenince aynı kartlar webhook havuzunda da varsa onlar da o sıraya dizilir.

## Formül kartları (tarayıcıda değerlendirilir)

`=P1*2 + T3`, `sqrt`, `avg`, `if`, `coalesce`, `pi`, `[YAG-SIC]` (özel karakterli ad), **`ma(x, n)`** hareketli ortalama.
Hesap kartının değişken adı `CALC_<AD>` (`calcIdFor`). Kart adı değişince değişken adı da değişir (`changeCardId` → `/rename`) ve onu kullanan diğer formüller `fxRename` ile aynı anda yeniden yazılır; grafik serileri, renk, alarm bağlantısı taşınır. Düz kartlarda (InfluxDB alanı) ad sadece etikettir.
Formülde havuzda kartı **olmayan** bir ad (ör. `=P1 - P9`) hata değildir: InfluxDB alanı sayılır ve doğrudan okunur (`fxExternal`); sadece geçersiz ad (`fxBadNames`) hatadır.
`ma` penceresi **örnek sayısıdır**: grafikte 1 örnek = 1 GROUP BY kovası, alarmda 1 örnek = 1 yoklama (`refresh_ms`). Pencere dolana kadar boş; penceredeki boşluklar atlanır; iç içe kullanılabilir.
Low-pass filtre fonksiyonu yok; yumuşatma için `ma()` kullanılıyor.

## Malzeme tabloları (`materials.json`, veri klasöründe; tüm PC'ler ortak)

- Üst çubuk "Malzemeler" penceresi: CSV dosyası seç ya da yapıştır → sunucu doğrular (`parse_material_csv`) → kaydet. Değişiklik WS `{"type":"materials"}` ile tüm ekranlara gider.
- CSV başlığı ve **zorunlu birimler**: `temp [degC], dens [kg/m3], cp [J/kg·K], dvis [Pa·s], cond [W/m·K]` (sütun sırası serbest; `;` ayraç + ondalık virgül kabul; birim karşılaştırması `_unit_key` ile `·`/`.`/`*`/boşluk/Excel bozulması `Â·` toleranslı). Birim dönüşümü yapılmaz.
- Formülde `AD_dens(x)`, `AD_cp(x)`, `AD_dvis(x)`, `AD_cond(x)` (büyük/küçük harf önemsiz; `x` sayı, sensör ya da ifade). `temp` sütununda doğrusal ara değer (`matInterp`); tablo aralığı dışında sonuç **boş**. Malzeme yoksa derleme hatası "Malzeme bulunamadı". Malzeme listesi değişince `FXCACHE` temizlenir.
- Ad: harfle başlar, `A-Z a-z 0-9 _`, en fazla 32. Aynı ad (harf farkı dahil) üzerine yazar.
- Uçlar: `GET /api/materials`, `POST /api/materials/{ad}` (JSON `{csv, dry}`; `dry=true` sadece doğrular), `DELETE /api/materials/{ad}`.

## Testler (`tests/`)

Ortamda FastAPI olmadığında çalışsın diye shim'ler kullanıldı:
- `serve_test.py`: fastapi/pydantic/httpx/uvicorn için asgari shim kurar, **gerçek** `main.py`'yi stdlib http.server ile 8989'da servis eder. `python tests/serve_test.py [port]`
- `stub.py`: arayüzü test etmek için sahte (eski) sunucu; `serve_test.py` tercih edin.
- Python birim testleri: `python tests/test_backend.py` (84 test), `python tests/test_source.py` (47 test). Son durum: hepsi geçiyor.
- Tarayıcı testleri (Node + Playwright, sunucu 8989'da açık olmalı): `test_ui.js`, `test_fx.js` (126), `test_ma.js` (34), `test_alarm_regress.js` (25), `test_pool_order.js` (20, sıralama / yeni kart üstte / havuz dışı sensör / preset sırası), `test_materials.js` (31; WebSocket kontrolü olduğu için **gerçek** `python main.py` ile çalıştırın, shim WS desteklemez), `test.js`. Bilinen: `test_ui.js` alarm zamanlaması bölümünde ve `test.js` 7. bölümde önceden beri kalan (zamanlamaya bağlı) hatalar var. `shot*.js` ekran görüntüsü alır → `tests/out/`.
- `demo_queries.py`: P1/T3/N5 için üretilen InfluxQL sorgularını yazdırır (InfluxDB'ye bağlanmaz).

Gerçek FastAPI kuruluysa doğrudan `python main.py` ile de test edilebilir.

## Neden Docker değil?

Hedef PC'de sanallaştırma/WSL kapalı olmak zorunda → Docker çalışmıyor. `Dockerfile` referans için duruyor.
GitHub Pages da olmaz: uygulama Python sunucusu, InfluxDB yerel ağda (HTTP, CORS yok).

## Windows paketleme (`derle.bat` → `Output\kur.exe`)

Hedef: çift tıklanan tek kurulum dosyası (`kur.exe`). Kimse yanlışlıkla kapatamamalı/silememeli.

- **`main.py` (donmuş mod):** `sys.frozen` iken veri klasörü `C:\ProgramData\BenchMonitor` (`BENCHMONITOR_DATA` ortam değişkeniyle değiştirilebilir); `config.json`, `presets.json`, `logs\benchmonitor.log` (5 MB × 5 dönen) orada. `VENDOR_DIR` sırası: veri klasörü\vendor → exe yanı\vendor → `_MEIPASS`\vendor. Konsol yoksa (`FROZEN` ya da `stdout is None`) `print` + uvicorn logları dosyaya, `uvicorn.run(log_config=None, access_log=False)`. `python main.py` davranışı değişmedi.
- **PyInstaller:** `--onedir --noconsole --collect-submodules uvicorn` (`--onefile` değil: servis her durdurulduğunda/öldürüldüğünde `%TEMP%\_MEI*` artığı bırakır, açılış yavaş, antivirüs daha çok takılır. Tek dosya ihtiyacını zaten `kur.exe` karşılıyor).
- **Servis:** NSSM (`packaging\tools\nssm.exe`, `derle.bat` ilk seferde indirir, repoya girmez). Ad `BenchMonitor`, LocalSystem, otomatik başlar, çökerse 5 sn sonra yeniden başlar (+ `sc failure`).
- **Kurulum:** `packaging/BenchMonitor.iss` (Inno Setup 6). Sihirbaz portu bir kez sorar (varsayılan 8989, güncellemede önceki değer; sessiz kurulumda `kur.exe /SILENT /PORT=9000`) → servise `BENCHMONITOR_PORT` olarak verilir. Program `C:\Program Files\BenchMonitor`, veri `C:\ProgramData\BenchMonitor` (Kullanıcılar sadece okur), güvenlik duvarı kuralı "Bench Monitor" (seçilen TCP port), masaüstü + Başlat menüsü `http://localhost:<port>` kısayolu. Portu sonradan değiştirmek: `kur.exe`'yi yeniden çalıştır. Güncellemede önce servis durur. Kaldırmada servis + güvenlik duvarı kuralı silinir, veri korunur.
- **CI:** `.github/workflows/windows-kurulum.yml`: Linux'ta Python testleri → `windows-latest`'te `derle.bat` + exe duman testi → `kur.exe` artifact. `v*` etiketi basılınca Releases'a da konur (`git tag v1.0.0 && git push origin v1.0.0`).

Notlar: PyInstaller exe'leri bazı antivirüslerde yanlış alarm verebilir (gerekirse Nuitka alternatif). Kaynak kod exe'den çıkarılabilir, gizlilik sağlamaz.
