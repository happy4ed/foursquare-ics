import os
import json
import time
import threading
from datetime import datetime, timedelta
from flask import Flask, Response, request
import requests
from ics import Calendar, Event
from ics.grammar.parse import ContentLine
from apscheduler.schedulers.background import BackgroundScheduler
import logging

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("FoursquareICS")

app = Flask(__name__)

# --- 환경 변수 ---
FS_OAUTH_TOKEN = os.environ.get('FS_OAUTH_TOKEN')
CALENDAR_NAME = os.environ.get('CALENDAR_NAME', 'My Foursquare History')
PARTIAL_SYNC_MINUTES = int(os.environ.get('PARTIAL_SYNC_MINUTES', 1440))
FULL_SYNC_MINUTES = int(os.environ.get('FULL_SYNC_MINUTES', 10080))
DATA_DIR = os.environ.get('DATA_DIR', '/data')
BACKUP_FILE = os.path.join(DATA_DIR, 'checkins_backup.json')

# --- 전역 데이터 저장소 ---
CHECKIN_DB = {}
RAW_DATA_STORE = {}
CACHED_ICS_STRING = None
DB_LOCK = threading.Lock()

def save_to_disk():
    try:
        if not os.path.exists(DATA_DIR):
            os.makedirs(DATA_DIR)
        with DB_LOCK:
            with open(BACKUP_FILE, 'w', encoding='utf-8') as f:
                json.dump(RAW_DATA_STORE, f, ensure_ascii=False, indent=2)
        logger.info(f"💾 Data saved to disk: {len(RAW_DATA_STORE)} items.")
    except Exception as e:
        logger.error(f"Failed to save backup: {e}")

