#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time, requests, statistics

URL = "https://fapi.bitunix.com/api/v1/futures/account?marginCoin=USDT"

def measure_latency(runs=10):
    times = []
    print(f"Testing latency to {URL}")
    for i in range(runs):
        start = time.perf_counter()
        try:
            resp = requests.get(URL, timeout=5)
            ok = resp.status_code == 200
        except Exception as e:
            ok = False
        elapsed = (time.perf_counter() - start) * 1000  # convert to ms
        times.append(elapsed)
        print(f"Run {i+1:02d}: {elapsed:.2f} ms {'✅' if ok else '❌'}")
        time.sleep(0.3)  # small pause so we don't hammer the server

    avg = statistics.mean(times)
    med = statistics.median(times)
    best = min(times)
    worst = max(times)
    print("\n📊 Summary:")
    print(f"Average: {avg:.2f} ms")
    print(f"Median : {med:.2f} ms")
    print(f"Best   : {best:.2f} ms")
    print(f"Worst  : {worst:.2f} ms")
    return avg, med, best, worst

if __name__ == "__main__":
    measure_latency()

