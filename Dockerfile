FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 TZ=Asia/Tehran
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY roshd_bot ./roshd_bot
CMD ["python", "-m", "roshd_bot.main"]
