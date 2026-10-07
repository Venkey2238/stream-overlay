import os
import re
import time
import asyncio
import httpx

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


# ============================================================
# 1. ENVIRONMENT
# ============================================================

load_dotenv()

SESSION_SECRET = os.getenv(
    "SESSION_SECRET",
    "change-this-session-secret",
)

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# How often the backend is willing to ask YouTube for a fresh
# subscriber count for the same streamer.
#
# This is NOT a promise that YouTube itself will instantly change
# subscriberCount. It only controls our side of the polling.
SUBSCRIBER_REFRESH_SECONDS = 10

# Live video statistics can be refreshed independently.
LIVE_STATS_REFRESH_SECONDS = 5

# Cache only database/profile metadata. Never use this cache to
# decide that a subscriber count is fresh.
PROFILE_CACHE_SECONDS = 30

# OAuth token refresh safety margin.
TOKEN_REFRESH_BUFFER_SECONDS = 180


# ============================================================
# 2. APP / CLIENTS
# ============================================================

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
)

supabase: Client = create_client(
    SUPABASE_URL,
    SUPABASE_KEY,
)


# ============================================================
# 3. GOOGLE OAUTH
# ============================================================

oauth = OAuth()

oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url=(
        "https://accounts.google.com/"
        ".well-known/openid-configuration"
    ),
    client_kwargs={
        "scope": (
            "openid email profile "
            "https://www.googleapis.com/auth/youtube.readonly"
        ),
    },
)


# ============================================================
# 4. RUNTIME STATE
# ============================================================

# IMPORTANT:
# This is process-local runtime state.
# Do not treat it as permanent storage.
#
# On Vercel/serverless, a new function instance can have a new
# CACHE. Supabase remains the persistent source of truth.

CACHE: dict[str, dict[str, Any]] = {}

CACHE_LOCK = asyncio.Lock()


# ============================================================
# 5. REQUEST MODELS
# ============================================================

class SettingsPayload(BaseModel):
    sub_goal: int
    video_id: Optional[str] = ""
    ticker_text: Optional[str] = ""
    twitch_user: Optional[str] = ""
    kick_user: Optional[str] = ""


# ============================================================
# 6. HELPERS
# ============================================================

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def unix_now() -> int:
    return int(time.time())


def normalize_handle(handle: str) -> str:
    return (
        (handle or "")
        .strip()
        .lower()
        .replace("@", "")
    )


def extract_youtube_video_id(value: str) -> str:
    """
    Accept:
      ABC123...
      https://www.youtube.com/watch?v=ABC123...
      https://youtu.be/ABC123...
      https://www.youtube.com/live/ABC123...
      https://www.youtube.com/shorts/ABC123...

    Returns an 11-character YouTube video ID when possible.
    """

    if not value:
        return ""

    value = value.strip()

    if re.fullmatch(r"[\w-]{11}", value):
        return value

    match = re.search(
        r"(?:v=|live/|shorts/|youtu\.be/)([\w-]{11})",
        value,
    )

    if match:
        return match.group(1)

    return ""


def parse_expires_at(value: Any) -> int:
    """
    Convert common Supabase/Authlib expiration formats into
    a Unix timestamp.

    Supported:
      - int / float
      - numeric string
      - ISO timestamp
    """

    if value is None:
        return 0

    if isinstance(value, (int, float)):
        return int(value)

    if isinstance(value, str):
        try:
            numeric = float(value)
            if numeric > 1_000_000_000:
                return int(numeric)
        except (ValueError, TypeError):
            pass

        try:
            iso = value.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)

            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

            return int(dt.timestamp())
        except (ValueError, TypeError):
            pass

    return 0


def google_headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }


def clear_runtime_cache(handle: str) -> None:
    user = normalize_handle(handle)
    CACHE.pop(user, None)


# ============================================================
# 7. SUPABASE PROFILE
# ============================================================

