# Bench Monitor: AI devir notu

Bu dosya, projeye yeni katılan bir yapay zekâ asistanı (Claude Code vb.) ya da geliştirici için yazıldı. Önce bunu oku.
Kullanıcı Türkçe konuşuyor ve kısa, net cevap istiyor. Formülleri LaTeX ile gösterin.

## Proje ne?

Endüstriyel test tezgâhı için canlı izleme paneli. Tek dosya: `main.py` (~4950 satır).

- Backend: Python 3.11, FastAPI + uvicorn, port **8989**, `python main.py` ile başlar.
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
| 9 | ENTRYPOINT: dosya loglama (donmuş mod) + `uvicorn.run(..., port=8989)` | 4960 |

## Ayarlar (`config.json`, `main.py` yanında)

`influx_url` (varsayılan `http://10.10.2.6:8086`), `db` (`lc_100`), `measurement` (`telemetry`), `username`, `password`, `refresh_ms` (min 250).
Arayüzde "InfluxDB Bağlantı Ayarları" penceresinden değiştirilir. Bunlar **varsayılan** değerlerdir: her sensör kendi `influx_url` / `db` / `measurement` değerini taşıyabilir (üçü birbirinden bağımsız; boş olan genel ayardan gelir).
`config.json` ve `presets.json` **asla repoya girmemeli** (şifre içerebilir) → `.gitignore`'da.

## Sorgular

- Grafik `/api/data`: `SELECT last("P1") AS "P1", ... FROM "<meas>" WHERE time > now() - <range> GROUP BY time(<ivl>) fill(none)`
- Canlı/alarm `/api/last`: `SELECT last("P1") AS "P1" FROM "<meas>" WHERE time > now() - 10m`
- Aynı (url, db, measurement) üçlüsünü paylaşan sensörler tek sorguda; farklı kaynaklar ayrı sorgu, ortak zaman ekseninde birleştirilir. Bir kaynak düşerse diğerleri çalışır, hata `error` alanında döner.
- `MOCK_*` sensörler InfluxDB'ye gitmez, sentetik dalga üretir.

## Önemli uç noktalar

- `GET /api/webhook/sensor?influx_name=P1&name=Pompa&unit=Bar&min=0&max=250[&db=..&measurement=..&influx_url=..&expr=..]` (gövdesiz, tarayıcıdan yazılabilir; parametre sırası önemsiz). Boş değer (`db=`) o geçersiz kılmayı kaldırır.
- `GET /api/webhook/sensors?s=P1|Pompa|Bar|0|250|db|meas|url,...` (pipe formatı, **sıra zorunlu**: `influx_name|name|unit|min|max|db|measurement|influx_url`).
- `/api/sensors/{id}/limits`, `/source`, `/expr`; `/api/sensors/bulk`, `/clear`; `/api/presets*`; `/api/config`; `/api/health`; `/ws`.

## Formül kartları (tarayıcıda değerlendirilir)

`=P1*2 + T3`, `sqrt`, `avg`, `if`, `coalesce`, `pi`, `[YAG-SIC]` (özel karakterli ad), **`ma(x, n)`** hareketli ortalama.
`ma` penceresi **örnek sayısıdır**: grafikte 1 örnek = 1 GROUP BY kovası, alarmda 1 örnek = 1 yoklama (`refresh_ms`). Pencere dolana kadar boş; penceredeki boşluklar atlanır; iç içe kullanılabilir.
Low-pass filtre fonksiyonu yok; yumuşatma için `ma()` kullanılıyor.

## Testler (`tests/`)

Ortamda FastAPI olmadığında çalışsın diye shim'ler kullanıldı:
- `serve_test.py`: fastapi/pydantic/httpx/uvicorn için asgari shim kurar, **gerçek** `main.py`'yi stdlib http.server ile 8989'da servis eder. `python tests/serve_test.py [port]`
- `stub.py`: arayüzü test etmek için sahte (eski) sunucu; `serve_test.py` tercih edin.
- Python birim testleri: `python tests/test_backend.py` (54 test), `python tests/test_source.py` (47 test). Son durum: hepsi geçiyor.
- Tarayıcı testleri (Node + Playwright, sunucu 8989'da açık olmalı): `test_ui.js`, `test_fx.js`, `test_ma.js`, `test_alarm_regress.js`, `test.js`. `shot*.js` ekran görüntüsü alır → `tests/out/`.
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
- **Kurulum:** `packaging/BenchMonitor.iss` (Inno Setup 6). Program `C:\Program Files\BenchMonitor`, veri `C:\ProgramData\BenchMonitor` (Kullanıcılar sadece okur), güvenlik duvarı TCP 8989, masaüstü + Başlat menüsü `http://localhost:8989` kısayolu. Güncellemede önce servis durur. Kaldırmada servis + güvenlik duvarı kuralı silinir, veri korunur.
- **CI:** `.github/workflows/windows-kurulum.yml`: Linux'ta Python testleri → `windows-latest`'te `derle.bat` + exe duman testi → `kur.exe` artifact. `v*` etiketi basılınca Releases'a da konur (`git tag v1.0.0 && git push origin v1.0.0`).

Notlar: PyInstaller exe'leri bazı antivirüslerde yanlış alarm verebilir (gerekirse Nuitka alternatif). Kaynak kod exe'den çıkarılabilir, gizlilik sağlamaz.
