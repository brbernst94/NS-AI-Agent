# Deploying NS-AI-Agent to Railway

Your NetSuite Migration AI Agent is ready to deploy to Railway for cloud access!

## Quick Setup (5 minutes)

### 1. **Connect Railway to GitHub**

1. Go to [railway.app](https://railway.app)
2. Sign in with your Railway account
3. Click **"New Project"**
4. Select **"Deploy from GitHub"**
5. Connect your GitHub account
6. Select the `NS-AI-Agent` repository
7. Click **"Deploy Now"**

Railway will automatically:
- Build the Docker image
- Install all dependencies
- Start the FastAPI backend + Streamlit web UI
- Give you a public URL

### 2. **Set Environment Variables**

Once the project is created, go to **Variables** and add:

```
ANTHROPIC_API_KEY = your_api_key_here
```

(Get your API key from https://console.anthropic.com)

That's it! Railway handles everything else.

### 3. **Access Your Agent**

Railway will give you a URL like:
```
https://ns-ai-agent-production.up.railway.app
```

You can now:
- Access it from **any device** (phone, tablet, laptop)
- Share the URL with **team members**
- Train it together in the cloud

## How It Works

**Deployment Architecture:**
```
GitHub (code + training data)
    ↓
Railway (builds & runs)
    ↓
FastAPI Backend (port 8000)
    + Streamlit UI (port 8501)
    ↓
Public URL (accessible from anywhere)
```

**Your data:**
- Training data (uploaded documents) → Backed up to GitHub
- Conversations → Stored in Railway's filesystem
- Projects → Stored in Railway's database

## Updating Your Agent

After you train the agent or make changes:

1. Push to GitHub:
```bash
git add .
git commit -m "Trained agent with new documents"
git push origin claude/funny-ride-Oq16Q
```

2. Railway automatically redeploys within 1 minute
3. Your public URL stays the same
4. All your training data persists

## Important Notes

### Cold Starts
- First request may take 10-15 seconds (Railway starting the container)
- Subsequent requests are instant

### Data Persistence
- Training data (ChromaDB) is stored on Railway's filesystem
- Conversations persist across restarts
- If you need permanent storage, upgrade to a paid Railway plan

### Cost
- **Free tier**: 500 hours/month (enough for continuous use)
- **Paid tier**: $5/month for additional hours + databases

## Troubleshooting

**"API Offline" on the web page?**
- Wait 30 seconds for Railway to start all services
- Check Railway Dashboard for build/deploy errors

**Can't upload large files?**
- Default limit is 200MB
- Edit `start.sh` and change `maxUploadSize` if needed

**Want to run locally again?**
```bash
# Stop the cloud app
# Then locally:
python -m api.main  # Terminal 1
streamlit run web/app.py  # Terminal 2
```

## Next Steps

1. Deploy to Railway (follow steps above)
2. Share the public URL with your team
3. Start training the agent with your NetSuite knowledge
4. All your training data syncs to GitHub automatically

---

**Questions?** Check the [Railway docs](https://docs.railway.app) or see CLAUDE.md for more about the agent itself.
