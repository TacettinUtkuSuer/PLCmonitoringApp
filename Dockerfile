# =========================================================================================
#  INDUSTRIAL TEST BENCH MONITOR  --  FastAPI + InfluxQL + uPlot   (PORT: 8989)
# =========================================================================================
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Istanbul

WORKDIR /app

# --- Bagimliliklar -----------------------------------------------------------------------
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir "fastapi==0.115.6" "uvicorn[standard]==0.34.0" "httpx==0.28.1" "pydantic==2.10.4"

# --- Cizim motoru -------------------------------------------------------------------------
# main.py uPlot API'si ile uyumlu cizim motorunu KENDI ICINDE tasir; internet/CDN GEREKMEZ.
# Istege bagli: gercek uPlot'u kullanmak isterseniz uPlot.iife.min.js ve uPlot.min.css
# dosyalarini bu Dockerfile'in yanindaki vendor/ klasorune koyun, otomatik tercih edilir.
# (Gercek uPlot kullanmak icin:  docker run ... -v %cd%\vendor:/app/vendor ...)
RUN mkdir -p /app/vendor

# --- Uygulama ----------------------------------------------------------------------------
COPY main.py /app/main.py

# --- SENSOR EKLEME (govdesiz, tek URL -- tarayiciya yazilabilir) ---------------------------
#   Tek sensor :  /api/webhook/sensor?influx_name=P1&name=Pompa&unit=Bar&min=0&max=250
#   Coklu      :  /api/webhook/sensors?s=P1|Pompa|Bar|0|250,T3|Sicaklik|C|-10|120
#   Esik       :  /api/sensors/P1/limits?min=10&max=200
# Content-Type header'i ve JSON govde GEREKMEZ; eski POST /api/webhook/sensor da calisir.

# --- SENSOR BAZLI KAYNAK: db / measurement / influx_url ------------------------------------
# Bu uc alan webhook'ta GONDERILEBILIR. Gonderilen alan o sensor icin ayarlardaki
# degerin YERINE gecer; gonderilmeyen alan ayarlardan okunmaya devam eder.
# UCU BIRBIRINDEN BAGIMSIZDIR -- biri digerini beklemez:
#   /api/webhook/sensor?influx_name=P1&db=hat2                 -> sadece db degisti
#   /api/webhook/sensor?influx_name=P1&measurement=press       -> sadece measurement
#   /api/webhook/sensor?influx_name=P1&influx_url=http://10.0.0.9:8086
#   /api/webhook/sensor?influx_name=P1&db=hat2&measurement=press
# Sonradan degistirmek / kaldirmak (bos deger gecersiz kilmayi KALDIRIR):
#   /api/sensors/P1/source?db=hat2        |  /api/sensors/P1/source?db=
# Coklu URL'de sira:  influx_name|name|unit|min|max|db|measurement|influx_url
#   /api/webhook/sensors?s=P1|Pompa|Bar|0|250|hat2,T3|Sicaklik|C||||press
# (Konum saymak hataya acik; tercihen tek tek adli parametreleri kullanin.)
# Ayni (url, db, measurement) uclusunu paylasan sensorler TEK sorguda toplanir;
# farkli kaynaklar ayri sorgulara boluner ve ortak zaman ekseninde birlestirilir.
# Bir kaynak duserse digerleri calismaya devam eder.

# --- WEBHOOKSUZ KULLANIM + HESAP (FORMUL) KARTLARI -----------------------------------------
# Sol paneldeki "+ New" dugmesi havuzda yazilabilir bir kart acar:
#   * Duz ad yazilirsa   -> o InfluxDB alani izlenir (HMI webhook'u GEREKMEZ).
#   * "=" ile baslanirsa -> formul olur; havuzdaki sensorlerin influx adlariyla
#                           hesap yapar ve sonuc grafige surukleyebilen bir seri olur.
#        ornek:  =P1*2 + T3        =sqrt(N5) + avg(A,B)        =[YAG-SIC]*1.8 + 32
#   HAREKETLI ORTALAMA:  =ma(P1, 5)   =ma(P1*2 + T3, 10)   =ma(ma(P1,5), 3)
#        ma()'nin penceresi ORNEK sayisidir, sure degil:
#          * grafikte  1 ornek = 1 GROUP BY kovasi (5 Dk araliginda 1s)
#          * alarmda   1 ornek = 1 yoklama (refresh_ms)
#        Pencere dolana dek sonuc bostur; penceredeki tekil bosluklar atlanir.
#   Formul metni sunucuda saklanir (presets.json / havuz) ve TUM ekranlara yayilir;
#   degerlendirme tarayicida yapilir, InfluxDB'ye ek yuk binmez.
#   Ayni islem URL ile de yapilabilir:
#        /api/webhook/sensor?influx_name=CALC_GUC&name=Mil Gucu&unit=kW&expr=P1*N5/9550
#        /api/sensors/CALC_GUC/expr?expr=P1*N5/9550        (formulu degistir)

# --- KALICI VERI ---------------------------------------------------------------------------
# /app/config.json   : InfluxDB baglanti ayarlari
# /app/presets.json  : GLOBAL presetler (tum PC / tum tarayici ayni listeyi gorur)
# Konteyner yeniden kurulunca kaybolmamalari icin bu klasoru bir volume'e baglayin:
#   docker run -d -p 8989:8989 -v tbm_data:/app/data ...
# ya da dosyalari dogrudan baglayin:
#   docker run ... -v %cd%\presets.json:/app/presets.json -v %cd%\config.json:/app/config.json ...
VOLUME ["/app/data"]

EXPOSE 8989

CMD ["python", "main.py"]