async def get_streamer_profile(
    handle: str,
    force_refresh: bool = False,
) -> Optional[dict[str, Any]]:

    user = normalize_handle(handle)

    if not force_refresh:
        cached = CACHE.get(user, {})
        profile = cached.get("profile")
        expires_at = cached.get("profile_expires_at", 0)

        if profile and expires_at > time.time():
            return profile

    result = (
        supabase
        .table("streamers")
        .select("*")
        .eq("handle", user)
        .execute()
    )

    if not result.data:
        return None

    profile = result.data[0]

    CACHE.setdefault(user, {})
    CACHE[user]["profile"] = profile
    CACHE[user]["profile_expires_at"] = (
        time.time() + PROFILE_CACHE_SECONDS
    )

    return profile


# ============================================================
# 8. GOOGLE TOKEN REFRESH
# ============================================================

async def refresh_google_access_token(
    handle: str,
    refresh_token: str,
) -> str:

    user = normalize_handle(handle)

    async with httpx.AsyncClient(timeout=15.0) as client:
        token_response = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )

    try:
        token_data = token_response.json()
    except Exception:
        token_data = {}

    if token_response.status_code != 200:
        print(
            "[GOOGLE TOKEN REFRESH FAILED]",
            token_response.status_code,
            token_data,
        )

        raise HTTPException(
            status_code=401,
            detail=(
                "Google rejected the refresh token. "
                "Reconnect Google/YouTube."
            ),
        )

    new_access_token = token_data.get("access_token")

    if not new_access_token:
        raise HTTPException(
            status_code=401,
            detail="Google returned no access token.",
        )

    expires_in = int(
        token_data.get("expires_in", 3600)
    )

    new_expires_at = (
        unix_now() + expires_in
    )

    # Google normally does NOT return a new refresh_token on
    # every refresh. Therefore never overwrite the existing
    # refresh token with null/missing data.

    update_data = {
        "access_token": new_access_token,
        "token_expires_at": new_expires_at,
        "updated_at": utc_now_iso(),
    }

    returned_refresh_token = token_data.get(
        "refresh_token"
    )

    if returned_refresh_token:
        update_data["refresh_token"] = (
            returned_refresh_token
        )

    supabase.table("streamers") \
        .update(update_data) \
        .eq("handle", user) \
        .execute()

    # Profile cache contains the old token/expiry, so invalidate it.
    clear_runtime_cache(user)

    print(
        f"[GOOGLE] Access token refreshed for @{user}; "
        f"expires_at={new_expires_at}"
    )

    return new_access_token


async def get_valid_access_token(
    handle: str,
) -> str:

    user = normalize_handle(handle)

    result = (
        supabase
        .table("streamers")
        .select(
            "access_token, refresh_token, token_expires_at"
        )
        .eq("handle", user)
        .execute()
    )

    if not result.data:
        raise HTTPException(
            status_code=404,
            detail="Streamer record not found.",
        )

    record = result.data[0]

    access_token = record.get("access_token")
    refresh_token = record.get("refresh_token")

    expires_at = parse_expires_at(
        record.get("token_expires_at")
    )

    # If the timestamp is valid and sufficiently far from
    # expiration, use the current access token.
    if (
        access_token
        and expires_at > (
            unix_now()
            + TOKEN_REFRESH_BUFFER_SECONDS
        )
    ):
        return access_token

    if not refresh_token:
        raise HTTPException(
            status_code=401,
            detail=(
                "No Google refresh token exists. "
                "Reconnect Google/YouTube."
            ),
        )

    return await refresh_google_access_token(
        user,
        refresh_token,
    )


# ============================================================
# 9. YOUTUBE REQUEST WITH AUTOMATIC 401 RECOVERY
# ============================================================

async def youtube_get_with_refresh(
    handle: str,
    url: str,
    params: dict[str, Any],
) -> tuple[httpx.Response, str]:

    user = normalize_handle(handle)

    token = await get_valid_access_token(user)

    async with httpx.AsyncClient(
        timeout=15.0
    ) as client:

        response = await client.get(
            url,
            params=params,
            headers=google_headers(token),
        )

        # Access tokens can be revoked/expired unexpectedly even
        # when our local expiry timestamp says otherwise.
        #
        # On 401:
        #   1. load the refresh token
        #   2. refresh once
        #   3. retry exactly once
        #
        # This prevents an infinite retry loop.

        if response.status_code == 401:

            record = await get_streamer_profile(
                user,
                force_refresh=True,
            )

            if not record:
                raise HTTPException(
                    status_code=404,
                    detail="Streamer not found.",
                )

            refresh_token = record.get(
                "refresh_token"
            )

            if not refresh_token:
                raise HTTPException(
                    status_code=401,
                    detail=(
                        "YouTube authorization expired. "
                        "Reconnect Google."
                    ),
                )

            token = await refresh_google_access_token(
                user,
                refresh_token,
            )

            response = await client.get(
                url,
                params=params,
                headers=google_headers(token),
            )

    return response, token


