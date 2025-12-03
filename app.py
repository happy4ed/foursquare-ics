import os
import json
import time
import threading
from datetime import datetime, timedelta, timezone
from flask import Flask, Response, request
import requests
from ics import Calendar, Event
from ics.grammar.parse import ContentLine
from apscheduler.schedulers.background import BackgroundScheduler
import logging
from werkzeug.middleware.proxy_fix import ProxyFix

# Google API Libraries
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("FoursquareICS")

app = Flask(__name__)

# 앱이 프록시 뒤에 있음을 명시
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# --- 환경 변수 ---
FS_OAUTH_TOKEN = os.environ.get('FS_OAUTH_TOKEN')
CALENDAR_NAME = os.environ.get('CALENDAR_NAME', 'My Foursquare History')
PARTIAL_SYNC_MINUTES = int(os.environ.get('PARTIAL_SYNC_MINUTES', 10)) # 기본 10분으로 단축
FULL_SYNC_MINUTES = int(os.environ.get('FULL_SYNC_MINUTES', 10080))
DATA_DIR = os.environ.get('DATA_DIR', '/data')
BACKUP_FILE = os.path.join(DATA_DIR, 'checkins_backup.json')
RESET_DB_ON_STARTUP = os.environ.get('RESET_DB_ON_STARTUP', 'false').lower() == 'true'

# [구글 캘린더 설정]
GOOGLE_CREDENTIALS_FILE = os.environ.get('GOOGLE_CREDENTIALS_FILE', '/data/service_account.json')
GOOGLE_CALENDAR_ID = os.environ.get('GOOGLE_CALENDAR_ID')
PUSH_HISTORY_TO_GOOGLE = os.environ.get('PUSH_HISTORY_TO_GOOGLE', 'false').lower() == 'true'

# [보안 설정]
ACCESS_KEY = os.environ.get('ACCESS_KEY')

# --- 전역 데이터 저장소 ---
CHECKIN_DB = {}
RAW_DATA_STORE = {}
CACHED_ICS_STRING = None
DB_LOCK = threading.Lock()

# --- 보안 검사 ---
@app.before_request
def check_access_key():
    if not ACCESS_KEY: return
    request_key = request.args.get('key')
    if request_key != ACCESS_KEY:
        logger.warning(f"⛔ Blocked unauthorized access from {request.remote_addr}")
        return Response("⛔ Access Denied", status=403)

# --- Google Calendar API Helper ---
def get_google_service():
    if not GOOGLE_CREDENTIALS_FILE or not GOOGLE_CALENDAR_ID: return None
    if not os.path.exists(GOOGLE_CREDENTIALS_FILE): return None
    try:
        SCOPES = ['https://www.googleapis.com/auth/calendar']
        creds = service_account.Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=SCOPES)
        return build('calendar', 'v3', credentials=creds)
    except Exception as e:
        logger.error(f"❌ Google Service Error: {e}")
        return None

def push_to_google_calendar(event_obj, checkin_id):
    service = get_google_service()
    if not service: return
    try:
        gcal_event_id = f"fq{checkin_id}".lower().replace('_', '')
        event_body = {
            'summary': event_obj.name,
            'location': event_obj.location,
            'description': event_obj.description,
            'start': {'dateTime': event_obj.begin.isoformat(), 'timeZone': 'UTC'},
            'end': {'dateTime': event_obj.end.isoformat(), 'timeZone': 'UTC'},
            'id': gcal_event_id,
            'reminders': {'useDefault': False}
        }
        service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event_body).execute()
        logger.info(f"✅ Pushed to Google: {event_obj.name}")
    except HttpError as error:
        if error.resp.status != 409: logger.error(f"❌ Google Push Error: {error}")
    except Exception as e: logger.error(f"❌ Push Failed: {e}")

def delete_from_google_calendar(checkin_id):
    service = get_google_service()
    if not service: return
    try:
        gcal_event_id = f"fq{checkin_id}".lower().replace('_', '')
        service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=gcal_event_id).execute()
        logger.info(f"🗑️ Deleted from Google: {checkin_id}")
    except HttpError as error:
        if error.resp.status != 404: logger.error(f"❌ Google Delete Error: {error}")
    except Exception as e: logger.error(f"❌ Delete Failed: {e}")

def backfill_google_calendar():
    logger.info("🚀 Starting Google Backfill...")
    with DB_LOCK: events = list(CHECKIN_DB.values())
    for i, event in enumerate(events):
        push_to_google_calendar(event, event._fs_id)
        if i % 20 == 0: logger.info(f"   - Backfill: {i}/{len(events)}")
        time.sleep(0.2)
    logger.info("✅ Backfill Completed!")

# --- Core Logic ---
def save_to_disk():
    if not os.path.exists(DATA_DIR): os.makedirs(DATA_DIR)
    with DB_LOCK:
        with open(BACKUP_FILE, 'w', encoding='utf-8') as f:
            json.dump(RAW_DATA_STORE, f, ensure_ascii=False, indent=2)

def load_from_disk():
    global RAW_DATA_STORE, CHECKIN_DB
    if not os.path.exists(BACKUP_FILE): return
    try:
        with open(BACKUP_FILE, 'r', encoding='utf-8') as f: data = json.load(f)
        with DB_LOCK:
            RAW_DATA_STORE = data
            CHECKIN_DB.clear()
            for item in RAW_DATA_STORE.values():
                event = item_to_event(item)
                if event: CHECKIN_DB[event._fs_id] = event
        regenerate_ics_string()
        logger.info(f"✅ Data restored: {len(CHECKIN_DB)} events")
    except: pass

