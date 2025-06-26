import argparse
import uvicorn
import logging
from fastapi import FastAPI, UploadFile, File
from celery.exceptions import TimeoutError
from fastapi.middleware.cors import CORSMiddleware
from marker_api.celery_worker import celery_app
from marker_api.celery_tasks import process_batch
from marker_api.utils import print_markerapi_text_art
from marker.logger import configure_logging
from marker_api.celery_routes import (
    celery_convert_pdf,
    celery_result,
    celery_convert_pdf_concurrent_await,
    celery_batch_convert,
    celery_batch_result,
)
import gradio as gr
from marker_api.demo import demo_ui
from marker_api.model.schema import (
    BatchConversionResponse,
    BatchResultResponse,
    CeleryResultResponse,
    CeleryTaskResponse,
    ConversionResponse,
    HealthResponse,
    ServerType,
)
from starlette.datastructures import UploadFile as StarletteUploadFile
import glob
from typing import List
from webdav3.client import Client
import os
from tqdm import tqdm
import time
import hashlib
from datetime import datetime

# Initialize logging
configure_logging()
logger = logging.getLogger(__name__)

# Global variable to hold model list
app = FastAPI()

logger.info("Configuring CORS middleware")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)


#
class PDFDownloader:
    def __init__(
        self,
        hostname,
        username,
        password,
        remote_folder="/PDF",
        local_folder="../../input",
    ):
        self.options = {
            "webdav_hostname": hostname,
            "webdav_login": username,
            "webdav_password": password,
        }
        self.remote_folder = remote_folder
        self.local_folder = local_folder
        self.setup_logging()

    def setup_logging(self):
        # Create log directory if it doesn't exist
        log_folder = "log"
        if not os.path.exists(log_folder):
            os.makedirs(log_folder)

        # Configure logging
        log_file = os.path.join(
            log_folder, f"pdf_download_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        )
        logging.basicConfig(
            filename=log_file,
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
        )
        self.logger = logging.getLogger(__name__)

    def calculate_md5(self, file_path):
        """Calculate MD5 hash of file"""
        hash_md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()

    def verify_download(self, local_path, remote_path, client):
        """Check integrity of downloaded file"""
        try:
            # Download file temporarily to compare
            temp_path = local_path + ".temp"
            client.download_sync(remote_path=remote_path, local_path=temp_path)

            # Compare MD5
            original_md5 = self.calculate_md5(local_path)
            temp_md5 = self.calculate_md5(temp_path)

            # Delete temporary file
            os.remove(temp_path)

            return original_md5 == temp_md5
        except Exception as e:
            self.logger.error(f"Error checking file {local_path}: {str(e)}")
            return False

    def download_with_retry(self, client, remote_path, local_path, max_retries=3):
        """Download file with retry mechanism"""
        for attempt in range(max_retries):
            try:
                client.download_sync(remote_path=remote_path, local_path=local_path)

                # Check integrity
                if self.verify_download(local_path, remote_path, client):
                    return True
                else:
                    self.logger.warning(
                        f"File {local_path} does not match the original, retrying..."
                    )

            except Exception as e:
                self.logger.error(
                    f"Error at attempt {attempt + 1} when downloading {remote_path}: {str(e)}"
                )
                if attempt < max_retries - 1:
                    time.sleep(5)  # Wait 5 seconds before retrying

        return False

    def download_files(self):
        try:
            # Create local directory
            if not os.path.exists(self.local_folder):
                os.makedirs(self.local_folder)
                self.logger.info(f"Created directory {self.local_folder}")

            # Connect client
            client = Client(self.options)

            # Get list of files
            self.logger.info("Getting list of files...")
            remote_files = client.list(self.remote_folder)
            pdf_files = [f for f in remote_files if f.lower().endswith(".pdf")]

            if not pdf_files:
                self.logger.warning(f"No PDF files found in {self.remote_folder}")
                return

            self.logger.info(f"Found {len(pdf_files)} PDF files")

            # Download files
            successful_downloads = 0
            failed_downloads = 0

            for pdf_file in tqdm(pdf_files, desc="Downloading files"):
                remote_path = f"{self.remote_folder}/{pdf_file}"
                local_path = os.path.join(self.local_folder, pdf_file)

                # Check if file already exists
                if os.path.exists(local_path):
                    self.logger.info(f"File {pdf_file} already exists, skipping...")
                    continue

                # Download file with retry
                if self.download_with_retry(client, remote_path, local_path):
                    successful_downloads += 1
                    self.logger.info(f"Successfully downloaded: {pdf_file}")
                else:
                    failed_downloads += 1
                    self.logger.error(f"Failed to download: {pdf_file}")

            # Report results
            self.logger.info(f"\nSummary:")
            self.logger.info(f"- Successfully downloaded: {successful_downloads} files")
            self.logger.info(f"- Failed to download: {failed_downloads} files")

            # List downloaded files
            downloaded_files = [
                f for f in os.listdir(self.local_folder) if f.endswith(".pdf")
            ]
            total_size = sum(
                os.path.getsize(os.path.join(self.local_folder, f))
                for f in downloaded_files
            ) / (1024 * 1024)

            self.logger.info(
                f"\nList of downloaded files ({len(downloaded_files)} files, total {total_size:.2f} MB):"
            )
            for file in downloaded_files:
                file_size = os.path.getsize(os.path.join(self.local_folder, file)) / (
                    1024 * 1024
                )
                self.logger.info(f"- {file} ({file_size:.2f} MB)")

        except Exception as e:
            self.logger.error(f"Unhandled error: {str(e)}")


# Use class
downloader = PDFDownloader(
    hostname="https://files.dataq.vn/remote.php/dav/files/admin",
    username="admin",
    password="admin",
    remote_folder="/PDF",
    local_folder="input",
)


@app.get("/health", response_model=HealthResponse)
def server():
    """
    Root endpoint to check server status.

    Returns:
    HealthResponse: A welcome message, server type, and number of workers (if distributed).
    """
    worker_count = len(celery_app.control.inspect().stats() or {})
    server_type = ServerType.distributed if worker_count > 0 else ServerType.simple
    return HealthResponse(
        message="Welcome to Marker-api",
        type=server_type,
        workers=worker_count if server_type == ServerType.distributed else None,
    )


@app.post("/download_pdfs")
def trigger_pdf_download():
    try:
        downloader.download_files()
        return {"status": "success", "message": "Download triggered."}
    except Exception as e:
        logger.error(f"Error triggering download: {str(e)}")
        return {"status": "error", "message": str(e)}


def is_celery_alive() -> bool:
    logger.debug("Checking if Celery is alive")
    try:
        result = celery_app.send_task("celery.ping")
        result.get(timeout=3)
        logger.info("Celery is alive")
        return True
    except (TimeoutError, Exception) as e:
        logger.warning(f"Celery is not responding: {str(e)}")
        return False


def setup_routes(app: FastAPI, celery_live: bool):
    logger.info("Setting up routes")
    if celery_live:
        logger.info("Adding Celery routes")

        @app.post("/convert", response_model=ConversionResponse)
        async def convert_pdf(pdf_file: UploadFile = File(...)):
            return await celery_convert_pdf_concurrent_await(pdf_file)

        @app.post("/celery/convert", response_model=CeleryTaskResponse)
        async def celery_convert(pdf_file: UploadFile = File(...)):
            return await celery_convert_pdf(pdf_file)

        @app.get("/celery/result/{task_id}", response_model=CeleryResultResponse)
        async def get_celery_result(task_id: str):
            return await celery_result(task_id)

        @app.post("/batch_convert", response_model=BatchConversionResponse)
        async def batch_convert(pdf_files: List[UploadFile] = File(...)):
            return await celery_batch_convert(pdf_files)

        # New
        @app.post("/batch_convert_from_local", response_model=BatchConversionResponse)
        async def batch_convert_from_local():
            input_folder = "input"

        #
        @app.get("/batch_convert/result/{task_id}", response_model=BatchResultResponse)
        async def get_batch_result(task_id: str):
            return await celery_batch_result(task_id)

        logger.info("Adding real-time conversion route")
    else:
        logger.warning("Celery routes not added as Celery is not alive")
    app = gr.mount_gradio_app(app, demo_ui, path="/gradio")


def parse_args():
    logger.debug("Parsing command line arguments")
    parser = argparse.ArgumentParser(description="Run FastAPI with Uvicorn.")
    parser.add_argument(
        "--host", type=str, default="0.0.0.0", help="Host to run the FastAPI app"
    )
    parser.add_argument(
        "--port", type=int, default=8080, help="Port to run the FastAPI app"
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    print_markerapi_text_art()
    logger.info(f"Starting FastAPI app on {args.host}:{args.port}")
    celery_alive = is_celery_alive()
    setup_routes(app, celery_alive)
    try:
        uvicorn.run(app, host=args.host, port=args.port)
    except Exception as e:
        logger.critical(f"Failed to start the application: {str(e)}")
        raise
