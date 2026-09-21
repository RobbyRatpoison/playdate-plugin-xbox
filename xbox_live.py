"""
xbox_live.py -- real Xbox Live auth + library fetch.

Protocol researched from JosefNemec/PlayniteExtensions
(source/Libraries/XboxLibrary/Services/XboxAccountClient.cs) -- reference
only, fresh implementation (Playnite is GPL-3, PlayDate is MIT).

Auth is a 3-step chain, each step's token feeding the next:
  1. Microsoft Live OAuth (login.live.com) -- normal oauth_popup flow,
     ends with an authorization `code` in the redirect URL.
  2. Exchange that code for a Live access/refresh token pair.
  3. Xbox User Token: trade the Live access token for an Xbox "user token"
     at user.auth.xboxlive.com.
  4. XSTS Token: trade the user token for the actual XSTS token at
     xsts.auth.xboxlive.com -- this response also carries the account's
     xuid and user hash (uhs), both needed for later API calls.

Every Xbox Live API call after that uses `Authorization: XBL3.0 x=<uhs>;<xsts_token>`.
Full owned-title history comes from titlehub.xboxlive.com, not a simple
"installed packages" scan -- confirmed this is a real full-library
endpoint, not just local install state.
"""

import logging
import threading
from datetime import date, datetime, timezone

import requests

from config import load_config, _save_config_data
from database import next_negative_appid, update_game_data

log = logging.getLogger(__name__)

# titlehub's titlehistory lists every title with Xbox Live SDK integration,
# including plain PC games (often from Steam) that merely report achievements/
# presence to Xbox Live -- those show up with devices == ['Win32'] only and
# were never installed as a real Microsoft Store/UWP package. Confirmed live:
# of a 288-title account, 249 were Win32-only and matched the user's existing
# Steam library.
#
# The API has no ownership/entitlement field at all -- checked both this
# endpoint's full field set (detail, gamepass, achievement decorations) and
# JosefNemec/PlayniteExtensions's real XboxLibrary.cs: it doesn't detect
# current access either. `gamePass.isGamePass` is catalog-wide ("is this
# title currently in the Game Pass lineup"), not personal -- confirmed live,
# it was False on titles with real earned achievement progress. Playnite's
# own signal for "a real Xbox PC title" is a non-null `pfn` (Package Family
# Name -- it was installed as an actual Store/UWP package on PC at some
# point), which is what we use here too. This still can't tell current
# access from a lapsed Game Pass game -- neither can Playnite's -- so a
# previously-played-via-Game-Pass title you no longer have access to will
# still show up in every sync. Surfaced to the user via the Connect screen's
# instructions in __init__.py's manage_ui().

CLIENT_ID    = '38cd2fa8-66fd-4760-afb2-405eb65d5b0c'  # Playnite's public client id; same public OAuth app model as other plugins' popup flows
REDIRECT_URI = 'https://login.live.com/oauth20_desktop.srf'
SCOPE        = 'Xboxlive.signin Xboxlive.offline_access'

AUTHORIZE_URL   = 'https://login.live.com/oauth20_authorize.srf'
TOKEN_URL       = 'https://login.live.com/oauth20_token.srf'
USER_AUTH_URL   = 'https://user.auth.xboxlive.com/user/authenticate'
XSTS_URL        = 'https://xsts.auth.xboxlive.com/xsts/authorize'
TITLEHUB_URL    = 'https://titlehub.xboxlive.com/users/xuid({xuid})/titles/titlehistory/decoration/{fields}'


def get_auth_url():
    from urllib.parse import urlencode
    params = {
        'client_id': CLIENT_ID,
        'response_type': 'code',
        'approval_prompt': 'auto',
        'scope': SCOPE,
        'redirect_uri': REDIRECT_URI,
    }
    return f'{AUTHORIZE_URL}?{urlencode(params)}'


def _cfg():
    return (load_config() or {}).get('xbox', {})


def _save_cfg(data):
    cfg = load_config() or {}
    cfg['xbox'] = data
    _save_config_data(cfg)


