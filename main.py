from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl
from typing import List
import os
import asyncio
import aiohttp
import logging
from dotenv import load_dotenv
import psutil
import sys
import fitz
import io
import base64

# Import Google Drive API libraries
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

app = FastAPI()

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)


class PDFUrl(BaseModel):
    url: HttpUrl


# Load Google Service Account Credentials
SERVICE_ACCOUNT_INFO = os.getenv("SERVICE_ACCOUNT_INFO")
if not SERVICE_ACCOUNT_INFO:
    raise Exception("Service account info not found in environment variables.")

import json

service_account_info = json.loads(SERVICE_ACCOUNT_INFO)
credentials = service_account.Credentials.from_service_account_info(
    service_account_info,
    scopes=["https://www.googleapis.com/auth/drive"]
)

# Initialize Google Drive API client
drive_service = build('drive', 'v3', credentials=credentials)


def log_resource_usage(stage):
    process = psutil.Process(os.getpid())
    mem_info = process.memory_info()
    cpu_percent = process.cpu_percent(interval=None)
    logging.info(f"{stage} - Memory Usage: {mem_info.rss / (1024 * 1024):.2f} MB, CPU Usage: {cpu_percent}%")


async def download_pdf(url: str) -> bytes:
    try:
        url = str(url)
        headers = {'User-Agent': 'Mozilla/5.0'}
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, allow_redirects=True) as response:
                logging.info(f"Response status: {response.status}")
                logging.info(f"Response headers: {response.headers}")
                if response.status != 200:
                    raise HTTPException(status_code=400,
                                        detail=f"Failed to download PDF. Status code: {response.status}")
                content_type = response.headers.get('Content-Type', '')
                logging.info(f"Content-Type: {content_type}")
                if 'pdf' not in content_type.lower():
                    raise HTTPException(status_code=400,
                                        detail=f"URL does not point to a PDF file. Content-Type: {content_type}")
                pdf_bytes = await response.read()
                MAX_PDF_SIZE = 10 * 1024 * 1024  # 10 MB
                if len(pdf_bytes) > MAX_PDF_SIZE:
                    raise HTTPException(status_code=400, detail="PDF file is too large.")
                return pdf_bytes
    except aiohttp.ClientError as e:
        logging.error(f"Client error: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail="Client error occurred while downloading PDF.")
    except Exception as e:
        logging.error(f"Unexpected error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unexpected error occurred.")


async def convert_pdf_to_images(pdf_bytes: bytes) -> List[bytes]:
    try:
        log_resource_usage("Before Conversion")
        image_bytes_list = []
        MAX_PAGE_COUNT = 300  # Limit the number of pages to process
        DPI = 100  # Set DPI to reduce resource usage
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            page_count = doc.page_count
            logging.info(f"PDF has {page_count} pages.")
            if page_count > MAX_PAGE_COUNT:
                raise HTTPException(
                    status_code=400,
                    detail=f"PDF has too many pages ({page_count}). Maximum allowed is {MAX_PAGE_COUNT}."
                )
            for page_num in range(min(page_count, MAX_PAGE_COUNT)):
                page = doc.load_page(page_num)
                pix = page.get_pixmap(dpi=DPI)
                image_bytes = pix.tobytes("png")
                image_bytes_list.append(image_bytes)
        log_resource_usage("After Conversion")
        return image_bytes_list
    except Exception as e:
        logging.error(f"Error converting PDF to images: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error converting PDF to images.")


async def upload_image_to_gdrive(image_bytes: bytes, filename: str) -> str:
    try:
        # Upload the image to Google Drive
        file_metadata = {
            'name': filename,
            'parents': [os.getenv("GDRIVE_FOLDER_ID")],  # ID of the folder shared with the service account
        }
        media = MediaIoBaseUpload(io.BytesIO(image_bytes), mimetype='image/png', resumable=True)
        file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        ).execute()
        file_id = file.get('id')

        # Make the file publicly accessible
        drive_service.permissions().create(
            fileId=file_id,
            body={'type': 'anyone', 'role': 'reader'}
        ).execute()

        # Get the shareable link
        shareable_link = f"https://drive.google.com/uc?id={file_id}"
        return shareable_link
    except Exception as e:
        logging.error(f"Error uploading image to Google Drive: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error uploading images.")


@app.post("/convert-pdf")
async def convert_pdf(pdf: PDFUrl):
    pdf_bytes = await download_pdf(str(pdf.url))
    image_bytes_list = await convert_pdf_to_images(pdf_bytes)

    # Upload images asynchronously
    upload_tasks = [
        upload_image_to_gdrive(image_bytes, f"page{idx + 1}.png")
        for idx, image_bytes in enumerate(image_bytes_list)
    ]
    image_urls = await asyncio.gather(*upload_tasks)

    if not image_urls:
        raise HTTPException(status_code=500, detail="No images were generated.")

    return {"images": image_urls}


@app.get("/health")
async def health():
    return {"status": "ok"}
