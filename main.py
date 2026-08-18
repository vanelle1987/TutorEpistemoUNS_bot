import os
import glob
import logging
import asyncio
import nest_asyncio
import json
import base64
from pathlib import Path
from pypdf import PdfReader
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

# Google Drive client
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io

nest_asyncio.apply()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 1. CONFIGURACIÓN DE CLAVES Y MODELO ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME", "gemini-3.6-flash")

# Google Drive env vars
# You can provide the service account JSON either as raw JSON in GOOGLE_SERVICE_ACCOUNT_JSON
# or as base64-encoded JSON in GOOGLE_SERVICE_ACCOUNT_JSON_B64.
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

# --- Google Drive helpers ---
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
        logger.info("No Google service account credentials provided; skipping Drive sync.")
        return None
    creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    drive = build("drive", "v3", credentials=creds)
    return drive


def list_files_in_folder(drive, folder_id):
    # Query for files in the folder (not trashed)
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


def download_file_to_folder(drive, file_id, filename, target_folder="bibliografia"):
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
        logger.info(f"Downloaded {filename} to {target_path}")
        return True
    except Exception as e:
        logger.warning(f"Failed to download {filename}: {e}")
        return False


def sync_drive_files(folder_id):
    drive = init_drive_service()
    if drive is None:
        return
    if not folder_id:
        logger.warning("No GOOGLE_DRIVE_FOLDER_ID provided; skipping Drive sync.")
        return

    # List files and download PDFs + instrucciones.txt
    files = list_files_in_folder(drive, folder_id)
    logger.info(f"Found {len(files)} file(s) in Drive folder {folder_id}.")
    for f in files:
        file_id = f.get("id")
        name = f.get("name")
        lower_name = name.lower() if name else ""
        should_download = False
        if lower_name.endswith('.pdf'):
            should_download = True
        if lower_name == 'instrucciones.txt':
            should_download = True

        if not should_download:
            logger.info(f"Skipping Drive file (not pdf/instrucciones): {name}")
            continue

        target_path = Path("bibliografia") / name if lower_name.endswith('.pdf') else Path(name)
        # If instrucciones.txt, save at repo root so cargar_instrucciones picks it up; otherwise save in bibliografia/
        if name.lower() == 'instrucciones.txt':
            local_exists = Path(name).exists()
        else:
            local_exists = Path("bibliografia") / name

        if local_exists and not FORCE_DRIVE_SYNC:
            logger.info(f"Skipping existing file {name}")
            continue

        # Download
        if name.lower() == 'instrucciones.txt':
            # download to repo root
            success = download_file_to_folder(drive, file_id, name, target_folder='.')
        else:
            success = download_file_to_folder(drive, file_id, name, target_folder='bibliografia')

        if not success:
            logger.warning(f"Failed to download {name}")

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

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    
    logger.info("🤖 Bot activo y escuchando en Telegram con la bibliografía sincronizada desde Google Drive...")
    app.run_polling(drop_pending_updates=True)
