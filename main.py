import os
import time
import re
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
from datetime import datetime, timezone

load_dotenv()

==========================================

1. CONFIGURATION & ENVIRONMENT VARIABLES

==========================================

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
allow_origins=[""],
allow_credentials=True,
allow_methods=[""],
allow_headers=["*"],
)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

==========================================

2. UTILITY FUNCTIONS

==========================================

def get_utc_now() -> str:
"""Returns a valid ISO formatted timestamp for Supabase."""
return datetime.now(timezone.utc).isoformat()

def extract_youtube_video_id(url: str) -> str:
"""Extracts the 11-character video ID from any YouTube URL format."""
if not url:
return ""
url = url.strip()

# If it's already exactly 11 characters with no URL fluff
if len(url) == 11 and " " not in url and "/" not in url:
    return url

# Regex to handle youtube.com/watch?v=ID, youtu.be/ID, youtube.com/live/ID, etc.
match = re.search(r'(?:v=|live/|shorts/|youtu\.be/)([\w-]{11})', url)
if match:
    return match.group(1)
    
return url # Return original as fallback

==========================================

3. GOOGLE OAUTH 2.0 SETUP

==========================================

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

==========================================

4. TOKEN MANAGEMENT (Fixed Expiration Crash)

==========================================

async def get_valid_access_token(handle: str) -> str:
res = supabase.table("streamers").select("access_token, refresh_token, token_expires_at").eq("handle", handle.lower()).execute()
if not res.data:
raise HTTPException(status_code=404, detail="Streamer record not found.")

record = res.data[0]
access_token = record.get("access_token")
refresh_token = record.get("refresh_token")

# Safely parse expires_at to prevent crashes
raw_expires_at = record.get("token_expires_at")
try:
    expires_at = int(float(raw_expires_at)) if raw_expires_at is not None else 0
except (ValueError, TypeError):
    expires_at = 0
    
now = int(time.time())

# Token is still valid (give a 2-minute buffer)
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
    
if token_res.status_code != 200:
    raise HTTPException(status_code=401, detail="Google rejected token refresh. Re-authenticate.")

token_data = token_res.json()
new_access_token = token_data["access_token"]

update_data = {
    "access_token": new_access_token,
    "token_expires_at": now + int(token_data.get("expires_in", 3600)),
    "updated_at": get_utc_now() # Fixed the "now()" bug
}
if "refresh_token" in token_data:
    update_data["refresh_token"] = token_data["refresh_token"]

supabase.table("streamers").update(update_data).eq("handle", handle.lower()).execute()
return new_access_token

==========================================

5. AUTHENTICATION ROUTES

==========================================

@app.get("/login")
async def login(request: Request):
redirect_uri = str(request.url_for('auth_callback')).replace("127.0.0.1", "localhost")
if "localhost" not in redirect_uri:
redirect_uri = redirect_uri.replace("http://", "https://")

# Forced access_type and prompt ensure Google ALWAYS returns a refresh_token
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

try:
    supabase.table("streamers").upsert(record, on_conflict="handle").execute()
    if handle in CACHE:
        del CACHE[handle]
    return RedirectResponse(url=f"/dashboard?user={handle}")
except Exception as db_error:
    return {"CRITICAL_DATABASE_ERROR": str(db_error)}

==========================================

6. LIVE DATA & OVERLAY ROUTES

==========================================

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
    return {"authenticated": False, "error": str(e.detail)}

current_subs = profile.get("current_subs", 0)
likes = 0
yt_viewers = 0

# Extract the 11 character ID safely
video_id = extract_youtube_video_id(profile.get("video_id", ""))

async with httpx.AsyncClient() as client:
    yt_res = await client.get(
        "https://www.googleapis.com/youtube/v3/channels?part=statistics&mine=true",
        headers={"Authorization": f"Bearer {token}"}
    )
    if yt_res.status_code == 200 and yt_res.json().get("items"):
        current_subs = int(yt_res.json()["items"][0]["statistics"].get("subscriberCount", current_subs))
        # Fixed Real-time DB Update
        supabase.table("streamers").update({
            "current_subs": current_subs, 
            "updated_at": get_utc_now()
        }).eq("handle", user).execute()

    if video_id:
        vid_res = await client.get(
            f"https://www.googleapis.com/youtube/v3/videos?part=statistics,liveStreamingDetails&id={video_id}",
            headers={"Authorization": f"Bearer {token}"}
        )
        if vid_res.status_code == 200 and vid_res.json().get("items"):
            item = vid_res.json()["items"][0]
            likes = int(item["statistics"].get("likeCount", 0))
            yt_viewers = int(item.get("liveStreamingDetails", {}).get("concurrentViewers", 0))

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

