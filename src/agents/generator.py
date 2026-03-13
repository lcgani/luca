from jinja2 import Environment, FileSystemLoader
from datetime import datetime
from typing import Dict, List
from src.config import Config

import logging
import hashlib
import time
import re

logger = logging.getLogger(__name__)

class ToolGenerator:
    def __init__(self, es_client, templates_dir='templates', skip_index=False):
        self.es = es_client
        self.jinja_env = Environment(loader=FileSystemLoader(templates_dir))
        self.skip_index = skip_index
        self.config = Config()
    
    def generate(self, api_url: str) -> Dict:
        start_time = time.time()
        
        discovery_data = self._get_discovery_data(api_url)
        if not discovery_data:
            raise ValueError(f"No discovery data found for: {api_url}")
        
        existing_tool = self._check_existing_tool(api_url)
        if existing_tool:
            return existing_tool
        
        tool_code = self._generate_tool_code(discovery_data)
        mcp_code = self._generate_mcp_code(discovery_data)
        readme = self._generate_readme(discovery_data)
        
        tool_data = {
            'tool_id': self._generate_tool_id(discovery_data['api_name']),
            'tool_name': self._to_snake_case(discovery_data['api_name']),
            'display_name': discovery_data['api_name'],
            'description': discovery_data['api_description'],
            'api_base_url': discovery_data['base_url'],
            'auth_type': discovery_data['auth_type'],
            'generated_at': datetime.utcnow().isoformat(),
            'updated_at': datetime.utcnow().isoformat(),
            'source_api_discovery_id': api_url,
            'tool_code': tool_code,
            'mcp_server_code': mcp_code,
            'readme': readme,
            'endpoints_count': discovery_data['total_endpoints'],
            'usage_count': 0,
            'rating': 0.0,
            'review_count': 0,
            'is_verified': False,
            'generation_time_seconds': time.time() - start_time
        }
        
        if not self.skip_index:
            self._store_tool(tool_data)
        
        return tool_data

    
    def _get_discovery_data(self, api_url: str) -> Dict:
        doc_id = api_url.replace('/', '_').replace(':', '_').replace('.', '_')
        try:
            result = self.es.client.get(index="api-discoveries", id=doc_id)
            return result['_source']
        except Exception as e:
            query = {"query": {"term": {"api_url.keyword": api_url}}}
            result = self.es.search(index="api-discoveries", body=query)
            if result['hits']['total']['value'] > 0:
                return result['hits']['hits'][0]['_source']
        return None
    
    def _check_existing_tool(self, api_url: str) -> Dict:
        query = {"query": {"term": {"source_api_discovery_id.keyword": api_url}}}
        result = self.es.search(index="agent-tools", body=query)
        if result['hits']['total']['value'] > 0:
            return result['hits']['hits'][0]['_source']
        return None
    
    def _generate_tool_code(self, discovery_data: Dict) -> str:
        tool_class = self._to_class_name(discovery_data['api_name'])
        endpoints = discovery_data.get('endpoints', [])
        
        code = f'''"""
{discovery_data['api_name']} - Auto-generated Tool
Base URL: {discovery_data['base_url']}
"""
import requests
from typing import Dict, List, Optional, Any

class {tool_class}:
    def __init__(self, api_key=None):
        self.base_url = "{discovery_data['base_url']}"
        self.api_key = api_key
    
    def _headers(self):
        headers = {{"Content-Type": "application/json"}}
        if self.api_key:
            headers["Authorization"] = f"Bearer {{self.api_key}}"
        return headers
    
    def _request(self, method: str, path: str, **kwargs) -> Dict:
        url = f"{{self.base_url}}{{path}}"
        response = requests.request(method, url, headers=self._headers(), **kwargs)
        response.raise_for_status()
        return response.json() if response.content else {{}}
'''
        
        for endpoint in endpoints[:20]:
            method_code = self._generate_method(endpoint)
            code += f"\n{method_code}"
        
        return code
    
    def _generate_method(self, endpoint: Dict) -> str:
        method_name = self._endpoint_to_method_name(endpoint['path'], endpoint['method'])
        params = self._extract_parameters(endpoint)
        
        requires_auth = endpoint.get('requires_auth', False)
        auth_note = endpoint.get('auth_note', '')
        
        param_list = ['self']
        param_docs = []
        path_params = []
        query_params = []
        
        for param in params:
            param_name = param['name']
            param_type = param.get('type', 'str')
            is_required = param.get('required', False)
            
            if param.get('in') == 'path':
                path_params.append(param_name)
                param_list.append(param_name)
            elif param.get('in') == 'query':
                query_params.append(param_name)
                if is_required:
                    param_list.append(param_name)
                else:
                    param_list.append(f"{param_name}=None")
            
            param_docs.append(f"        {param_name}: {param.get('description', 'No description')}")
        
        if endpoint['method'] in ['POST', 'PUT', 'PATCH']:
            param_list.append('data=None')
            param_docs.append("        data: Request body data")
        
        params_str = ', '.join(param_list)
        docs_str = '\n'.join(param_docs) if param_docs else '        No parameters'
        
        path = endpoint['path']
        for param in path_params:
            path = path.replace(f"{{{param}}}", f"{{{param}}}")
        
        auth_comment = ""
        if auth_note:
            auth_comment = f"\n        # {auth_note}"
        
        method_body = f'''    def {method_name}({params_str}) -> Dict:
        """
        {endpoint.get('summary', 'No description')}
        
        Args:
{docs_str}
        
        Returns:
            Dict: API response
        """{auth_comment}
        path = f"{path}"
'''
        
        if query_params:
            method_body += "        params = {}\n"
            for qp in query_params:
                method_body += f"        if {qp} is not None:\n"
                method_body += f"            params['{qp}'] = {qp}\n"
        
        if endpoint['method'] in ['POST', 'PUT', 'PATCH']:
            if query_params:
                method_body += f"        return self._request('{endpoint['method']}', path, params=params, json=data)\n"
            else:
                method_body += f"        return self._request('{endpoint['method']}', path, json=data)\n"
        else:
            if query_params:
                method_body += f"        return self._request('{endpoint['method']}', path, params=params)\n"
            else:
                method_body += f"        return self._request('{endpoint['method']}', path)\n"
        
        return method_body
    
    def _endpoint_to_method_name(self, path: str, method: str) -> str:
        
        parts = [p for p in path.split('/') if p and not p.startswith('{')]
        
        verb_map = {
            'GET': 'get' if '{' in path else 'list',
            'POST': 'create',
            'PUT': 'update',
            'PATCH': 'update',
            'DELETE': 'delete'
        }
        verb = verb_map.get(method, 'call')
        
        if parts:
            name_parts = [verb] + parts
            method_name = '_'.join(name_parts)
        else:
            method_name = verb
        
        method_name = re.sub(r'[^a-z0-9_]', '_', method_name.lower())
        method_name = re.sub(r'_+', '_', method_name)
        
        return method_name
    
    def _extract_parameters(self, endpoint: Dict) -> List[Dict]:
        params = []
        
        for param in endpoint.get('parameters', []):
            params.append({
                'name': param.get('name'),
                'type': param.get('schema', {}).get('type', 'str'),
                'required': param.get('required', False),
                'in': param.get('in', 'query'),
                'description': param.get('description', '')
            })
        
        return params
    
    def _generate_mcp_code(self, discovery_data: Dict) -> str:
        return f"# MCP Server for {discovery_data['api_name']}\n# Coming soon"
    
    def _generate_readme(self, discovery_data: Dict) -> str:
        return f"# {discovery_data['api_name']}\n\n{discovery_data['api_description']}\n\nEndpoints: {discovery_data['total_endpoints']}"
    
    def _generate_tool_id(self, api_name: str) -> str:
        return hashlib.md5(api_name.encode()).hexdigest()[:12]
    
    def _to_class_name(self, name: str) -> str:
        words = name.replace('-', ' ').replace('_', ' ').replace('.', ' ').split()
        return ''.join(word.capitalize() for word in words)
    
    def _to_snake_case(self, name: str) -> str:
        s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
        return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower().replace(' ', '_').replace('-', '_')
    
    def _store_tool(self, tool_data: Dict) -> None:
        try:
            self.es.index(index='agent-tools', id=tool_data['tool_id'], document=tool_data, timeout='2s')
        except Exception as e:
            logger.warning(f"Failed to store tool {tool_data.get('tool_name')}: {e}")
