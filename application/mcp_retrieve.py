import boto3
import logging
import sys
import os
import json
from urllib import parse
from botocore.exceptions import ClientError

logging.basicConfig(
    level=logging.INFO,
    format="%(filename)s:%(lineno)d | %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("retrieve")

script_dir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(script_dir, "config.json")


def load_config():
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


config = load_config()

bedrock_region = config.get("region", "us-west-2")
projectName = config.get("projectName")
knowledge_base_id = config.get("knowledge_base_id")
number_of_results = 5

# Prefer HYBRID on OpenSearch Serverless (vector + keyword). Falls back to
# SEMANTIC automatically when the store does not support hybrid.
# See: https://docs.aws.amazon.com/bedrock/latest/userguide/kb-test-config.html
DEFAULT_SEARCH_TYPE = "HYBRID"

doc_prefix = f"docs/{projectName}/" if projectName else "docs/"
path = config.get("sharing_url", "")

aws_access_key = config.get("aws", {}).get("access_key_id")
aws_secret_key = config.get("aws", {}).get("secret_access_key")
aws_session_token = config.get("aws", {}).get("session_token")

if aws_access_key and aws_secret_key:
    bedrock_agent_runtime_client = boto3.client(
        "bedrock-agent-runtime",
        region_name=bedrock_region,
        aws_access_key_id=aws_access_key,
        aws_secret_access_key=aws_secret_key,
        aws_session_token=aws_session_token,
    )
else:
    bedrock_agent_runtime_client = boto3.client(
        "bedrock-agent-runtime", region_name=bedrock_region
    )


def _current_user_id() -> str:
    """User id injected into the MCP process env by langgraph_agent.create_agent()."""
    return (os.environ.get("RAG_USER_ID") or "").strip()


def _owner_filter(user_id: str) -> dict:
    """Filter documents whose STRING_LIST ``owner`` contains user_id.

    Uses listContains (OpenSearch Serverless). Neptune GraphRAG does not support
    list metadata — that stack must use STRING + equals instead.
    See: https://docs.aws.amazon.com/bedrock/latest/userguide/kb-test-config.html
    """
    return {
        "listContains": {
            "key": "owner",
            "value": user_id,
        }
    }


def _retrieval_configuration(user_id: str) -> dict:
    return {
        "vectorSearchConfiguration": {
            "numberOfResults": number_of_results,
            "overrideSearchType": DEFAULT_SEARCH_TYPE,
            "filter": _owner_filter(user_id),
        }
    }


def _call_retrieve(query: str, user_id: str):
    return bedrock_agent_runtime_client.retrieve(
        retrievalQuery={"text": query},
        knowledgeBaseId=knowledge_base_id,
        retrievalConfiguration=_retrieval_configuration(user_id),
    )


def retrieve(query):
    global knowledge_base_id

    user_id = _current_user_id()
    if not user_id:
        logger.error("RAG_USER_ID is empty; refusing unscoped RAG retrieve")
        return json.dumps(
            {"error": "User session required for RAG retrieve"},
            ensure_ascii=False,
        )

    logger.info("RAG retrieve for user_id=%s query=%s", user_id, query)

    try:
        response = _call_retrieve(query, user_id)
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")

        if error_code == "ResourceNotFoundException":
            logger.warning("ResourceNotFoundException occurred: %s", e)
            logger.info("Attempting to update knowledge_base_id...")

            bedrock_agent_client = boto3.client(
                "bedrock-agent", region_name=bedrock_region
            )
            knowledge_base_list = bedrock_agent_client.list_knowledge_bases()

            updated = False
            for knowledge_base in knowledge_base_list.get(
                "knowledgeBaseSummaries", []
            ):
                if knowledge_base["name"] == projectName:
                    new_knowledge_base_id = knowledge_base["knowledgeBaseId"]
                    knowledge_base_id = new_knowledge_base_id
                    config["knowledge_base_id"] = new_knowledge_base_id
                    with open(config_path, "w", encoding="utf-8") as f:
                        json.dump(config, f, ensure_ascii=False, indent=4)
                    logger.info(
                        "Updated knowledge_base_id to: %s", new_knowledge_base_id
                    )
                    updated = True
                    break

            if updated:
                try:
                    response = _call_retrieve(query, user_id)
                    logger.info("Retry successful after updating knowledge_base_id")
                except Exception as retry_error:
                    logger.error(
                        "Retry failed after updating knowledge_base_id: %s",
                        retry_error,
                    )
                    raise
            else:
                logger.error(
                    "Could not find knowledge base with name: %s", projectName
                )
                raise
        else:
            logger.error("Error retrieving: %s", e)
            raise
    except Exception as e:
        logger.error("Unexpected error retrieving: %s", e)
        raise

    retrieval_results = response.get("retrievalResults", [])

    json_docs = []
    for result in retrieval_results:
        text = url = name = None
        if "content" in result:
            content = result["content"]
            if "text" in content:
                text = content["text"]

        if "location" in result:
            location = result["location"]
            if "s3Location" in location:
                uri = (
                    location["s3Location"]["uri"]
                    if location["s3Location"]["uri"] is not None
                    else ""
                )
                name = uri.split("/")[-1]
                encoded_name = parse.quote(name)
                # Prefer full key under docs/ when URI includes user prefix
                if "/docs/" in uri:
                    relative = uri.split("/docs/", 1)[1]
                    url = f"{path}/docs/{parse.quote(relative, safe='/')}"
                else:
                    url = f"{path}/{doc_prefix}{encoded_name}"
            elif "webLocation" in location:
                url = (
                    location["webLocation"]["url"]
                    if location["webLocation"]["url"] is not None
                    else ""
                )
                name = "WEB"

        page = None
        # Foundation Model parser / KB page attribution for PDFs on OpenSearch Serverless
        raw_page = (result.get("metadata") or {}).get(
            "x-amz-bedrock-kb-document-page-number"
        )
        if raw_page is not None:
            try:
                page = int(raw_page) + 1
            except (TypeError, ValueError):
                page = raw_page

        reference = {
            "url": url,
            "title": name,
            "from": "RAG",
        }
        if page is not None:
            reference["page"] = page

        json_docs.append(
            {
                "contents": text,
                "reference": reference,
            }
        )
    logger.info("json_docs: %s", json_docs)

    return json.dumps(json_docs, ensure_ascii=False)