# Strip the URL to just the ID
raw_video_id = res.data[0].get("video_id", "")
video_id = extract_youtube_video_id(raw_video_id)

try:
    token = await get_valid_access_token(user)
except Exception:
    return {"error": "Auth failed", "pollingIntervalMillis": 15000}

if user not in CACHE:
    CACHE[user] = {}

async with httpx.AsyncClient() as client:
    live_chat_id = CACHE[user].get("live_chat_id")
    
    # Resolve Chat ID if not cached, or if the video ID changed
    if not live_chat_id or CACHE[user].get("cached_video_id") != video_id:
        found_chat_id = None
        
        # Approach 1: Try using the extracted video ID
        if video_id:
            vid_res = await client.get(
                f"https://www.googleapis.com/youtube/v3/videos?part=liveStreamingDetails&id={video_id}",
                headers={"Authorization": f"Bearer {token}"}
            )
            if vid_res.status_code == 200 and vid_res.json().get("items"):
                found_chat_id = vid_res.json()["items"][0].get("liveStreamingDetails", {}).get("activeLiveChatId")

        # Approach 2: If we still don't have it, auto-detect the active broadcast
        if not found_chat_id:
            broadcast_res = await client.get(
                "https://www.googleapis.com/youtube/v3/liveBroadcasts?part=snippet&broadcastStatus=active&broadcastType=all",
                headers={"Authorization": f"Bearer {token}"}
            )
            if broadcast_res.status_code == 200 and broadcast_res.json().get("items"):
                found_chat_id = broadcast_res.json()["items"][0].get("snippet", {}).get("liveChatId")
        
        if not found_chat_id:
            return {"error": "Chat is disabled or stream not live", "pollingIntervalMillis": 15000}
            
        CACHE[user]["live_chat_id"] = found_chat_id
        CACHE[user]["cached_video_id"] = video_id
        live_chat_id = found_chat_id

    # Fetch Messages
    chat_url = f"https://www.googleapis.com/youtube/v3/liveChat/messages?liveChatId={live_chat_id}&part=snippet,authorDetails"
    if pageToken:
        chat_url += f"&pageToken={pageToken}"

    chat_res = await client.get(chat_url, headers={"Authorization": f"Bearer {token}"})
    
    if chat_res.status_code == 200:
        return chat_res.json()
    elif chat_res.status_code == 401:
        # Token issue, clear cache so it retries completely next time
        if "live_chat_id" in CACHE[user]: del CACHE[user]["live_chat_id"]
        return {"error": "Unauthorized", "pollingIntervalMillis": 10000}
    elif chat_res.status_code == 403:
        return {"error": "Quota limit reached", "pollingIntervalMillis": 30000}
    else:
        # Stream might have ended, clear cache to force re-detection next check
        if "live_chat_id" in CACHE[user]: del CACHE[user]["live_chat_id"]
        return {"error": f"API Error {chat_res.status_code}", "pollingIntervalMillis": 10000}

==========================================

7. KICK VIEWER PROXY (OBS CLOUDFLARE BYPASS)

==========================================

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
                count = data["livestream"].get("viewer_count", 0) if data["livestream"] else 0
                return {"viewers": count}
    except:
        pass
        
    try:
        res = await client.get(f"https://api.allorigins.win/get?url=https://kick.com/api/v1/channels/{username}", headers=headers, timeout=5.0)
        if res.status_code == 200:
            import json
            data = json.loads(res.json().get("contents", "{}"))
            if data and "livestream" in data:
                count = data["livestream"].get("viewer_count", 0) if data["livestream"] else 0
                return {"viewers": count}
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
"updated_at": get_utc_now() # Fixed the "now()" bug
}).eq("handle", user).execute()

if not res.data:
    raise HTTPException(status_code=404, detail="Streamer profile not found.")

# Instantly clear cache so the dashboard overlay updates right away
if user in CACHE:
    if "data" in CACHE[user]: del CACHE[user]["data"]
    if "live_chat_id" in CACHE[user]: del CACHE[user]["live_chat_id"]
    
return {"status": "success"}

==========================================

8. STATIC FILES (Vercel Fix)

==========================================

BASE_DIR = os.path.dirname(os.path.abspath(file))
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
