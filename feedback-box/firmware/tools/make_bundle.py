"""Make data/cert/x509_crt_bundle.bin from Mozilla's root CAs (cacert.pem, curl.se).

Format read from the core's libraries/WiFiClientSecure/src/esp_crt_bundle.c (arduino-esp32 2.0.17):
  2 bytes  number of certificates (big-endian)
  per certificate: 2 bytes subject length, 2 bytes public key length,
                   subject (raw DER Name), public key (DER SubjectPublicKeyInfo)
  sorted by subject bytes, because the core finds the issuer with a binary search (memcmp).

usage: python3 tools/make_bundle.py OUT.bin cacert.pem [cacrt_deprecated.pem ...]
cacrt_deprecated.pem (esp-idf) holds roots Mozilla removed that servers still chain to through a
cross-signed copy (Google Trust Services via GlobalSign Root CA): the core checks only the TOP
certificate the server sends, so without them those chains fail on the box.
GlobalSign Root CA (R1) is in neither file but signs the GTS Root R4 cross-certificate that
Google and Cloudflare (workers.cloudflare.com) send, so build.sh adds it from GlobalSign's site
and check_bundle.py proves it by the signature on that live certificate.
"""
import struct
import sys

from cryptography import x509
from cryptography.hazmat.primitives import serialization


def der_items(buf, pos, end):
    """yield (start, content_start, next) for each DER element in buf[pos:end]"""
    while pos < end:
        start = pos
        pos += 1  # tag (low-tag-number form is all a certificate uses)
        ln = buf[pos]
        pos += 1
        if ln & 0x80:
            n = ln & 0x7F
            ln = int.from_bytes(buf[pos : pos + n], "big")
            pos += n
        yield start, pos, pos + ln
        pos += ln


def raw_subject_and_key(der):
    """subject Name and SubjectPublicKeyInfo, as raw DER, read straight from the certificate bytes.
    Used for old roots the strict parser refuses (GlobalSign Root CA R1, 1998)."""
    (_, c0, _), = list(der_items(der, 0, len(der)))[:1]
    tbs_start, t0, t_end = next(der_items(der, c0, len(der)))
    fields = list(der_items(der, t0, t_end))
    if der[fields[0][0]] == 0xA0:  # [0] version
        fields = fields[1:]
    # serial, signature alg, issuer, validity, subject, spki
    subj, spki = fields[4], fields[5]
    return der[subj[0] : subj[2]], der[spki[0] : spki[2]]


def main(dst, *srcs):
    entries = {}
    for src in srcs:
        data = open(src, "rb").read()
        if b"-----BEGIN CERTIFICATE-----" in data:  # PEM files may open with comment lines
            for c in x509.load_pem_x509_certificates(data):
                name = c.subject.public_bytes()
                key = c.public_key().public_bytes(
                    serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
                )
                entries[name] = key  # same subject twice: the later one wins, as in the IDF script
        else:  # a single DER certificate (.crt from the CA's own site)
            name, key = raw_subject_and_key(data)
            entries[name] = key
    out = bytearray(struct.pack(">H", len(entries)))
    for name in sorted(entries):
        key = entries[name]
        out += struct.pack(">HH", len(name), len(key)) + name + key
    open(dst, "wb").write(out)
    print(f"{len(entries)} root certificates, {len(out)} bytes -> {dst}")


if __name__ == "__main__":
    main(sys.argv[1], *sys.argv[2:])
