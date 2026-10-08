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

# ==========================================
# 1. CONFIGURATION & ENVIRONMENT VARIABLES
# ==========================================
SESSION_SECRET = os.getenv("SESSION_SECRET", "super-secure-random-token-xyz")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# ==========================================
# QUOTA PROTECTION ENGINE (Guaranteed 10+ Hours)
# Stats update every 30s | Subs every 5m | Chat minimum 6s
# ==========================================
CACHE = {}
STATS_CACHE_TTL = 30   
SUBS_CACHE_TTL = 300   

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def get_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def extract_youtube_video_id(url: str) -> str:
    if not url: return ""
    url = url.strip()
    if len(url) == 11 and " " not in url and "/" not in url: return url
    match = re.search(r'(?:v=|live/|shorts/|youtu\.be/)([\w-]{11})', url)
    if match: return match.group(1)
    return url 

oauth = OAuth()
oauth.register(
    name='google',
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile https://www.googleapis.com/auth/youtube.readonly'}
)

class SettingsPayload(BaseModel):
    sub_goal: int
    video_id: Optional[str] = ""
    ticker_text: Optional[str] = ""
    twitch_user: Optional[str] = ""
    kick_user: Optional[str] = ""

async def get_valid_access_token(handle: str, force_refresh: bool = False) -> str:
    res = supabase.table("streamers").select("*").eq("handle", handle.lower()).execute()
    if not res.data: raise HTTPException(status_code=404, detail="Streamer not found.")
    
    record = res.data[0]
    access_token, refresh_token = record.get("access_token"), record.get("refresh_token")
    
    try: expires_at = int(float(record.get("token_expires_at") or 0))
    except (ValueError, TypeError): expires_at = 0
        
    now = int(time.time())
    
    if not force_refresh and now < (expires_at - 120) and access_token:
        return access_token

    if not refresh_token:
        raise HTTPException(status_code=401, detail="No refresh token available.")

    async with httpx.AsyncClient() as client:
        token_res = await client.post(GOOGLE_TOKEN_URL, data={
            "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET, 
            "refresh_token": refresh_token, "grant_type": "refresh_token"
        })
        
    if token_res.status_code != 200: raise HTTPException(status_code=401, detail="Google rejected token refresh.")
    
    token_data = token_res.json()
    new_access_token = token_data["access_token"]
    update_data = {
        "access_token": new_access_token, 
        "token_expires_at": now + int(token_data.get("expires_in", 3600)), 
        "updated_at": get_utc_now()
    }
    if "refresh_token" in token_data: update_data["refresh_token"] = token_data["refresh_token"]
    
    supabase.table("streamers").update(update_data).eq("handle", handle.lower()).execute()
    return new_access_token

async def safe_youtube_api_get(url: str, handle: str) -> httpx.Response:
    token = await get_valid_access_token(handle)
    async with httpx.AsyncClient() as client:
        res = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        if res.status_code == 401:
            token = await get_valid_access_token(handle, force_refresh=True)
            res = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        return res

@app.get("/login")
async def login(request: Request):
    redirect_uri = str(request.url_for('auth_callback')).replace("127.0.0.1", "localhost").replace("http://", "https://")
    return await oauth.google.authorize_redirect(request, redirect_uri, access_type='offline', prompt='consent')

@app.get("/auth/callback")
async def auth_callback(request: Request):
    try: token = await oauth.google.authorize_access_token(request)
    except Exception as e: return {"error": "Google Auth Denied", "details": str(e)}

    access_token, refresh_token = token.get('access_token'), token.get('refresh_token')
    expires_at = token.get('expires_at', int(time.time()) + 3600)

    async with httpx.AsyncClient() as client:
        res = await client.get("https://www.googleapis.com/youtube/v3/channels?part=snippet,statistics&mine=true", headers={"Authorization": f"Bearer {access_token}"})
        data = res.json()

    if not data.get("items"): return RedirectResponse(url="/dashboard?error=no_channel")
    channel = data["items"][0]
    handle = channel["snippet"].get("customUrl", channel["snippet"]["title"]).replace("@", "").lower()
    avatar_url = channel["snippet"].get("thumbnails", {}).get("medium", {}).get("url", "")

    record = {
        "handle": handle, "channel_id": channel["id"], "title": channel["snippet"]["title"],
        "avatar": avatar_url, "access_token": access_token, "token_expires_at": expires_at,
        "current_subs": int(channel["statistics"].get("subscriberCount", 0)), "updated_at": get_utc_now()
    }
    if refresh_token: record["refresh_token"] = refresh_token
    
    supabase.table("streamers").upsert(record, on_conflict="handle").execute()
    if handle in CACHE: del CACHE[handle]
    return RedirectResponse(url=f"/dashboard?user={handle}")

