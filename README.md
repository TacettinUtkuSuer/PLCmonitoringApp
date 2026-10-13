# Bench Monitor

Endustriyel test tezgahi icin canli izleme paneli (FastAPI + InfluxDB 1.x + gomulu arayuz).

## Calistirma

```
pip install -r requirements.txt
python main.py
```

Tarayicida: http://localhost:8989 (agdaki diger PC'lerden `http://<pc-ip>:8989`).
InfluxDB ayarlari arayuzdeki ayarlar penceresinden yapilir ve `config.json` dosyasina kaydedilir.

## Testler

```
python tests/test_backend.py
python tests/test_source.py
```

Tarayici testleri icin Node + Playwright ve 8989'da calisan sunucu gerekir (`python tests/serve_test.py`).

## Gelistirici / AI notlari

Mimari, uc noktalar ve siradaki isler (Windows paketleme) icin `CLAUDE.md`.
