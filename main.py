import os
import time
import httpx
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware
from authlib.integrations.starlette_client import OAuth
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# 1. CONFIGURATION & ENVIRONMENT VARIABLES
# ==========================================

SESSION_SECRET = os.getenv("SESSION_SECRET", "super-secure-random-token-xyz")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

CACHE = {}
CACHE_TTL = 5  

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ==========================================
# 2. GOOGLE OAUTH 2.0 SETUP
# ==========================================

oauth = OAuth()
oauth.register(
    name='google',
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={
        'scope': 'openid email profile https://www.googleapis.com/auth/youtube.readonly',
        'access_type': 'offline',
        'prompt': 'consent'
    }
)

class SettingsPayload(BaseModel):
    sub_goal: int
    video_id: Optional[str] = ""
    ticker_text: Optional[str] = ""
    twitch_user: Optional[str] = ""
    kick_user: Optional[str] = ""

# ==========================================
# 3. TOKEN MANAGEMENT
# ==========================================

async def get_valid_access_token(handle: str) -> str:
    res = supabase.table("streamers").select("access_token, refresh_token, token_expires_at").eq("handle", handle.lower()).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Streamer record not found.")

    record = res.data[0]
    access_token = record.get("access_token")
    refresh_token = record.get("refresh_token")
    expires_at = record.get("token_expires_at", 0)
    now = int(time.time())
    
    if now < (expires_at - 120) and access_token:
        return access_token

    if not refresh_token:
        raise HTTPException(status_code=401, detail="No refresh token available. Re-authenticate.")

    async with httpx.AsyncClient() as client:
        token_res = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }
        )
        token_data = token_res.json()

    if token_res.status_code != 200:
        raise HTTPException(status_code=401, detail="Google rejected token refresh. Re-authenticate.")

    new_access_token = token_data["access_token"]
    update_data = {
        "access_token": new_access_token,
        "token_expires_at": now + token_data.get("expires_in", 3600),
        "updated_at": "now()"
    }
    if "refresh_token" in token_data:
        update_data["refresh_token"] = token_data["refresh_token"]

    supabase.table("streamers").update(update_data).eq("handle", handle.lower()).execute()
    return new_access_token

# ==========================================
# 4. AUTHENTICATION ROUTES
# ==========================================

@app.get("/login")
async def login(request: Request):
    redirect_uri = str(request.url_for('auth_callback')).replace("127.0.0.1", "localhost")
    if "localhost" not in redirect_uri:
        redirect_uri = redirect_uri.replace("http://", "https://")
    return await oauth.google.authorize_redirect(request, redirect_uri)

