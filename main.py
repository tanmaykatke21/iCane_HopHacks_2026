import os
import io
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse
from dotenv import load_dotenv
from google import genai
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