# ==========================================
# 6. LIVE DATA (STATS QUOTA FIREWALL)
# ==========================================
@app.get("/api/streamer/{handle}")
async def get_live_overlay_data(handle: str, response: Response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    user = handle.strip().lower()
    now = time.time()
    user_cache = CACHE.setdefault(user, {})

    if now < user_cache.get("stats_expires_at", 0) and "stats_data" in user_cache:
        return user_cache["stats_data"]

    res = supabase.table("streamers").select("*").eq("handle", user).execute()
    if not res.data: return {"authenticated": False}
    profile = res.data[0]
    
    # Iron-Clad Fallbacks
    current_subs = profile.get("current_subs") or 0
    likes = profile.get("last_likes") or 0
    yt_viewers = user_cache.get("last_yt_viewers", 0)
    video_id = extract_youtube_video_id(profile.get("video_id") or "")

    try:
        # SUBS API
        if now > user_cache.get("subs_expires_at", 0):
            yt_res = await safe_youtube_api_get("https://www.googleapis.com/youtube/v3/channels?part=statistics&mine=true", user)
            if yt_res.status_code == 200 and yt_res.json().get("items"):
                current_subs = int(yt_res.json()["items"][0]["statistics"].get("subscriberCount", current_subs))
            user_cache["subs_expires_at"] = now + SUBS_CACHE_TTL

        # LIKES & VIEWERS API
        if video_id:
            vid_res = await safe_youtube_api_get(f"https://www.googleapis.com/youtube/v3/videos?part=statistics,liveStreamingDetails&id={video_id}", user)
            if vid_res.status_code == 200 and vid_res.json().get("items"):
                item = vid_res.json()["items"][0]
                
                api_likes = int(item["statistics"].get("likeCount", 0))
                if api_likes > likes: likes = api_likes # Never let likes drop
                
                live_details = item.get("liveStreamingDetails", {})
                if "concurrentViewers" in live_details:
                    api_viewers = int(live_details["concurrentViewers"])
                    if api_viewers > 0: # Never let viewers randomly drop to 0
                        yt_viewers = api_viewers
                        user_cache["last_yt_viewers"] = yt_viewers
                
                user_cache["live_chat_id"] = live_details.get("activeLiveChatId")

        # Background Update
        try:
            supabase.table("streamers").update({
                "current_subs": current_subs, "last_likes": likes, "updated_at": get_utc_now()
            }).eq("handle", user).execute()
        except Exception: pass

    except Exception as e: print(f"API Error: {e}")

    response_data = {
        "authenticated": True, "title": profile.get("title") or "", "avatar": profile.get("avatar") or "",
        "subs": current_subs, "likes": likes, "yt_viewers": yt_viewers,
        "sub_goal": profile.get("sub_goal") if profile.get("sub_goal") else 5000, "video_id": video_id,
        "ticker_text": profile.get("ticker_text") or "", "twitch_user": profile.get("twitch_user") or "", "kick_user": profile.get("kick_user") or ""
    }

    user_cache["stats_data"] = response_data
    user_cache["stats_expires_at"] = now + STATS_CACHE_TTL
    return response_data

# ==========================================
# 7. LIVE CHAT (CHAT QUOTA FIREWALL)
# ==========================================
@app.get("/api/streamer/{handle}/chat")
async def get_live_chat(handle: str, response: Response, pageToken: str = ""):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    user = handle.strip().lower()
    now = time.time()
    user_cache = CACHE.setdefault(user, {})

    next_poll = user_cache.get("chat_next_poll", 0)
    if now < next_poll:
        return {"items": [], "nextPageToken": pageToken, "pollingIntervalMillis": max(int((next_poll - now) * 1000), 6000)}

    res = supabase.table("streamers").select("video_id").eq("handle", user).execute()
    if not res.data: return {"error": "Not found", "pollingIntervalMillis": 10000}
    video_id = extract_youtube_video_id(res.data[0].get("video_id") or "")
    if not video_id: return {"error": "No Video ID", "pollingIntervalMillis": 10000}

    live_chat_id = user_cache.get("live_chat_id")
    chat_resolve_cooldown = user_cache.get("chat_resolve_cooldown", 0)

    if not live_chat_id or user_cache.get("cached_video_id") != video_id:
        if now < chat_resolve_cooldown:
            return {"error": "Chat disabled or stream not live", "pollingIntervalMillis": 15000}
            
        vid_res = await safe_youtube_api_get(f"https://www.googleapis.com/youtube/v3/videos?part=liveStreamingDetails&id={video_id}", user)
        if vid_res.status_code == 200 and vid_res.json().get("items"):
            live_chat_id = vid_res.json()["items"][0].get("liveStreamingDetails", {}).get("activeLiveChatId")
            
        if not live_chat_id: 
            user_cache["chat_resolve_cooldown"] = now + 300 
            return {"error": "Chat disabled or stream not live", "pollingIntervalMillis": 15000}
            
        user_cache["live_chat_id"] = live_chat_id
        user_cache["cached_video_id"] = video_id
        user_cache["chat_resolve_cooldown"] = 0 

    chat_url = f"https://www.googleapis.com/youtube/v3/liveChat/messages?liveChatId={live_chat_id}&part=snippet,authorDetails"
    if pageToken: chat_url += f"&pageToken={pageToken}"

    chat_res = await safe_youtube_api_get(chat_url, user)
    
    if chat_res.status_code == 200:
        data = chat_res.json()
        # HARD QUOTA ENFORCEMENT: Never poll faster than 6 seconds (600 calls/hr max)
        interval_ms = max(data.get("pollingIntervalMillis", 6000), 6000)
        user_cache["chat_next_poll"] = time.time() + (interval_ms / 1000.0)
        data["pollingIntervalMillis"] = interval_ms
        return data
    elif chat_res.status_code == 403:
        user_cache["chat_next_poll"] = time.time() + 60 
        return {"error": "Quota limit reached", "pollingIntervalMillis": 60000}
    else:
        if "live_chat_id" in user_cache: del user_cache["live_chat_id"]
        return {"error": f"API Error {chat_res.status_code}", "pollingIntervalMillis": 10000}

# ==========================================
# 8. KICK VIEWER PROXY (Zero-Drop Cache)
# ==========================================
@app.get("/api/kick_viewers/{username}")
async def get_kick_viewers(username: str, response: Response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    
    now = time.time()
    cache_key = f"kick_{username}"
    kick_cache = CACHE.setdefault(cache_key, {"viewers": 0, "expires": 0})
    
    if now < kick_cache["expires"]:
        return {"viewers": kick_cache["viewers"]}
        
    async with httpx.AsyncClient() as client:
        try:
            res = await client.get(f"https://api.codetabs.com/v1/proxy?quest=https://kick.com/api/v1/channels/{username}", headers=headers, timeout=5.0)
            if res.status_code == 200:
                data = res.json()
                if data and "livestream" in data and data["livestream"]:
                    api_viewers = data["livestream"].get("viewer_count", 0)
                    if api_viewers > 0: # Ignore random 0 drops from proxy
                        kick_cache["viewers"] = api_viewers
                    kick_cache["expires"] = now + 60
                    return {"viewers": kick_cache["viewers"]}
        except: pass
        
    return {"viewers": kick_cache["viewers"]}

@app.post("/api/streamer/{handle}/settings")
async def save_streamer_settings(handle: str, payload: SettingsPayload):
    user = handle.strip().lower()
    res = supabase.table("streamers").update({
        "sub_goal": payload.sub_goal, "video_id": extract_youtube_video_id(payload.video_id),
        "ticker_text": payload.ticker_text, "twitch_user": payload.twitch_user, "kick_user": payload.kick_user,
        "updated_at": get_utc_now()
    }).eq("handle", user).execute()

    if not res.data: raise HTTPException(status_code=404, detail="Profile not found.")
    
    if user in CACHE:
        if "stats_data" in CACHE[user]: del CACHE[user]["stats_data"]
        if "stats_expires_at" in CACHE[user]: del CACHE[user]["stats_expires_at"]
        if "live_chat_id" in CACHE[user]: del CACHE[user]["live_chat_id"]
        if "chat_next_poll" in CACHE[user]: del CACHE[user]["chat_next_poll"]
        
    return {"status": "success"}

# ==========================================
# 9. STATIC FILES
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC_DIR = os.path.join(BASE_DIR, "public")
if os.path.exists(PUBLIC_DIR):
    @app.get("/dashboard")
    async def serve_dashboard(): return FileResponse(os.path.join(PUBLIC_DIR, "dashboard.html"))
    @app.get("/overlay")
    async def serve_overlay(): return FileResponse(os.path.join(PUBLIC_DIR, "overlay.html"))
    @app.get("/chat")
    async def serve_chat(): return FileResponse(os.path.join(PUBLIC_DIR, "chat-overlay.html"))

    app.mount("/", StaticFiles(directory=PUBLIC_DIR, html=True), name="public")