@app.get("/auth/callback")
async def auth_callback(request: Request):
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception:
        return RedirectResponse(url="/dashboard?error=auth_denied")

    access_token = token.get('access_token')
    refresh_token = token.get('refresh_token')
    expires_at = token.get('expires_at', int(time.time()) + 3600)

    async with httpx.AsyncClient() as client:
        res = await client.get(
            "https://www.googleapis.com/youtube/v3/channels?part=snippet,statistics&mine=true",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        data = res.json()

    if not data.get("items"):
        return RedirectResponse(url="/dashboard?error=no_channel")

    channel = data["items"][0]
    handle = channel["snippet"].get("customUrl", channel["snippet"]["title"]).replace("@", "").lower()

    record = {
        "handle": handle,
        "channel_id": channel["id"],
        "title": channel["snippet"]["title"],
        "avatar": channel["snippet"]["thumbnails"]["medium"]["url"],
        "access_token": access_token,
        "token_expires_at": expires_at,
        "current_subs": int(channel["statistics"].get("subscriberCount", 0)),
        "updated_at": "now()"
    }
    if refresh_token:
        record["refresh_token"] = refresh_token

    supabase.table("streamers").upsert(record, on_conflict="handle").execute()
    if handle in CACHE:
        del CACHE[handle]

    return RedirectResponse(url=f"/dashboard?user={handle}")

# ==========================================
# 5. LIVE DATA & OVERLAY ROUTES
# ==========================================

@app.get("/api/streamer/{handle}")
async def get_live_overlay_data(handle: str, response: Response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    user = handle.strip().lower()
    now = time.time()

    if user in CACHE and "data" in CACHE[user] and CACHE[user]["expires_at"] > now:
        return CACHE[user]["data"]

    res = supabase.table("streamers").select("*").eq("handle", user).execute()
    if not res.data:
        return {"authenticated": False}

    profile = res.data[0]

    try:
        token = await get_valid_access_token(user)
    except HTTPException as e:
        return {"authenticated": False, "error": e.detail}

    current_subs = profile.get("current_subs", 0)
    likes = 0
    yt_viewers = 0
    video_id = profile.get("video_id")

    async with httpx.AsyncClient() as client:
        yt_res = await client.get(
            "https://www.googleapis.com/youtube/v3/channels?part=statistics&mine=true",
            headers={"Authorization": f"Bearer {token}"}
        )
        if yt_res.status_code == 200 and yt_res.json().get("items"):
            current_subs = int(yt_res.json()["items"][0]["statistics"].get("subscriberCount", current_subs))
            supabase.table("streamers").update({"current_subs": current_subs, "updated_at": "now()"}).eq("handle", user).execute()

        if video_id:
            # Added liveStreamingDetails to fetch exact concurrent viewers
            vid_res = await client.get(
                f"https://www.googleapis.com/youtube/v3/videos?part=statistics,liveStreamingDetails&id={video_id}",
                headers={"Authorization": f"Bearer {token}"}
            )
            if vid_res.status_code == 200 and vid_res.json().get("items"):
                item = vid_res.json()["items"][0]
                likes = int(item["statistics"].get("likeCount", 0))
                # Safely extract YouTube viewers
                yt_viewers = int(item.get("liveStreamingDetails", {}).get("concurrentViewers", 0))

    response_data = {
        "authenticated": True,
        "title": profile["title"],
        "avatar": profile["avatar"],
        "subs": current_subs,
        "likes": likes,
        "yt_viewers": yt_viewers, # Now passed to the frontend
        "sub_goal": profile["sub_goal"] if profile["sub_goal"] else 5000,
        "video_id": video_id,
        "ticker_text": profile["ticker_text"],
        "twitch_user": profile.get("twitch_user", ""),
        "kick_user": profile.get("kick_user", "")
    }

    if user not in CACHE:
        CACHE[user] = {}
    CACHE[user]["data"] = response_data
    CACHE[user]["expires_at"] = now + CACHE_TTL

    return response_data

@app.get("/api/streamer/{handle}/chat")
async def get_live_chat(handle: str, response: Response, pageToken: str = ""):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    user = handle.strip().lower()
    
    res = supabase.table("streamers").select("video_id").eq("handle", user).execute()
    if not res.data:
        return {"error": "Not found", "pollingIntervalMillis": 10000}
    
    video_id = res.data[0].get("video_id")
    if not video_id:
        return {"error": "No stream URL", "pollingIntervalMillis": 15000}

    try:
        token = await get_valid_access_token(user)
    except Exception:
        return {"error": "Auth failed", "pollingIntervalMillis": 15000}

    if user not in CACHE:
        CACHE[user] = {}

    async with httpx.AsyncClient() as client:
        if "live_chat_id" not in CACHE[user] or CACHE[user].get("cached_video_id") != video_id:
            vid_res = await client.get(
                f"https://www.googleapis.com/youtube/v3/videos?part=liveStreamingDetails&id={video_id}",
                headers={"Authorization": f"Bearer {token}"}
            )
            if vid_res.status_code == 200 and vid_res.json().get("items"):
                details = vid_res.json()["items"][0].get("liveStreamingDetails", {})
                live_chat_id = details.get("activeLiveChatId")
                if not live_chat_id:
                    return {"error": "Chat is disabled", "pollingIntervalMillis": 15000}
                CACHE[user]["live_chat_id"] = live_chat_id
                CACHE[user]["cached_video_id"] = video_id
            else:
                return {"error": "Failed to resolve live chat", "pollingIntervalMillis": 10000}

        live_chat_id = CACHE[user]["live_chat_id"]
        chat_url = f"https://www.googleapis.com/youtube/v3/liveChat/messages?liveChatId={live_chat_id}&part=snippet,authorDetails"
        if pageToken:
            chat_url += f"&pageToken={pageToken}"

        chat_res = await client.get(chat_url, headers={"Authorization": f"Bearer {token}"})
        if chat_res.status_code == 200:
            return chat_res.json()
        elif chat_res.status_code == 403:
            return {"error": "Quota limit reached", "pollingIntervalMillis": 30000}
        else:
            return {"error": "API Error", "pollingIntervalMillis": 10000}

# Bypass Kick CORS Protection
@app.get("/api/kick_id/{kick_username}")
async def get_kick_id(kick_username: str):
    async with httpx.AsyncClient() as client:
        res = await client.get(f"https://kick.com/api/v1/channels/{kick_username}", headers={"User-Agent": "Mozilla/5.0"})
        if res.status_code == 200:
            data = res.json()
            return {"chatroom_id": data.get("chatroom", {}).get("id")}
    return {"error": "Failed to connect to Kick API"}

@app.post("/api/streamer/{handle}/settings")
async def save_streamer_settings(handle: str, payload: SettingsPayload):
    user = handle.strip().lower()
    res = supabase.table("streamers").update({
        "sub_goal": payload.sub_goal,
        "video_id": payload.video_id,
        "ticker_text": payload.ticker_text,
        "twitch_user": payload.twitch_user,
        "kick_user": payload.kick_user,
        "updated_at": "now()"
    }).eq("handle", user).execute()

    if not res.data:
        raise HTTPException(status_code=404, detail="Streamer profile not found.")
    
    if user in CACHE:
        CACHE[user] = {} 
        
    return {"status": "success"}

# ==========================================
# 6. STATIC FILES (Vercel Fix)
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC_DIR = os.path.join(BASE_DIR, "public")

if os.path.exists(PUBLIC_DIR):
    @app.get("/dashboard")
    async def serve_dashboard():
        return FileResponse(os.path.join(PUBLIC_DIR, "dashboard.html"))

    @app.get("/overlay")
    async def serve_overlay():
        return FileResponse(os.path.join(PUBLIC_DIR, "overlay.html"))
        
    @app.get("/chat")
    async def serve_chat():
        return FileResponse(os.path.join(PUBLIC_DIR, "chat-overlay.html"))

    app.mount("/", StaticFiles(directory=PUBLIC_DIR, html=True), name="public")
