import os
import re
import time
import httpx
import asyncio

from typing import Optional, Any
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware
from authlib.integrations.starlette_client import OAuth
from supabase import create_client, Client
from dotenv import load_dotenv

# ==========================================
# 1. CONFIGURATION & ENVIRONMENT VARIABLES
# ==========================================
load_dotenv()

SESSION_SECRET = os.getenv("SESSION_SECRET", "super-secure-random-token-xyz")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Polling Limits (Protects YouTube Quotas)
STATS_REFRESH_SECONDS = 5  # Fetch new likes/subs/viewers every 5 seconds
DB_UPDATE_SECONDS = 60     # Only write stats to Supabase once a minute to prevent DB lag

app = FastAPI()

# FIX: Regex ".*" bypasses the FastAPI wildcard crash while allowing credentials for Auth
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Server-Side Cache to prevent API Spam
CACHE: dict[str, dict[str, Any]] = {}

# ==========================================
# 2. UTILITY FUNCTIONS
# ==========================================
def get_utc_now() -> str:
    """Returns valid ISO formatted timestamp for Supabase."""
    return datetime.now(timezone.utc).isoformat()

def extract_youtube_video_id(url: str) -> str:
    """Extracts the 11-character video ID safely."""
    if not url: return ""
    url = url.strip()
    if len(url) == 11 and " " not in url and "/" not in url:
        return url
    match = re.search(r'(?:v=|live/|shorts/|youtu\.be/)([\w-]{11})', url)
    return match.group(1) if match else url

# ==========================================
# 3. GOOGLE OAUTH 2.0 SETUP
# ==========================================
oauth = OAuth()
oauth.register(
    name='google',
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={
        'scope': 'openid email profile https://www.googleapis.com/auth/youtube.readonly',
    }
)

class SettingsPayload(BaseModel):
    sub_goal: int
    video_id: Optional[str] = ""
    ticker_text: Optional[str] = ""
    twitch_user: Optional[str] = ""
    kick_user: Optional[str] = ""

# ==========================================
# 4. BULLETPROOF TOKEN MANAGEMENT
# ==========================================
async def get_valid_access_token(handle: str, force_refresh: bool = False) -> str:
    user = handle.lower().strip()
    res = supabase.table("streamers").select("access_token, refresh_token, token_expires_at").eq("handle", user).execute()
    
    if not res.data:
        raise HTTPException(status_code=404, detail="Streamer record not found.")
    
    record = res.data[0]
    access_token = record.get("access_token")
    refresh_token = record.get("refresh_token")
    
    try:
        expires_at = int(float(record.get("token_expires_at", 0)))
    except (ValueError, TypeError):
        expires_at = 0
        
    now = int(time.time())
    
    # If token is valid for at least 2 more minutes, return it
    if not force_refresh and now < (expires_at - 120) and access_token:
        return access_token

    if not refresh_token:
        raise HTTPException(status_code=401, detail="No refresh token available. User must re-authenticate.")

    # Token is expired or forced to refresh. Request a new one.
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
        
    if token_res.status_code != 200:
        raise HTTPException(status_code=401, detail="Google rejected token refresh. User must re-authenticate.")

    token_data = token_res.json()
    new_access_token = token_data["access_token"]
    
    update_data = {
        "access_token": new_access_token,
        "token_expires_at": now + int(token_data.get("expires_in", 3600)),
        "updated_at": get_utc_now()
    }
    
    # Google rarely sends a new refresh token, but if they do, save it
    if "refresh_token" in token_data:
        update_data["refresh_token"] = token_data["refresh_token"]

    supabase.table("streamers").update(update_data).eq("handle", user).execute()
    return new_access_token

async def fetch_youtube_api(handle: str, url: str, params: dict):
    """Wrapper that automatically handles 401 Expired Tokens mid-stream."""
    token = await get_valid_access_token(handle)
    async with httpx.AsyncClient() as client:
        res = await client.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
        
        # If Google suddenly rejects the token mid-stream, force a refresh and try exactly once more
        if res.status_code == 401:
            token = await get_valid_access_token(handle, force_refresh=True)
            res = await client.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
            
        return res

# ==========================================
# 5. AUTHENTICATION ROUTES
# ==========================================
@app.get("/login")
async def login(request: Request):
    redirect_uri = str(request.url_for('auth_callback')).replace("127.0.0.1", "localhost")
    if "localhost" not in redirect_uri:
        redirect_uri = redirect_uri.replace("http://", "https://")
    
    # access_type and prompt guarantee we get the crucial offline refresh_token
    return await oauth.google.authorize_redirect(
        request, 
        redirect_uri,
        access_type='offline',
        prompt='consent'
    )