def is_connected():
    return bool(_cfg().get('xsts_token'))


def get_username():
    return _cfg().get('gamertag') or 'Connected'


def disconnect():
    cfg = load_config() or {}
    cfg.pop('xbox', None)
    _save_config_data(cfg)


def _exchange_live_code(code):
    """Step 2: authorization code -> Live access/refresh token pair."""
    resp = requests.post(TOKEN_URL, data={
        'client_id': CLIENT_ID,
        'code': code,
        'grant_type': 'authorization_code',
        'redirect_uri': REDIRECT_URI,
        'scope': SCOPE,
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()  # {access_token, refresh_token, expires_in, ...}


def _xbl_user_token(live_access_token):
    """Step 3: Live access token -> Xbox user token."""
    resp = requests.post(USER_AUTH_URL, json={
        'RelyingParty': 'http://auth.xboxlive.com',
        'TokenType': 'JWT',
        'Properties': {
            'AuthMethod': 'RPS',
            'SiteName': 'user.auth.xboxlive.com',
            'RpsTicket': f'd={live_access_token}',
        },
    }, headers={'x-xbl-contract-version': '1'}, timeout=15)
    resp.raise_for_status()
    return resp.json()['Token']


def _xsts_authorize(user_token):
    """Step 4: user token -> XSTS token + xuid/uhs (DisplayClaims)."""
    resp = requests.post(XSTS_URL, json={
        'RelyingParty': 'http://xboxlive.com',
        'TokenType': 'JWT',
        'Properties': {
            'SandboxId': 'RETAIL',
            'UserTokens': [user_token],
        },
    }, headers={'x-xbl-contract-version': '1'}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    claims = data['DisplayClaims']['xui'][0]
    return {
        'xsts_token': data['Token'],
        'uhs':  claims['uhs'],
        'xuid': claims['xid'],
        'gamertag': claims.get('gtg', 'Connected'),
    }


def connect(code):
    """Full chain: authorization code -> stored, ready-to-use XSTS session."""
    try:
        live_tokens = _exchange_live_code(code)
        user_token  = _xbl_user_token(live_tokens['access_token'])
        xsts        = _xsts_authorize(user_token)
    except requests.HTTPError as e:
        return False, f'Xbox Live auth failed (HTTP {e.response.status_code})'
    except Exception as e:
        return False, f'Xbox Live auth failed: {e}'

    _save_cfg({
        'refresh_token': live_tokens.get('refresh_token'),
        'xsts_token': xsts['xsts_token'],
        'uhs': xsts['uhs'],
        'xuid': xsts['xuid'],
        'gamertag': xsts['gamertag'],
    })
    log.info(f"Xbox connected as {xsts['gamertag']!r}")
    return True, xsts['gamertag']


def _auth_header():
    cfg = _cfg()
    return {'Authorization': f"XBL3.0 x={cfg.get('uhs')};{cfg.get('xsts_token')}",
            'x-xbl-contract-version': '2',
            'Accept-Language': 'en-US'}


def fetch_library():
    """Full owned-title history via titlehub -- not just locally-installed
    packages. Returns the raw list of title dicts from the API.

    decoration=detail alone is the only value XboxAccountClient.cs's
    GetLibraryTitles() actually uses; the original invented
    'GamePass,Achievement,Stats,GameProgress,Image' list 400'd ('Stats'
    in particular isn't a valid titlehub decoration at all -- playtime
    comes from a separate userstats.xboxlive.com/batch POST). Confirmed
    live that 'detail,image,achievement' (correct casing, no 'stats') is
    accepted and adds real per-title box art and achievement progress on
    top of detail's developer/publisher/genre/release-date/store fields.
    """
    cfg = _cfg()
    xuid = cfg.get('xuid')
    if not xuid:
        raise RuntimeError('Not connected')
    url = TITLEHUB_URL.format(xuid=xuid, fields='detail,image,achievement')
    resp = requests.get(url, headers=_auth_header(), timeout=20)
    resp.raise_for_status()
    return resp.json().get('titles', [])


def _is_pc_title(title):
    """True if this title has a Package Family Name -- meaning it was
    installed as a real Microsoft Store/UWP package on PC at some point.
    Matches Playnite's own filter (XboxLibrary.cs: pcTitles requires a
    non-empty pfn + 'PC' in devices); confirmed 1:1 with 'PC' in devices
    in practice, so checking pfn alone is sufficient."""
    return bool(title.get('pfn'))


def _parse_iso_ts(raw):
    """ISO-8601 -> unix timestamp, or None. Fractional seconds can carry more
    than 6 digits (e.g. '...7449026Z'), which datetime.fromisoformat rejects
    -- truncate to microseconds. Shared by lastTimePlayed and releaseDate."""
    if not raw:
        return None
    try:
        raw = raw.rstrip('Z')
        if '.' in raw:
            head, frac = raw.split('.', 1)
            raw = f'{head}.{frac[:6]}'
        return int(datetime.fromisoformat(raw).replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return None


def _parse_last_played(title):
    """titleHistory.lastTimePlayed -> unix timestamp, or None."""
    return _parse_iso_ts((title.get('titleHistory') or {}).get('lastTimePlayed'))


def _extract_store_id(title):
    """Microsoft Store product id (e.g. '9P34LH5ZWBVG') for building a store
    link -- xbox.com/en-US/games/store/{any-slug}/{productId}. Confirmed live
    the slug portion is purely cosmetic (an unrelated or generic placeholder
    slug still 200s to the same product page), so no name-slugging needed."""
    avail = (title.get('detail') or {}).get('availabilities') or []
    return avail[0].get('ProductId') if avail else None


def _build_metadata(title):
    """Fields update_game_data can apply from the detail/achievement decorations."""
    detail = title.get('detail') or {}
    meta = {}
    if detail.get('developerName'):
        meta['developers'] = detail['developerName']
    if detail.get('publisherName'):
        meta['publishers'] = detail['publisherName']
    genres = detail.get('genres') or []
    if genres:
        meta['genres'] = ','.join(genres)
    release_ts = _parse_iso_ts(detail.get('releaseDate'))
    if release_ts:
        meta['release_date'] = release_ts

    achievement = title.get('achievement')
    if achievement and achievement.get('totalAchievements'):
        # totalAchievements is confirmed unreliable on this endpoint -- every
        # achievement-bearing title in a live 28-title sample came back with
        # totalAchievements: 0 even when currentAchievements/gamerscore were
        # correctly populated (e.g. 100% games showing 43 unlocked / 0
        # total). Storing unlocked without a usable total is worse than not
        # storing either -- it'd render as "23/0" -- so only write these
        # fields on the rare response where totalAchievements is actually set.
        meta['unlocked_achievements'] = achievement.get('currentAchievements') or 0
        meta['total_achievements']    = achievement['totalAchievements']
        meta['cheevos_fetched']       = date.today().isoformat()

    return meta


# Xbox's own store-catalog images, ranked best-first per art slot. Confirmed
# live: Poster is 1440x2160 (2:3, matches Steam's vertical capsule aspect
# exactly), TitledHeroArt is a branded 1920x1080 banner (closest match to
# Steam's header.jpg style), Logo is a clean 300x300 square icon. Real,
# officially-tied-to-the-title art beats an SGDB name-search guess, so these
# are tried first and SGDB is only the fallback when a type is missing.
_IMAGE_TYPE_PRIORITY = {
    'vertical':   ('Poster', 'BoxArt'),
    'horizontal': ('TitledHeroArt', 'Hero', 'SuperHeroArt'),
    'icon':       ('Logo',),
}


def _pick_xbox_image(title, orientation):
    by_type = {}
    for img in (title.get('images') or []):
        t = img.get('type')
        if t and t not in by_type and img.get('url'):
            by_type[t] = img['url']
    for t in _IMAGE_TYPE_PRIORITY.get(orientation, ()):
        if t in by_type:
            return by_type[t]
    return None


def art_urls(appid):
    """Where Xbox's own artwork for one game lives, for core's Artwork Sources
    "Store" option: {'vertical'|'horizontal'|'icon': url}, any subset. Re-fetches
    the title (as a Re-scrape does), so {} when not connected or not found."""
    if not is_connected():
        return {}
    from database import get_db
    db  = get_db()
    row = db.execute(
        "SELECT platform_appname FROM games WHERE appid = ? AND platform = 'xbox'", (appid,)
    ).fetchone()
    db.close()
    if not row or not row['platform_appname']:
        return {}
    try:
        title = _fetch_single_title(row['platform_appname'])
    except Exception as e:
        log.warning('Xbox: art_urls fetch failed for appid %s: %s', appid, e)
        return {}
    if not title:
        return {}
    urls = {}
    for kind in ('vertical', 'horizontal', 'icon'):
        url = _pick_xbox_image(title, kind)
        if url:
            urls[kind] = url
    return urls


def _fetch_art(appid, name, title):
    try:
        from images import (_sgdb_search_game_id, download_vertical, download_horizontal,
                             download_icon, download_from_url)
        sgdb_id = None

        vert_url = _pick_xbox_image(title, 'vertical')
        vert = download_from_url(appid, vert_url, 'vertical') if vert_url else 'missing'
        if vert == 'missing':
            sgdb_id = sgdb_id or _sgdb_search_game_id(name)
            vert = download_vertical(appid, sgdb_id=sgdb_id, game_name=name)

        horiz_url = _pick_xbox_image(title, 'horizontal')
        horiz = download_from_url(appid, horiz_url, 'horizontal') if horiz_url else 'missing'
        if horiz == 'missing':
            sgdb_id = sgdb_id or _sgdb_search_game_id(name)
            horiz = download_horizontal(appid, sgdb_id=sgdb_id, game_name=name)

        icon_url = _pick_xbox_image(title, 'icon')
        icon = download_from_url(appid, icon_url, 'icon') if icon_url else 'missing'
        if icon == 'missing':
            sgdb_id = sgdb_id or _sgdb_search_game_id(name)
            icon = download_icon(appid, icon_hash=None, sgdb_id=sgdb_id, game_name=name)

        if vert != 'missing' or horiz != 'missing' or icon != 'missing':
            update_game_data(appid, art_fetched=date.today().isoformat())
    except Exception as e:
        log.warning('Xbox: art fetch failed for %r: %s', name, e)


def _fetch_playtimes(xuid, title_ids):
    """Batched MinutesPlayed lookup via userstats.xboxlive.com -- one POST
    for every title at once (matches XboxAccountClient.cs's
    GetUserStatsMinutesPlayed). Returns {titleId: minutes}; a title with no
    tracked stat is simply absent from the result, not an error."""
    if not title_ids:
        return {}
    payload = {
        'arrangebyfield': 'xuid',
        'stats': [{'name': 'MinutesPlayed', 'titleid': tid} for tid in title_ids],
        'xuids': [str(xuid)],
    }
    try:
        resp = requests.post('https://userstats.xboxlive.com/batch',
                              headers=_auth_header(), json=payload, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning('Xbox: playtime fetch failed: %s', e)
        return {}
    out = {}
    for coll in data.get('statlistscollection', []):
        for stat in coll.get('stats', []):
            try:
                out[stat['titleid']] = int(stat.get('value') or 0)
            except (TypeError, ValueError):
                out[stat['titleid']] = 0
    return out


def _fetch_single_title(pfn, fields='detail,image,achievement'):
    """Re-fetch one title by pfn via the batch endpoint (XboxAccountClient.cs's
    GetTitleInfo) -- titlehistory only comes back in one big page, so a
    single-game refresh (rescrape/scrape-single) needs its own lookup."""
    resp = requests.post(
        f'https://titlehub.xboxlive.com/titles/batch/decoration/{fields}',
        headers=_auth_header(),
        json={'pfns': [pfn], 'windowsPhoneProductIds': []},
        timeout=15,
    )
    resp.raise_for_status()
    titles = resp.json().get('titles') or []
    return titles[0] if titles else None


def fetch_description(appid, platform_id):
    """Fetch a short description for one Xbox game on demand -- titlehistory
    doesn't carry it for anything but must be re-requested per-title via the
    batch-by-pfn endpoint."""
    from database import get_db
    db  = get_db()
    row = db.execute(
        "SELECT platform_appname FROM games WHERE appid = ? AND platform = 'xbox'",
        (appid,)
    ).fetchone()
    db.close()
    pfn = row['platform_appname'] if row else None
    if not pfn:
        return None
    try:
        title = _fetch_single_title(pfn, fields='detail')
        if not title:
            return None
        detail = title.get('detail') or {}
        return detail.get('shortDescription') or detail.get('description') or None
    except Exception as e:
        log.warning('Xbox: description fetch failed for appid %s: %s', appid, e)
        return None


def scrape_single(appid):
    """Re-fetch metadata + art + playtime for one Xbox game on demand.
    Shared by the edit modal's per-game refresh button
    (POST /api/xbox/scrape-single/<appid>) and bulk_rescrape_games()'s
    plugin.rescrape(appid) hook. Returns a dict for update_game_data(**meta),
    or None if the game can't be found/re-resolved."""
    from database import get_db
    db  = get_db()
    row = db.execute(
        "SELECT platform_appname, name FROM games WHERE appid = ? AND platform = 'xbox'",
        (appid,)
    ).fetchone()
    db.close()
    if not row or not row['platform_appname']:
        return None
    pfn = row['platform_appname']

    try:
        title = _fetch_single_title(pfn)
    except Exception as e:
        log.warning('Xbox: scrape_single fetch failed for appid %s: %s', appid, e)
        return None
    if not title:
        return None

    meta = _build_metadata(title)
    meta['meta_fetched'] = date.today().isoformat()

    playtimes = _fetch_playtimes(_cfg().get('xuid'), [title['titleId']])
    minutes = playtimes.get(title['titleId'])
    if minutes is not None:
        meta['playtime_forever'] = minutes

    _fetch_art(appid, row['name'] or title['name'], title)
    return meta


# ── Library sync ─────────────────────────────────────────────────────────────

_sync_state = {
    'running': False, 'phase': None, 'done': 0, 'total': 0, 'current_game': '',
    'new_games': 0, 'total_games': 0, 'duplicates_detected': 0, 'error': None,
}
_sync_lock   = threading.Lock()
_sync_cancel = threading.Event()


def get_sync_state():
    with _sync_lock:
        return dict(_sync_state)


def start_library_sync():
    with _sync_lock:
        if _sync_state['running']:
            return {'status': 'already_running'}
        _sync_cancel.clear()
        _sync_state.update({
            'running': True, 'phase': 'fetching', 'done': 0, 'total': 0,
            'current_game': '', 'new_games': 0, 'total_games': 0,
            'duplicates_detected': 0, 'error': None,
        })
    threading.Thread(target=_run_sync, daemon=True).start()
    return {'status': 'started'}


def cancel_library_sync():
    _sync_cancel.set()


def _run_sync():
    try:
        _do_sync()
    except Exception as e:
        log.error('Xbox library sync thread: %s', e, exc_info=True)
        with _sync_lock:
            _sync_state.update({'running': False, 'phase': 'error', 'error': str(e)})


def _do_sync():
    from database import get_db

    titles = fetch_library()
    owned = [t for t in titles if _is_pc_title(t)]
    log.info('Xbox sync: %d of %d titles are real Xbox PC titles (have a pfn)',
             len(owned), len(titles))

    db = get_db()
    try:
        existing = {row['platform_id']: dict(row) for row in db.execute(
            "SELECT appid, platform_id, art_fetched FROM games WHERE platform = 'xbox'"
        ).fetchall()}
        blacklisted = {
            row[0] for row in db.execute(
                "SELECT platform_id FROM blacklist WHERE platform_id IS NOT NULL"
            ).fetchall()
        }
    finally:
        db.close()

    new_items = [t for t in owned
                 if t['titleId'] not in existing and t['titleId'] not in blacklisted]
    total_new = len(new_items)

    with _sync_lock:
        _sync_state.update({'phase': 'processing', 'done': 0, 'total': total_new})

    playtimes = _fetch_playtimes(_cfg().get('xuid'), [t['titleId'] for t in owned])

    today = date.today().isoformat()
    new_games_count = 0

    db = get_db()
    try:
        # Refresh last_played/playtime/metadata on games already in the
        # library -- titlehistory and userstats are re-fetched in full every
        # sync, so a game played again since the last sync reflects that
        # without waiting on a full re-import. Metadata (developers, genres,
        # release date, achievements) is cheap to reapply every time since
        # it's already in hand from this same fetch; art is only backfilled
        # once (art_fetched gate), same as everywhere else in the app, since
        # it costs a network round-trip per game.
        for t in owned:
            row = existing.get(t['titleId'])
            if not row:
                continue
            last_played = _parse_last_played(t)
            minutes = playtimes.get(t['titleId'])
            if last_played and minutes is not None:
                db.execute("UPDATE games SET last_played = ?, playtime_forever = ? WHERE appid = ?",
                           (last_played, minutes, row['appid']))
            elif last_played:
                db.execute("UPDATE games SET last_played = ? WHERE appid = ?",
                           (last_played, row['appid']))
            elif minutes is not None:
                db.execute("UPDATE games SET playtime_forever = ? WHERE appid = ?",
                           (minutes, row['appid']))
            db.commit()

            meta = _build_metadata(t)
            if meta:
                meta['meta_fetched'] = today
                try:
                    update_game_data(row['appid'], **meta)
                except Exception as e:
                    log.warning('Xbox metadata refresh failed for %r: %s', t['name'], e)

            if not row.get('art_fetched') or row['art_fetched'] == '0':
                _fetch_art(row['appid'], t['name'], t)

        for t in new_items:
            if _sync_cancel.is_set():
                break
            with _sync_lock:
                _sync_state['current_game'] = t['name']

            appid = next_negative_appid(db)
            minutes = playtimes.get(t['titleId'], 0)
            completion_status = 'Unfinished' if minutes > 0 else 'Never Played'
            db.execute(
                """INSERT OR IGNORE INTO games
                   (appid, name, platform, platform_id, platform_appname, platform_slug,
                    date_added, last_played, playtime_forever,
                    completion_status, installed,
                    art_fetched, meta_fetched, cheevos_fetched,
                    protondb_fetched, hltb_fetched)
                   VALUES (?, ?, 'xbox', ?, ?, ?, ?, ?, ?, ?, 0,
                           '0', '0', '0', '0', '0')""",
                (appid, t['name'], t['titleId'], t.get('pfn'), _extract_store_id(t),
                 int(datetime.now(timezone.utc).timestamp()), _parse_last_played(t),
                 minutes, completion_status),
            )
            db.commit()

            meta = _build_metadata(t)
            if meta:
                meta['meta_fetched'] = today
                try:
                    update_game_data(appid, **meta)
                except Exception as e:
                    log.warning('Xbox metadata update failed for %r: %s', t['name'], e)

            _fetch_art(appid, t['name'], t)
            log.info('Xbox sync: added %r as appid %d (titleId %s)',
                     t['name'], appid, t['titleId'])
            new_games_count += 1
            with _sync_lock:
                _sync_state['done'] += 1
    finally:
        db.close()

    if _sync_cancel.is_set():
        with _sync_lock:
            _sync_state.update({'running': False, 'phase': 'stopped',
                                'new_games': new_games_count, 'total_games': len(owned)})
        return

    from database import auto_detect_duplicates
    from plugins import get_platform_priority
    dupes = auto_detect_duplicates(platform_priority=get_platform_priority())

    with _sync_lock:
        _sync_state.update({
            'running': False, 'phase': 'done',
            'new_games': new_games_count, 'total_games': len(owned),
            'duplicates_detected': dupes,
        })
