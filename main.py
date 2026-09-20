import os
import io
import time
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
    "You are helping a blind or low-vision person walk safely. Look at the "
    "image and describe ONLY a hazard or obstacle directly in their path ahead.\n"
    "Rules:\n"
    "- Maximum 8 words.\n"
    "- Start directly with the hazard, no preamble.\n"
    "- Never say 'I see', 'I can see', 'based on the image', 'in this image', "
    "or anything similar.\n"
    "- No hedging words like 'appears to be' or 'possibly' — state it plainly.\n"
    "- If there is no hazard, respond with exactly: CLEAR\n"
    "Examples of correct output:\n"
    "Trash can ahead on the left.\n"
    "Person crossing your path.\n"
    "Low branch overhead.\n"
    "CLEAR"
)

DETAILED_PROMPT = (
    "You are helping a blind or low-vision person understand their surroundings. "
    "Describe the scene ahead in exactly 2-3 short sentences: general layout, "
    "notable objects, people, and any hazards, with rough distances if you can "
    "judge them.\n"
    "Rules:\n"
    "- Start directly with the description, no preamble.\n"
    "- Never say 'I see', 'I can see', 'based on the image', 'in this image', "
    "or anything similar.\n"
    "- No hedging words like 'appears to be' or 'possibly' unless genuinely "
    "uncertain.\n"
    "- Be concrete and concise — every word should carry useful information."
)

# Backup filter in case Gemini adds preamble/filler despite the prompt rules
# above — cheap insurance that costs nothing when the prompt already worked,
# and quietly fixes it when it didn't.
_FILLER_PREFIXES = [
    "i can see that",
    "i can see",
    "i see that",
    "i see",
    "based on the image,",
    "based on the image",
    "in this image,",
    "in this image",
    "looking at the image,",
    "looking at the image",
    "the image shows",
]


def strip_filler(text: str) -> str:
    stripped = text.strip()
    lowered = stripped.lower()
    for phrase in _FILLER_PREFIXES:
        if lowered.startswith(phrase):
            stripped = stripped[len(phrase):].lstrip(" ,:-—")
            if stripped:
                stripped = stripped[0].upper() + stripped[1:]
            break
    return stripped


def classify_image(image_bytes: bytes, prompt: str) -> str:
    image = Image.open(io.BytesIO(image_bytes))
    # Gemini occasionally returns a transient 503 ("high demand ... usually
    # temporary") — confirmed directly in testing. Retry once on the same
    # model, then fall back to a different model tier before giving up,
    # instead of hard-failing the whole hazard-detection request on a
    # brief spike.
    models_to_try = ["gemini-3.5-flash-lite", "gemini-3.5-flash-lite", "gemini-3.5-flash"]
    last_error = None
    for i, model_name in enumerate(models_to_try):
        try:
            response = gemini_client.models.generate_content(
                model=model_name,
                contents=[image, prompt]
            )
            return strip_filler(response.text.strip())
        except Exception as e:
            last_error = e
            if i < len(models_to_try) - 1:
                time.sleep(1)
    raise last_error


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