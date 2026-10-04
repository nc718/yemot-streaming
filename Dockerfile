FROM python:3.11-slim

WORKDIR /app

# התקנת תלויות
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# העתקת הקוד
COPY yemot_streaming_app.py .

# חשיפת הפורט
EXPOSE 5000

# הפעלת האפליקציה
CMD ["python", "yemot_streaming_app.py"]
