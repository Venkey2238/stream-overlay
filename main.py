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

CACHE = {}
SUBS_CACHE_TTL = 60
STATS_CACHE_TTL = 10

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

# ==========================================
# 2. UTILITY FUNCTIONS
# ==========================================
def get_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def extract_youtube_video_id(url: str) -> str:
    if not url: return ""
    url = url.strip()
    if len(url) == 11 and " " not in url and "/" not in url: return url
    match = re.search(r'(?:v=|live/|shorts/|youtu\.be/)([\w-]{11})', url)
    if match: return match.group(1)
    return url 

# ==========================================
# 3. GOOGLE OAUTH 2.0 SETUP
# ==========================================
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

# ==========================================
# 4. ZERO-QUOTA SCRAPERS
# ==========================================
async def scrape_video_stats(video_id: str, last_known_likes: int) -> tuple[int, int]:
    url = f"https://www.youtube.com/watch?v={video_id}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    likes, viewers = last_known_likes, 0
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, headers=headers, timeout=5.0)
            html = res.text
            viewers_match = re.search(r'"concurrentViewers"\s*:\s*\{\s*"simpleText"\s*:\s*"([\d,]+)"', html)
            if viewers_match: viewers = int(viewers_match.group(1).replace(",", ""))
            
            likes_match = re.search(r'"accessibilityData"\s*:\s*\{\s*"label"\s*:\s*"([\d,]+)\s+likes"', html)
            if likes_match:
                likes = max(last_known_likes, int(likes_match.group(1).replace(",", "")))
            else:
                likes_match_2 = re.search(r'"likeCount"\s*:\s*"([\d]+)"', html)
                if likes_match_2: likes = max(last_known_likes, int(likes_match_2.group(1)))
    except Exception: pass
    return likes, viewers

async def scrape_innertube_chat(video_id: str, continuation: str = "") -> dict:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    cache_key = f"innertube_{video_id}"
    api_key = CACHE.get(cache_key)

    async with httpx.AsyncClient() as client:
        if not continuation or not api_key:
            res = await client.get(f"https://www.youtube.com/live_chat?v={video_id}", headers=headers)
            key_match, token_match = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', res.text), re.search(r'"continuation":"([^"]+)"', res.text)
            if not key_match or not token_match: return {"error": "Chat unavailable", "pollingIntervalMillis": 10000}
            api_key, continuation = key_match.group(1), token_match.group(1)
            CACHE[cache_key] = api_key

        url = f"https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?key={api_key}"
        payload = {"context": {"client": {"clientName": "WEB", "clientVersion": "2.20231017.00.00"}}, "continuation": continuation}
        
        try:
            res = await client.post(url, json=payload, headers=headers)
            data = res.json()
            mapped_items, next_token = [], continuation
            actions = data.get("continuationContents", {}).get("liveChatContinuation", {}).get("actions", [])
            continuations = data.get("continuationContents", {}).get("liveChatContinuation", {}).get("continuations", [])
            
            if continuations:
                c = continuations[0]
                if "invalidationContinuationData" in c: next_token = c["invalidationContinuationData"]["continuation"]
                elif "timedContinuationData" in c: next_token = c["timedContinuationData"]["continuation"]
                        
            for action in actions:
                msg_data = action.get("addChatItemAction", {}).get("item", {}).get("liveChatTextMessageRenderer")
                if msg_data:
                    text = "".join([r.get("text", "") for r in msg_data.get("message", {}).get("runs", [])])
                    author = msg_data.get("authorName", {}).get("simpleText", "User")
                    avatars = msg_data.get("authorPhoto", {}).get("thumbnails", [])
                    mapped_items.append({"snippet": {"displayMessage": text}, "authorDetails": {"displayName": author, "profileImageUrl": avatars[0]["url"] if avatars else ""}})
            return {"pollingIntervalMillis": 5000, "nextPageToken": next_token, "items": mapped_items}
        except Exception: return {"error": "Failed to parse chat", "pollingIntervalMillis": 10000}