# ============================================================
# 10. GOOGLE LOGIN
# ============================================================

@app.get("/login")
async def login(request: Request):

    redirect_uri = str(
        request.url_for("auth_callback")
    )

    redirect_uri = redirect_uri.replace(
        "127.0.0.1",
        "localhost",
    )

    if (
        "localhost" not in redirect_uri
        and redirect_uri.startswith("http://")
    ):
        redirect_uri = redirect_uri.replace(
            "http://",
            "https://",
            1,
        )

    print("[OAUTH] redirect_uri =", redirect_uri)

    return await oauth.google.authorize_redirect(
        request,
        redirect_uri,
        access_type="offline",
        prompt="consent",
    )


# ============================================================
# 11. GOOGLE CALLBACK
# ============================================================

@app.get("/auth/callback")
async def auth_callback(request: Request):

    try:
        token = await oauth.google.authorize_access_token(
            request
        )
    except Exception as error:
        print(
            "[OAUTH CALLBACK ERROR]",
            repr(error),
        )

        return {
            "error": "Google Auth Denied",
            "details": str(error),
        }

    access_token = token.get("access_token")
    refresh_token = token.get("refresh_token")

    if not access_token:
        return {
            "error": (
                "Google did not return an access token."
            )
        }

    expires_at = parse_expires_at(
        token.get("expires_at")
    )

    if not expires_at:
        expires_at = (
            unix_now()
            + int(token.get("expires_in", 3600))
        )

    async with httpx.AsyncClient(
        timeout=15.0
    ) as client:

        response = await client.get(
            "https://www.googleapis.com/youtube/v3/channels",
            params={
                "part": "snippet,statistics",
                "mine": "true",
            },
            headers=google_headers(access_token),
        )

    try:
        data = response.json()
    except Exception:
        data = {}

    if response.status_code != 200:
        print(
            "[YOUTUBE CHANNEL ERROR]",
            response.status_code,
            data,
        )

        return {
            "error": "YouTube channel request failed.",
            "status": response.status_code,
            "details": data,
        }

    if not data.get("items"):
        return RedirectResponse(
            url="/dashboard?error=no_channel"
        )

    channel = data["items"][0]

    snippet = channel.get("snippet", {})
    statistics = channel.get("statistics", {})

    raw_handle = snippet.get(
        "customUrl",
        snippet.get("title", "channel"),
    )

    handle = normalize_handle(raw_handle)

    thumbnails = snippet.get(
        "thumbnails",
        {},
    )

    avatar_url = (
        thumbnails
        .get("medium", {})
        .get("url")
        or thumbnails
        .get("default", {})
        .get("url")
        or ""
    )

    subscriber_count = int(
        statistics.get(
            "subscriberCount",
            0,
        )
    )

    record = {
        "handle": handle,
        "channel_id": channel["id"],
        "title": snippet.get(
            "title",
            handle,
        ),
        "avatar": avatar_url,
        "access_token": access_token,
        "token_expires_at": expires_at,
        "current_subs": subscriber_count,
        "updated_at": utc_now_iso(),
    }

    # Do NOT write refresh_token unless Google actually gave us one.
    # This protects an existing refresh token.
    if refresh_token:
        record["refresh_token"] = refresh_token

    try:

        result = (
            supabase
            .table("streamers")
            .upsert(
                record,
                on_conflict="handle",
            )
            .execute()
        )

        print(
            "[SUPABASE] OAuth streamer saved:",
            result.data,
        )

        clear_runtime_cache(handle)

        return RedirectResponse(
            url=f"/dashboard?user={handle}"
        )

    except Exception as error:

        print(
            "[SUPABASE ERROR]",
            repr(error),
        )

        return {
            "CRITICAL_DATABASE_ERROR": str(error)
        }


