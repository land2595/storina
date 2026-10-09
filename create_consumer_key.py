"""Genera una consumer key OVH con i soli permessi necessari.

Uso (con OVH_ENDPOINT, OVH_APPLICATION_KEY, OVH_APPLICATION_SECRET nell'ambiente o nel .env):
  docker compose run --rm ovh-ks-sniper python create_consumer_key.py
oppure localmente:
  pip install ovh && python create_consumer_key.py

Apri l'URL stampato, accedi con l'account OVH, imposta la validità su "Unlimited" e
copia la consumer key in OVH_CONSUMER_KEY nel file .env.
"""
import os
import sys

import ovh

RULES = [
    ("GET", "/order/cart"),
    ("POST", "/order/cart"),
    ("GET", "/order/cart/*"),
    ("POST", "/order/cart/*"),
    ("DELETE", "/order/cart/*"),
    ("GET", "/order/catalog/*"),
    ("GET", "/me"),
    ("GET", "/me/*"),
    ("GET", "/auth/currentCredential"),
]


def main() -> int:
    endpoint = os.getenv("OVH_ENDPOINT", "ovh-eu")
    ak, secret = os.getenv("OVH_APPLICATION_KEY", ""), os.getenv("OVH_APPLICATION_SECRET", "")
    if not ak or not secret:
        print("Imposta OVH_APPLICATION_KEY e OVH_APPLICATION_SECRET (vedi README).", file=sys.stderr)
        return 1
    client = ovh.Client(endpoint=endpoint, application_key=ak, application_secret=secret)
    req = client.new_consumer_key_request()
    for method, path in RULES:
        req.add_rule(method, path)
    res = req.request()
    print("\nPermessi richiesti:")
    for method, path in RULES:
        print(f"  {method:6} {path}")
    print(f"\n1) Apri e valida (scegli validità 'Unlimited'):\n   {res['validationUrl']}")
    print(f"\n2) Poi metti nel .env:\n   OVH_CONSUMER_KEY={res['consumerKey']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