# ==========================================
# 5. TOKEN MANAGEMENT
# ==========================================
async def get_valid_access_token(handle: str) -> str:
    res = supabase.table("streamers").select("access_token, refresh_token, token_expires_at").eq("handle", handle.lower()).execute()
    if not res.data: raise HTTPException(status_code=404, detail="Streamer record not found.")
    record = res.data[0]
    access_token, refresh_token = record.get("access_token"), record.get("refresh_token")
    
    try: expires_at = int(float(record.get("token_expires_at") or 0))
    except (ValueError, TypeError): expires_at = 0
        
    now = int(time.time())
    if now < (expires_at - 120) and access_token: return access_token
    if not refresh_token: raise HTTPException(status_code=401, detail="No refresh token available. Re-authenticate.")

    async with httpx.AsyncClient() as client:
        token_res = await client.post(GOOGLE_TOKEN_URL, data={"client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET, "refresh_token": refresh_token, "grant_type": "refresh_token"})
        
    if token_res.status_code != 200: raise HTTPException(status_code=401, detail="Google rejected token refresh.")
    token_data = token_res.json()
    new_access_token = token_data["access_token"]
    update_data = {"access_token": new_access_token, "token_expires_at": now + int(token_data.get("expires_in", 3600)), "updated_at": get_utc_now()}
    if "refresh_token" in token_data: update_data["refresh_token"] = token_data["refresh_token"]
    supabase.table("streamers").update(update_data).eq("handle", handle.lower()).execute()
    return new_access_token

# ==========================================
# 6. AUTHENTICATION ROUTES
# ==========================================
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
# 7. LIVE DATA & OVERLAY ROUTES
# ==========================================
@app.get("/api/streamer/{handle}")
async def get_live_overlay_data(handle: str, response: Response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    user = handle.strip().lower()
    now = time.time()

    if user in CACHE and "stats_data" in CACHE[user] and CACHE[user]["stats_expires_at"] > now:
        return CACHE[user]["stats_data"]

    res = supabase.table("streamers").select("*").eq("handle", user).execute()
    if not res.data: return {"authenticated": False}
    profile = res.data[0]
    
    # NULL SAFETY FIX: Replaces missing or NULL database values with 0
    current_subs = profile.get("current_subs") or 0
    last_likes = profile.get("last_likes") or 0
    yt_viewers = 0
    video_id = extract_youtube_video_id(profile.get("video_id") or "")

    if CACHE.get(user, {}).get("subs_expires_at", 0) < now:
        try:
            token = await get_valid_access_token(user)
            async with httpx.AsyncClient() as client:
                yt_res = await client.get("https://www.googleapis.com/youtube/v3/channels?part=statistics&mine=true", headers={"Authorization": f"Bearer {token}"})
                if yt_res.status_code == 200 and yt_res.json().get("items"):
                    current_subs = int(yt_res.json()["items"][0]["statistics"].get("subscriberCount", current_subs))
            CACHE.setdefault(user, {})["subs_expires_at"] = now + SUBS_CACHE_TTL
        except Exception: pass

    if video_id:
        likes, yt_viewers = await scrape_video_stats(video_id, last_likes)
    else:
        likes = last_likes

    final_likes = max(likes or 0, last_likes)

    # CRASH PROTECTION FIX: Prevents server crash if database is missing a column
    try:
        supabase.table("streamers").update({
            "current_subs": current_subs, 
            "last_likes": final_likes,
            "updated_at": get_utc_now()
        }).eq("handle", user).execute()
    except Exception as e:
        print(f"WARNING: DB Update failed. Missing 'last_likes' column? Error: {e}")

    response_data = {
        "authenticated": True, "title": profile.get("title") or "", "avatar": profile.get("avatar") or "",
        "subs": current_subs, "likes": final_likes, "yt_viewers": yt_viewers,
        "sub_goal": profile.get("sub_goal") if profile.get("sub_goal") else 5000, "video_id": video_id,
        "ticker_text": profile.get("ticker_text") or "", "twitch_user": profile.get("twitch_user") or "", "kick_user": profile.get("kick_user") or ""
    }

    CACHE.setdefault(user, {})["stats_data"] = response_data
    CACHE[user]["stats_expires_at"] = now + STATS_CACHE_TTL
    return response_data

@app.get("/api/streamer/{handle}/chat")
async def get_live_chat(handle: str, response: Response, pageToken: str = ""):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    user = handle.strip().lower()
    
    res = supabase.table("streamers").select("video_id").eq("handle", user).execute()
    if not res.data: return {"error": "Not found", "pollingIntervalMillis": 10000}
    
    video_id = extract_youtube_video_id(res.data[0].get("video_id") or "")
    if not video_id: return {"error": "No Video ID", "pollingIntervalMillis": 10000}

    return await scrape_innertube_chat(video_id, pageToken)

# ==========================================
# 8. KICK VIEWER PROXY
# ==========================================
@app.get("/api/kick_viewers/{username}")
async def get_kick_viewers(username: str, response: Response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "application/json"}
    
    async with httpx.AsyncClient() as client:
        try:
            res = await client.get(f"https://api.codetabs.com/v1/proxy?quest=https://kick.com/api/v1/channels/{username}", headers=headers, timeout=5.0)
            if res.status_code == 200:
                data = res.json()
                if data and "livestream" in data:
                    return {"viewers": data["livestream"].get("viewer_count", 0) if data["livestream"] else 0}
        except: pass
    return {"viewers": 0}

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
        if "subs_expires_at" in CACHE[user]: del CACHE[user]["subs_expires_at"]
        
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