# ============================================================
# 12. SUBSCRIBER COUNT
# ============================================================

async def fetch_subscriber_count(
    handle: str,
) -> tuple[int, int]:

    user = normalize_handle(handle)

    response, _token = await youtube_get_with_refresh(
        user,
        "https://www.googleapis.com/youtube/v3/channels",
        {
            "part": "statistics",
            "mine": "true",
        },
    )

    try:
        data = response.json()
    except Exception:
        data = {}

    if response.status_code != 200:

        print(
            "[YOUTUBE SUBSCRIBERS ERROR]",
            response.status_code,
            data,
        )

        raise HTTPException(
            status_code=response.status_code,
            detail={
                "message": (
                    "YouTube subscriber request failed."
                ),
                "youtube": data,
            },
        )

    items = data.get("items", [])

    if not items:
        raise HTTPException(
            status_code=404,
            detail="YouTube channel not found.",
        )

    statistics = items[0].get(
        "statistics",
        {},
    )

    count = int(
        statistics.get(
            "subscriberCount",
            0,
        )
    )

    # Persist the newest value.
    #
    # This is storage, not the realtime mechanism.
    # The frontend receives the value directly in the API response.

    supabase.table("streamers").update({
        "current_subs": count,
        "updated_at": utc_now_iso(),
    }).eq(
        "handle",
        user,
    ).execute()

    return count, unix_now()


# ============================================================
# 13. LIVE OVERLAY DATA
# ============================================================

@app.get("/api/streamer/{handle}")
async def get_live_overlay_data(
    handle: str,
    response: Response,
):

    user = normalize_handle(handle)

    # Tell browsers/proxies not to serve an old response.
    response.headers[
        "Cache-Control"
    ] = "no-store, no-cache, must-revalidate, max-age=0"

    response.headers[
        "Pragma"
    ] = "no-cache"

    profile = await get_streamer_profile(
        user
    )

    if not profile:
        return {
            "authenticated": False,
            "error": "Streamer not found.",
        }

    # --------------------------------------------------------
    # Subscriber count
    #
    # We deliberately do NOT return CACHE["data"] here.
    # Every request is allowed to receive a newly fetched count,
    # subject to a small server-side YouTube refresh guard.
    # --------------------------------------------------------

    runtime = CACHE.setdefault(
        user,
        {},
    )

    last_sub_refresh = runtime.get(
        "last_subscriber_refresh",
        0,
    )

    current_subs = int(
        profile.get(
            "current_subs",
            0,
        ) or 0
    )

    subscriber_updated_at = (
        runtime.get(
            "subscriber_updated_at"
        )
    )

    if (
        time.time()
        - last_sub_refresh
        >= SUBSCRIBER_REFRESH_SECONDS
    ):

        try:

            current_subs, updated_at = (
                await fetch_subscriber_count(user)
            )

            runtime["last_subscriber_refresh"] = (
                time.time()
            )

            runtime["subscriber_updated_at"] = (
                updated_at
            )

        except Exception as error:

            print(
                "[SUBSCRIBER REFRESH ERROR]",
                repr(error),
            )

            # Keep the last known good count.
            runtime["last_subscriber_refresh"] = (
                time.time()
            )

    else:

        # If another serverless request already refreshed
        # recently, use the last known runtime value.
        current_subs = int(
            runtime.get(
                "subscriber_count",
                current_subs,
            )
        )

    runtime["subscriber_count"] = current_subs

    # --------------------------------------------------------
    # Video information
    # --------------------------------------------------------

    raw_video_id = profile.get(
        "video_id",
        "",
    )

    video_id = extract_youtube_video_id(
        raw_video_id
    )

    likes = 0
    yt_viewers = 0
    live_status = "unknown"
    live_chat_id = None

    if video_id:

        try:

            video_response, _token = (
                await youtube_get_with_refresh(
                    user,
                    "https://www.googleapis.com/youtube/v3/videos",
                    {
                        "part": (
                            "snippet,"
                            "statistics,"
                            "liveStreamingDetails"
                        ),
                        "id": video_id,
                    },
                )
            )

            if video_response.status_code == 200:

                video_data = video_response.json()

                if video_data.get("items"):

                    item = video_data["items"][0]

                    statistics = item.get(
                        "statistics",
                        {},
                    )

                    details = item.get(
                        "liveStreamingDetails",
                        {},
                    )

                    likes = int(
                        statistics.get(
                            "likeCount",
                            0,
                        )
                    )

                    yt_viewers = int(
                        details.get(
                            "concurrentViewers",
                            0,
                        )
                    )

                    live_status = (
                        details.get(
                            "actualEndTime"
                        )
                        and "ended"
                        or (
                            "live"
                            if details.get(
                                "actualStartTime"
                            )
                            else "not_live"
                        )
                    )

                    live_chat_id = details.get(
                        "activeLiveChatId"
                    )

        except Exception as error:

            print(
                "[LIVE VIDEO ERROR]",
                repr(error),
            )

    # --------------------------------------------------------
    # IMPORTANT:
    # Return current_subs directly.
    # Do not return a stale cached response object.
    # --------------------------------------------------------

    result = {
        "authenticated": True,
        "title": profile.get(
            "title",
            "",
        ),
        "avatar": profile.get(
            "avatar",
            "",
        ),
        "subs": current_subs,
        "likes": likes,
        "yt_viewers": yt_viewers,
        "sub_goal": (
            profile.get(
                "sub_goal"
            )
            or 5000
        ),
        "video_id": video_id,
        "ticker_text": profile.get(
            "ticker_text",
            "",
        ),
        "twitch_user": profile.get(
            "twitch_user",
            "",
        ),
        "kick_user": profile.get(
            "kick_user",
            "",
        ),
        "youtube": {
            "live": live_status == "live",
            "live_status": live_status,
            "active_live_chat": bool(
                live_chat_id
            ),
            "subscriber_count": current_subs,
            "subscriber_updated_at": (
                subscriber_updated_at
                or unix_now()
            ),
        },
    }

    return result


