import os
import glob
import logging
import asyncio
import nest_asyncio
import json
import base64
import re
from pathlib import Path
from pypdf import PdfReader
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

# Optional web server for Render / Fly
from fastapi import FastAPI

# Google Drive client (optional service account)
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io
import requests

nest_asyncio.apply()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 1. CONFIGURACIÓN DE CLAVES Y MODELO ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME", "gemini-3.6-flash")

# Google Drive env vars
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SERVICE_ACCOUNT_JSON_B64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64")
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")  # folder containing PDFs and instrucciones.txt

# Optional: limit re-downloads; set to "true" to always re-sync
FORCE_DRIVE_SYNC = os.getenv("FORCE_DRIVE_SYNC", "false").lower() == "true"

# If you keep GENAI usage, keep client init here (import as needed)
try:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
except Exception:
    client = None
    logger.info("genai client not configured (missing GEMINI_API_KEY or google-genai package).")

# --- Helpers for service-account Drive access (preferred if available) ---

def load_service_account_info():
    if GOOGLE_SERVICE_ACCOUNT_JSON:
        return json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    if GOOGLE_SERVICE_ACCOUNT_JSON_B64:
        decoded = base64.b64decode(GOOGLE_SERVICE_ACCOUNT_JSON_B64).decode("utf-8")
        return json.loads(decoded)
    return None


def init_drive_service():
    sa_info = load_service_account_info()
    if not sa_info:
        logger.info("No Google service account credentials provided; skipping authenticated Drive sync.")
        return None
    creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    drive = build("drive", "v3", credentials=creds)
    return drive


def list_files_in_folder_authenticated(drive, folder_id):
    q = f"'{folder_id}' in parents and trashed = false"
    files = []
    page_token = None
    while True:
        res = drive.files().list(q=q, fields="nextPageToken, files(id, name, mimeType)", pageToken=page_token).execute()
        files.extend(res.get("files", []))
        page_token = res.get("nextPageToken")
        if not page_token:
            break
    return files


def download_file_to_folder_authenticated(drive, file_id, filename, target_folder="bibliografia"):
    Path(target_folder).mkdir(parents=True, exist_ok=True)
    request = drive.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    try:
        while not done:
            status, done = downloader.next_chunk()
        fh.seek(0)
        target_path = Path(target_folder) / filename
        with open(target_path, "wb") as f:
            f.write(fh.read())
        logger.info(f"Downloaded {filename} to {target_path} (auth)")
        return True
    except Exception as e:
        logger.warning(f"Failed to download {filename} via auth: {e}")
        return False

# --- Helpers for public Drive access (no Google Cloud keys) ---

def list_files_in_public_folder(folder_id):
    """Attempt to extract file IDs from a public Drive folder page by scraping the HTML.
    This is a best-effort approach and may break if Google changes their page structure.
    Returns a list of file IDs (strings).
    """
    url = f"https://drive.google.com/drive/folders/{folder_id}"
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            logger.warning(f"Failed to fetch Drive folder page: status {r.status_code}")
            return []
        html = r.text
        # Find occurrences of /file/d/<id>/ or /open?id=<id>
        ids = set(re.findall(r"/file/d/([A-Za-z0-9_-]{10,})", html))
        ids.update(re.findall(r"\?id=([A-Za-z0-9_-]{10,})", html))
        # Also try to find any doc ids present in the page data
        ids.update(re.findall(r'"(1[A-Za-z0-9_-]{20,})"', html))
        ids_list = list(ids)
        logger.info(f"Scraped {len(ids_list)} file id(s) from public folder page (best-effort).")
        return ids_list
    except Exception as e:
        logger.warning(f"Error scraping Drive folder page: {e}")
        return []


def download_public_drive_file(file_id, target_folder="bibliografia", prefer_name=None):
    Path(target_folder).mkdir(parents=True, exist_ok=True)
    # Use the uc?export=download endpoint
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    try:
        with requests.get(url, stream=True, timeout=30) as r:
            if r.status_code != 200:
                logger.warning(f"Drive download returned status {r.status_code} for {file_id}")
                return False, None
            # Try to get filename from headers
            cd = r.headers.get('content-disposition')
            filename = None
            if cd:
                m = re.search(r'filename\*=UTF-8''(.+)', cd)
                if m:
                    filename = requests.utils.unquote(m.group(1))
                else:
                    m = re.search(r'filename="?([^\";]+)"?', cd)
                    if m:
                        filename = m.group(1)
            if not filename and prefer_name:
                filename = prefer_name
            if not filename:
                # fallback
                filename = f"{file_id}.pdf"

            # Decide target path: instrucciones.txt stored at repo root, otherwise bibliografia/
            if filename.lower() == 'instrucciones.txt':
                target_path = Path(filename)
            else:
                target_path = Path(target_folder) / filename

            # Stream to file
            with open(target_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=32768):
                    if chunk:
                        f.write(chunk)
            logger.info(f"Downloaded public file {filename} to {target_path}")
            return True, filename
    except Exception as e:
        logger.warning(f"Exception downloading public file {file_id}: {e}")
        return False, None

# --- Sync logic that picks method (auth > public scraping > nothing) ---

