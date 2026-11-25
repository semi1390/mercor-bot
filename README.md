# Mercor → Telegram job alerts

This bot checks [Mercor's job board](https://work.mercor.com/explore) every 15 minutes and sends you a Telegram message when a new listing matches your filters. It runs free on GitHub Actions, with no server and no paid hosting.

**How it works:** the bot reads Mercor's public job list (the JSON that the explore page itself loads; no login needed) and compares it with `seen.json`, the list of job IDs it has already seen. It sends each new match to Telegram, then commits the updated `seen.json` back to this repo.

---

## 1. Create your Telegram bot (≈2 min)

1. In Telegram, search for **@BotFather** (it has a blue check) and open a chat.
2. Send `/newbot`.
3. Give it a display name (e.g. `My Mercor Alerts`), then a username ending in `bot` (e.g. `my_mercor_alerts_bot`).
4. BotFather replies with a **token** like `123456789:ABCdef...`. Copy it and keep it private, because anyone with the token can control your bot.

## 2. Get your chat ID

1. Open a chat with your new bot (BotFather gives you a link) and send it any message, e.g. `hi`.
2. In a browser, open this address, putting your token in place of `<TOKEN>` (keep the word `bot` in front of it):
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
3. Find `"chat":{"id":` in the page. The number after it (e.g. `987654321`) is your **chat ID**.
   - If you see `{"ok":true,"result":[]}`, send your bot another message and refresh the page.

## 3. Put the code on GitHub

1. Go to [github.com/new](https://github.com/new).
2. Name the repo (e.g. `mercor-bot`), choose **Public** (public repos get unlimited free Actions minutes), and click **Create repository**.
3. Upload the files using either option:
   - **Terminal:** run the `git push` commands GitHub shows under "…or push an existing repository".
   - **Browser:** click **uploading an existing file** and drag in *everything* in this folder, including the hidden `.github` folder. Then click **Commit changes**.
4. Check that the repo now has `main.py`, `config.yaml`, `requirements.txt`, `README.md` and `.github/workflows/check.yml`.

> Never commit `seen.json` from your own computer. The bot creates it on its first run.

## 4. Add your two secrets

In your repo on GitHub: **Settings → Secrets and variables → Actions → New repository secret**. Add:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the token from BotFather |
| `TELEGRAM_CHAT_ID` | your chat ID number |

Secrets are encrypted and hidden from visitors to your public repo. GitHub also hides them in the run logs.

*(On mobile, the Settings tab is in the repo's top menu. You may need to tap "…" or switch to desktop view.)*

## 5. Turn on Actions and run it the first time

1. Open the **Actions** tab. If GitHub asks, click **I understand my workflows, go ahead and enable them**.
2. Click **Check Mercor** in the left list, then **Run workflow → Run workflow**.
3. Within about a minute you should get: **"✅ Mercor bot is live — tracking N listings."**

   The first run saves every current job as "seen" *without* messaging you about each one. After that, you only hear about new ones.
4. From then on it runs every 15 minutes by itself. GitHub's timer is "best effort", so runs can be 5–20 minutes late at busy times.

**Optional:** to test Telegram alone, open `.github/workflows/check.yml`, change `run: python main.py` to `run: python main.py --test-telegram`, run the workflow, then change it back.

## Optional: real 15-minute checks with cron-job.org

GitHub's built-in timer often runs free repos only every few hours. A free outside timer can start the workflow every 15 minutes instead. GitHub's timer stays on as a backup.

**A. Make a GitHub token that can only start this workflow**
1. GitHub → your profile picture → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
2. Name: `mercor-cron`. Expiration: the longest option offered (set a reminder to renew it).
3. Repository access: **Only select repositories** → `mercor-bot`.
4. Permissions → Repository permissions → **Actions: Read and write**. Leave everything else as it is.
5. Click **Generate token** and copy it. GitHub shows it only once.

**B. Create the timer on [cron-job.org](https://cron-job.org)** (free account)
1. **Create cronjob**. Title: `Mercor bot`.
2. URL: `https://api.github.com/repos/<your-username>/mercor-bot/actions/workflows/check.yml/dispatches`
3. Schedule: **Every 15 minutes**.
4. **Advanced** tab → Request method **POST**. Add these headers:
   - `Accept` = `application/vnd.github+json`
   - `Authorization` = `Bearer <your token>`
   - `X-GitHub-Api-Version` = `2022-11-28`
   - `Content-Type` = `application/json`
5. Request body: `{"ref":"main"}`
6. Save, then click **Test run**. A response of **204** means it worked, and a new run appears in your Actions tab.

If the token expires, cron-job.org shows errors (401) and GitHub's own timer keeps the bot running more slowly. Make a new token and paste it into the `Authorization` header.

## Sending alerts to more than one person

1. Your friend opens your bot in Telegram (send them the `t.me/your_bot_name` link), taps **Start** and sends any message. A bot can't message anyone who hasn't done this.
2. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` again. Find your friend's message and the number after its `"chat":{"id":`. That is their chat ID.
3. On GitHub: **Settings → Secrets and variables → Actions**, click the ✏️ next to `TELEGRAM_CHAT_ID`, and enter both IDs separated by a comma, e.g. `987654321,123456789`.

Everyone on the list gets the same alerts and filters. If one person blocks the bot, the others still get their messages.

## 6. Change your filters

Open `config.yaml` on GitHub, tap the ✏️ pencil icon, edit, and click **Commit changes**. The next run uses the new settings.

```yaml
include_keywords: ["writing", "finance"]   # only jobs mentioning one of these; [] = all jobs
exclude_keywords: ["onsite"]               # skip jobs mentioning any of these
min_hourly_rate: 30                        # skip hourly jobs paying under $30/hr; 0 = no minimum
eligible_countries: ["NGA"]                # 3-letter country codes (NGA = Nigeria)
notify_if_eligibility_unknown: true        # jobs with no country info: true = notify, false = skip
```

- Keywords match anywhere in the title, description or domain, ignoring upper/lower case.
- Country rule: a job passes if it is open to one of your countries, or open worldwide (Mercor lists no restriction, or the location says "Worldwide", "Anywhere" or "Global"). Jobs with no country data at all follow `notify_if_eligibility_unknown`.
- Filters only affect new listings. Jobs already seen won't be re-sent after you loosen a filter.

## What you'll receive

- **One message per new job:** title, pay, location, eligible countries, commitment, spots left (when Mercor provides it; marked 🔥 closing soon once 90% are filled) and an apply link.
- **More than 10 new matches at once:** one summary message with links instead.
- **The bot breaks** (3 failed checks in a row): one ⚠️ warning, then a ✅ message when it recovers.

## Files

| File | What it is |
|---|---|
| `main.py` | the bot |
| `config.yaml` | your filters |
| `.github/workflows/check.yml` | the 15-minute schedule |
| `seen.json` | job IDs already seen (created by the bot; IDs unseen for 60 days are pruned) |
| `state.json` | failure counter (created by the bot) |
| `last_run.txt` | timestamp updated every ~25 days so GitHub doesn't pause the schedule for inactivity |

None of these files contain your token, chat ID or any personal info. Those live only in GitHub Secrets.

## Running on a computer (optional)

With Python 3.11+:

```bash
pip install -r requirements.txt
python main.py --dry-run          # shows what it would send; sends and saves nothing
```

`--test-telegram` and normal runs need the two environment variables `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` set first.

## Troubleshooting

- **No "bot is live" message:** open the run in the Actions tab and read the **Run bot** step. A `Telegram error 401` means the token is wrong. `400 chat not found` means the chat ID is wrong or you haven't messaged your bot yet.
- **Push failed / permission denied in the "Save state" step:** go to **Settings → Actions → General → Workflow permissions**, select **Read and write permissions**, and save.
- **Schedule stopped:** if GitHub ever pauses it, go to Actions → Check Mercor → **Enable workflow**.
