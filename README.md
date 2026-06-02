# Options Scanner Pro — Deployment Guide

## Deploy to Render.com (FREE)

### One-time setup:
1. Create account at https://render.com (free, no credit card)
2. Create new GitHub repo at https://github.com (free)
3. Upload these 3 files to the repo:
   - server.py
   - scanner.html
   - requirements.txt

### Deploy on Render:
1. Go to render.com → New → Web Service
2. Connect your GitHub repo
3. Set these fields:
   - **Name:** options-scanner
   - **Runtime:** Python 3
   - **Build Command:** pip install -r requirements.txt
   - **Start Command:** python server.py
   - **Instance Type:** Free
4. Click Create Web Service
5. Wait 3-5 min for build to finish
6. Your app URL: https://options-scanner.onrender.com

### Important notes:
- Free tier sleeps after 15 min inactivity — first load takes ~30 sec to wake up
- Open your app at 7:55 AM ET to wake it up before your 8:00 AM scan
- Keep the browser tab open during market hours so it stays awake

## Run Locally (no internet needed)
```
pip install flask yfinance pandas
python server.py
```
Then open http://localhost:8765
