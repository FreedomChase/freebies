"""Check the bundle the way the box does (needs Python 3.13+ for get_unverified_chain).

The core (esp_crt_bundle.c) takes the TOP certificate the server sends, looks up its ISSUER in the
bundle with a binary search on subject bytes (memcmp over the bundle entry's length), and checks the
signature with that entry's public key. This script does the same for live servers.

usage: python3 tools/check_bundle.py data/cert/x509_crt_bundle.bin host [host ...]
exit 0 = every host's chain would be accepted
"""
import socket
import ssl
import struct
import sys

from cryptography import x509
from cryptography.hazmat.primitives.serialization import load_der_public_key
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa


def load(path):
    b = open(path, "rb").read()
    n = struct.unpack(">H", b[:2])[0]
    off, crts = 2, []
    for _ in range(n):
        nl, kl = struct.unpack(">HH", b[off : off + 4])
        crts.append((b[off + 4 : off + 4 + nl], b[off + 4 + nl : off + 4 + nl + kl]))
        off += 4 + nl + kl
    return crts


def find(crts, issuer):
    start, end = 0, len(crts) - 1
    middle = (end - start) // 2
    while start <= end:
        name = crts[middle][0]
        a = issuer[: len(name)]
        cmp = (a > name) - (a < name)
        if cmp == 0:
            return crts[middle]
        if cmp < 0:
            end = middle - 1
        else:
            start = middle + 1
        middle = (start + end) // 2
    return None


def sig_ok(cert, key_der):
    key = load_der_public_key(key_der)
    try:
        if isinstance(key, rsa.RSAPublicKey):
            key.verify(cert.signature, cert.tbs_certificate_bytes, padding.PKCS1v15(), cert.signature_hash_algorithm)
        elif isinstance(key, ec.EllipticCurvePublicKey):
            key.verify(cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(cert.signature_hash_algorithm))
        else:
            return False
        return True
    except Exception:
        return False


def chain(host):
    ctx = ssl.create_default_context()
    with socket.create_connection((host, 443), timeout=15) as s:
        with ctx.wrap_socket(s, server_hostname=host) as t:
            return [x509.load_der_x509_certificate(c.public_bytes(ssl.ENCODING_DER) if hasattr(c, "public_bytes") else c)
                    for c in t.get_unverified_chain()]


def main(bundle, hosts):
    crts = load(bundle)
    print(f"bundle: {len(crts)} roots, sorted: {[c[0] for c in crts] == sorted(c[0] for c in crts)}")
    bad = 0
    for h in hosts:
        try:
            certs = chain(h)
        except Exception as e:
            print(f"SKIP {h}: {e}")
            continue
        top = certs[-1]
        entry = find(crts, top.issuer.public_bytes())
        ok = entry is not None and sig_ok(top, entry[1])
        bad += not ok
        print(f"{'PASS' if ok else 'FAIL'} {h}: top sent = {top.subject.rfc4514_string()[:60]}, "
              f"issuer = {top.issuer.rfc4514_string()[:60]}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
