API_DISCOVERIES_MAPPING = {
    "mappings": {
        "properties": {
            "api_url": {"type": "keyword"},
            "discovered_at": {"type": "date"},
            "api_name": {"type": "text"},
            "api_description": {"type": "text"},
            "base_url": {"type": "keyword"},
            "openapi_spec_url": {"type": "keyword"},
            "has_openapi_spec": {"type": "boolean"},
            "auth_type": {"type": "keyword"},
            "endpoints": {
                "type": "nested",
                "properties": {
                    "path": {"type": "keyword"},
                    "method": {"type": "keyword"},
                    "summary": {"type": "text"},
                    "description": {"type": "text"},
                    "parameters": {"type": "object", "enabled": False},
                    "request_body": {"type": "object", "enabled": False},
                    "responses": {"type": "object", "enabled": False}
                }
            },
            "total_endpoints": {"type": "integer"},
            "discovery_status": {"type": "keyword"},
            "error_message": {"type": "text"}
        }
    }
}

AGENT_TOOLS_MAPPING = {
    "mappings": {
        "properties": {
            "tool_id": {"type": "keyword"},
            "tool_name": {"type": "keyword"},
            "display_name": {"type": "text"},
            "description": {"type": "text"},
            "description_embedding": {
                "type": "dense_vector",
                "dims": 384,
                "index": True,
                "similarity": "cosine"
            },
            "api_base_url": {"type": "keyword"},
            "auth_type": {"type": "keyword"},
            "generated_at": {"type": "date"},
            "updated_at": {"type": "date"},
            "source_api_discovery_id": {"type": "keyword"},
            "tool_code": {"type": "text", "index": False},
            "mcp_server_code": {"type": "text", "index": False},
            "readme": {"type": "text"},
            "endpoints_count": {"type": "integer"},
            "categories": {"type": "keyword"},
            "tags": {"type": "keyword"},
            "usage_count": {"type": "integer"},
            "last_used": {"type": "date"},
            "success_rate": {"type": "float"},
            "avg_execution_time_ms": {"type": "float"},
            "rating": {"type": "float"},
            "review_count": {"type": "integer"},
            "is_verified": {"type": "boolean"},
            "generation_time_seconds": {"type": "float"},
            "generation_errors": {"type": "text"}
        }
    }
}

TOOL_USAGE_LOGS_MAPPING = {
    "mappings": {
        "properties": {
            "log_id": {"type": "keyword"},
            "tool_id": {"type": "keyword"},
            "timestamp": {"type": "date"},
            "user_query": {"type": "text"},
            "execution_success": {"type": "boolean"},
            "execution_time_ms": {"type": "float"},
            "error_message": {"type": "text"},
            "agent_id": {"type": "keyword"}
        }
    }
}
