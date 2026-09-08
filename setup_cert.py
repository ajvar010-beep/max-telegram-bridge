import os
import sys

import certifi
import requests

if not sys.stdout.isatty():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CA_BUNDLE = os.path.join(BASE_DIR, "ca_bundle.pem")
LOCAL_CERTS = [
    os.path.join(BASE_DIR, "root_ca.pem"),
    os.path.join(BASE_DIR, "sub_ca.pem"),
]
CERT_URLS = [
    "https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt",
    "https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt",
]
TEST_URL = "https://platform-api2.max.ru/me"


def fetch_certs():
    certs = []
    for path, url in zip(LOCAL_CERTS, CERT_URLS):
        data = None
        if os.path.exists(path):
            with open(path, "rb") as f:
                data = f.read()
        if not data or b"BEGIN CERTIFICATE" not in data:
            print(f"Скачиваю сертификат: {url}")
            resp = requests.get(url, timeout=30, verify=False)
            resp.raise_for_status()
            data = resp.content
            with open(path, "wb") as f:
                f.write(data)
        certs.append(data)
    return certs


def build_bundle():
    with open(certifi.where(), "rb") as f:
        base = f.read()
    parts = [base]
    for data in fetch_certs():
        if not data.endswith(b"\n"):
            data += b"\n"
        parts.append(data)
    with open(CA_BUNDLE, "wb") as f:
        f.write(b"".join(parts))
    print(f"Бандл сертификатов создан: {CA_BUNDLE}")


def verify():
    resp = requests.get(TEST_URL, verify=CA_BUNDLE, timeout=15)
    print(f"Проверка {TEST_URL}: HTTP {resp.status_code}")
    if resp.status_code == 401:
        print("Успех: сертификат Минцифры работает, API отвечает (401 — нет токена, это ожидаемо).")
        return True
    print(f"Неожиданный код ответа: {resp.text[:200]}")
    return False


def main():
    import urllib3

    urllib3.disable_warnings()
    try:
        build_bundle()
    except Exception as e:
        print(f"Ошибка создания бандла: {e}")
        return 1
    try:
        if verify():
            return 0
    except requests.exceptions.SSLError as e:
        print(f"SSL-ошибка осталась: {e}")
    except Exception as e:
        print(f"Ошибка проверки: {e}")
    print("Попробуйте запасной вариант: pip install truststore")
    return 1


if __name__ == "__main__":
    sys.exit(main())
