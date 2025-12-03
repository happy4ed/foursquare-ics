Foursquare (Swarm) to Google Calendar Sync 📅

Foursquare(Swarm)의 체크인 기록을 Google 캘린더와 실시간으로 동기화해주는 개인용 서버입니다. 단순한 ICS 구독뿐만 아니라, Google Calendar API를 직접 사용하여 즉각적인 반영(Push)과 과거 데이터 백업까지 지원합니다.

✨ 주요 기능

실시간 동기화 (Real-time Push): Foursquare 앱에서 체크인 후 약 15초 내에 구글 캘린더에 일정이 생성됩니다.

과거 데이터 백필 (History Backfill): 수천 개의 과거 체크인 기록을 한 번에 구글 캘린더로 옮길 수 있습니다.

삭제 연동: Foursquare에서 체크인을 지우면, 구글 캘린더에서도 해당 일정이 자동으로 삭제됩니다.

ICS 구독 지원: 구글 캘린더 외의 다른 캘린더 앱(Outlook, Apple Calendar)에서도 URL 구독 방식으로 볼 수 있습니다.

보안 접속: ACCESS_KEY를 통해 허가된 사용자(및 Foursquare 서버)만 접속하도록 보호합니다.

이중 백업: 로컬에 json 파일로 데이터를 백업하고, 구글 캘린더에도 저장합니다.

🛠️ 설치 및 실행 (Docker / Dockge)

1. 필수 준비물

Foursquare OAuth Token: 개발자 콘솔에서 발급받은 토큰.

Google Service Account Key: 구글 클라우드 콘솔에서 받은 json 파일.

Google Calendar ID: 동기화할 캘린더의 ID (예: user@gmail.com 또는 그룹 ID).

2. 구글 인증 키 파일 준비

서버(Host)의 데이터 폴더에 구글에서 받은 키 파일(xxxx.json)을 service_account.json이라는 이름으로 저장해야 합니다.

# 데이터 폴더 생성 (경로는 Dockge 설정에 따름)
mkdir -p /opt/stacks/foursquare-ics/data

# 파일 생성 및 내용 붙여넣기
nano /opt/stacks/foursquare-ics/data/service_account.json


3. Docker Compose 설정 (Dockge)

docker-compose.yml 파일에 다음 설정을 사용하세요.

version: '3.8'

services:
  foursquare-ics:
    build: [https://github.com/happy4ed/foursquare-ics.git#main](https://github.com/happy4ed/foursquare-ics.git#main)
    container_name: foursquare-ics
    ports:
      - "5120:5120"
    environment:
      # [필수] Foursquare 토큰
      - FS_OAUTH_TOKEN=YOUR_TOKEN_HERE
      # [필수] 보안 접속용 비밀번호 (원하는 값 설정)
      - ACCESS_KEY=MySecretPassword123
      
      # [구글 캘린더 설정]
      - GOOGLE_CALENDAR_ID=your_calendar_id@group.calendar.google.com
      - GOOGLE_CREDENTIALS_FILE=/data/service_account.json
      
      # [동기화 설정]
      - CALENDAR_NAME=My Foursquare Log
      - DATA_DIR=/data
      - PARTIAL_SYNC_MINUTES=10  # 푸시 누락 대비 10분마다 확인
      - FULL_SYNC_MINUTES=10080  # 1주일마다 전체 데이터 점검
      
      # [관리자 옵션] 평소에는 false로 두세요
      - PUSH_HISTORY_TO_GOOGLE=false  # true: 과거 데이터 전체 업로드 (1회용)
      - RESET_DB_ON_STARTUP=false     # true: 데이터 꼬였을 때 초기화
      
    volumes:
      - ./data:/data
    restart: unless-stopped


⚙️ 외부 서비스 설정 (필수)

서버를 띄운 후, 외부 서비스들이 내 서버에 접속할 수 있도록 주소를 설정해야 합니다.

1. Foursquare Developer Console (Push API)

체크인 시 실시간 알림을 받기 위해 설정합니다.

URL: https://내도메인.com/webhook?key=설정한비밀번호

Triggers: Checkins 항목에 반드시 체크해야 합니다.

주의: HTTPS(Caddy 등)가 적용되어 있어야 하며, 포트 번호 없이 접속 가능해야 합니다.

2. Google Calendar (공유 설정)

구글 캘린더가 봇(Service Account)의 쓰기 작업을 허용하도록 설정합니다.

구글 캘린더 > 설정 및 공유.

"특정 사용자와 공유" > 사용자 추가.

service_account.json 안에 있는 client_email 주소를 입력.

권한: "일정 변경 및 공유 관리" 또는 "일정 변경" 권한 부여.

📅 사용 방법

ICS 구독 (기본 뷰어)

웹 브라우저나 캘린더 앱에서 아래 주소로 구독하면, 읽기 전용으로 체크인 기록을 볼 수 있습니다.

https://내도메인.com/foursquare.ics?key=설정한비밀번호

과거 데이터 한 번에 올리기 (Backfill)

처음 설치했거나 과거 기록이 누락되었을 때 사용합니다.

docker-compose.yml에서 PUSH_HISTORY_TO_GOOGLE=true 로 설정 후 배포(Deploy).

로그(docker compose logs -f)에서 "🚀 Starting Google Calendar Backfill..." 메시지 확인.

작업 완료 후 다시 false로 변경하여 재배포.

⚡ 강제 업데이트 스크립트 (Manual Update)

GitHub에 코드를 올렸는데 Dockge가 업데이트를 안 해줄 때, 터미널(LXC Console)에 접속해서 아래 명령어를 입력하세요.

# 1. 스택 폴더로 이동 (경로는 본인 환경에 맞게 수정)
cd /opt/stacks/foursquare-ics

# 2. 캐시 무시하고 강제 재빌드 (핵심!)
docker compose build --no-cache

# 3. 컨테이너 재시작
docker compose up -d


❓ 문제 해결 (Troubleshooting)

Q. 구글 캘린더에 일정이 바로 안 떠요.

A. Push 방식은 10~20초 내에 뜹니다. 만약 ICS 구독 방식만 쓰고 계시다면 구글이 갱신할 때까지(최대 24시간) 기다려야 합니다.

Q. "Sync Triggered"라고 뜨는데 캘린더에 안 생겨요.

A. ACCESS_KEY가 주소 뒤에 ?key=...로 잘 붙어있는지 확인하세요.

A. 구글 캘린더 설정에서 봇 계정(Service Account)에게 "일정 변경" 권한을 줬는지 확인하세요.

Q. ERR_SSL_PROTOCOL_ERROR 에러가 나요.

A. 주소창에 포트 번호(:5120)가 붙어있으면 안 됩니다. Caddy(HTTPS)를 통하고 있다면 도메인만 입력하세요.

Q. 시간이 이상하게(9시간 차이) 나와요.

A. 최신 코드로 업데이트하면 해결됩니다. (timezone.utc 적용 완료)