# ============================================================
# 14. DEDICATED SUBSCRIBER ENDPOINT
# ============================================================

@app.get("/api/streamer/{handle}/subscribers")
async def get_subscribers(
    handle: str,
    response: Response,
):

    user = normalize_handle(handle)

    response.headers[
        "Cache-Control"
    ] = "no-store, no-cache, must-revalidate, max-age=0"

    response.headers[
        "Pragma"
    ] = "no-cache"

    profile = await get_streamer_profile(
        user,
        force_refresh=True,
    )

    if not profile:
        raise HTTPException(
            status_code=404,
            detail="Streamer not found.",
        )

    try:

        count, updated_at = (
            await fetch_subscriber_count(user)
        )

    except HTTPException:
        raise

    except Exception as error:

        print(
            "[SUBSCRIBER ENDPOINT ERROR]",
            repr(error),
        )

        raise HTTPException(
            status_code=502,
            detail=(
                "Unable to retrieve the current "
                "YouTube subscriber count."
            ),
        )

    runtime = CACHE.setdefault(
        user,
        {},
    )

    runtime["subscriber_count"] = count
    runtime["last_subscriber_refresh"] = (
        time.time()
    )
    runtime["subscriber_updated_at"] = (
        updated_at
    )

    return {
        "authenticated": True,
        "subs": count,
        "updated_at": updated_at,
        "source": "youtube-data-api",
    }


# ============================================================
# 15. YOUTUBE LIVE CHAT
# ============================================================

