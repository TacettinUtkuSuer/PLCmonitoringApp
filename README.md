# Bench Monitor

Endustriyel test tezgahi icin canli izleme paneli (FastAPI + InfluxDB 1.x + gomulu arayuz).

## Calistirma

```
pip install -r requirements.txt
python main.py
```

Tarayicida: http://localhost:8989 (agdaki diger PC'lerden `http://<pc-ip>:8989`).
InfluxDB ayarlari arayuzdeki ayarlar penceresinden yapilir ve `config.json` dosyasina kaydedilir.

## Windows kurulumu

`derle.bat` -> `Output\kur.exe` (Python 3.11 + Inno Setup 6 gerekir) ya da GitHub Actions artifact'i.
Kurulum servisi (`BenchMonitor`) kaydeder ve baslatir; veri `C:\ProgramData\BenchMonitor`.
Port kurulumda sorulur (varsayilan 8989); degistirmek icin `kur.exe` yeniden calistirilir.
Gelistirmede: `BENCHMONITOR_PORT=9000 python main.py`.

## Testler

```
python tests/test_backend.py
python tests/test_source.py
```

Tarayici testleri icin Node + Playwright ve 8989'da calisan sunucu gerekir (`python tests/serve_test.py`).

## Gelistirici / AI notlari

Mimari, uc noktalar ve siradaki isler (Windows paketleme) icin `CLAUDE.md`.
