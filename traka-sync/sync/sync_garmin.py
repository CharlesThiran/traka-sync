#!/usr/bin/env python3
"""
Traka sync — pulls rides + wellness from Garmin, tracks the power curve and
VO2max, and writes a morning weekly recap. One summary file the coach reads.

  --login    One-time on your computer. Prints a token bundle for the secret.
  (default)  Runs in GitHub Actions. Pulls the last N days, updates the
             all-time power curve, writes data/latest.{json,md}, rotates token.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from garminconnect import Garmin, GarminConnectAuthenticationError

TOKEN_ENV = "GARMIN_TOKEN_B64"
OUT = Path("data")
CURVE_FILE = OUT / "power_curve.json"
CURVE_WINDOWS = [5, 15, 30, 60, 300, 600, 1200, 3600]


def login_interactive() -> None:
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    if not email or not password:
        sys.exit("Set GARMIN_EMAIL and GARMIN_PASSWORD for --login.")
    g = Garmin(email, password, prompt_mfa=lambda: input("Garmin MFA code: ").strip())
    g.login()
    b64 = base64.b64encode(g.client.dumps().encode()).decode()
    print("\n=== Token bundle. Paste the WHOLE thing into the secret", TOKEN_ENV, "===\n")
    print(b64)
    print("\n=== end ===\nDon't run this again quickly — Garmin rate-limits logins.")


def client_from_env() -> Garmin:
    raw = os.getenv(TOKEN_ENV)
    if not raw:
        sys.exit(f"{TOKEN_ENV} not set. Run --login once and store the bundle.")
    try:
        bundle = base64.b64decode(raw).decode()
    except Exception:
        bundle = raw
    g = Garmin()
    try:
        g.login(bundle.strip())
    except GarminConnectAuthenticationError as e:
        sys.exit(
            f"Garmin rejected the stored token — expired, cannot refresh.\n"
            f"Re-mint: GARMIN_EMAIL=... GARMIN_PASSWORD=... python sync/sync_garmin.py --login\n"
            f"then update {TOKEN_ENV}. ({e})"
        )
    try:
        g.client._refresh_di_token()
    except Exception as e:
        print(f"[warn] token refresh failed: {e}", file=sys.stderr)
    return g


def persist_rotated_token(g: Garmin) -> None:
    out = os.getenv("GARMIN_TOKEN_OUT_DIR")
    original = os.getenv(TOKEN_ENV, "")
    if not out:
        return
    rotated = base64.b64encode(g.client.dumps().encode()).decode()
    if rotated.strip() == original.strip():
        return
    Path(out).mkdir(parents=True, exist_ok=True)
    (Path(out) / TOKEN_ENV).write_text(rotated)
    print("Token rotated.")


def _num(x, nd=0):
    try:
        v = float(x)
        return round(v, nd) if nd else int(round(v))
    except (TypeError, ValueError):
        return None


def _iso(d: date) -> str:
    return d.isoformat()


def pull_activities(g, start, end):
    rows = []
    for a in g.get_activities_by_date(_iso(start), _iso(end)) or []:
        t = (a.get("activityType") or {}).get("typeKey", "") or ""
        started = (a.get("startTimeLocal") or "")[:10]
        secs = _num(a.get("duration")) or 0
        if not started or secs <= 0:
            continue
        rows.append({
            "id": a.get("activityId"),
            "date": started,
            "name": a.get("activityName") or "",
            "sport": t,
            "indoor": "indoor" in t or "virtual" in t,
            "hours": round(secs / 3600, 2),
            "distanceKm": _num((a.get("distance") or 0) / 1000, 1),
            "elevationM": _num(a.get("elevationGain")),
            "avgWatts": _num(a.get("avgPower")),
            "normalizedWatts": _num(a.get("normPower")),
            "maxWatts": _num(a.get("maxPower")),
            "avgHr": _num(a.get("averageHR")),
            "maxHr": _num(a.get("maxHR")),
            "avgCadence": _num(a.get("averageBikingCadenceInRevPerMinute")),
            "tss": _num(a.get("trainingStressScore"), 1),
            "if": _num(a.get("intensityFactor"), 2),
            "kj": _num((a.get("calories") or 0) * 4.184),
        })
    rows.sort(key=lambda r: r["date"])
    return rows


def _rolling_best(power, win):
    n = len(power)
    if n < win:
        return None
    s = sum(power[:win])
    best = s
    for i in range(win, n):
        s += power[i] - power[i - win]
        if s > best:
            best = s
    return best / win


def update_power_curve(g, acts):
    curve = {"bests": {}, "updated": None}
    if CURVE_FILE.exists():
        try:
            curve = json.loads(CURVE_FILE.read_text())
        except Exception:
            pass
    bests = curve.get("bests", {})
    recent = [a for a in acts if a.get("avgWatts") and a.get("id")][-10:]
    for a in recent:
        try:
            det = g.get_activity_details(str(a["id"]), maxchart=2000)
        except Exception as e:
            print(f"[warn] details {a['id']}: {e}", file=sys.stderr)
            continue
        descs = det.get("metricDescriptors") or []
        pidx = None
        for d in descs:
            if (d.get("key") or "").lower() in ("directpower", "power"):
                pidx = d.get("metricsIndex")
                break
        if pidx is None:
            continue
        stream = []
        for pt in det.get("activityDetailMetrics", []) or []:
            m = pt.get("metrics") or []
            if pidx < len(m) and m[pidx] is not None:
                stream.append(float(m[pidx]))
        if len(stream) < 5:
            continue
        for w in CURVE_WINDOWS:
            b = _rolling_best(stream, w)
            if b is None:
                continue
            key = str(w)
            prev = bests.get(key, {})
            if not prev or b > prev.get("watts", 0):
                bests[key] = {"watts": round(b), "date": a["date"]}
    curve = {"bests": bests, "updated": datetime.utcnow().isoformat(timespec="seconds") + "Z"}
    OUT.mkdir(parents=True, exist_ok=True)
    CURVE_FILE.write_text(json.dumps(curve, indent=1))
    return curve


def pull_wellness(g, start, end):
    rows = []
    bb_by_date = {}
    try:
        for day in g.get_body_battery(_iso(start), _iso(end)) or []:
            d = day.get("date")
            vals = [v[1] for v in (day.get("bodyBatteryValuesArray") or []) if v and v[1] is not None]
            if d:
                bb_by_date[d] = {"bodyBatteryMax": max(vals) if vals else None,
                                 "bodyBatteryMin": min(vals) if vals else None}
    except Exception as e:
        print(f"[warn] body battery: {e}", file=sys.stderr)
    d = start
    while d <= end:
        ds = _iso(d)
        row = {"date": ds}
        try:
            s = g.get_sleep_data(ds) or {}
            dto = s.get("dailySleepDTO") or {}
            row["sleepScore"] = _num(((dto.get("sleepScores") or {}).get("overall") or {}).get("value"))
            secs = dto.get("sleepTimeSeconds")
            row["sleepHours"] = round(secs / 3600, 1) if secs else None
            row["restingHrSleep"] = _num(s.get("restingHeartRate"))
            row["overnightHrv"] = _num(s.get("avgOvernightHrv"))
        except Exception as e:
            print(f"[warn] sleep {ds}: {e}", file=sys.stderr)
        try:
            h = g.get_hrv_data(ds) or {}
            summ = h.get("hrvSummary") or {}
            row["hrv"] = _num(summ.get("lastNightAvg")) or row.get("overnightHrv")
            row["hrvWeeklyAvg"] = _num(summ.get("weeklyAvg"))
            row["hrvStatus"] = summ.get("status")
        except Exception as e:
            print(f"[warn] hrv {ds}: {e}", file=sys.stderr)
        try:
            r = g.get_rhr_day(ds) or {}
            metrics = ((r.get("allMetrics") or {}).get("metricsMap") or {})
            vals = metrics.get("WELLNESS_RESTING_HEART_RATE") or []
            row["restingHr"] = _num(vals[0].get("value")) if vals else row.get("restingHrSleep")
        except Exception as e:
            print(f"[warn] rhr {ds}: {e}", file=sys.stderr)
        try:
            mm = g.get_max_metrics(ds) or []
            row["vo2max"] = _num((mm[0] if mm else {}).get("generic", {}).get("vo2MaxPreciseValue"), 1)
        except Exception:
            row["vo2max"] = None
        row.update(bb_by_date.get(ds, {}))
        row.pop("restingHrSleep", None)
        row.pop("overnightHrv", None)
        if any(v is not None for k, v in row.items() if k != "date"):
            rows.append(row)
        d += timedelta(days=1)
    return rows


def build_recap(acts, well):
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    last_monday = monday - timedelta(days=7)

    def wk(a_start, a_end):
        rides = [a for a in acts if a_start <= date.fromisoformat(a["date"]) <= a_end]
        hrs = sum(a["hours"] for a in rides)
        tss = sum(a.get("tss") or 0 for a in rides)
        km = sum(a.get("distanceKm") or 0 for a in rides)
        hard = sum(1 for a in rides if (a.get("if") or 0) >= 0.85 or (a.get("normalizedWatts") or 0) >= 233)
        return {"sessions": len(rides), "hours": round(hrs, 1), "tss": round(tss),
                "km": round(km), "hardDays": hard}

    this_wk = wk(monday, today)
    last_wk = wk(last_monday, monday - timedelta(days=1))
    recent = [w for w in well if w.get("hrv")][-7:]
    hrv_now = recent[-1]["hrv"] if recent else None
    hrv_avg = round(sum(w["hrv"] for w in recent) / len(recent)) if recent else None
    sleeps = [w["sleepScore"] for w in well[-7:] if w.get("sleepScore")]
    rhr = [w["restingHr"] for w in well[-7:] if w.get("restingHr")]
    return {"thisWeek": this_wk, "lastWeek": last_wk, "hrvLast": hrv_now, "hrv7dAvg": hrv_avg,
            "sleep7dAvg": round(sum(sleeps) / len(sleeps)) if sleeps else None,
            "rhr7dAvg": round(sum(rhr) / len(rhr)) if rhr else None}


def write_markdown(acts, well, curve, recap, path, days):
    L = [f"# Garmin — last {days} days",
         f"_generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC_\n"]
    tw, lw = recap["thisWeek"], recap["lastWeek"]
    L.append("## This morning's recap\n")
    L.append(f"**This week so far:** {tw['sessions']} sessions · {tw['hours']}h · {tw['km']}km "
             f"· {tw['tss']} TSS · {tw['hardDays']} hard days")
    L.append(f"**Last week:** {lw['sessions']} sessions · {lw['hours']}h · {lw['km']}km "
             f"· {lw['tss']} TSS · {lw['hardDays']} hard days")
    L.append(f"**Recovery:** HRV {recap['hrvLast'] or '—'} (7d avg {recap['hrv7dAvg'] or '—'}) "
             f"· sleep {recap['sleep7dAvg'] or '—'} · resting HR {recap['rhr7dAvg'] or '—'}\n")
    if curve.get("bests"):
        L.append("## Power curve — all-time bests\n")
        L.append("| Duration | Watts | Set |")
        L.append("|---|---|---|")
        labels = {"5": "5 sec", "15": "15 sec", "30": "30 sec", "60": "1 min",
                  "300": "5 min", "600": "10 min", "1200": "20 min", "3600": "1 hour"}
        for k in ["5", "15", "30", "60", "300", "600", "1200", "3600"]:
            b = curve["bests"].get(k)
            if b:
                L.append(f"| {labels[k]} | {b['watts']} | {b['date']} |")
        L.append("")
    vo2 = sorted([(w["date"], w["vo2max"]) for w in well if w.get("vo2max")])
    if vo2:
        L.append("## VO2max\n")
        line = f"Latest: **{vo2[-1][1]}** ({vo2[-1][0]})"
        if len(vo2) > 1:
            line += f" · earliest in window {vo2[0][1]} ({vo2[0][0]})"
        L.append(line + "\n")
    L.append("## Wellness (night ending that morning)\n")
    L.append("| Date | Sleep | Hrs | HRV | RHR | Body Battery | VO2max |")
    L.append("|---|---|---|---|---|---|---|")
    for w in sorted(well, key=lambda r: r["date"], reverse=True):
        L.append(f"| {w['date']} | {w.get('sleepScore') or '—'} | {w.get('sleepHours') or '—'} "
                 f"| {w.get('hrv') or '—'} | {w.get('restingHr') or '—'} "
                 f"| {w.get('bodyBatteryMax') or '—'} | {w.get('vo2max') or '—'} |")
    L.append("\n## Activities\n")
    L.append("| Date | Name | Type | Hours | km | m | Avg W | NP | TSS | IF | Avg HR | Cad |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for a in sorted(acts, key=lambda r: r["date"], reverse=True):
        L.append(f"| {a['date']} | {a['name'] or '—'} | {a['sport']}{' (in)' if a['indoor'] else ''} "
                 f"| {a['hours']} | {a.get('distanceKm') or '—'} | {a.get('elevationM') or '—'} "
                 f"| {a.get('avgWatts') or '—'} | {a.get('normalizedWatts') or '—'} | {a.get('tss') or '—'} "
                 f"| {a.get('if') or '—'} | {a.get('avgHr') or '—'} | {a.get('avgCadence') or '—'} |")
    path.write_text("\n".join(L) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--days", type=int, default=int(os.getenv("SYNC_DAYS", "28")))
    args = ap.parse_args()
    if args.login:
        login_interactive()
        return
    g = client_from_env()
    end = date.today()
    start = end - timedelta(days=args.days - 1)
    print(f"Pulling {start} -> {end}")
    acts = pull_activities(g, start, end)
    print(f"  {len(acts)} activities")
    well = pull_wellness(g, start, end)
    print(f"  {len(well)} wellness days")
    curve = update_power_curve(g, acts)
    print(f"  power curve: {len(curve.get('bests', {}))} windows")
    recap = build_recap(acts, well)
    OUT.mkdir(parents=True, exist_ok=True)
    payload = {"generated": datetime.utcnow().isoformat(timespec="seconds") + "Z",
               "days": args.days, "recap": recap, "powerCurve": curve.get("bests", {}),
               "activities": acts, "wellness": well}
    (OUT / "latest.json").write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    write_markdown(acts, well, curve, recap, OUT / "latest.md", args.days)
    print("Wrote latest.json, latest.md, power_curve.json")
    persist_rotated_token(g)


if __name__ == "__main__":
    main()
