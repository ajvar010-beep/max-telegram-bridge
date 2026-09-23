import hashlib
import os
import sys

import certifi
import requests

if not sys.stdout.isatty():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CA_BUNDLE = os.path.join(BASE_DIR, "ca_bundle.pem")
LOCAL_CERTS = {
    "root_ca.pem": "936a43fea6e8e525bcc0f81acd9c3d21b4fc4b9b68acea7906d698005afc6504",
    "sub_ca.pem": "f0ae589f36774f29ef3648f7984b08d42fcce6f1ffeeb6236d773daeb2744ea6",
}
TEST_URL = "https://platform-api2.max.ru/me"


def load_certificates():
    certificates = []
    for name, expected_hash in LOCAL_CERTS.items():
        path = os.path.join(BASE_DIR, name)
        with open(path, "rb") as f:
            data = f.read()
        if b"BEGIN CERTIFICATE" not in data or b"END CERTIFICATE" not in data:
            raise ValueError(f"Некорректный PEM: {name}")
        actual_hash = hashlib.sha256(data).hexdigest()
        if actual_hash != expected_hash:
            raise ValueError(f"SHA-256 сертификата {name} не совпадает с закреплённым значением")
        certificates.append(data if data.endswith(b"\n") else data + b"\n")
    return certificates


def build_bundle():
    with open(certifi.where(), "rb") as f:
        base = f.read()
    bundle = base + (b"" if base.endswith(b"\n") else b"\n") + b"".join(load_certificates())
    tmp_path = CA_BUNDLE + ".tmp"
    with open(tmp_path, "wb") as f:
        f.write(bundle)
    os.replace(tmp_path, CA_BUNDLE)
    print(f"Бандл сертификатов создан: {CA_BUNDLE}")


def verify():
    resp = requests.get(TEST_URL, verify=CA_BUNDLE, timeout=15)
    print(f"Проверка {TEST_URL}: HTTP {resp.status_code}")
    if resp.status_code == 401:
        print("Успех: сертификат Минцифры работает, API отвечает.")
        return True
    print(f"Неожиданный код ответа: {resp.text[:200]}")
    return False


def main():
    try:
        build_bundle()
    except Exception as e:
        print(f"Ошибка создания бандла: {e}")
        return 1
    try:
        if verify():
            return 0
    except requests.exceptions.SSLError as e:
        print(f"SSL-ошибка: {e}")
    except Exception as e:
        print(f"Ошибка проверки: {e}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
