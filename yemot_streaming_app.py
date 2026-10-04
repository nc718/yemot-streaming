import asyncio
import ssl
import sys
import threading
import os
from datetime import datetime
from typing import Optional
from flask import Flask, jsonify, request
import requests
import numpy as np
from google import genai
from google.genai import types

# הגדרת UTF-8 ל-Windows
if sys.platform == 'win32':
    import locale
    try:
        locale.setlocale(locale.LC_ALL, 'he_IL.UTF-8')
    except:
        try:
            locale.setlocale(locale.LC_ALL, 'Hebrew_Israel.1255')
        except:
            pass

# Monkey patch ל-SSL
_original_create_default_context = ssl.create_default_context

def _create_unverified_context(*args, **kwargs):
    context = _original_create_default_context(*args, **kwargs)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context

ssl.create_default_context = _create_unverified_context

app = Flask(__name__)

# הגדרות
YMOT_TOKEN = os.getenv('YMOT_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

class YemotRealtimeStreamer:
    """
    מושך הקלטה בזמן אמת מימות ושולח ל-Gemini Live API
    """
    def __init__(self, yemot_token: str, gemini_api_key: str):
        self.yemot_token = yemot_token
        self.gemini_api_key = gemini_api_key
        self.base_url = "https://www.call2all.co.il/ym/api/"
        self.client = genai.Client(api_key=gemini_api_key)
        self.session = None
        
        # מעקב אחרי הגודל האחרון של ה-WAV המקורי
        self.last_wav_size = 0
        self.current_file = None
        self.final_transcription = None
        self.is_running = False
        
        # הגדרות המודל
        self.model = "gemini-3.5-transcribe-live"
        self.config = types.LiveConnectConfig(
            response_modalities=["TEXT"],
        )
    
    def get_latest_recording(self) -> Optional[str]:
        """
        מקבל את שם ההקלטה החדשה ביותר מתקיית Record בסל המחזור
        """
        url = f"{self.base_url}GetIVR2Dir"
        params = {
            'token': self.yemot_token,
            'path': 'ivr2:Trash/Record'
        }
        
        try:
            response = requests.post(url, data=params, verify=False)
            if response.status_code == 200:
                data = response.json()
                if data.get('responseStatus') == 'OK':
                    files = data.get('files', [])
                    if files:
                        # מיון לפי תאריך וזמן
                        files_with_dates = []
                        for f in files:
                            if 'date' in f:
                                try:
                                    # הפורמט הוא DD/MM/YYYY HH:MM
                                    dt = datetime.strptime(f['date'], '%d/%m/%Y %H:%M')
                                    files_with_dates.append((dt, f['name']))
                                except:
                                    pass
                        
                        if files_with_dates:
                            files_with_dates.sort(key=lambda x: x[0], reverse=True)
                            return files_with_dates[0][1]
        except Exception as e:
            pass
        
        return None
    
    def download_file(self, file_name: str) -> Optional[bytes]:
        """
        מוריד קובץ מה-API של ימות
        """
        url = f"{self.base_url}DownloadFile"
        params = {
            'token': self.yemot_token,
            'path': f'ivr2:Trash/Record/{file_name}'
        }
        
        try:
            response = requests.post(url, data=params, verify=False)
            if response.status_code == 200:
                return response.content
            return None
        except Exception as e:
            pass
            return None
    
    def convert_wav_to_pcm(self, wav_data: bytes, offset_bytes: int = 0) -> tuple:
        """
        ממיר WAV ל-PCM raw על ידי קריאת נתונים גולמיים
        """
        # דילוג על ה-header (44 bytes) וה-offset
        start_pos = 44 + offset_bytes
        if start_pos < len(wav_data):
            raw_data = wav_data[start_pos:]
            # המרה ל-int16 array
            audio_array = np.frombuffer(raw_data, dtype=np.int16)
            # המרה ל-16000Hz (מניחים 8000Hz כברירת מחדל להקלטות טלפון)
            audio_array_resampled = np.interp(
                np.linspace(0, len(audio_array) - 1, int(len(audio_array) * 2)),
                np.arange(len(audio_array)),
                audio_array
            ).astype(np.int16)
            return audio_array_resampled.tobytes(), 16000
        else:
            return b'', 16000
    
    async def send_audio_chunk(self, audio_data: bytes, sample_rate: int = 16000):
        """
        שולח דגימות אודיו בבת אחת
        """
        if not self.session:
            return
        
        try:
            print(f"שולח אודיו: {len(audio_data)} bytes, sample_rate: {sample_rate}")
            await self.session.send_realtime_input(
                audio=types.Blob(data=audio_data, mime_type='audio/pcm;rate=16000')
            )
            print("אודיו נשלח בהצלחה")
        except Exception as e:
            print(f"שגיאה בשליחת אודיו: {e}")
    
    async def receive_transcription(self):
        """
        מקבל תמלול ברקע
        """
        if not self.session:
            return
        
        try:
            async for response in self.session.receive():
                if hasattr(response, 'server_content'):
                    server_content = response.server_content
                    if server_content:
                        # בדיקת interim_input_transcription
                        if hasattr(server_content, 'interim_input_transcription') and server_content.interim_input_transcription:
                            print(f"תמלול זמני: {server_content.interim_input_transcription.text}")
                        # בדיקת input_transcription
                        elif hasattr(server_content, 'input_transcription') and server_content.input_transcription:
                            transcription_text = server_content.input_transcription.text
                            self.final_transcription = transcription_text
        except Exception as e:
            pass
    
    async def start_streaming(self, check_interval: int = 1.5):
        """
        מפעיל סטרימינג בזמן אמת
        """
        print("מפעיל סטרימינג בזמן אמת...")
        self.is_running = True
        
        # פתיחת session אחד לכל הזמן
        async with self.client.aio.live.connect(model=self.model, config=self.config) as session:
            self.session = session
            print("התחבר ל-Gemini Live API")
            
            # הפעלת קבלת תמלול ברקע
            transcription_task = None
            no_change_count = 0
            max_no_change = 1
            
            # קבלת ההקלטה החדשה ביותר
            latest_file = self.get_latest_recording()
            if latest_file:
                self.current_file = latest_file
                self.last_wav_size = 0
                print(f"הקלטה נבחרה: {latest_file}")
            else:
                print("לא נמצאה הקלטה")
                self.is_running = False
                return
            
            try:
                while self.is_running:
                    print(f"בודק תוספות... (קובץ: {self.current_file})")
                    
                    # הורדת הקובץ הנוכחי
                    wav_data = self.download_file(self.current_file)
                    if wav_data:
                        wav_size = len(wav_data) - 44
                        new_bytes = wav_size - self.last_wav_size
                        
                        if new_bytes > 0 or self.last_wav_size == 0:
                            pcm_data, sample_rate = self.convert_wav_to_pcm(wav_data, self.last_wav_size)
                            await self.send_audio_chunk(pcm_data, sample_rate)
                            self.last_wav_size = wav_size
                            no_change_count = 0
                            
                            if transcription_task is None:
                                transcription_task = asyncio.create_task(self.receive_transcription())
                        elif wav_size < self.last_wav_size:
                            self.last_wav_size = wav_size
                            no_change_count = 0
                        else:
                            no_change_count += 1
                            print("אין תוספת")
                            
                            if no_change_count >= max_no_change:
                                print("סימון סוף סטרים...")
                                await self.session.send_realtime_input(audio_stream_end=True)
                                await asyncio.sleep(2)
                                print(f"תמלול סופי: {self.final_transcription if self.final_transcription else 'לא התקבל'}")
                                self.is_running = False
                                break
                    
                    await asyncio.sleep(check_interval)
            except Exception as e:
                pass
                self.is_running = False

# משתנה גלובלי ל-streamer
streamer = None

@app.route('/start', methods=['POST'])
def start_streaming():
    """
    מפעיל סטרימינג חדש
    """
    global streamer
    
    if streamer is not None and streamer.is_running:
        return jsonify({'status': 'error', 'message': 'הסטרימינג כבר פעיל'})
    
    streamer = YemotRealtimeStreamer(YMOT_TOKEN, GEMINI_API_KEY)
    
    # הרצת הסטרימינג ב-thread נפרד עם המתנה של 3 שניות
    def run_streaming():
        import time
        time.sleep(3)
        asyncio.run(streamer.start_streaming())
    
    thread = threading.Thread(target=run_streaming)
    thread.daemon = True
    thread.start()
    
    return "go_to_folder=/1&"

@app.route('/status', methods=['GET'])
def get_status():
    """
    מחזיר את הסטטוס הנוכחי
    """
    global streamer
    
    if streamer is None:
        return jsonify({'status': 'idle', 'message': 'לא פעיל'})
    
    if streamer.is_running:
        return jsonify({
            'status': 'running',
            'message': 'סטרימינג פעיל',
            'current_file': streamer.current_file,
            'final_transcription': streamer.final_transcription
        })
    else:
        return jsonify({
            'status': 'completed',
            'message': 'הסטרימינג הסתיים',
            'final_transcription': streamer.final_transcription
        })

@app.route('/transcription', methods=['GET'])
def get_transcription():
    """
    מחזיר את התמלול הסופי
    """
    global streamer
    
    if streamer is None:
        return jsonify({'status': 'error', 'message': 'לא פעיל'})
    
    return jsonify({
        'transcription': streamer.final_transcription if streamer.final_transcription else 'לא התקבל'
    })

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000)