def get_foursquare_total_count():
    if not FS_OAUTH_TOKEN: return 0
    try:
        res = requests.get("https://api.foursquare.com/v2/users/self", params={'oauth_token': FS_OAUTH_TOKEN, 'v': '20231010'}, timeout=10)
        return res.json().get('response', {}).get('user', {}).get('checkins', {}).get('count', 0)
    except: return 0

def fetch_checkins_safe(after_timestamp=None):
    if not FS_OAUTH_TOKEN: return None
    limit, offset, items = 250, 0, []
    logger.info(f"🔄 Fetching data (Since: {after_timestamp})...")
    try:
        while True:
            params = {'oauth_token': FS_OAUTH_TOKEN, 'v': '20231010', 'limit': limit, 'sort': 'newestfirst', 'offset': offset}
            if after_timestamp: params['afterTimestamp'] = int(after_timestamp)
            res = requests.get("https://api.foursquare.com/v2/users/self/checkins", params=params, timeout=15)
            res.raise_for_status()
            batch = res.json().get('response', {}).get('checkins', {}).get('items', [])
            if not batch: break
            items.extend(batch)
            if len(batch) < limit: break
            offset += limit
        return items
    except Exception as e:
        logger.error(f"Fetch Error: {e}")
        return None

def item_to_event(item):
    try:
        v = item.get('venue', {})
        e = Event()
        e.uid = f"fq-{item.get('id')}@foursquare.com"
        e.name = f"@{v.get('name', 'Unknown')}"
        if item.get('createdAt'):
            e.begin = datetime.fromtimestamp(item.get('createdAt'), timezone.utc)
            e.duration = {"minutes": 15}
        addr = ", ".join(v.get('location', {}).get('formattedAddress', []))
        desc = []
        if item.get('shout'): desc.append(f"Comment: {item.get('shout')}")
        if addr: desc.append(f"Address: {addr}")
        desc.append(f"Link: https://foursquare.com/v/{v.get('id', '')}")
        e.description = "\n".join(desc)
        e.location = addr
        e._fs_id = item.get('id')
        e._fs_timestamp = item.get('createdAt')
        return e
    except: return None

def regenerate_ics_string():
    global CACHED_ICS_STRING
    c = Calendar()
    c.creator = "FoursquareToICS"
    c.extra.append(ContentLine(name="X-WR-CALNAME", value=CALENDAR_NAME))
    c.extra.append(ContentLine(name="X-WR-TIMEZONE", value="Asia/Seoul"))
    with DB_LOCK:
        for e in CHECKIN_DB.values(): c.events.add(e)
    CACHED_ICS_STRING = str(c)

def perform_full_sync():
    items = fetch_checkins_safe()
    if items is None: return
    with DB_LOCK:
        CHECKIN_DB.clear()
        RAW_DATA_STORE.clear()
        for item in items:
            e = item_to_event(item)
            if e:
                CHECKIN_DB[e._fs_id] = e
                RAW_DATA_STORE[e._fs_id] = item
    regenerate_ics_string()
    save_to_disk()
    logger.info(f"✅ Full Sync Done: {len(items)} items")
    if PUSH_HISTORY_TO_GOOGLE: threading.Thread(target=backfill_google_calendar).start()

def perform_partial_sync():
    ts = int((datetime.now() - timedelta(days=7)).timestamp())
    items = fetch_checkins_safe(ts)
    if items is None: return
    new_ids = set()
    with DB_LOCK:
        for item in items:
            e = item_to_event(item)
            if e:
                is_new = e._fs_id not in CHECKIN_DB
                CHECKIN_DB[e._fs_id] = e
                RAW_DATA_STORE[e._fs_id] = item
                new_ids.add(e._fs_id)
                if is_new: threading.Thread(target=push_to_google_calendar, args=(e, e._fs_id)).start()
        
        ids_del = [eid for eid, e in CHECKIN_DB.items() if e._fs_timestamp >= ts and eid not in new_ids]
        for eid in ids_del:
            threading.Thread(target=delete_from_google_calendar, args=(eid,)).start()
            del CHECKIN_DB[eid]
            if eid in RAW_DATA_STORE: del RAW_DATA_STORE[eid]
    regenerate_ics_string()
    save_to_disk()
    logger.info(f"--- Partial Sync Done. Updated: {len(items)} ---")

def webhook_worker():
    logger.info("⏳ Webhook received. Waiting 15s...")
    time.sleep(15)
    logger.info("▶️ Starting Delayed Sync...")
    perform_partial_sync()

@app.route('/foursquare.ics')
def get_ics():
    if CACHED_ICS_STRING is None: return Response("Initializing...", 503)
    return Response(CACHED_ICS_STRING, mimetype='text/calendar', headers={"Cache-Control": "no-cache"})

@app.route('/webhook', methods=['POST', 'GET'])
def webhook():
    threading.Thread(target=webhook_worker).start()
    return "Sync Triggered"

@app.route('/')
def index(): return f"Foursquare Sync Running. Events: {len(CHECKIN_DB)}"

def start_schedulers():
    if RESET_DB_ON_STARTUP and os.path.exists(BACKUP_FILE):
        try: os.remove(BACKUP_FILE)
        except: pass
    load_from_disk()
    sched = BackgroundScheduler()
    sched.add_job(perform_partial_sync, 'interval', minutes=PARTIAL_SYNC_MINUTES)
    sched.add_job(perform_full_sync, 'interval', minutes=FULL_SYNC_MINUTES)
    sched.start()
    if not CHECKIN_DB or len(CHECKIN_DB) < get_foursquare_total_count() or RESET_DB_ON_STARTUP:
        threading.Thread(target=perform_full_sync).start()
    else:
        threading.Thread(target=perform_partial_sync).start()

if __name__ == "__main__":
    start_schedulers()
    app.run(host='0.0.0.0', port=5120)