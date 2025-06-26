from celery import Task
from marker_api.celery_worker import celery_app
from marker.convert import convert_single_pdf
from marker.models import load_all_models
import io
import logging
from marker_api.utils import process_image_to_base64
from celery.signals import worker_process_init
import json
import os

logger = logging.getLogger(__name__)

model_list = None
metadata_dict = None


@worker_process_init.connect
def initialize_models(**kwargs):
    global model_list, metadata_dict
    if not model_list:
        model_list = load_all_models()
        print("Models loaded at worker startup")
    if metadata_dict is None:
        metadata_path = os.path.join(os.path.dirname(__file__), '../metadata_template.json')
        with open(metadata_path, 'r') as f:
            metadata_dict = json.load(f)
        print("Check metadata_path:", metadata_path)
        print("Check metadata_dict:", metadata_dict.get('certificates.pdf', {}))
        print("Metadata loaded at worker startup")


class PDFConversionTask(Task):
    abstract = True

    def __init__(self):
        super().__init__()

    def __call__(self, *args, **kwargs):
        # Use the global model_list initialized at worker startup
        return self.run(*args, **kwargs)


@celery_app.task(
    ignore_result=False, bind=True, base=PDFConversionTask, name="convert_pdf"
)
def convert_pdf_to_markdown(self, filename, pdf_content):
    pdf_file = io.BytesIO(pdf_content)
    # 
    print("Check metadata_dict:", metadata_dict)
    metadata = metadata_dict.get(filename, {})
    print("Check metadata:", metadata)
    markdown_text, images, out_metadata = convert_single_pdf(pdf_file, model_list)
    merged_metadata = {**out_metadata, **metadata}
    image_data = {}
    for i, (img_filename, image) in enumerate(images.items()):
        logger.debug(f"Processing image {img_filename}")
        image_base64 = process_image_to_base64(image, img_filename)
        image_data[img_filename] = image_base64

    return {
        "filename": filename,
        "markdown": markdown_text,
        "metadata": merged_metadata,
        "images": image_data,
        "status": "ok",
    }


# @celery_app.task(
#     ignore_result=False, bind=True, base=PDFConversionTask, name="process_batch"
# )
# def process_batch(self, batch_data):
#     results = []
#     for filename, pdf_content in batch_data:
#         try:
#             result = convert_pdf_to_markdown(filename, pdf_content)
#             results.append(result)
#         except Exception as e:
#             logger.error(f"Error processing {filename}: {str(e)}")
#             results.append({"filename": filename, "status": "Error", "error": str(e)})
#     return results


@celery_app.task(
    ignore_result=False, bind=True, base=PDFConversionTask, name="process_batch"
)
def process_batch(self, batch_data):
    results = []
    total = len(batch_data)
    for i, (filename, pdf_content) in enumerate(batch_data, start=1):
        try:
            result = convert_pdf_to_markdown(filename, pdf_content)
            results.append(result)
        except Exception as e:
            logger.error(f"Error processing {filename}: {str(e)}")
            results.append({"filename": filename, "status": "Error", "error": str(e)})

        # Update progress
        self.update_state(state="PROGRESS", meta={"current": i, "total": total})

    return results
