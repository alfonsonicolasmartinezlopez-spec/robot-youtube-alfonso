"""Daily video factory for the Canal Automático project."""
from __future__ import annotations
import datetime as dt
import json, os, re, subprocess, textwrap
from pathlib import Path
from zoneinfo import ZoneInfo
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

ROOT = Path.cwd()
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Madrid"))
NOW = dt.datetime.now(TZ)

def env(name: str, required=True) -> str:
    value = os.getenv(name, "").strip()
    if required and not value: raise RuntimeError(f"Missing GitHub secret: {name}")
    return value

SUPABASE_URL = env("SUPABASE_URL")
SUPABASE_KEY = env("SUPABASE_KEY")
GEMINI_API_KEY = env("GEMINI_API_KEY")
ELEVENLABS_API_KEY = env("ELEVENLABS_API_KEY")
VOICE_ID = env("ELEVENLABS_VOICE_ID", False) or "21m00Tcm4TlvDq8ikWAM"
PEXELS_API_KEY = env("PEXELS_API_KEY", False)
LANGUAGE = os.getenv("VIDEO_LANGUAGE", "American English")
CAPTIONS = os.getenv("CAPTIONS", "false").lower() in {"1", "true", "yes", "on"}

DAYS = [["monday","lunes"],["tuesday","martes"],["wednesday","miércoles","miercoles"],["thursday","jueves"],["friday","viernes"],["saturday","sábado","sabado"],["sunday","domingo"]]

def supa(path, params):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}", params=params, headers={"apikey":SUPABASE_KEY,"Authorization":f"Bearer {SUPABASE_KEY}"}, timeout=30)
    r.raise_for_status(); return r.json()

def get_topic():
    for day in DAYS[NOW.weekday()]:
        rows = supa("weekly_plan", {"select":"*", "day":f"eq.{day}", "limit":"1"})
        if rows and (rows[0].get("topic") or "").strip(): return rows[0]["topic"].strip()
    raise RuntimeError("No topic saved for today")

def parse_type(topic):
    upper = topic.upper().strip()
    if upper.startswith("SHORT:"): return "short", topic.split(":",1)[1].strip()
    if upper.startswith("VIDEO:") or upper.startswith("VÍDEO:"): return "video", topic.split(":",1)[1].strip()
    return "short", topic

def gemini(topic, kind):
    duration = "up to 110 seconds" if kind == "short" else "up to 5 minutes"
    prompt = f'''Create an original YouTube {kind} in natural {LANGUAGE} about: {topic}\nDuration: {duration}.\nDo not copy creators or existing videos. Make it engaging, coherent and family-friendly. Include a strong hook, clear story structure and satisfying ending. Return ONLY JSON with title, description, narration, search_terms (array), visual_style.''' 
    r = requests.post("https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent", params={"key":GEMINI_API_KEY}, json={"contents":[{"parts":[{"text":prompt}]}],"generationConfig":{"temperature":0.85}}, timeout=90)
    r.raise_for_status(); text=r.json()["candidates"][0]["content"]["parts"][0]["text"]
    text=re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    data=json.loads(text)
    for k in ("title","description","narration"):
        if not data.get(k): raise RuntimeError(f"Gemini missing {k}")
    return data

def voice(text, out):
    r=requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}",headers={"xi-api-key":ELEVENLABS_API_KEY,"Content-Type":"application/json"},json={"text":text,"model_id":"eleven_multilingual_v2"},timeout=120)
    r.raise_for_status(); out.write_bytes(r.content)

def background(terms, out, kind):
    if PEXELS_API_KEY:
        orientation="portrait" if kind=="short" else "landscape"
        for term in terms or ["cinematic mystery story"]:
            r=requests.get("https://api.pexels.com/videos/search",headers={"Authorization":PEXELS_API_KEY},params={"query":term,"orientation":orientation,"per_page":5},timeout=45)
            if r.ok:
                for v in r.json().get("videos",[]):
                    files=sorted(v.get("video_files",[]),key=lambda x:x.get("width",0),reverse=True)
                    if files:
                        m=requests.get(files[0]["link"],timeout=90)
                        if m.ok: out.write_bytes(m.content); return
    size="1080x1920" if kind=="short" else "1920x1080"
    subprocess.run(["ffmpeg","-y","-f","lavfi","-i",f"color=c=0x17152b:s={size}","-t","1",str(out)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

def srt(text,out,seconds):
    chunks=[x.strip() for x in re.split(r"(?<=[.!?])\s+",text) if x.strip()] or [text]
    def stamp(v):
        ms=int((v%1)*1000); t=int(v); return f"{t//3600:02d}:{(t%3600)//60:02d}:{t%60:02d},{ms:03d}"
    d=seconds/len(chunks); lines=[]
    for i,c in enumerate(chunks,1): lines += [str(i),f"{stamp((i-1)*d)} --> {stamp(i*d)}","\\n".join(textwrap.wrap(c,32)),""]
    out.write_text("\n".join(lines),encoding="utf-8")

def render(bg,audio,captions,out,kind,duration):
    size="1080:1920" if kind=="short" else "1920:1080"
    vf=f"scale={size}:force_original_aspect_ratio=increase,crop={size},eq=saturation=1.08:contrast=1.04"
    if CAPTIONS: vf += f",subtitles={captions.as_posix()}:force_style='FontName=Arial,FontSize=22,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=4,Alignment=2,MarginV=90'"
    subprocess.run(["ffmpeg","-y","-stream_loop","-1","-i",str(bg),"-i",str(audio),"-t",str(duration),"-vf",vf,"-map","0:v:0","-map","1:a:0","-c:v","libx264","-preset","veryfast","-pix_fmt","yuv420p","-c:a","aac","-shortest",str(out)],check=True)

def upload(video,title,description,kind):
    creds=Credentials(None,refresh_token=env("YOUTUBE_REFRESH_TOKEN"),client_id=env("YOUTUBE_CLIENT_ID"),client_secret=env("YOUTUBE_CLIENT_SECRET"),token_uri="https://oauth2.googleapis.com/token")
    yt=build("youtube","v3",credentials=creds)
    publish=NOW.replace(hour=int(os.getenv("PUBLISH_HOUR","19").split(":")[0]),minute=int(os.getenv("PUBLISH_HOUR","19").split(":")[1]),second=0,microsecond=0)
    if publish <= NOW: publish += dt.timedelta(days=1)
    body={"snippet":{"title":title[:100],"description":description,"categoryId":"24","tags":["shorts" if kind=="short" else "youtube","original","AI"]},"status":{"privacyStatus":"private","publishAt":publish.astimezone(dt.timezone.utc).isoformat().replace("+00:00","Z")}}
    res=yt.videos().insert(part="snippet,status",body=body,media_body=MediaFileUpload(str(video),mimetype="video/mp4",resumable=True)).execute()
    print(f"Scheduled: https://youtu.be/{res['id']} at {publish.isoformat()}")

def main():
    raw=get_topic(); kind,topic=parse_type(raw); data=gemini(topic,kind)
    duration=110 if kind=="short" else 300
    audio=ROOT/"voice.mp3"; bg=ROOT/"background.mp4"; sub=ROOT/"captions.srt"; video=ROOT/"video_final.mp4"
    voice(data["narration"],audio); background(data.get("search_terms",[]),bg,kind); srt(data["narration"],sub,duration); render(bg,audio,sub,video,kind,duration); upload(video,data["title"],data["description"],kind)

if __name__=="__main__": main()
