
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from src.agents.introspector import APIIntrospector
from src.agents.generator import ToolGenerator
from src.elasticsearch.client import ESClient
from src.config import Config
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Forge Agent Builder Tools")

config = Config()
es_client = ESClient(config.elasticsearch_url, config.elasticsearch_api_key)


class DiscoverRequest(BaseModel):
    api_url: str
    api_key: str = None


class GenerateRequest(BaseModel):
    api_url: str


@app.post("/discover")
async def discover_api(request: DiscoverRequest):
    try:
        introspector = APIIntrospector(es_client, skip_index=False, api_key=request.api_key)
        result = introspector.discover(request.api_url)
        
        return {
            "success": True,
            "data": result
        }
    except Exception as e:
        logger.error(f"Discovery failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/generate")
async def generate_tool(request: GenerateRequest):
    try:
        generator = ToolGenerator(es_client, skip_index=False)
        result = generator.generate(request.api_url)
        
        return {
            "success": True,
            "data": {
                "tool_name": result['tool_name'],
                "endpoints_count": result['endpoints_count'],
                "tool_code": result['tool_code'][:500] + "...",  # Truncate for response
                "generation_time": result['generation_time_seconds']
            }
        }
    except Exception as e:
        logger.error(f"Generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "forge-agent-builder-tools"}


@app.get("/")
async def mcp_get():
    return JSONResponse(
        status_code=405,
        content={"detail": "Method Not Allowed - This server does not support server-initiated streaming"}
    )


@app.post("/")
async def mcp_jsonrpc(request: dict):
    method = request.get("method")
    params = request.get("params", {})
    request_id = request.get("id")
    
    try:
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "forge-mcp-server", "version": "0.1.0"}
                }
            }
            
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "discover_api",
                        "description": "Discovers API endpoints by probing common REST patterns. Analyzes responses to detect auth requirements, parameters, and endpoint structure. Works with or without OpenAPI specs.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "api_url": {
                                    "type": "string",
                                    "description": "Base URL of the API to discover"
                                },
                                "api_key": {
                                    "type": "string",
                                    "description": "Optional API key for authenticated endpoints"
                                }
                            },
                            "required": ["api_url"]
                        }
                    },
                    {
                        "name": "generate_tool",
                        "description": "Generates complete Python integration code from discovered API endpoints. Creates a class with methods for each endpoint, including auth, parameters, and error handling.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "api_url": {
                                    "type": "string",
                                    "description": "Base URL of the API to generate code for"
                                }
                            },
                            "required": ["api_url"]
                        }
                    }
                ]
            }
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
            
        elif method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments", {})
            
            if tool_name == "discover_api":
                api_url = arguments["api_url"]
                api_key = arguments.get("api_key")
                
                introspector = APIIntrospector(es_client, skip_index=False, api_key=api_key)
                result = introspector.discover(api_url)
                
                endpoints_summary = "\n".join([
                    f"- {ep['method']} {ep['path']}" 
                    for ep in result.get('endpoints', [])[:10]
                ])
                
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{
                            "type": "text",
                            "text": f"Discovered {len(result.get('endpoints', []))} endpoints:\n{endpoints_summary}"
                        }],
                        "isError": False
                    }
                }
                
            elif tool_name == "generate_tool":
                api_url = arguments["api_url"]
                
                generator = ToolGenerator(es_client, skip_index=False)
                result = generator.generate(api_url)
                
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{
                            "type": "text",
                            "text": f"Generated tool: {result['tool_name']}\n"
                                 f"Endpoints: {result['endpoints_count']}\n"
                                 f"Time: {result['generation_time_seconds']}s\n\n"
                                 f"Code preview:\n{result['tool_code'][:500]}..."
                        }],
                        "isError": False
                    }
                }
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}
                }
        else:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"}
            }
            
    except Exception as e:
        logger.error(f"MCP request failed: {e}")
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32603, "message": str(e)}
        }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