@app.get("/auth/callback")
async def auth_callback(request: Request):
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception as e:
        return {"error": "Google Auth Denied", "details": str(e)}
        
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
    thumbnails = channel["snippet"].get("thumbnails", {})
    avatar_url = thumbnails.get("medium", thumbnails.get("default", {})).get("url", "")

    record = {
        "handle": handle,
        "channel_id": channel["id"],
        "title": channel["snippet"]["title"],
        "avatar": avatar_url,
        "access_token": access_token,
        "token_expires_at": expires_at,
        "current_subs": int(channel["statistics"].get("subscriberCount", 0)),
        "updated_at": get_utc_now()
    }
    if refresh_token:
        record["refresh_token"] = refresh_token

    supabase.table("streamers").upsert(record, on_conflict="handle").execute()
    CACHE.pop(handle, None) # Clear old cache
    
    return RedirectResponse(url=f"/dashboard?user={handle}")

# ==========================================
# 6. LIVE DATA & OVERLAY ROUTES (Fully Real-Time)
# ==========================================
@app.get("/api/streamer/{handle}")
async def get_live_overlay_data(handle: str, response: Response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    user = handle.strip().lower()
    now = time.time()
    
    # Init cache structure for user
    if user not in CACHE:
        CACHE[user] = {"data": {}, "last_fetched": 0, "last_db_update": 0}
        
    # Return quick cache if we fetched YouTube within the last 5 seconds (Protects Quota)
    if (now - CACHE[user]["last_fetched"]) < STATS_REFRESH_SECONDS and CACHE[user]["data"]:
        return CACHE[user]["data"]

    # 1. Load Profile
    db_res = supabase.table("streamers").select("*").eq("handle", user).execute()
    if not db_res.data:
        return {"authenticated": False}
    profile = db_res.data[0]

    # Initialize stats
    current_subs = profile.get("current_subs", 0)
    likes = 0
    yt_viewers = 0
    video_id = extract_youtube_video_id(profile.get("video_id", ""))

    try:
        # 2. Fetch Real-time Subscribers
        sub_res = await fetch_youtube_api(user, "https://www.googleapis.com/youtube/v3/channels", {"part": "statistics", "mine": "true"})
        if sub_res.status_code == 200 and sub_res.json().get("items"):
            current_subs = int(sub_res.json()["items"][0]["statistics"].get("subscriberCount", current_subs))

        # 3. Auto-Detect Video ID if it's missing (Fixes the Likes = 0 bug)
        if not video_id:
            broadcast_res = await fetch_youtube_api(user, "https://www.googleapis.com/youtube/v3/liveBroadcasts", {"part": "snippet", "broadcastStatus": "active", "broadcastType": "all"})
            if broadcast_res.status_code == 200 and broadcast_res.json().get("items"):
                video_id = broadcast_res.json()["items"][0]["snippet"]["id"]

        # 4. Fetch Real-time Likes and Viewers
        if video_id:
            vid_res = await fetch_youtube_api(user, "https://www.googleapis.com/youtube/v3/videos", {"part": "statistics,liveStreamingDetails", "id": video_id})
            if vid_res.status_code == 200 and vid_res.json().get("items"):
                item = vid_res.json()["items"][0]
                likes = int(item["statistics"].get("likeCount", 0))
                yt_viewers = int(item.get("liveStreamingDetails", {}).get("concurrentViewers", 0))

        # 5. Prevent Supabase Database rate-limiting by only pushing stats once every 60 seconds
        if (now - CACHE[user]["last_db_update"]) > DB_UPDATE_SECONDS:
            supabase.table("streamers").update({
                "current_subs": current_subs,
                "updated_at": get_utc_now()
            }).eq("handle", user).execute()
            CACHE[user]["last_db_update"] = now

        # Compile final response
        response_data = {
            "authenticated": True,
            "title": profile.get("title", ""),
            "avatar": profile.get("avatar", ""),
            "subs": current_subs,
            "likes": likes,
            "yt_viewers": yt_viewers,
            "sub_goal": profile.get("sub_goal") if profile.get("sub_goal") else 5000,
            "video_id": video_id,
            "ticker_text": profile.get("ticker_text", ""),
            "twitch_user": profile.get("twitch_user", ""),
            "kick_user": profile.get("kick_user", "")
        }

        # Save to memory cache
        CACHE[user]["data"] = response_data
        CACHE[user]["last_fetched"] = now

        return response_data
        
    except HTTPException as e:
        return {"authenticated": False, "error": str(e.detail)}
    except Exception as e:
        # Fallback to cache if a random network error occurs
        if CACHE[user]["data"]:
            return CACHE[user]["data"]
        return {"authenticated": False, "error": "Internal Server Error during fetch."}

@app.get("/api/streamer/{handle}/chat")
async def get_live_chat(handle: str, response: Response, pageToken: str = ""):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    user = handle.strip().lower()
    
    db_res = supabase.table("streamers").select("video_id").eq("handle", user).execute()
    if not db_res.data:
        return {"error": "Not found", "pollingIntervalMillis": 10000}

    video_id = extract_youtube_video_id(db_res.data[0].get("video_id", ""))

    if user not in CACHE:
        CACHE[user] = {}

    live_chat_id = CACHE[user].get("live_chat_id")
    
    try:
        # 1. Resolve Chat ID if not cached, or if the video ID changed
        if not live_chat_id or CACHE[user].get("cached_video_id") != video_id:
            found_chat_id = None
            
            # Approach A: Try using the provided video ID
            if video_id:
                vid_res = await fetch_youtube_api(user, "https://www.googleapis.com/youtube/v3/videos", {"part": "liveStreamingDetails", "id": video_id})
                if vid_res.status_code == 200 and vid_res.json().get("items"):
                    found_chat_id = vid_res.json()["items"][0].get("liveStreamingDetails", {}).get("activeLiveChatId")

            # Approach B: Auto-detect active broadcast if video ID failed or was empty
            if not found_chat_id:
                broadcast_res = await fetch_youtube_api(user, "https://www.googleapis.com/youtube/v3/liveBroadcasts", {"part": "snippet", "broadcastStatus": "active", "broadcastType": "all"})
                if broadcast_res.status_code == 200 and broadcast_res.json().get("items"):
                    found_chat_id = broadcast_res.json()["items"][0].get("snippet", {}).get("liveChatId")
            
            if not found_chat_id:
                return {"error": "Chat is disabled or stream not live", "pollingIntervalMillis": 15000}
                
            CACHE[user]["live_chat_id"] = found_chat_id
            CACHE[user]["cached_video_id"] = video_id
            live_chat_id = found_chat_id

        # 2. Fetch Messages
        params = {"liveChatId": live_chat_id, "part": "snippet,authorDetails"}
        if pageToken:
            params["pageToken"] = pageToken
            
        chat_res = await fetch_youtube_api(user, "https://www.googleapis.com/youtube/v3/liveChat/messages", params)
        
        if chat_res.status_code == 200:
            return chat_res.json()
        elif chat_res.status_code == 403:
            return {"error": "Quota limit reached", "pollingIntervalMillis": 30000}
        else:
            # Clear cache to force a re-detection on the next check
            CACHE[user].pop("live_chat_id", None)
            return {"error": f"API Error {chat_res.status_code}", "pollingIntervalMillis": 10000}

    except Exception:
        return {"error": "Auth failed", "pollingIntervalMillis": 15000}

# ==========================================
# 7. KICK VIEWER PROXY (OBS CLOUDFLARE BYPASS)
# ==========================================
@app.get("/api/kick_viewers/{username}")
async def get_kick_viewers(username: str, response: Response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }
    
    async with httpx.AsyncClient() as client:
        try:
            res = await client.get(f"https://api.codetabs.com/v1/proxy?quest=https://kick.com/api/v1/channels/{username}", headers=headers, timeout=5.0)
            if res.status_code == 200:
                data = res.json()
                if data and "livestream" in data:
                    return {"viewers": data["livestream"].get("viewer_count", 0) if data["livestream"] else 0}
        except:
            pass
            
        try:
            res = await client.get(f"https://api.allorigins.win/get?url=https://kick.com/api/v1/channels/{username}", headers=headers, timeout=5.0)
            if res.status_code == 200:
                import json
                data = json.loads(res.json().get("contents", "{}"))
                if data and "livestream" in data:
                    return {"viewers": data["livestream"].get("viewer_count", 0) if data["livestream"] else 0}
        except:
            pass

    return {"viewers": 0}

@app.post("/api/streamer/{handle}/settings")
async def save_streamer_settings(handle: str, payload: SettingsPayload):
    user = handle.strip().lower()
    res = supabase.table("streamers").update({
        "sub_goal": payload.sub_goal,
        "video_id": payload.video_id,
        "ticker_text": payload.ticker_text,
        "twitch_user": payload.twitch_user,
        "kick_user": payload.kick_user,
        "updated_at": get_utc_now()
    }).eq("handle", user).execute()
    
    if not res.data:
        raise HTTPException(status_code=404, detail="Streamer profile not found.")

    # Instantly clear cache so the dashboard overlay updates right away
    CACHE.pop(user, None)
    return {"status": "success"}

# ==========================================
# 8. STATIC FILES (Vercel Fix)
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