def load_from_disk():
    global RAW_DATA_STORE, CHECKIN_DB
    if not os.path.exists(BACKUP_FILE):
        logger.info("No backup file found. Starting fresh.")
        return

    try:
        logger.info("📂 Loading data from backup file...")
        with open(BACKUP_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        with DB_LOCK:
            RAW_DATA_STORE = data
            CHECKIN_DB.clear()
            for item in RAW_DATA_STORE.values():
                event = item_to_event(item)
                if event:
                    CHECKIN_DB[event._fs_id] = event
        regenerate_ics_string()
        logger.info(f"✅ Data restored from disk: {len(CHECKIN_DB)} events.")
    except Exception as e:
        logger.error(f"Failed to load backup: {e}")

def get_foursquare_total_count():
    """
    Foursquare 사용자 프로필에서 '총 체크인 수'를 조회합니다.
    """
    if not FS_OAUTH_TOKEN:
        return 0
    
    url = "https://api.foursquare.com/v2/users/self"
    params = {
        'oauth_token': FS_OAUTH_TOKEN,
        'v': '20231010'
    }
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        count = data.get('response', {}).get('user', {}).get('checkins', {}).get('count', 0)
        logger.info(f"🔎 Foursquare Remote Total Count: {count}")
        return count
    except Exception as e:
        logger.error(f"Failed to fetch user profile for count check: {e}")
        return 0

def fetch_checkins_safe(after_timestamp=None, retry=3):
    if not FS_OAUTH_TOKEN:
        logger.error("FS_OAUTH_TOKEN is missing.")
        return None

    url = "https://api.foursquare.com/v2/users/self/checkins"
    limit = 250
    offset = 0
    fetched_items = []
    
    # 로그 메시지: 전체 가져오기인지 부분 가져오기인지 표시
    mode_msg = f"since {after_timestamp}" if after_timestamp else "ALL history (Full Sync)"
    logger.info(f"🔄 Fetching Foursquare data: {mode_msg}")
    
    for attempt in range(retry):
        try:
            current_batch = []
            temp_offset = 0
            
            while True:
                params = {
                    'oauth_token': FS_OAUTH_TOKEN,
                    'v': '20231010',
                    'limit': limit,
                    'sort': 'newestfirst',
                    'offset': temp_offset
                }
                if after_timestamp:
                    params['afterTimestamp'] = int(after_timestamp)

                response = requests.get(url, params=params, timeout=15)
                response.raise_for_status()
                data = response.json()
                items = data.get('response', {}).get('checkins', {}).get('items', [])
                
                if not items:
                    break
                
                # 가져온 개수 로그 찍기 (진행 상황 확인용)
                logger.info(f"   - Fetched {len(items)} items (Offset: {temp_offset})")
                
                current_batch.extend(items)
                
                if len(items) < limit:
                    break
                
                temp_offset += limit
            
            return current_batch

        except requests.exceptions.RequestException as e:
            logger.warning(f"⚠️ Network error (Attempt {attempt+1}/{retry}): {e}")
            time.sleep(2 * (attempt + 1))
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            break
            
    return None

def item_to_event(item):
    try:
        venue = item.get('venue', {})
        checkin_id = item.get('id')
        event = Event()
        event.uid = f"fq-{checkin_id}@foursquare.com"
        venue_name = venue.get('name', 'Unknown Place')
        event.name = f"@{venue_name}"
        timestamp = item.get('createdAt')
        if timestamp:
            event.begin = datetime.fromtimestamp(timestamp)
            event.duration = {"minutes": 15}
        location_parts = venue.get('location', {}).get('formattedAddress', [])
        address = ", ".join(location_parts)
        shout = item.get('shout', '')
        description = []
        if shout: description.append(f"Comment: {shout}")
        if address: description.append(f"Address: {address}")
        description.append(f"Link: https://foursquare.com/v/{venue.get('id', '')}")
        event.description = "\n".join(description)
        event.location = address
        event._fs_id = checkin_id
        event._fs_timestamp = timestamp
        return event
    except Exception as e:
        logger.error(f"Error parsing item: {e}")
        return None

def regenerate_ics_string():
    global CACHED_ICS_STRING
    c = Calendar()
    c.creator = "FoursquareToICS"
    c.extra.append(ContentLine(name="X-WR-CALNAME", value=CALENDAR_NAME))
    with DB_LOCK:
        for event in CHECKIN_DB.values():
            c.events.add(event)
    CACHED_ICS_STRING = str(c)
    logger.info(f"📅 ICS regenerated. Total events: {len(CHECKIN_DB)}")

def perform_full_sync():
    logger.info("🚀 --- Starting FULL SYNC (Getting ALL History) ---")
    items = fetch_checkins_safe(after_timestamp=None)
    
    if items is None:
        logger.warning("Full Sync failed. Skipping.")
        return

    with DB_LOCK:
        CHECKIN_DB.clear()
        RAW_DATA_STORE.clear()
        for item in items:
            event = item_to_event(item)
            if event:
                CHECKIN_DB[event._fs_id] = event
                RAW_DATA_STORE[event._fs_id] = item
    
    regenerate_ics_string()
    save_to_disk()
    logger.info(f"✅ FULL SYNC Completed. Total Items: {len(items)}")

def perform_partial_sync():
    logger.info("--- Starting PARTIAL SYNC (Recent 7 days) ---")
    seven_days_ago = datetime.now() - timedelta(days=7)
    timestamp_threshold = int(seven_days_ago.timestamp())
    
    new_items = fetch_checkins_safe(after_timestamp=timestamp_threshold)
    if new_items is None: return

    new_ids = set()
    with DB_LOCK:
        for item in new_items:
            event = item_to_event(item)
            if event:
                CHECKIN_DB[event._fs_id] = event
                RAW_DATA_STORE[event._fs_id] = item
                new_ids.add(event._fs_id)
        
        ids_to_remove = []
        for eid, event in CHECKIN_DB.items():
            if event._fs_timestamp >= timestamp_threshold:
                if eid not in new_ids:
                    ids_to_remove.append(eid)
        
        for eid in ids_to_remove:
            del CHECKIN_DB[eid]
            if eid in RAW_DATA_STORE: del RAW_DATA_STORE[eid]

    regenerate_ics_string()
    save_to_disk()
    logger.info(f"--- PARTIAL SYNC Completed. Updated: {len(new_items)} ---")

@app.route('/foursquare.ics')
def get_foursquare_ics():
    if CACHED_ICS_STRING is None:
        return Response("Initializing...", status=503)
    return Response(CACHED_ICS_STRING, mimetype='text/calendar')

@app.route('/webhook', methods=['POST', 'GET'])
def webhook_trigger():
    logger.info("📢 Webhook received!")
    threading.Thread(target=perform_partial_sync).start()
    return "Sync Triggered"

@app.route('/')
def index():
    count = len(CHECKIN_DB) if CHECKIN_DB else 0
    return f"Foursquare Sync Running.<br>Total Events: {count}<br>Storage: {BACKUP_FILE}"

def start_schedulers():
    # 1. 디스크 복구 시도
    load_from_disk()
    
    # 2. 스케줄러 설정
    scheduler = BackgroundScheduler()
    scheduler.add_job(func=perform_partial_sync, trigger="interval", minutes=PARTIAL_SYNC_MINUTES)
    scheduler.add_job(func=perform_full_sync, trigger="interval", minutes=FULL_SYNC_MINUTES)
    scheduler.start()
    
    # 3. [스마트 동기화]
    # DB가 비어있거나, 로컬 데이터 개수가 Foursquare 실제 개수보다 적으면 Full Sync 발동
    local_count = len(CHECKIN_DB)
    remote_count = get_foursquare_total_count()
    
    if not CHECKIN_DB or local_count < remote_count:
        logger.info(f"⚡ Data Mismatch Detected (Local: {local_count}, Remote: {remote_count}). Triggering FULL SYNC...")
        threading.Thread(target=perform_full_sync).start()
    else:
        logger.info(f"✅ Data looks consistent (Local: {local_count}). Triggering Partial Sync...")
        threading.Thread(target=perform_partial_sync).start()

if __name__ == "__main__":
    start_schedulers()
    app.run(host='0.0.0.0', port=5120)