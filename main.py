import os
import io
from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from dotenv import load_dotenv
from google import genai
from PIL import Image
from elevenlabs.client import ElevenLabs

load_dotenv()

app = FastAPI()

# Allow your frontend to call this backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # fine for a hackathon; tighten to your actual frontend URL later if you want
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


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/describe")
async def describe(photo: UploadFile = File(...)):
    # 1. Read the uploaded photo
    image_bytes = await photo.read()
    image = Image.open(io.BytesIO(image_bytes))

    # 2. Ask Gemini to describe hazards
    response = gemini_client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=[image, HAZARD_PROMPT]
    )
    description = response.text.strip()

    # 3. Convert the description to speech
    audio_chunks = eleven_client.text_to_speech.convert(
        text=description,
        voice_id="JBFqnCBsd6RMkjVDRZzb",  # "George"
        model_id="eleven_v3",
        output_format="mp3_44100_128"
    )
    audio_bytes = b"".join(audio_chunks)

    # 4. Return the audio directly
    return Response(content=audio_bytes, media_type="audio/mpeg")