FROM python:3.11-slim

WORKDIR /app

# התקנת תלויות
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# העתקת הקוד
COPY yemot_streaming_app.py .

# חשיפת הפורט
EXPOSE 5000

# הפעלת האפליקציה עם gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "yemot_streaming_app:app"]