def sync_drive_files(folder_id):
    # Try authenticated first
    drive = init_drive_service()
    if drive is not None:
        try:
            files = list_files_in_folder_authenticated(drive, folder_id)
            logger.info(f"Found {len(files)} files via authenticated Drive API")
            for f in files:
                file_id = f.get('id')
                name = f.get('name')
                lower_name = (name or '').lower()
                if lower_name.endswith('.pdf'):
                    target = Path('bibliografia') / name
                elif lower_name == 'instrucciones.txt':
                    target = Path('instrucciones.txt')
                else:
                    # skip other files
                    logger.info(f"Skipping non-pdf/instrucciones file (auth): {name}")
                    continue

                if target.exists() and not FORCE_DRIVE_SYNC:
                    logger.info(f"Skipping existing {target}")
                    continue

                download_file_to_folder_authenticated(drive, file_id, name, target_folder='.' if lower_name == 'instrucciones.txt' else 'bibliografia')
            return
        except Exception as e:
            logger.warning(f"Authenticated Drive sync failed: {e}")

    # Fallback to public folder scraping
    if folder_id:
        logger.info("Attempting public Drive folder scraping (no service account).")
        ids = list_files_in_public_folder(folder_id)
        if not ids:
            logger.warning("No file ids found in public folder scraping; skipping Drive sync.")
            return
        for file_id in ids:
            # Try to download each file; Drive will provide filename in headers when possible
            success, filename = download_public_drive_file(file_id, target_folder='bibliografia')
            if not success:
                logger.warning(f"Failed to download public file {file_id}")
    else:
        logger.info("No Drive folder id provided; skipping Drive sync.")

# --- 2. LEER INSTRUCCIONES DEL TUTOR ---
def cargar_instrucciones():
    # Prefer repo-root instrucciones.txt (downloaded from Drive), otherwise check bibliografia/instrucciones.txt
    candidates = [Path('instrucciones.txt'), Path('bibliografia') / 'instrucciones.txt']
    for p in candidates:
        if p.exists():
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    return f.read()
            except Exception as e:
                logger.warning(f"Error reading {p}: {e}")
    return "Sos Episte, tutor socrático en Epistemología (UNS)."

# --- 3. EXTRAER TEXTO DE TODOS LOS PDFS EN LA CARPETA 'bibliografia/' ---
def extraer_texto_bibliografia():
    texto_consolidado = ""
    archivos_pdf = glob.glob("bibliografia/*.pdf")
    
    logger.info(f"Procesando {len(archivos_pdf)} archivos PDF de la carpeta 'bibliografia/'...")
    
    for ruta_pdf in archivos_pdf:
        try:
            reader = PdfReader(ruta_pdf)
            texto_pdf = ""
            for page in reader.pages:
                page_text = page.extract_text()
                if page_text:
                    texto_pdf += page_text + "\n"
            
            nombre_archivo = os.path.basename(ruta_pdf)
            texto_consolidado += f"\n\n--- DOCUMENTO: {nombre_archivo} ---\n{texto_pdf}"
            logger.info(f"Cargado con éxito: {nombre_archivo}")
        except Exception as e:
            logger.warning(f"Error al leer {ruta_pdf}: {e}")
            
    return texto_consolidado

# Sync Drive files before loading bibliografía
if __name__ != "__main__":
    # when imported, do not auto-sync
    pass
else:
    # Only sync when running as script (startup)
    try:
        sync_drive_files(GOOGLE_DRIVE_FOLDER_ID)
    except Exception as e:
        logger.warning(f"Drive sync failed: {e}")

BIBLIOGRAFIA_TEXTO = extraer_texto_bibliografia()
INSTRUCCIONES_BASE = cargar_instrucciones()

SYSTEM_INSTRUCTION = f"""
{INSTRUCCIONES_BASE}

--- BASE DE CONOCIMIENTO DE LA CÁTEDRA (BIBLIOGRAFÍA) ---
Usá EXCLUSIVAMENTE la siguiente información extraída de los textos oficiales para elaborar tus preguntas socráticas, ejemplos y respuestas:
{BIBLIOGRAFIA_TEXTO}
---------------------------------------------------------
"""

# --- 4. MANEJO DE TELEGRAM ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Crear sesión de chat individual para el estudiante
    if client is None:
        await update.message.reply_text("AI client no configurado. Contactá al administrador.")
        return

    context.user_data['chat'] = client.aio.chats.create(
        model=MODEL_NAME,
        config=types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION)
    )
    
    prompt_inicial = "El estudiante quiere preparar el final. Planteá directamente un dilema cotidiano sobre un auto que no arranca a la mañana y hacé la primera pregunta socrática."
    
    response = await context.user_data['chat'].send_message(prompt_inicial)
    await update.message.reply_text(response.text)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if client is None:
        await update.message.reply_text("AI client no configurado. Contactá al administrador.")
        return

    if 'chat' not in context.user_data:
        context.user_data['chat'] = client.aio.chats.create(
            model=MODEL_NAME,
            config=types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION)
        )
    
    try:
        response = await context.user_data['chat'].send_message(update.message.text)
        await update.message.reply_text(response.text)
    except Exception as e:
        logger.exception("Error procesando mensaje")
        await update.message.reply_text("Ocurrió un error al procesar el mensaje. Por favor intentá de nuevo.")

# --- FastAPI wrapper so app can run on platforms that expect a web process (eg. Fly.io) ---
app = FastAPI()
bot_app = None

@app.on_event("startup")
async def startup_event():
    # Sync Drive files at startup
    try:
        sync_drive_files(GOOGLE_DRIVE_FOLDER_ID)
    except Exception as e:
        logger.warning(f"Drive sync failed at startup: {e}")

    # Start the telegram bot if token present
    global bot_app
    if not TELEGRAM_TOKEN:
        logger.warning("TELEGRAM_TOKEN not set; bot will not start.")
        return

    logger.info("Starting Telegram bot application...")
    bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    bot_app.add_handler(CommandHandler('start', start))
    bot_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    # Run polling in background
    asyncio.create_task(bot_app.run_polling())

@app.on_event("shutdown")
async def shutdown_event():
    global bot_app
    if bot_app:
        try:
            await bot_app.stop()
        except Exception:
            pass

@app.get("/")
async def health():
    return {"status": "ok"}
