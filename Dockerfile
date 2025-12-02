FROM python:3.9-slim

WORKDIR /app

# 시간대 설정 (로그 시간이 한국 시간으로 나오도록 설정)
ENV TZ=Asia/Seoul
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# 포트 5120 노출
EXPOSE 5120

# 앱 실행 시 5120 포트로 동작하도록 코드 수정됨
CMD ["python", "app.py"]