@app.get("/api/streamer/{handle}/chat")
async def get_live_chat(
    handle: str,
    response: Response,
    pageToken: str = "",
):

    user = normalize_handle(handle)

    response.headers[
        "Cache-Control"
    ] = "no-store, no-cache, must-revalidate, max-age=0"

    response.headers[
        "Pragma"
    ] = "no-cache"

    profile = await get_streamer_profile(
        user,
        force_refresh=True,
    )

    if not profile:
        return {
            "error": "Streamer not found.",
            "pollingIntervalMillis": 10000,
        }

    video_id = extract_youtube_video_id(
        profile.get("video_id", "")
    )

    if not video_id:
        return {
            "error": (
                "No valid YouTube live video ID is configured."
            ),
            "pollingIntervalMillis": 10000,
        }

    runtime = CACHE.setdefault(
        user,
        {},
    )

    cached_video_id = runtime.get(
        "live_chat_video_id"
    )

    live_chat_id = runtime.get(
        "live_chat_id"
    )

    # If the configured video changes, the old chat ID is invalid.
    if cached_video_id != video_id:
        live_chat_id = None
        runtime.pop("live_chat_id", None)
        runtime["live_chat_video_id"] = video_id

    # --------------------------------------------------------
    # Resolve activeLiveChatId
    # --------------------------------------------------------

    if not live_chat_id:

        try:

            video_response, _token = (
                await youtube_get_with_refresh(
                    user,
                    "https://www.googleapis.com/youtube/v3/videos",
                    {
                        "part": "liveStreamingDetails",
                        "id": video_id,
                    },
                )
            )

        except HTTPException as error:

            return {
                "error": error.detail,
                "pollingIntervalMillis": 15000,
            }

        try:
            video_data = video_response.json()
        except Exception:
            video_data = {}

        if video_response.status_code != 200:
            return {
                "error": "YouTube video lookup failed.",
                "status": video_response.status_code,
                "details": video_data,
                "pollingIntervalMillis": 10000,
            }

        items = video_data.get("items", [])

        if not items:
            return {
                "error": (
                    "YouTube video was not found. "
                    "Check the configured video ID."
                ),
                "pollingIntervalMillis": 10000,
            }

        details = items[0].get(
            "liveStreamingDetails",
            {},
        )

        live_chat_id = details.get(
            "activeLiveChatId"
        )

        if not live_chat_id:
            return {
                "error": (
                    "No active YouTube live chat. "
                    "The stream may be offline or "
                    "live chat may be disabled."
                ),
                "pollingIntervalMillis": 15000,
            }

        runtime["live_chat_id"] = live_chat_id
        runtime["live_chat_video_id"] = video_id

    # --------------------------------------------------------
    # Fetch messages
    # --------------------------------------------------------

    params = {
        "liveChatId": live_chat_id,
        "part": "snippet,authorDetails",
        "maxResults": 200,
    }

    if pageToken:
        params["pageToken"] = pageToken

    try:

        chat_response, _token = (
            await youtube_get_with_refresh(
                user,
                "https://www.googleapis.com/youtube/v3/liveChat/messages",
                params,
            )
        )

    except HTTPException as error:

        return {
            "error": error.detail,
            "pollingIntervalMillis": 15000,
        }

    try:
        chat_data = chat_response.json()
    except Exception:
        chat_data = {}

    if chat_response.status_code == 200:

        # YouTube gives the correct recommended polling interval.
        # The frontend should use this rather than a hard-coded
        # 1-second/2-second loop.

        polling_interval = int(
            chat_data.get(
                "pollingIntervalMillis",
                5000,
            )
        )

        return {
            **chat_data,
            "source": "youtube",
            "pollingIntervalMillis": polling_interval,
        }

    if chat_response.status_code == 403:

        # A 403 can be quota/permission related.
        # Do not falsely label every 403 as quota.
        return {
            "error": (
                "YouTube denied the live-chat request."
            ),
            "status": 403,
            "details": chat_data,
            "pollingIntervalMillis": 30000,
        }

    if chat_response.status_code in (
        404,
        410,
    ):

        # The broadcast/chat can disappear when the stream ends.
        runtime.pop("live_chat_id", None)

        return {
            "error": (
                "The YouTube live chat is no longer active."
            ),
            "status": chat_response.status_code,
            "pollingIntervalMillis": 10000,
        }

    return {
        "error": "YouTube Chat API Error.",
        "status": chat_response.status_code,
        "details": chat_data,
        "pollingIntervalMillis": 10000,
    }


# ============================================================
# 16. KICK VIEWER PROXY
# ============================================================

