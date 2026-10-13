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
| 9 | ENTRYPOINT: `uvicorn.run(app, host="0.0.0.0", port=8989)` | 4938 |

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

## SIRADAKİ İŞ: Windows paketleme (henüz yapılmadı)

Hedef: çift tıklanan tek bir kurulum dosyası (`kur.exe`). Kimse yanlışlıkla kapatamamalı/silememeli.

1. **`main.py` düzeltmeleri (PyInstaller için şart):**
   - `BASE_DIR = os.path.dirname(os.path.abspath(__file__))` → `--onefile` modunda geçici `_MEIPASS` klasörünü gösterir, `config.json`/`presets.json` her açılışta kaybolur. Donmuş (`sys.frozen`) iken veri klasörü `C:\ProgramData\BenchMonitor` olmalı (yoksa oluştur). `VENDOR_DIR` için exe yanını veya `_MEIPASS`'ı kullan.
   - `--noconsole` ile `sys.stdout`/`sys.stderr` `None` olur → uvicorn loglaması çöker. Log'u `ProgramData\BenchMonitor\logs\` altına dosyaya yönlendir, `uvicorn.run(..., log_config=None)` veya özel log config.
   - Mevcut davranışı (`python main.py`) bozma; testler geçmeye devam etmeli.
2. **PyInstaller:** `pyinstaller --onefile --noconsole --name BenchMonitor main.py`. uvicorn gizli importları için `--collect-submodules uvicorn` (ya da `--hidden-import` ile `uvicorn.logging`, `uvicorn.loops.auto`, `uvicorn.protocols.http.auto`, `uvicorn.protocols.websockets.auto`, `uvicorn.lifespan.on`). Bir `derle.bat` ile tekrar edilebilir yap.
3. **Windows servisi:** pencere yok, açılışta (oturum açmadan) başlar, çökerse yeniden başlar. Seçenekler: NSSM (basit) veya pywin32 servis sarmalayıcısı.
4. **Kurulum (Inno Setup):** program `C:\Program Files\BenchMonitor` (normal kullanıcı silemez), veri `C:\ProgramData\BenchMonitor`, servisi kaydet + başlat, güvenlik duvarında TCP 8989 izni, masaüstüne `http://localhost:8989` kısayolu. Kaldırmada servisi durdur/sil, veriyi koru.
5. **Sonra (isteğe bağlı):** GitHub Actions `windows-latest` ile `kur.exe` üretip Releases'a koymak.

Notlar: PyInstaller exe'leri bazı antivirüslerde yanlış alarm verebilir (gerekirse Nuitka alternatif). Kaynak kod exe'den çıkarılabilir, gizlilik sağlamaz.
