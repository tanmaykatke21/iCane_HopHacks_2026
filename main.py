import os
import io
import math
import requests
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse
from dotenv import load_dotenv
from google import genai
from google.genai import types
from PIL import Image
from elevenlabs.client import ElevenLabs

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

gemini_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
eleven_client = ElevenLabs(api_key=os.environ.get("ELEVENLABS_API_KEY"))

HAZARD_PROMPT = (
    "You are helping a blind or low-vision person walk safely. "
    "Describe only hazards or obstacles directly in their path ahead, "
    "in one short, clear sentence. If the path is clear, respond with exactly: CLEAR."
)

DETAILED_PROMPT = (
    "You are helping a blind or low-vision person understand their surroundings. "
    "Describe the scene ahead in 2-3 clear sentences: general layout, notable objects, "
    "people, and any hazards, with rough distances if you can judge them."
)


def classify_image(image_bytes: bytes, prompt: str) -> str:
    image = Image.open(io.BytesIO(image_bytes))
    response = gemini_client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=[image, prompt]
    )
    return response.text.strip()


def synthesize_speech(text: str) -> bytes:
    # eleven_flash_v2_5 trades a little vocal expressiveness for far lower
    # synthesis latency (sub-100ms model time vs. eleven_v3's much slower,
    # more expressive generation) — the right tradeoff for a real-time
    # hazard-warning device where speed matters more than polish.
    audio_chunks = eleven_client.text_to_speech.convert(
        text=text,
        voice_id="JBFqnCBsd6RMkjVDRZzb",
        model_id="eleven_flash_v2_5",
        output_format="mp3_44100_128"
    )
    return b"".join(audio_chunks)


@app.get("/health")
def health():
    return {"status": "ok"}


# Original combined endpoint — kept as-is so nothing that already depends on
# it breaks. The continuous monitoring loop no longer uses this directly.
@app.post("/describe")
async def describe(photo: UploadFile = File(...)):
    image_bytes = await photo.read()
    description = classify_image(image_bytes, HAZARD_PROMPT)
    audio_bytes = synthesize_speech(description)
    return Response(content=audio_bytes, media_type="audio/mpeg")


# Fast, text-only classification — no ElevenLabs call, so this is cheap and
# quick enough to poll every ~2 seconds. The frontend uses this for the
# continuous loop and decides client-side whether the result is worth
# actually speaking out loud.
@app.post("/classify")
async def classify(photo: UploadFile = File(...)):
    image_bytes = await photo.read()
    description = classify_image(image_bytes, HAZARD_PROMPT)
    return JSONResponse({"description": description})


# Given text, returns spoken audio. Called only when the frontend's debounce
# logic decides a change is actually worth announcing.
@app.post("/speak")
async def speak(text: str = Form(...)):
    audio_bytes = synthesize_speech(text)
    return Response(content=audio_bytes, media_type="audio/mpeg")


# On-demand "describe my surroundings" trigger — a fuller description than
# the one-line hazard alert, always spoken immediately regardless of the
# debounce state, since it's an explicit user request.
@app.post("/describe-detailed")
async def describe_detailed(photo: UploadFile = File(...)):
    image_bytes = await photo.read()
    description = classify_image(image_bytes, DETAILED_PROMPT)
    audio_bytes = synthesize_speech(description)
    return Response(content=audio_bytes, media_type="audio/mpeg")


# ============================================================================
# NEW: Voice-activated navigation. Everything below is purely additive —
# nothing above this line was changed. If any of this breaks, the existing
# hazard-detection endpoints keep working exactly as before.
# ============================================================================

def transcribe_destination(audio_bytes: bytes, mime_type: str) -> str:
    # Gemini can transcribe AND extract intent in a single call — no
    # separate speech-to-text service needed. gemini-3.5-flash-lite was
    # tried first but silently ignored the audio and hallucinated a
    # plausible-sounding destination instead of erroring (confirmed via
    # curl: same audio file gave a different wrong answer every call).
    # gemini-3.5-flash actually transcribes correctly and consistently.
    response = gemini_client.models.generate_content(
        model="gemini-3.5-flash",
        contents=[
            types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
            "The audio is a spoken request from a blind or low-vision person "
            "asking to be guided somewhere. Transcribe it and respond with "
            "ONLY the destination they want to go to, in a few plain words "
            "(e.g. 'nearest park', 'the pharmacy', '123 Main Street'). "
            "No other text, no punctuation beyond what's needed."
        ]
    )
    return response.text.strip()


