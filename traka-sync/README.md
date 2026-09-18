# traka-sync

Pulls your rides and recovery data from Garmin Connect every morning and writes one summary file. That file is what your coach reads.

No app, no server. GitHub Actions is the robot; your Garmin login is the only credential; the token renews itself.

## What it produces

Every morning, two files in `data/`:

- **`latest.md`** — human-readable. Wellness table (sleep score, hours, HRV, resting HR, Body Battery, stress), activity table (power, NP, TSS, HR, cadence), weekly totals. This is the one to give your coach.
- **`latest.json`** — same data, machine-readable.

Both cover the last 28 days.

## Setup — about 20 minutes, once

### 1. Create the repo

New repository on GitHub, private is fine. Copy these files in. Push.

### 2. Log in to Garmin once, on your own computer

You need Python 3.10+ installed.

```bash
pip install garminconnect==0.3.6
GARMIN_EMAIL='you@example.com' GARMIN_PASSWORD='your-password' python sync/sync_garmin.py --login
```

If Garmin asks for a code (texted or emailed), type it. On success it prints a long block of text between `=== Token bundle ===` markers. **Copy the whole thing.**

Don't run this twice in quick succession — Garmin rate-limits logins and you'll see 429 errors. Wait a few minutes between attempts.

### 3. Store the token as a secret

Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

- Name: `GARMIN_TOKEN_B64`
- Value: the block you copied

### 4. Create a master key so the token can renew itself

Garmin tokens now expire every few days. The workflow refreshes the token each morning and saves the new one back — but writing a secret needs permission.

1. Go to **github.com/settings/personal-access-tokens** → **Generate new token** (fine-grained)
2. Name: `traka-sync renewer` · Expiration: one year
3. Repository access: **Only select repositories** → this repo
4. Permissions → Repository permissions → **Secrets: Read and write**
5. Generate. Copy the `github_pat_…` string — it is shown once.

Then a second secret in the repo:

- Name: `GH_PAT`
- Value: the `github_pat_…` string

### 5. Run it

Repo → **Actions** → **garmin-sync** → **Run workflow**. Wait a minute, refresh.

Green tick means it worked. Open the run and look for `Renewed GARMIN_TOKEN_B64` in the last step — that confirms the self-renewal loop is live.

`data/latest.md` now exists in your repo. It updates every morning from here on.

### 6. Point your coach at it

Open `data/latest.md` in GitHub, click **Raw**, copy the URL. It looks like:

```
https://raw.githubusercontent.com/YOURNAME/traka-sync/main/data/latest.md
```

Put that URL in your Claude Project instructions with a line like: *"At the start of each conversation, fetch this URL for my latest training and recovery data."*

If the repo is private, raw URLs need a token. Simplest fix: make the repo public — it contains training numbers, not credentials. Or keep it private and paste the file content into the chat when asked.

## If it breaks

**Red run, "Garmin rejected the stored token".** The token expired before it could be renewed — usually because the workflow didn't run for several days. Re-do step 2 and update the `GARMIN_TOKEN_B64` secret.

**Red run, 429 errors.** Garmin rate-limiting. Wait an hour and re-run.

**Green run but no "Renewed" line.** Token was unchanged today — normal, nothing wrong.

**Green run but `GH_PAT not set` in the log.** Step 4 wasn't completed. Sync still works; it just won't self-renew until the PAT is in place.

## What it does not do

- Push workouts to Garmin. Build those in TrainerRoad or MyWhoosh from the coach's prescription.
- Store anything sensitive in the repo. Credentials live only in secrets.
- Depend on anyone else's account. This is yours.
