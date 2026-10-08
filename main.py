import os
import time
import re
import json
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

# CACHE TIMERS
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

def get_utc_now() -> str: return datetime.now(timezone.utc).isoformat()

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

# ==========================================
# 2. QUOTA-FREE YOUTUBE SCRAPERS
# ==========================================
async def scrape_youtube_video_stats(video_id: str):
    """Fetches viewers and likes using ZERO Google API Quota."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0", "Accept-Language": "en-US,en;q=0.9"}
    async with httpx.AsyncClient() as client:
        res = await client.get(url, headers=headers)
        if res.status_code != 200: return 0, 0
        html = res.text
        viewers, likes = 0, 0
        
        # Viewers
        v_match = re.search(r'"concurrentViewers"\s*:\s*\{\s*"runs"\s*:\s*\[\s*\{\s*"text"\s*:\s*"([\d,]+)"', html)
        if v_match: viewers = int(v_match.group(1).replace(',', ''))
        else:
            v_match2 = re.search(r'"viewCount"\s*:\s*\{\s*"videoViewCountRenderer"\s*:\s*\{.*?"isLive"\s*:\s*true.*?"originalViewCount"\s*:\s*"([\d,]+)"', html)
            if v_match2: viewers = int(v_match2.group(1).replace(',', ''))
                
        # Likes
        l_match = re.search(r'"like this video along with ([\d,]+) other people"', html)
        if l_match: likes = int(l_match.group(1).replace(',', ''))
        else:
            l_match2 = re.search(r'"label"\s*:\s*"([\d,]+) likes"', html)
            if l_match2: likes = int(l_match2.group(1).replace(',', ''))
            else:
                l_match3 = re.search(r'"factoidRenderer"\s*:\s*\{\s*"value"\s*:\s*\{\s*"simpleText"\s*:\s*"([\d,\.KM]+)"', html)
                if l_match3:
                    l_str = l_match3.group(1).upper()
                    if 'K' in l_str: likes = int(float(l_str.replace('K', '')) * 1000)
                    elif 'M' in l_str: likes = int(float(l_str.replace('M', '')) * 1000000)
                    else: likes = int(l_str.replace(',', ''))
                    
        return viewers, likes

async def get_yt_chat_keys(video_id: str):
    """Extracts the Innertube API Key required for Quota-Free Chat."""
    url = f"https://www.youtube.com/live_chat?v={video_id}"
    headers = {"User-Agent": "Mozilla/5.0"}
    async with httpx.AsyncClient() as client:
        res = await client.get(url, headers=headers)
        api_key_match = re.search(r'"INNERTUBE_API_KEY":"(.*?)"', res.text)
        cont_match = re.search(r'"continuation":"(.*?)"', res.text)
        return (api_key_match.group(1) if api_key_match else None, cont_match.group(1) if cont_match else None)

async def fetch_yt_chat_messages(api_key: str, continuation: str):
    """Fetches chat messages directly from Innertube (ZERO Quota)."""
    url = f"https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?key={api_key}"
    payload = {"context": {"client": {"clientName": "WEB", "clientVersion": "2.20231010.00.00"}}, "continuation": continuation}
    async with httpx.AsyncClient() as client:
        res = await client.post(url, json=payload)
        if res.status_code != 200: return [], None, 10000
            
        data = res.json()
        messages = []
        contents = data.get("continuationContents", {}).get("liveChatContinuation", {})
        
        for action in contents.get("actions", []):
            item = action.get("addChatItemAction", {}).get("item", {})
            if "liveChatTextMessageRenderer" in item:
                renderer = item["liveChatTextMessageRenderer"]
                author = renderer.get("authorName", {}).get("simpleText", "Unknown")
                text = "".join([run.get("text", run.get("emoji", {}).get("shortcuts", [""])[0]) for run in renderer.get("message", {}).get("runs", [])])
                thumbs = renderer.get("authorPhoto", {}).get("thumbnails", [])
                avatar = thumbs[-1].get("url", "") if thumbs else ""
                messages.append({"u": author, "m": text, "a": avatar, "p": "yt"})
                
        next_cont, timeout_ms = None, 5000
        continuations = contents.get("continuations", [])
        if continuations:
            cont_data = continuations[0]
            if "invalidationContinuationData" in cont_data:
                next_cont = cont_data["invalidationContinuationData"].get("continuation")
                timeout_ms = cont_data["invalidationContinuationData"].get("timeoutMs", 5000)
            elif "timedContinuationData" in cont_data:
                next_cont = cont_data["timedContinuationData"].get("continuation")
                timeout_ms = cont_data["timedContinuationData"].get("timeoutMs", 5000)
                
        return messages, next_cont, timeout_ms

# ==========================================
# 3. GOOGLE API (ONLY FOR SUBS - 1 Quota / 5 mins)
# ==========================================
async def get_valid_access_token(handle: str) -> str:
    res = supabase.table("streamers").select("*").eq("handle", handle.lower()).execute()
    if not res.data: raise HTTPException(status_code=404, detail="Streamer not found.")
    record = res.data[0]
    if int(time.time()) < (int(float(record.get("token_expires_at") or 0)) - 120) and record.get("access_token"):
        return record["access_token"]
    async with httpx.AsyncClient() as client:
        token_res = await client.post(GOOGLE_TOKEN_URL, data={"client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET, "refresh_token": record.get("refresh_token"), "grant_type": "refresh_token"})
    token_data = token_res.json()
    new_access_token = token_data["access_token"]
    supabase.table("streamers").update({"access_token": new_access_token, "token_expires_at": int(time.time()) + int(token_data.get("expires_in", 3600))}).eq("handle", handle.lower()).execute()
    return new_access_token

@app.get("/login")
async def login(request: Request):
    redirect_uri = str(request.url_for('auth_callback')).replace("127.0.0.1", "localhost").replace("http://", "https://")
    return await oauth.google.authorize_redirect(request, redirect_uri, access_type='offline', prompt='consent')

@app.get("/auth/callback")
async def auth_callback(request: Request):
    token = await oauth.google.authorize_access_token(request)
    access_token, refresh_token = token.get('access_token'), token.get('refresh_token')
    async with httpx.AsyncClient() as client:
        res = await client.get("https://www.googleapis.com/youtube/v3/channels?part=snippet,statistics&mine=true", headers={"Authorization": f"Bearer {access_token}"})
        data = res.json()
    channel = data["items"][0]
    handle = channel["snippet"].get("customUrl", channel["snippet"]["title"]).replace("@", "").lower()
    record = {
        "handle": handle, "channel_id": channel["id"], "title": channel["snippet"]["title"],
        "avatar": channel["snippet"].get("thumbnails", {}).get("medium", {}).get("url", ""), 
        "access_token": access_token, "token_expires_at": token.get('expires_at', int(time.time()) + 3600),
        "current_subs": int(channel["statistics"].get("subscriberCount", 0)), "updated_at": get_utc_now()
    }
    if refresh_token: record["refresh_token"] = refresh_token
    supabase.table("streamers").upsert(record, on_conflict="handle").execute()
    if handle in CACHE: del CACHE[handle]
    return RedirectResponse(url=f"/dashboard?user={handle}")

# ==========================================
# 4. LIVE OVERLAY DATA
# ==========================================
@app.get("/api/streamer/{handle}")
async def get_live_overlay_data(handle: str, response: Response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    user = handle.strip().lower()
    now = time.time()
    user_cache = CACHE.setdefault(user, {})

    if now < user_cache.get("stats_expires_at", 0) and "stats_data" in user_cache: return user_cache["stats_data"]

    res = supabase.table("streamers").select("*").eq("handle", user).execute()
    if not res.data: return {"authenticated": False}
    profile = res.data[0]
    
    current_subs = profile.get("current_subs") or 0
    likes, yt_viewers = profile.get("last_likes") or 0, user_cache.get("last_yt_viewers", 0)
    video_id = extract_youtube_video_id(profile.get("video_id") or "")

    try:
        # Subs from API (Once every 5 mins)
        if now > user_cache.get("subs_expires_at", 0):
            token = await get_valid_access_token(user)
            async with httpx.AsyncClient() as client:
                yt_res = await client.get("https://www.googleapis.com/youtube/v3/channels?part=statistics&mine=true", headers={"Authorization": f"Bearer {token}"})
                if yt_res.status_code == 200 and yt_res.json().get("items"):
                    current_subs = int(yt_res.json()["items"][0]["statistics"].get("subscriberCount", current_subs))
            user_cache["subs_expires_at"] = now + SUBS_CACHE_TTL

        # Views & Likes from Scraping (ZERO QUOTA)
        if video_id:
            v, l = await scrape_youtube_video_stats(video_id)
            if v > 0: yt_viewers = v; user_cache["last_yt_viewers"] = yt_viewers
            if l > 0: likes = max(l, likes)

        try: supabase.table("streamers").update({"current_subs": current_subs, "last_likes": likes, "updated_at": get_utc_now()}).eq("handle", user).execute()
        except: pass
    except Exception as e: print(e)

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
# 5. LIVE CHAT (ZERO QUOTA YOUTUBE)
# ==========================================
@app.get("/api/streamer/{handle}/chat")
async def get_live_chat(handle: str, response: Response, pageToken: str = ""):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    user = handle.strip().lower()
    now = time.time()
    user_cache = CACHE.setdefault(user, {})

    next_poll = user_cache.get("chat_next_poll", 0)
    if now < next_poll: return {"items": [], "nextPageToken": pageToken, "pollingIntervalMillis": max(int((next_poll - now) * 1000), 3000)}

    res = supabase.table("streamers").select("video_id").eq("handle", user).execute()
    if not res.data: return {"items": [], "nextPageToken": "", "pollingIntervalMillis": 10000}
    video_id = extract_youtube_video_id(res.data[0].get("video_id") or "")
    if not video_id: return {"items": [], "nextPageToken": "", "pollingIntervalMillis": 10000}

    api_key = user_cache.get("yt_chat_api_key")
    if not api_key or not pageToken:
        api_key, pageToken = await get_yt_chat_keys(video_id)
        if not api_key: return {"items": [], "nextPageToken": "", "pollingIntervalMillis": 15000}
        user_cache["yt_chat_api_key"] = api_key
        
    messages, next_cont, timeout_ms = await fetch_yt_chat_messages(api_key, pageToken)
    user_cache["chat_next_poll"] = time.time() + (timeout_ms / 1000.0)
    
    return {"items": messages, "nextPageToken": next_cont or pageToken, "pollingIntervalMillis": timeout_ms}

# ==========================================
# 6. CLOUDFLARE BYPASS KICK PROXY
# ==========================================
@app.get("/api/kick_viewers/{username}")
async def get_kick_viewers(username: str, response: Response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    now = time.time()
    cache_key = f"kick_{username}"
    kick_cache = CACHE.setdefault(cache_key, {"viewers": 0, "expires": 0})
    
    if now < kick_cache["expires"]: return {"viewers": kick_cache["viewers"]}
        
    async with httpx.AsyncClient() as client:
        try:
            # Bypasses Cloudflare strictly using allorigins
            proxy_url = f"https://api.allorigins.win/get?url=https://kick.com/api/v2/channels/{username}"
            res = await client.get(proxy_url, timeout=5.0)
            if res.status_code == 200:
                data = json.loads(res.json()["contents"])
                if data and "livestream" in data and data["livestream"]:
                    viewers = data["livestream"].get("viewer_count", 0)
                    kick_cache["viewers"] = viewers
                    kick_cache["expires"] = now + 60
                    return {"viewers": viewers}
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
        CACHE[user].pop("stats_data", None); CACHE[user].pop("stats_expires_at", None)
        CACHE[user].pop("yt_chat_api_key", None); CACHE[user].pop("chat_next_poll", None)
        
    return {"status": "success"}

# ==========================================
# 7. STATIC FILES
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
