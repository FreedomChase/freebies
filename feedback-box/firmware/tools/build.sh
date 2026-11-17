#!/bin/sh
# Build the firmware inside python:3.11-slim (PlatformIO cache mounted at /root/.platformio).
# Output: .pio/build/esp32dev/{bootloader,partitions,firmware}.bin and boot_app0.bin copied to /fw/out.
set -eu
pip install -q platformio==6.1.18 cryptography 2>/dev/null
cd /fw
mkdir -p data/cert out
python3 -c "import urllib.request; urllib.request.urlretrieve('https://curl.se/ca/cacert.pem', '/tmp/cacert.pem')"
python3 -c "import urllib.request; urllib.request.urlretrieve('https://raw.githubusercontent.com/espressif/esp-idf/master/components/mbedtls/esp_crt_bundle/cacrt_deprecated.pem', '/tmp/deprecated.pem')"
python3 -c "import urllib.request; urllib.request.urlretrieve('https://secure.globalsign.net/cacert/Root-R1.crt', '/tmp/globalsign-r1.crt')"
python3 tools/make_bundle.py data/cert/x509_crt_bundle.bin /tmp/cacert.pem /tmp/deprecated.pem /tmp/globalsign-r1.crt
pio run -e esp32dev 2>&1 | tail -25
B=.pio/build/esp32dev
cp $B/bootloader.bin $B/partitions.bin $B/firmware.bin out/
cp /root/.platformio/packages/framework-arduinoespressif32/tools/partitions/boot_app0.bin out/
cd out && sha256sum *.bin > SHA256SUMS && ls -l
