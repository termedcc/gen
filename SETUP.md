# Python Generator Bot - Setup Guide

## Local Development

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Environment Variables
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```

Edit `.env` and add:
```
BOT_TOKEN=your_bot_token_here
OWNER_ID=your_discord_id_here
```

### 3. Run Locally
```bash
python main.py
```

---

## Deployment (Railway/Fly.io)

### Required Environment Variables

**In Railway/Fly.io dashboard, add these variables:**

1. **`BOT_TOKEN`** (Required)
   - Your Discord bot token
   - Get from: Discord Developer Portal → Applications → Your Bot → Copy TOKEN
   - Example: `MTE2NjUzNjkwMzc2OTEwNTA3.GxX-Xx.XxXxXxXxXxXxXxXxXxXx`

2. **`OWNER_ID`** (Required)
   - Your Discord user ID (who can use owner-only commands)
   - Enable Developer Mode in Discord, right-click your name, copy ID
   - Example: `123456789012345678`

3. **`DATA_DIR`** (Optional)
   - Path to persistent data directory
   - For Railway with volume: `/mnt/data`
   - Defaults to current directory if not set

### Deploy to Railway

1. Create new GitHub repo
2. Upload all files from this folder
3. Go to railway.app → "Deploy from GitHub"
4. Select your repo
5. Add Variables tab:
   - `BOT_TOKEN=your_token`
   - `OWNER_ID=your_id`
6. Deploy

Bot goes live automatically.

### Deploy to Fly.io

1. Install Fly CLI
2. `flyctl auth login`
3. `flyctl launch`
4. Set secrets:
   ```bash
   flyctl secrets set BOT_TOKEN=your_token OWNER_ID=your_id
   ```
5. Deploy: `flyctl deploy`

---

## Commands

All commands are slash commands (`/`).

- `/generate` - Generate account
- `/stock` - View stock
- `/admin` - Admin commands
- `/drops` - Drop system
- And more...

---

## Troubleshooting

**Bot won't start:**
- Check `BOT_TOKEN` is correct and set in variables
- Check logs: Railway → Deploy Logs

**Commands not showing:**
- Restart bot after adding new commands
- Discord client cache: Close Discord completely, reopen

**Data not persisting:**
- Set `DATA_DIR=/mnt/data` and mount volume in Railway

---

## Support

Check logs in Railway dashboard for errors.
