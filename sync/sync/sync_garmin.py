#!/usr/bin/env python3
"""
Traka sync — pulls rides and wellness from Garmin Connect, writes one summary file.

Two modes:

  --login       One-time, on your own computer. Logs in with email + password,
                handles the MFA code if Garmin asks, and prints a token bundle.
                Paste that bundle into the GitHub secret GARMIN_TOKEN_B64.

  (default)     Runs in GitHub Actions every morning. Loads the token from the
                environment, refreshes it, pulls the last N days, writes
                data/latest.json and data/latest.md, and saves the rotated token
                to GARMIN_TOKEN_OUT_DIR so the workflow can store it back.

Never commit credentials. The token lives only in GitHub secrets.
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
OUT_DIR = Path("data")


# --------------------------------------------------------------------------- auth
def login_interactive() -> None:
    """One-time login with email/password. Prints the token bundle to store as a secret."""
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    if not email or not password:
        sys.exit("Set GARMIN_EMAIL and GARMIN_PASSWORD in the environment for --login.")

    g = Garmin(email, password, prompt_mfa=lambda: input("Garmin MFA code: ").strip())
    g.login()
    bundle = g.client.dumps()
    b64 = base64.b64encode(bundle.encode()).decode()

    print("\n=== Token bundle (base64). Paste the WHOLE thing into the GitHub secret", TOKEN_ENV, "===\n")
    print(b64)
    print("\n=== end ===")
    print("\nDon't run this again quickly — Garmin rate-limits logins.")


def client_from_env() -> Garmin:
    """Load the stored token, refresh it so it keeps rolling forward."""
    raw = os.getenv(TOKEN_ENV)
    if not raw:
        sys.exit(f"{TOKEN_ENV} is not set. Run --login once and store the bundle as a secret.")
    try:
        bundle = base64.b64decode(raw).decode()
    except Exception:
        bundle = raw  # tolerate an un-encoded bundle

    g = Garmin()
    try:
        g.login(bundle.strip())
    except GarminConnectAuthenticationError as e:
        sys.exit(
            f"Garmin rejected the stored token — it has expired and cannot be refreshed.\n"
            f"Re-mint it: GARMIN_EMAIL=... GARMIN_PASSWORD=... python sync/sync_garmin.py --login\n"
            f"then update the {TOKEN_ENV} secret. ({e})"
        )

    # Rotate the DI token so its expiry clock resets. Best-effort.
    try:
        g.client._refresh_di_token()
    except Exception as e:
        print(f"[warn] token refresh failed, won't roll forward this run: {e}", file=sys.stderr)
    return g


def persist_rotated_token(g: Garmin) -> None:
    """If the token changed, write it out so the workflow can store it back as a secret."""
    out = os.getenv("GARMIN_TOKEN_OUT_DIR")
    original = os.getenv(TOKEN_ENV, "")
    if not out:
        return
    rotated = base64.b64encode(g.client.dumps().encode()).decode()
    if rotated.strip() == original.strip():
        return
    Path(out).mkdir(parents=True, exist_ok=True)
    (Path(out) / TOKEN_ENV).write_text(rotated)
    print("Token rotated — will be stored back by the workflow.")


# --------------------------------------------------------------------------- helpers
def _num(x, nd=0):
    try:
        v = float(x)
        return round(v, nd) if nd else int(round(v))
    except (TypeError, ValueError):
        return None


def _iso(d: date) -> str:
    return d.isoformat()


def _first(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", 0):
            return v
    return None


# --------------------------------------------------------------------------- pull
def pull_activities(g: Garmin, start: date, end: date) -> list[dict]:
    rows = []
    for a in g.get_activities_by_date(_iso(start), _iso(end)) or []:
        t = (a.get("activityType") or {}).get("typeKey", "") or ""
        started = (a.get("startTimeLocal") or "")[:10]
        secs = _num(a.get("duration")) or 0
        if not started or secs <= 0:
            continue
        rows.append({
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
            "aerobicEffect": _num(a.get("aerobicTrainingEffect"), 1),
            "anaerobicEffect": _num(a.get("anaerobicTrainingEffect"), 1),
        })
    rows.sort(key=lambda r: r["date"])
    return rows


def pull_wellness(g: Garmin, start: date, end: date) -> list[dict]:
    """One row per day: sleep, HRV, resting HR, Body Battery, stress."""
    rows = []

    # Body Battery comes back as a range in one call
    bb_by_date: dict[str, dict] = {}
    try:
        for day in g.get_body_battery(_iso(start), _iso(end)) or []:
            d = day.get("date")
            vals = [v[1] for v in (day.get("bodyBatteryValuesArray") or []) if v and v[1] is not None]
            if d:
                bb_by_date[d] = {
                    "bodyBatteryMax": max(vals) if vals else None,
                    "bodyBatteryMin": min(vals) if vals else None,
                    "bodyBatteryCharged": _num(day.get("charged")),
                    "bodyBatteryDrained": _num(day.get("drained")),
                }
    except Exception as e:
        print(f"[warn] body battery: {e}", file=sys.stderr)

    d = start
    while d <= end:
        ds = _iso(d)
        row: dict = {"date": ds}

        try:
            s = g.get_sleep_data(ds) or {}
            dto = s.get("dailySleepDTO") or {}
            row["sleepScore"] = _num(((dto.get("sleepScores") or {}).get("overall") or {}).get("value"))
            secs = _first(dto, "sleepTimeSeconds")
            row["sleepHours"] = round(secs / 3600, 1) if secs else None
            row["deepSleepHours"] = round((dto.get("deepSleepSeconds") or 0) / 3600, 1) or None
            row["remSleepHours"] = round((dto.get("remSleepSeconds") or 0) / 3600, 1) or None
            row["overnightHrv"] = _num(s.get("avgOvernightHrv"))
            row["restingHrSleep"] = _num(s.get("restingHeartRate"))
        except Exception as e:
            print(f"[warn] sleep {ds}: {e}", file=sys.stderr)

        try:
            h = g.get_hrv_data(ds) or {}
            summ = h.get("hrvSummary") or {}
            row["hrv"] = _num(summ.get("lastNightAvg")) or row.get("overnightHrv")
            row["hrvWeeklyAvg"] = _num(summ.get("weeklyAvg"))
            row["hrvStatus"] = summ.get("status")
            base = summ.get("baseline") or {}
            row["hrvBaselineLow"] = _num(base.get("balancedLow"))
            row["hrvBaselineHigh"] = _num(base.get("balancedUpper"))
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
            st = g.get_stress_data(ds) or {}
            row["stressAvg"] = _num(st.get("avgStressLevel"))
        except Exception as e:
            print(f"[warn] stress {ds}: {e}", file=sys.stderr)

        row.update(bb_by_date.get(ds, {}))
        row.pop("restingHrSleep", None)
        row.pop("overnightHrv", None)

        # keep the row only if it carries something
        if any(v is not None for k, v in row.items() if k != "date"):
            rows.append(row)
        d += timedelta(days=1)
    return rows


# --------------------------------------------------------------------------- write
def write_markdown(acts: list[dict], well: list[dict], path: Path, days: int) -> None:
    """A human-readable version. This is what the coach reads."""
    L = []
    L.append(f"# Garmin — last {days} days")
    L.append(f"_generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC_\n")

    L.append("## Wellness (night ending that morning)\n")
    L.append("| Date | Sleep | Hrs | HRV | HRV status | RHR | Body Battery | Stress |")
    L.append("|---|---|---|---|---|---|---|---|")
    for w in sorted(well, key=lambda r: r["date"], reverse=True):
        L.append(
            f"| {w['date']} | {w.get('sleepScore') or '—'} | {w.get('sleepHours') or '—'} "
            f"| {w.get('hrv') or '—'} | {w.get('hrvStatus') or '—'} | {w.get('restingHr') or '—'} "
            f"| {w.get('bodyBatteryMax') or '—'} | {w.get('stressAvg') or '—'} |"
        )

    L.append("\n## Activities\n")
    L.append("| Date | Name | Type | Hours | km | m | Avg W | NP | TSS | IF | Avg HR | Cad |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for a in sorted(acts, key=lambda r: r["date"], reverse=True):
        L.append(
            f"| {a['date']} | {a['name'] or '—'} | {a['sport']}{' (indoor)' if a['indoor'] else ''} "
            f"| {a['hours']} | {a.get('distanceKm') or '—'} | {a.get('elevationM') or '—'} "
            f"| {a.get('avgWatts') or '—'} | {a.get('normalizedWatts') or '—'} | {a.get('tss') or '—'} "
            f"| {a.get('if') or '—'} | {a.get('avgHr') or '—'} | {a.get('avgCadence') or '—'} |"
        )

    # weekly totals
    L.append("\n## Weekly totals\n")
    weeks: dict[str, dict] = {}
    for a in acts:
        d = date.fromisoformat(a["date"])
        monday = d - timedelta(days=d.weekday())
        w = weeks.setdefault(_iso(monday), {"hours": 0.0, "tss": 0.0, "km": 0.0, "n": 0})
        w["hours"] += a["hours"]
        w["tss"] += a.get("tss") or 0
        w["km"] += a.get("distanceKm") or 0
        w["n"] += 1
    L.append("| Week of | Sessions | Hours | km | TSS |")
    L.append("|---|---|---|---|---|")
    for k in sorted(weeks, reverse=True):
        w = weeks[k]
        L.append(f"| {k} | {w['n']} | {w['hours']:.1f} | {w['km']:.0f} | {w['tss']:.0f} |")

    path.write_text("\n".join(L) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true", help="one-time login, prints token bundle")
    ap.add_argument("--days", type=int, default=int(os.getenv("SYNC_DAYS", "28")))
    ap.add_argument("--out", default=str(OUT_DIR))
    args = ap.parse_args()

    if args.login:
        login_interactive()
        return

    g = client_from_env()
    end = date.today()
    start = end - timedelta(days=args.days - 1)

    print(f"Pulling {start} → {end}")
    acts = pull_activities(g, start, end)
    print(f"  {len(acts)} activities")
    well = pull_wellness(g, start, end)
    print(f"  {len(well)} wellness days")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "days": args.days,
        "activities": acts,
        "wellness": well,
    }
    (out / "latest.json").write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    write_markdown(acts, well, out / "latest.md", args.days)
    print(f"Wrote {out/'latest.json'} and {out/'latest.md'}")

    persist_rotated_token(g)


if __name__ == "__main__":
    main()
