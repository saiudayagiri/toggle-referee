#!/usr/bin/env python3
"""Send simulated visitors through the Hack Night signup page.

Each visitor is a separate HTTP session with its own cookie. The visitor loads
the page, looks at the form it was actually served, and decides whether to
finish it. Nothing here knows which version is "supposed" to win: the only
behaviour modelled is that every extra field loses a share of visitors, and
that a few people mistype. The app and Flagsmith do the rest, exactly as they
would with real traffic.

Standard library only. Example:

    python3 demo/simulate.py --visitors 2000 --url http://localhost:8080
"""

import argparse
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

FIELD = re.compile(r'<input id="([a-z_]+)"')
VERSION = re.compile(r'data-version="([a-z_]+)"')

ANSWERS = {
    "name": "Asha Rao",
    "email": "asha@example.com",
    "password": "correct-horse-battery",
    "college": "NST",
    "phone": "5550100",
    "github": "asharao",
}


def visit(url: str, rng: random.Random, interest: float, drop_per_field: float, typo_rate: float) -> str:
    """One visitor. Returns what happened, for the summary line."""
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    try:
        page = opener.open(url + "/", timeout=30).read().decode("utf-8")
    except (urllib.error.URLError, OSError):
        return "failed"
    fields = FIELD.findall(page)
    version = (VERSION.search(page) or [None, "unknown"])[1]
    if rng.random() > interest:
        return version + ":left"
    for _ in fields:
        if rng.random() < drop_per_field:
            return version + ":left"
    form = {name: ANSWERS.get(name, "x") for name in fields}
    if rng.random() < typo_rate:
        form["email"] = "asha.example.com"
    data = urllib.parse.urlencode(form).encode("utf-8")
    try:
        opener.open(url + "/signup", data=data, timeout=30).read()
        return version + ":signed_up"
    except urllib.error.HTTPError as error:
        return version + (":error" if error.code == 400 else ":failed")
    except (urllib.error.URLError, OSError):
        return "failed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://localhost:8080")
    parser.add_argument("--visitors", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--interest", type=float, default=0.40, help="share of visitors who start the form")
    parser.add_argument("--drop-per-field", type=float, default=0.09, help="share who give up at each field")
    parser.add_argument("--typo-rate", type=float, default=0.04, help="share of submissions with a bad email")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--rate", type=float, default=0, help="visitors per second (0 = as fast as possible)")
    args = parser.parse_args()

    seed = args.seed if args.seed is not None else int(time.time())
    master = random.Random(seed)
    seeds = [master.random() for _ in range(args.visitors)]
    url = args.url.rstrip("/")
    counts = {}
    started = time.time()

    def run(index: int) -> str:
        if args.rate > 0:
            delay = started + index / args.rate - time.time()
            if delay > 0:
                time.sleep(delay)
        return visit(url, random.Random(seeds[index]), args.interest, args.drop_per_field, args.typo_rate)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for done, outcome in enumerate(pool.map(run, range(args.visitors)), start=1):
            counts[outcome] = counts.get(outcome, 0) + 1
            if done % 250 == 0:
                print("  %d visitors..." % done, flush=True)

    print("%d visitors in %.0fs (seed %d)" % (args.visitors, time.time() - started, seed))
    versions = sorted({key.split(":")[0] for key in counts if ":" in key})
    for version in versions:
        seen = sum(v for k, v in counts.items() if k.startswith(version + ":"))
        signed = counts.get(version + ":signed_up", 0)
        errors = counts.get(version + ":error", 0)
        print("  %-13s %5d visitors, %4d signed up (%.1f%%), %3d errors" % (
            version, seen, signed, 100.0 * signed / seen if seen else 0, errors,
        ))
    if counts.get("failed"):
        print("  %d requests failed" % counts["failed"])
        return 1 if counts["failed"] > args.visitors / 2 else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