@app.get("/api/kick_viewers/{username}")
async def get_kick_viewers(
    username: str,
    response: Response,
):

    response.headers[
        "Cache-Control"
    ] = "no-store, no-cache, must-revalidate, max-age=0"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/120.0.0.0 "
            "Safari/537.36"
        ),
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(
        timeout=5.0
    ) as client:

        try:

            result = await client.get(
                (
                    "https://api.codetabs.com/v1/proxy"
                    f"?quest=https://kick.com/api/v1/channels/{username}"
                ),
                headers=headers,
            )

            if result.status_code == 200:

                data = result.json()

                if "livestream" in data:

                    livestream = data.get(
                        "livestream"
                    )

                    return {
                        "viewers": (
                            livestream.get(
                                "viewer_count",
                                0,
                            )
                            if livestream
                            else 0
                        )
                    }

        except Exception:
            pass

        try:

            result = await client.get(
                (
                    "https://api.allorigins.win/get"
                    f"?url=https://kick.com/api/v1/channels/{username}"
                ),
                headers=headers,
            )

            if result.status_code == 200:

                import json

                data = json.loads(
                    result.json().get(
                        "contents",
                        "{}",
                    )
                )

                if "livestream" in data:

                    livestream = data.get(
                        "livestream"
                    )

                    return {
                        "viewers": (
                            livestream.get(
                                "viewer_count",
                                0,
                            )
                            if livestream
                            else 0
                        )
                    }

        except Exception:
            pass

    return {
        "viewers": 0
    }


# ============================================================
# 17. SAVE SETTINGS
# ============================================================

@app.post(
    "/api/streamer/{handle}/settings"
)
async def save_streamer_settings(
    handle: str,
    payload: SettingsPayload,
):

    user = normalize_handle(handle)

    video_id = extract_youtube_video_id(
        payload.video_id or ""
    )

    result = (
        supabase
        .table("streamers")
        .update({
            "sub_goal": payload.sub_goal,
            "video_id": video_id,
            "ticker_text": payload.ticker_text,
            "twitch_user": payload.twitch_user,
            "kick_user": payload.kick_user,
            "updated_at": utc_now_iso(),
        })
        .eq("handle", user)
        .execute()
    )

    if not result.data:
        raise HTTPException(
            status_code=404,
            detail="Streamer profile not found.",
        )

    # Video ID and settings changed, therefore absolutely no
    # old live/chat/profile state should survive.
    clear_runtime_cache(user)

    return {
        "status": "success",
        "video_id": video_id,
        "message": (
            "Settings saved. Runtime state cleared."
        ),
    }


# ============================================================
# 18. MANUAL RECONNECT / CACHE RESET
# ============================================================

@app.post(
    "/api/streamer/{handle}/refresh"
)
async def force_refresh(
    handle: str,
):

    user = normalize_handle(handle)

    profile = await get_streamer_profile(
        user,
        force_refresh=True,
    )

    if not profile:
        raise HTTPException(
            status_code=404,
            detail="Streamer not found.",
        )

    clear_runtime_cache(user)

    return {
        "status": "success",
        "message": (
            "Runtime cache cleared. "
            "The next request will use fresh YouTube data."
        ),
    }


# ============================================================
# 19. HEALTH CHECK
# ============================================================

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "streamer-dashboard",
        "youtube_oauth": bool(
            GOOGLE_CLIENT_ID
            and GOOGLE_CLIENT_SECRET
        ),
        "supabase": bool(
            SUPABASE_URL
            and SUPABASE_KEY
        ),
        "timestamp": utc_now_iso(),
    }


# ============================================================
# 20. STATIC FILES
# ============================================================

BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

PUBLIC_DIR = os.path.join(
    BASE_DIR,
    "public",
)


if os.path.exists(PUBLIC_DIR):

    @app.get("/dashboard")
    async def serve_dashboard():
        return FileResponse(
            os.path.join(
                PUBLIC_DIR,
                "dashboard.html",
            )
        )

    @app.get("/overlay")
    async def serve_overlay():
        return FileResponse(
            os.path.join(
                PUBLIC_DIR,
                "overlay.html",
            )
        )

    @app.get("/chat")
    async def serve_chat():
        return FileResponse(
            os.path.join(
                PUBLIC_DIR,
                "chat-overlay.html",
            )
        )

    app.mount(
        "/",
        StaticFiles(
            directory=PUBLIC_DIR,
            html=True,
        ),
        name="public",
    )
