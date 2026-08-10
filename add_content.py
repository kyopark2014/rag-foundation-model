#!/usr/bin/env python3
"""
Upload content to S3 and sync Knowledge Base data source
"""

import boto3
import json
import os
import logging
import requests
from botocore.exceptions import ClientError

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

workingDir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(workingDir, "application", "config.json")

def load_config():
    """Load configuration from config.json"""
    try:
        with open(config_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading config: {e}")
        logger.error(f"Try again after using 'python installer.py' to install the application")
        exit(1)

def check_file_exists_in_s3(s3_client, bucket_name, key):
    """Check if file already exists in S3"""
    try:
        s3_client.head_object(Bucket=bucket_name, Key=key)
        return True
    except ClientError as e:
        if e.response['Error']['Code'] == '404':
            return False
        raise

def get_contents_type(file_name):
    if file_name.lower().endswith((".jpg", ".jpeg")):
        content_type = "image/jpeg"
    elif file_name.lower().endswith((".pdf")):
        content_type = "application/pdf"
    elif file_name.lower().endswith((".txt")):
        content_type = "text/plain"
    elif file_name.lower().endswith((".csv")):
        content_type = "text/csv"
    elif file_name.lower().endswith((".ppt", ".pptx")):
        content_type = "application/vnd.ms-powerpoint"
    elif file_name.lower().endswith((".doc", ".docx")):
        content_type = "application/msword"
    elif file_name.lower().endswith((".xls")):
        content_type = "application/vnd.ms-excel"
    elif file_name.lower().endswith((".py")):
        content_type = "text/x-python"
    elif file_name.lower().endswith((".js")):
        content_type = "application/javascript"
    elif file_name.lower().endswith((".md")):
        content_type = "text/markdown"
    elif file_name.lower().endswith((".png")):
        content_type = "image/png"
    else:
        content_type = "no info"    
    return content_type

def upload_file_to_s3(s3_client, local_file, bucket_name, s3_key):
    """Upload file to S3"""
    try:
        # Read file content
        with open(local_file, 'rb') as f:
            file_bytes = f.read()
        
        content_type = get_contents_type(s3_key)
        logger.info(f"Uploading {local_file} to s3://{bucket_name}/{s3_key}")
        logger.info(f"Content type: {content_type}")

        # Prepare metadata
        user_meta = {  # user-defined metadata
            "content_type": content_type
        }
        
        # Prepare put_object parameters
        put_params = {
            'Bucket': bucket_name,
            'Key': s3_key,
            'Body': file_bytes,
            'Metadata': user_meta
        }
        
        # Set ContentType if it's not "no info"
        if content_type != "no info":
            put_params['ContentType'] = content_type
        
        # Set ContentDisposition to "inline" so browser displays the file instead of downloading
        # For PDF files, this allows them to be viewed directly in the browser
        if content_type == "application/pdf":
            put_params['ContentDisposition'] = 'inline'
        
        # Upload to S3
        response = s3_client.put_object(**put_params)
        logger.info(f"✓ Successfully uploaded to S3. ETag: {response.get('ETag', 'N/A')}")

        return True
    
    except FileNotFoundError:
        logger.error(f"File not found: {local_file}")
        return False
    except Exception as e:
        logger.error(f"Error uploading to S3: {str(e)}")
        return False

def upload_url_to_s3(s3_client, url, bucket_name, s3_key):
    """Download file from URL and upload to S3"""
    try:
        # Download file from URL
        logger.info(f"Downloading file from URL: {url}")
        response = requests.get(url, timeout=60)
        response.raise_for_status()
        file_bytes = response.content
        logger.info(f"Downloaded {len(file_bytes)} bytes from URL")
        
        content_type = get_contents_type(s3_key)
        logger.info(f"Uploading to s3://{bucket_name}/{s3_key}")
        logger.info(f"Content type: {content_type}")

        # Prepare metadata
        user_meta = {  # user-defined metadata
            "content_type": content_type,
            "source_url": url
        }
        
        # Prepare put_object parameters
        put_params = {
            'Bucket': bucket_name,
            'Key': s3_key,
            'Body': file_bytes,
            'Metadata': user_meta
        }
        
        # Set ContentType if it's not "no info"
        if content_type != "no info":
            put_params['ContentType'] = content_type
        
        # Set ContentDisposition to "inline" so browser displays the file instead of downloading
        # For PDF files, this allows them to be viewed directly in the browser
        if content_type == "application/pdf":
            put_params['ContentDisposition'] = 'inline'
        
        # Upload to S3
        s3_response = s3_client.put_object(**put_params)
        logger.info(f"✓ Successfully uploaded to S3. ETag: {s3_response.get('ETag', 'N/A')}")

        return True
    
    except requests.exceptions.RequestException as e:
        logger.error(f"Error downloading from URL: {str(e)}")
        return False
    except Exception as e:
        logger.error(f"Error uploading to S3: {str(e)}")
        return False


def sync_knowledge_base(bedrock_client, knowledge_base_id):
    """Sync Knowledge Base data source"""
    try:
        # Get data sources for the knowledge base
        response = bedrock_client.list_data_sources(knowledgeBaseId=knowledge_base_id)
        
        if not response['dataSourceSummaries']:
            logger.error("No data sources found for knowledge base")
            return False
            
        data_source_id = response['dataSourceSummaries'][0]['dataSourceId']
        
        # Start ingestion job
        ingestion_response = bedrock_client.start_ingestion_job(
            knowledgeBaseId=knowledge_base_id,
            dataSourceId=data_source_id
        )
        
        job_id = ingestion_response['ingestionJob']['ingestionJobId']
        logger.info(f"✓ Started ingestion job: {job_id}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to sync knowledge base: {e}")
        return False

def main():
    # Load configuration
    config = load_config()
    region = config['region']
    s3_bucket = config['s3_bucket']
    knowledge_base_id = config['knowledge_base_id']
    
    # Initialize AWS clients
    s3_client = boto3.client('s3', region_name=region)
    bedrock_client = boto3.client('bedrock-agent', region_name=region)
    
    # URL sources to upload
    url_sources = [
        {
            "url": "https://db3lw8u6vdvwu.cloudfront.net/ds/Suzano_SA.pdf",
            "s3_key": "docs/Suzano_SA.pdf"
        }
    ]
    
    # Local file sources to upload
    file_sources = [
        {
            "local_file": "contents/error_code.pdf",
            "s3_key": "docs/error_code.pdf"
        }
    ]
    
    upload_success = True
    
    # Upload from URLs
    for source in url_sources:
        url = source["url"]
        s3_key = source["s3_key"]
        if check_file_exists_in_s3(s3_client, s3_bucket, s3_key):
            logger.info(f"File already exists in S3, skipping upload: {s3_key}")
        else:
            if not upload_url_to_s3(s3_client, url, s3_bucket, s3_key):
                upload_success = False
    
    # Upload from local files
    for source in file_sources:
        local_file = source["local_file"]
        s3_key = source["s3_key"]
        if not os.path.exists(local_file):
            logger.error(f"File not found: {local_file}")
            upload_success = False
            continue
        if check_file_exists_in_s3(s3_client, s3_bucket, s3_key):
            logger.info(f"File already exists in S3, skipping upload: {s3_key}")
        else:
            if not upload_file_to_s3(s3_client, local_file, s3_bucket, s3_key):
                upload_success = False
    
    if not upload_success:
        logger.warning("Some files failed to upload")
    
    # Sync Knowledge Base
    if sync_knowledge_base(bedrock_client, knowledge_base_id):
        logger.info("✓ Knowledge Base sync initiated successfully")
        return upload_success
    else:
        return False

if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
