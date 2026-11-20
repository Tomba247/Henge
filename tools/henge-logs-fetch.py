#!/usr/bin/env python3
"""Reads the log drop a Henge user set up for whoever helps them (ios/Henge/LogUpload.swift sends the app's log there
a piece a minute; a guest can add files with curl). The drop is a Cloudflare Worker over a KV namespace titled
HENGE_LOGS in the user's own account; this reads it through the Cloudflare API, so it needs API access to that account
(CLOUDFLARE_API_TOKEN, or a session whose requests to api.cloudflare.com are already authorized).

    henge-logs-fetch.py             what is there: name, pieces, bytes, age of the newest piece
    henge-logs-fetch.py NAME OUT    NAME's pieces, joined at their offsets, written to OUT
"""
import json, os, sys, time, urllib.parse, urllib.request

CF = "https://api.cloudflare.com/client/v4"
AUTH = {"Authorization": "Bearer " + os.environ.get("CLOUDFLARE_API_TOKEN", "from-the-session")}


def call(path, raw=False):
    body = urllib.request.urlopen(urllib.request.Request(CF + path, headers=AUTH), timeout=60).read()
    return body if raw else json.loads(body)


account = call("/accounts")["result"][0]["id"]
spaces = [n["id"] for n in call(f"/accounts/{account}/storage/kv/namespaces?per_page=100")["result"] if n["title"] == "HENGE_LOGS"]
if not spaces:
    sys.exit("no KV namespace titled HENGE_LOGS in this account")
KV = f"/accounts/{account}/storage/kv/namespaces/{spaces[0]}"


def keys(prefix=""):
    out, cursor = [], ""
    while True:
        d = call(f"{KV}/keys?limit=1000&prefix={urllib.parse.quote(prefix)}" + (f"&cursor={cursor}" if cursor else ""))
        out += d["result"]
        cursor = (d.get("result_info") or {}).get("cursor") or ""
        if not cursor:
            return out


if len(sys.argv) < 3:
    names = {}
    for k in keys():
        name = k["name"].rsplit("/", 1)[0]
        meta = k.get("metadata") or {}
        e = names.setdefault(name, [0, 0, 0])
        e[0] += 1
        e[1] += meta.get("bytes", 0)
        e[2] = max(e[2], meta.get("at", 0))
    for name, (pieces, size, at) in sorted(names.items(), key=lambda x: x[1][2]):
        age = f"{int(time.time() - at / 1000)} s ago" if at else "age unknown"
        print(f"{name}: {pieces} pieces, {size} bytes, newest {age}")
else:
    name, out = sys.argv[1], sys.argv[2]
    pieces = sorted(k["name"] for k in keys(name + "/"))
    with open(out, "wb") as f:
        for k in pieces:
            f.seek(int(k.rsplit("/", 1)[1]))
            f.write(call(f"{KV}/values/{urllib.parse.quote(k, safe='')}", raw=True))
    print(f"{name}: {len(pieces)} pieces -> {out}")
