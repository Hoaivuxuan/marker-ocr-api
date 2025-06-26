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
import yaml

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


def get_subfolder_path(out_folder, fname):
    subfolder_name = fname.rsplit('.', 1)[0]
    subfolder_path = os.path.join(out_folder, subfolder_name)
    return subfolder_path


def get_markdown_filepath(out_folder, fname):
    subfolder_path = get_subfolder_path(out_folder, fname)
    out_filename = fname.rsplit(".", 1)[0] + ".md"
    out_filename = os.path.join(subfolder_path, out_filename)
    return out_filename


def markdown_exists(out_folder, fname):
    out_filename = get_markdown_filepath(out_folder, fname)
    return os.path.exists(out_filename)

def save_markdown(out_folder, fname, full_text, images, out_metadata):
    subfolder_path = get_subfolder_path(out_folder, fname)
    os.makedirs(subfolder_path, exist_ok=True)

    markdown_filepath = get_markdown_filepath(out_folder, fname)
    out_meta_filepath = markdown_filepath.rsplit(".", 1)[0] + "_meta.json"

    with open(markdown_filepath, "w+", encoding='utf-8') as f:
        if out_metadata:
            f.write('---\n')
            f.write(yaml.safe_dump(out_metadata, allow_unicode=True))
            f.write('---\n\n')
        f.write(full_text)
    with open(out_meta_filepath, "w+", encoding='utf-8') as f:
        f.write(json.dumps(out_metadata, indent=4, ensure_ascii=False))

    for filename, image in images.items():
        image_filepath = os.path.join(subfolder_path, filename)
        image.save(image_filepath, "PNG")

    return subfolder_path

@celery_app.task(
    ignore_result=False, bind=True, base=PDFConversionTask, name="convert_pdf"
)
def convert_pdf_to_markdown(self, filename, pdf_content):
    pdf_file = io.BytesIO(pdf_content)
    out_folder = '/home/dataq/marker-system/marker-api/output'
    # 
    print("Check metadata_dict:", metadata_dict)
    metadata = metadata_dict.get(filename, {})
    print("Check metadata:", metadata)
    markdown_text, images, out_metadata = convert_single_pdf(pdf_file, model_list, metadata=metadata)
    if len(markdown_text.strip()) > 0:
        # Merge custom metadata with out_metadata
        if metadata:
            out_metadata.update(metadata)
        save_markdown(out_folder, filename, markdown_text, images, metadata)
    else:
        print(f"Empty file. Could not convert.")
    merged_metadata = {**out_metadata, **metadata}
    image_data = {}
    for i, (img_filename, image) in enumerate(images.items()):
        logger.debug(f"Processing image {img_filename}")
        image_base64 = process_image_to_base64(image, img_filename)
        image_data[img_filename] = image_base64

    return {
        "filename": filename,
        "markdown": markdown_text,
        "metadata": metadata,
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
