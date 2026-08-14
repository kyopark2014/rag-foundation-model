import logging
import sys
import json
import traceback
import boto3
import os
from langchain_community.utilities.tavily_search import TavilySearchAPIWrapper

logging.basicConfig(
    level=logging.INFO,  # Default to INFO level
    format='%(filename)s:%(lineno)d | %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger("utils")

aws_access_key = os.environ.get('AWS_ACCESS_KEY_ID')
aws_secret_key = os.environ.get('AWS_SECRET_ACCESS_KEY')
aws_session_token = os.environ.get('AWS_SESSION_TOKEN')

workingDir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(workingDir, "config.json")
favorite_tools_path = os.path.join(os.path.dirname(config_path), "favorite_tools.json")
SKILLS_DIR = os.path.join(workingDir, "skills")
SESSION_STORAGE_DIR = os.environ.get(
    "SESSION_STORAGE_DIR",
    os.path.join(workingDir, ".session_storage"),
)


def sanitize_user_path_segment(user_id: str | None) -> str | None:
    """Return a safe single path segment for per-user workspace folders, or None."""
    if not user_id:
        return None
    raw = str(user_id).strip()
    if raw.startswith("v1.") and raw.count(".") >= 2:
        logger.warning("Refusing signed session token as artifacts path segment")
        return None
    if len(raw) > 128:
        logger.warning("Refusing oversized user_id as artifacts path segment")
        return None
    segment = (
        raw
        .replace("/", "_")
        .replace("\\", "_")
        .replace("..", "_")
    )
    return segment or None


def get_user_artifacts_dir(user_id: str | None) -> str:
    segment = sanitize_user_path_segment(user_id) or "default"
    return os.path.join(SESSION_STORAGE_DIR, segment, "artifacts")


def ensure_user_artifacts_dir(user_id: str | None) -> str:
    segment = sanitize_user_path_segment(user_id)
    if not segment:
        raise ValueError(
            "Invalid user_id for artifacts path; expected a plain user id, "
            "not a signed session cookie"
        )
    artifacts_dir = os.path.join(SESSION_STORAGE_DIR, segment, "artifacts")
    os.makedirs(artifacts_dir, exist_ok=True)
    return artifacts_dir


def get_user_skills_dir(user_id: str | None) -> str:
    segment = sanitize_user_path_segment(user_id) or "default"
    return os.path.join(SESSION_STORAGE_DIR, segment, "skills")


def ensure_user_skills_dir(user_id: str | None) -> str:
    segment = sanitize_user_path_segment(user_id)
    if not segment:
        raise ValueError(
            "Invalid user_id for skills path; expected a plain user id, "
            "not a signed session cookie"
        )
    skills_dir = os.path.join(SESSION_STORAGE_DIR, segment, "skills")
    os.makedirs(skills_dir, exist_ok=True)
    return skills_dir



def get_user_db_path(user_id: str | None) -> str:
    """Durable per-user tasks/messages DB: {SESSION_STORAGE_DIR}/{user_id}/{user_id}.db."""
    segment = sanitize_user_path_segment(user_id) or "default"
    return os.path.join(SESSION_STORAGE_DIR, segment, f"{segment}.db")


def get_user_skills_list_path(user_id: str | None) -> str:
    segment = sanitize_user_path_segment(user_id) or "default"
    return os.path.join(SESSION_STORAGE_DIR, segment, "skills.list")


def _list_skill_dir_names(skills_dir: str) -> list[str]:
    if not os.path.isdir(skills_dir):
        return []
    names: list[str] = []
    try:
        entries = sorted(os.listdir(skills_dir))
    except OSError as e:
        logger.warning("Failed to list skills directory %s: %s", skills_dir, e)
        return []
    for entry in entries:
        if os.path.isfile(os.path.join(skills_dir, entry, "SKILL.md")):
            names.append(entry)
    return names


def _load_skills_list_file(path: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return [
                line.strip()
                for line in f
                if line.strip() and not line.strip().startswith("#")
            ]
    except FileNotFoundError:
        return []
    except OSError as e:
        logger.warning("Failed to read skills.list %s: %s", path, e)
        return []


def _seed_skill_names(user_id: str | None) -> list[str]:
    default_path = os.path.join(workingDir, "skills.list")
    builtin = _load_skills_list_file(default_path)
    user_skills = _list_skill_dir_names(get_user_skills_dir(user_id))
    merged: list[str] = []
    seen: set[str] = set()
    for name in builtin + user_skills:
        if name not in seen:
            merged.append(name)
            seen.add(name)
    return merged


def write_user_skills_list(user_id: str | None, names: list[str] | None = None) -> str:
    ensure_user_skills_dir(user_id)
    path = get_user_skills_list_path(user_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    merged = names if names is not None else _seed_skill_names(user_id)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(merged) + ("\n" if merged else ""))
    return path


def _builtin_skill_exists(name: str) -> bool:
    return os.path.isfile(os.path.join(workingDir, "skills", name, "SKILL.md"))


def _user_skill_exists(user_id: str | None, name: str) -> bool:
    return os.path.isfile(
        os.path.join(get_user_skills_dir(user_id), name, "SKILL.md")
    )


def ensure_user_skills_list(user_id: str | None) -> str:
    ensure_user_skills_dir(user_id)
    path = get_user_skills_list_path(user_id)
    if not os.path.isfile(path):
        return write_user_skills_list(user_id)

    existing = _load_skills_list_file(path)
    kept = [
        name
        for name in existing
        if _builtin_skill_exists(name) or _user_skill_exists(user_id, name)
    ]
    seen = set(kept)
    default_path = os.path.join(workingDir, "skills.list")
    candidates = _load_skills_list_file(default_path) + _list_skill_dir_names(
        get_user_skills_dir(user_id)
    )
    appended = [name for name in candidates if name not in seen]
    updated = kept + appended
    if updated != existing:
        return write_user_skills_list(user_id, updated)
    return path


def load_config():
    config = None

    try: 
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception as e:
        logger.error(f"Error loading config: {e}")
        config = {}

        projectName = "rag-automation"
        session = boto3.Session()
        region = session.region_name
        config['region'] = region
        config['projectName'] = projectName
        
        sts = boto3.client("sts")
        response = sts.get_caller_identity()
        accountId = response["Account"]
        config['accountId'] = accountId
        config['s3_bucket'] = f'storage-for-{projectName}-{accountId}-{region}'
        
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)    
    return config



def load_favorite_tools() -> dict[str, list[str]]:
    """Load favorite tool defaults for initial selections."""
    fallback = {"MCP": [], "SKILL": []}
    try:
        with open(favorite_tools_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.warning("favorite_tools.json not found: %s", favorite_tools_path)
        return fallback
    except Exception as e:
        logger.warning("Failed to load favorite_tools.json: %s", e)
        return fallback

    if not isinstance(data, dict):
        return fallback

    favorites: dict[str, list[str]] = {}
    for key in ("MCP", "SKILL"):
        values = data.get(key, [])
        if isinstance(values, list):
            favorites[key] = [v for v in values if isinstance(v, str) and v.strip()]
        else:
            favorites[key] = []
    return favorites


def save_favorite_tools(
    *, skills: list[str] | None = None, mcp_servers: list[str] | None = None
) -> dict[str, list[str]]:
    """Persist favorite tool defaults in favorite_tools.json."""
    favorites = load_favorite_tools()
    if skills is not None:
        favorites["SKILL"] = [v for v in skills if isinstance(v, str) and v.strip()]
    if mcp_servers is not None:
        favorites["MCP"] = [v for v in mcp_servers if isinstance(v, str) and v.strip()]

    with open(favorite_tools_path, "w", encoding="utf-8") as f:
        json.dump(favorites, f, ensure_ascii=False, indent=2)
    return favorites


def get_initial_tool_defaults() -> tuple[list[str], list[str]]:
    """Return initial skill/MCP defaults from favorite_tools.json."""
    favorite_tools = load_favorite_tools()
    default_skills = favorite_tools.get("SKILL") or []
    default_mcp_servers = favorite_tools.get("MCP") or []
    return default_skills, default_mcp_servers

config = load_config()

accountId = config.get('accountId')
if not accountId:
    sts = boto3.client("sts")
    response = sts.get_caller_identity()
    accountId = response["Account"]
    config['accountId'] = accountId
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

bedrock_region = config.get('region', 'us-west-2')
logger.info(f"bedrock_region: {bedrock_region}")
projectName = config.get('projectName', 'mop')
logger.info(f"projectName: {projectName}")


def persist_config_updates(updates):
    """Merge values fetched from Secrets Manager into config and write config.json."""
    global config
    if not updates:
        return
    changed = False
    for key, value in updates.items():
        if value is None:
            continue
        s = value.strip() if isinstance(value, str) else str(value)
        if not s:
            continue
        if config.get(key) != s:
            config[key] = s
            changed = True
    if not changed:
        return
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        logger.info(
            "Saved Secrets Manager values to config.json: %s",
            ", ".join(str(k) for k in updates if updates.get(k)),
        )
    except Exception as e:
        logger.warning("Failed to write config.json: %s", e)


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

def load_mcp_env():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    mcp_env_path = os.path.join(script_dir, "mcp.env")
    
    with open(mcp_env_path, "r", encoding="utf-8") as f:
        mcp_env = json.load(f)
    return mcp_env

def save_mcp_env(mcp_env):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    mcp_env_path = os.path.join(script_dir, "mcp.env")
    
    with open(mcp_env_path, "w", encoding="utf-8") as f:
        json.dump(mcp_env, f)

def sanitize_data_source_name(name):
    """
    Sanitize a name to comply with AWS Bedrock data source name pattern:
    ([0-9a-zA-Z][_-]?){1,100}
    - Pattern means: alphanumeric, optionally followed by underscore or hyphen, repeated 1-100 times
    - Cannot have consecutive underscores or hyphens
    - Must start with alphanumeric
    """
    import re
    # Remove any characters that are not alphanumeric, underscore, or hyphen
    sanitized = re.sub(r'[^0-9a-zA-Z_-]', '', name)
    
    # Replace consecutive underscores/hyphens with single hyphen
    # This ensures the pattern [0-9a-zA-Z][_-]? is followed correctly
    sanitized = re.sub(r'[_-]{2,}', '-', sanitized)
    
    # Ensure it starts with alphanumeric character
    if sanitized and not sanitized[0].isalnum():
        sanitized = 'ds' + sanitized
    
    # Remove trailing hyphens/underscores (they must be followed by alphanumeric per pattern)
    sanitized = sanitized.rstrip('_-')
    
    # Ensure it's not empty and limit to 100 characters
    if not sanitized:
        sanitized = 'datasource'
    
    # Final validation: ensure it matches the pattern exactly
    pattern = re.compile(r'^([0-9a-zA-Z][_-]?){1,100}$')
    if not pattern.match(sanitized):
        # If still doesn't match, create a safe default name
        # Use project name or create a simple alphanumeric name
        safe_name = re.sub(r'[^0-9a-zA-Z]', '', name.lower())
        if not safe_name:
            safe_name = 'datasource'
        sanitized = safe_name[:100]
    
    return sanitized[:100]

knowledge_base_id = config.get('knowledge_base_id')
data_source_id = config.get('data_source_id')
region = config.get('region', 'us-west-2')
s3_bucket = config.get('s3_bucket', f'storage-for-{projectName}-{accountId}-{region}')
sharing_url = config.get('sharing_url', '')

def update_sharing_url():
    """Look up CloudFront distribution domain for this project and save as sharing_url."""
    try:
        cf_client = boto3.client('cloudfront', region_name=region)
        paginator = cf_client.get_paginator('list_distributions')
        target_origin_id = f"s3-{projectName}"

        for page in paginator.paginate():
            dist_list = page.get('DistributionList', {})
            for dist in dist_list.get('Items', []):
                origins = dist.get('Origins', {}).get('Items', [])
                for origin in origins:
                    if origin['Id'] == target_origin_id:
                        domain = dist['DomainName']
                        url = f"https://{domain}"
                        logger.info(f"sharing_url found: {url}")
                        config['sharing_url'] = url
                        with open(config_path, "w", encoding="utf-8") as f:
                            json.dump(config, f, indent=2)
                        return url
        logger.warning(f"CloudFront distribution with origin '{target_origin_id}' not found")
    except Exception:
        err_msg = traceback.format_exc()
        logger.info(f"Failed to look up sharing_url: {err_msg}")
    return ''

if not sharing_url:
    sharing_url = update_sharing_url()

def update_rag_info():
    knowledge_base_id = None
    data_source_id = None
    try: 
        client = boto3.client(
            service_name='bedrock-agent',
            region_name=region
        )

        response = client.list_knowledge_bases(
            maxResults=50
        )
        logger.info(f"(list_knowledge_bases) response: {response}")
        
        knowledge_base_name = projectName
        if "knowledgeBaseSummaries" in response:
            summaries = response["knowledgeBaseSummaries"]
            for summary in summaries:
                if summary["name"] == knowledge_base_name:
                    knowledge_base_id = summary["knowledgeBaseId"]
                    logger.info(f"knowledge_base_id: {knowledge_base_id}")

        if not knowledge_base_id:
            logger.warning(f"Knowledge Base not found for project: {knowledge_base_name}")
            return knowledge_base_id, data_source_id

        if not s3_bucket:
            logger.warning(f"s3_bucket is not configured, skipping data source lookup")
            return knowledge_base_id, data_source_id

        response = client.list_data_sources(
            knowledgeBaseId=knowledge_base_id,
            maxResults=10
        )        
        logger.info(f"(list_data_sources) response: {response}")
        
        data_source_name = sanitize_data_source_name(s3_bucket)
        if 'dataSourceSummaries' in response:
            for data_source in response['dataSourceSummaries']:
                logger.info(f"data_source: {data_source}")
                if data_source['name'] == data_source_name:
                    data_source_id = data_source['dataSourceId']
                    logger.info(f"data_source_id: {data_source_id}")
                    break    
        
        # save config
        config['knowledge_base_id'] = knowledge_base_id
        config['data_source_id'] = data_source_id
        config['s3_bucket'] = s3_bucket
        config['region'] = region
        config['projectName'] = projectName
        config['accountId'] = accountId
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

    except Exception:
        err_msg = traceback.format_exc()
        logger.info(f"error message: {err_msg}")

    return knowledge_base_id, data_source_id

if not knowledge_base_id or not data_source_id:
    knowledge_base_id, data_source_id = update_rag_info()

def sync_data_source():
    """Start a Knowledge Base ingestion job for the configured data source."""
    global knowledge_base_id, data_source_id
    if not knowledge_base_id or not data_source_id:
        knowledge_base_id, data_source_id = update_rag_info()
    if not knowledge_base_id or not data_source_id:
        logger.error("knowledge_base_id or data_source_id is not configured")
        return None

    try:
        bedrock_client = boto3.client(
            service_name='bedrock-agent',
            region_name=region
        )
        response = bedrock_client.start_ingestion_job(
            knowledgeBaseId=knowledge_base_id,
            dataSourceId=data_source_id
        )
        logger.info(f"(start_ingestion_job) response: {response}")
        job = response.get("ingestionJob", {})
        return {
            "ingestion_job_id": job.get("ingestionJobId"),
            "status": job.get("status"),
        }
    except Exception:
        err_msg = traceback.format_exc()
        logger.info(f"error message: {err_msg}")
        return None


ACTIVE_INGESTION_STATUSES = ("STARTING", "IN_PROGRESS")


def get_active_ingestion_job() -> dict | None:
    """Return an in-flight ingestion job if Knowledge Base sync is already running."""
    global knowledge_base_id, data_source_id
    if not knowledge_base_id or not data_source_id:
        knowledge_base_id, data_source_id = update_rag_info()
    if not knowledge_base_id or not data_source_id:
        logger.error("knowledge_base_id or data_source_id is not configured")
        return None

    try:
        bedrock_client = boto3.client(
            service_name="bedrock-agent",
            region_name=region,
        )
        for status in ACTIVE_INGESTION_STATUSES:
            response = bedrock_client.list_ingestion_jobs(
                knowledgeBaseId=knowledge_base_id,
                dataSourceId=data_source_id,
                filters=[
                    {
                        "attribute": "STATUS",
                        "operator": "EQ",
                        "values": [status],
                    }
                ],
                maxResults=1,
                sortBy={
                    "attribute": "STARTED_AT",
                    "order": "DESCENDING",
                },
            )
            summaries = response.get("ingestionJobSummaries") or []
            if not summaries:
                continue
            job = summaries[0]
            return {
                "ingestion_job_id": job.get("ingestionJobId"),
                "status": job.get("status"),
                "started_at": str(job["startedAt"]) if job.get("startedAt") else None,
            }
        return None
    except Exception:
        logger.error("Error listing ingestion jobs: %s", traceback.format_exc())
        raise


def docs_s3_prefix(project: str | None = None) -> str:
    name = (project or projectName or "").strip().strip("/")
    if not name:
        name = "default"
    return f"docs/{name}"


def upload_to_s3(
    file_bytes: bytes,
    file_name: str,
    user_id: str | None = None,
) -> dict | None:
    """Upload a file to S3 under docs/{projectName}/ (or images/) and return metadata."""
    from urllib import parse

    if not s3_bucket:
        logger.error("s3_bucket is not configured")
        return None

    try:
        s3_client = boto3.client(service_name="s3", region_name=bedrock_region)
        content_type = get_contents_type(file_name)
        logger.info("content_type: %s", content_type)

        prefix = (
            "images"
            if isinstance(content_type, str) and content_type.startswith("image/")
            else docs_s3_prefix()
        )
        user_segment = sanitize_user_path_segment(user_id)
        if user_segment:
            s3_key = f"{prefix}/{user_segment}/{file_name}"
            relative_url_path = f"{prefix}/{parse.quote(user_segment)}/{parse.quote(file_name)}"
        else:
            s3_key = f"{prefix}/{file_name}"
            relative_url_path = f"{prefix}/{parse.quote(file_name)}"
        user_meta = {"content_type": content_type}

        put_params = {
            "Bucket": s3_bucket,
            "Key": s3_key,
            "Metadata": user_meta,
            "Body": file_bytes,
        }
        if content_type and content_type != "no info":
            put_params["ContentType"] = content_type
        if content_type == "application/pdf":
            put_params["ContentDisposition"] = "inline"

        response = s3_client.put_object(**put_params)
        logger.info("upload response: %s", response)

        url = None
        if sharing_url:
            url = f"{sharing_url.rstrip('/')}/{relative_url_path}"

        return {
            "file_name": file_name,
            "s3_key": s3_key,
            "content_type": content_type,
            "url": url,
        }
    except Exception:
        logger.error("Error uploading to S3: %s", traceback.format_exc())
        return None
