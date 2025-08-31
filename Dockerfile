# از یک ایمیج سبک پایتون استفاده می‌کنیم
FROM python:3.11-slim

# تنظیمات محیطی (از utf-8 استفاده شود)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC

# نصب پکیج‌های سیستمی لازم
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    aria2 \
    && rm -rf /var/lib/apt/lists/*

# ساخت پوشه کاری
WORKDIR /app

# کپی requirements و نصب پکیج‌ها
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# کپی سورس کد
COPY . .

# فرمان اجرای بات
CMD ["python", "bot.py"]