# Keyword -> OpenStreetMap tag, for "nearest <category>" style requests.
# Free-text destinations that don't match any of these fall through to a
# plain place-name search instead.
POI_CATEGORIES = {
    "park": ("leisure", "park"),
    "hospital": ("amenity", "hospital"),
    "pharmacy": ("amenity", "pharmacy"),
    "restaurant": ("amenity", "restaurant"),
    "cafe": ("amenity", "cafe"),
    "coffee": ("amenity", "cafe"),
    "grocery": ("shop", "supermarket"),
    "supermarket": ("shop", "supermarket"),
    "bus stop": ("highway", "bus_stop"),
    "bathroom": ("amenity", "toilets"),
    "restroom": ("amenity", "toilets"),
    "toilet": ("amenity", "toilets"),
    "bank": ("amenity", "bank"),
    "atm": ("amenity", "atm"),
}


def haversine_distance_m(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def find_nearest_poi(lat: float, lon: float, category: tuple, radius_m: int = 3000):
    key, value = category
    query = f"""
    [out:json][timeout:10];
    (
      node(around:{radius_m},{lat},{lon})["{key}"="{value}"];
      way(around:{radius_m},{lat},{lon})["{key}"="{value}"];
    );
    out center 20;
    """
    resp = requests.post(
        "https://overpass-api.de/api/interpreter",
        data={"data": query},
        headers={"User-Agent": "WalkAssistCane-HopHacks2026/1.0 (hackathon project)"},
        timeout=12,
    )
    resp.raise_for_status()
    elements = resp.json().get("elements", [])

    best = None
    best_dist = None
    for el in elements:
        el_lat = el.get("lat") or el.get("center", {}).get("lat")
        el_lon = el.get("lon") or el.get("center", {}).get("lon")
        if el_lat is None or el_lon is None:
            continue
        dist = haversine_distance_m(lat, lon, el_lat, el_lon)
        if best_dist is None or dist < best_dist:
            best_dist = dist
            name = el.get("tags", {}).get("name", value.replace("_", " ").title())
            best = {"name": name, "lat": el_lat, "lon": el_lon, "distance_m": round(dist)}
    return best


def geocode_place(query: str, lat: float, lon: float):
    headers = {"User-Agent": "WalkAssistCane-HopHacks2026/1.0 (hackathon project)"}
    params = {
        "q": query,
        "format": "json",
        "limit": 1,
        "viewbox": f"{lon - 0.1},{lat + 0.1},{lon + 0.1},{lat - 0.1}",
        "bounded": 0,
    }
    resp = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params=params,
        headers=headers,
        timeout=10,
    )
    resp.raise_for_status()
    results = resp.json()
    if not results:
        return None
    r = results[0]
    r_lat, r_lon = float(r["lat"]), float(r["lon"])
    return {
        "name": r.get("display_name", query),
        "lat": r_lat,
        "lon": r_lon,
        "distance_m": round(haversine_distance_m(lat, lon, r_lat, r_lon)),
    }


@app.post("/voice-command")
async def voice_command(audio: UploadFile = File(...)):
    audio_bytes = await audio.read()
    mime_type = audio.content_type or "audio/webm"
    destination = transcribe_destination(audio_bytes, mime_type)
    return JSONResponse({"destination": destination})


@app.post("/find-destination")
async def find_destination(query: str = Form(...), lat: float = Form(...), lon: float = Form(...)):
    normalized = query.strip().lower()
    matched_category = None
    for keyword, category in POI_CATEGORIES.items():
        if keyword in normalized:
            matched_category = category
            break

    try:
        result = None
        if matched_category:
            result = find_nearest_poi(lat, lon, matched_category)
        if not result:
            result = geocode_place(query, lat, lon)
        if not result:
            return JSONResponse({"error": "Could not find that destination"}, status_code=404)
        return JSONResponse(result)
    except requests.RequestException as e:
        return JSONResponse({"error": f"Location lookup failed: {str(e)}"}, status_code=502)