from typing import Dict, List, Optional
from datetime import datetime
import requests
import logging

logger = logging.getLogger(__name__)


class APIIntrospector:
    
    COMMON_SPEC_PATHS = [
        "/openapi.json", "/openapi.yaml",
        "/swagger.json", "/swagger.yaml",
        "/api-docs", "/docs/openapi.json",
        "/v1/openapi.json", "/api/openapi.json"
    ]
    
    COMMON_RESOURCES = [
        'users', 'user', 'accounts', 'account',
        'items', 'products', 'data', 'resources',
        'databases', 'pages', 'blocks', 'posts',
        'comments', 'notes', 'documents', 'files',
        'search', 'webhooks', 'events'
    ]
    
    VERSION_PATTERNS = ['v1', 'v2', 'v3', 'api/v1', 'api/v2']
    
    def __init__(self, es_client, skip_index=False, api_key=None):
        self.es = es_client
        self.skip_index = skip_index
        self.api_key = api_key
    
    def discover(self, api_url: str) -> Dict:
        api_url = api_url.rstrip('/')
        
        existing = self._check_existing(api_url)
        if existing:
            return existing
        
        spec_data = self._find_openapi_spec(api_url)
        
        if spec_data:
            discovery_result = self._parse_openapi_spec(spec_data, api_url)
        else:
            discovery_result = self._manual_discovery(api_url)
        
        discovery_result['discovered_at'] = datetime.utcnow().isoformat()
        
        if not self.skip_index:
            self._store_discovery(discovery_result)
        
        return discovery_result
    
    def _check_existing(self, api_url: str) -> Optional[Dict]:
        query = {
            "query": {"term": {"api_url.keyword": api_url}},
            "sort": [{"discovered_at": "desc"}],
            "size": 1
        }
        result = self.es.search(index="api-discoveries", body=query)
        if result['hits']['total']['value'] > 0:
            return result['hits']['hits'][0]['_source']
        return None
    
    def _find_openapi_spec(self, api_url: str) -> Optional[Dict]:
        for spec_path in self.COMMON_SPEC_PATHS:
            spec_url = f"{api_url}{spec_path}"
            try:
                response = requests.get(spec_url, timeout=2)
                if response.status_code == 200:
                    content_type = response.headers.get('content-type', '')
                    if 'json' in content_type:
                        spec = response.json()
                    elif 'yaml' in content_type or 'yml' in content_type:
                        import yaml
                        spec = yaml.safe_load(response.text)
                    else:
                        try:
                            spec = response.json()
                        except:
                            import yaml
                            spec = yaml.safe_load(response.text)
                    
                    if 'openapi' in spec or 'swagger' in spec:
                        return spec
            except Exception:
                continue
        return None
    
    def _parse_openapi_spec(self, spec: Dict, api_url: str) -> Dict:
        try:
            info = spec.get('info', {})
            servers = spec.get('servers', [])
            base_url = servers[0]['url'] if servers else api_url
            
            endpoints = []
            paths = spec.get('paths', {})
            
            for path, methods in paths.items():
                for method, details in methods.items():
                    if method in ['get', 'post', 'put', 'patch', 'delete']:
                        endpoints.append({
                            'path': path,
                            'method': method.upper(),
                            'summary': details.get('summary', ''),
                            'description': details.get('description', ''),
                            'parameters': details.get('parameters', []),
                            'request_body': details.get('requestBody'),
                            'responses': details.get('responses', {})
                        })
            
            auth_type = self._detect_auth_from_spec(spec)
            
            return {
                'api_url': api_url,
                'api_name': info.get('title', 'Unknown API'),
                'api_description': info.get('description', ''),
                'base_url': base_url,
                'has_openapi_spec': True,
                'openapi_spec_url': api_url,
                'auth_type': auth_type,
                'endpoints': endpoints,
                'total_endpoints': len(endpoints),
                'discovery_status': 'complete'
            }
        except Exception as e:
            logger.error(f"Error parsing OpenAPI spec: {e}")
            return {
                'api_url': api_url,
                'discovery_status': 'failed',
                'error_message': str(e)
            }
    
    def _detect_auth_from_spec(self, spec: Dict) -> str:
        security_schemes = spec.get('components', {}).get('securitySchemes', {})
        if not security_schemes:
            return 'none' if 'security' not in spec else 'unknown'
        
        first_scheme = list(security_schemes.values())[0]
        scheme_type = first_scheme.get('type', '').lower()
        
        if scheme_type == 'http':
            scheme = first_scheme.get('scheme', '').lower()
            return 'bearer' if scheme == 'bearer' else 'basic' if scheme == 'basic' else 'unknown'
        elif scheme_type == 'oauth2':
            return 'oauth2'
        elif scheme_type == 'apikey':
            return 'api_key'
        return 'unknown'
    
    def _manual_discovery(self, api_url: str) -> Dict:
        all_endpoints = []
        auth_type = 'unknown'
        
        version_patterns = ['', '/v1', '/v2', '/v3', '/api', '/api/v1']
        
        for base_path in version_patterns:
            endpoints = self._probe_resources(api_url, base_path, auth_type)
            all_endpoints.extend(endpoints)
        
        seen_resources = {}
        for ep in all_endpoints:
            resource = ep['path'].split('/')[-1].replace('{id}', '')
            if resource not in seen_resources:
                seen_resources[resource] = ep
            else:
                if '/v' in ep['path'] and '/v' not in seen_resources[resource]['path']:
                    seen_resources[resource] = ep
        
        all_endpoints = list(seen_resources.values())
        
        if not all_endpoints:
            all_endpoints = [{
                'path': '/',
                'method': 'GET',
                'status_code': 200,
                'summary': 'API root endpoint',
                'description': '',
                'parameters': []
            }]
        
        return {
            'api_url': api_url,
            'api_name': api_url.split('//')[1].split('/')[0],
            'api_description': f'Reverse-engineered API for {api_url}',
            'base_url': api_url,
            'has_openapi_spec': False,
            'auth_type': auth_type,
            'endpoints': all_endpoints,
            'total_endpoints': len(all_endpoints),
            'discovery_status': 'complete'
        }
    
    def _discover_version_path(self, api_url: str) -> str:
        
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["Notion-Version"] = "2022-06-28"
        
        valid_paths = []
        
        for version in self.VERSION_PATTERNS:
            test_url = f"{api_url}/{version}"
            try:
                response = requests.get(test_url, headers=headers, timeout=2, allow_redirects=False)
                if response.status_code == 200:
                    valid_paths.append((f"/{version}", 200))
                elif response.status_code in [401, 403]:
                    valid_paths.append((f"/{version}", response.status_code))
            except:
                continue
        
        for path, status in valid_paths:
            if status == 200:
                logger.info(f"Found version path: {path}")
                return path
        
        if valid_paths:
            path = valid_paths[0][0]
            logger.info(f"Found version path (auth required): {path}")
            return path
        
        try:
            response = requests.get(f"{api_url}/api", headers=headers, timeout=2)
            if response.status_code == 200:
                return "/api"
        except:
            pass
        
        return ""
    
    def _probe_resources(self, api_url: str, base_path: str, auth_type: str) -> List[Dict]:
        discovered = []
        
        for resource in self.COMMON_RESOURCES:
            path = f"{base_path}/{resource}" if base_path else f"/{resource}"
            test_url = f"{api_url}{path}"
            
            try:
                response = requests.get(test_url, timeout=2, allow_redirects=False)
                status = response.status_code
                
                if status in [200, 201, 204, 400]:
                    discovered.append({
                        'path': path,
                        'method': 'GET',
                        'status_code': status,
                        'summary': f'List {resource}',
                        'description': '',
                        'parameters': [],
                        'requires_auth': False
                    })
                    
                    if not resource.endswith('ch'):
                        singular_path = f"{path}/{{id}}"
                        discovered.append({
                            'path': singular_path,
                            'method': 'GET',
                            'status_code': 200,
                            'summary': f'Get single {resource[:-1] if resource.endswith("s") else resource}',
                            'description': '',
                            'parameters': [
                                {'name': 'id', 'in': 'path', 'required': True, 'type': 'string'}
                            ],
                            'requires_auth': False
                        })
                
                elif status in [401, 403] and self.api_key:
                    headers = {
                        "Authorization": f"Bearer {self.api_key}",
                        "Notion-Version": "2022-06-28"
                    }
                    response_with_auth = requests.get(test_url, headers=headers, timeout=2, allow_redirects=False)
                    auth_status = response_with_auth.status_code
                    
                    if auth_status in [200, 201, 204, 400]:
                        auth_type = self._detect_auth_from_headers(response.headers)
                        discovered.append({
                            'path': path,
                            'method': 'GET',
                            'status_code': auth_status,
                            'summary': f'List {resource}',
                            'description': '',
                            'parameters': [],
                            'requires_auth': True
                        })
                        
                        if not resource.endswith('ch'):
                            singular_path = f"{path}/{{id}}"
                            discovered.append({
                                'path': singular_path,
                                'method': 'GET',
                                'status_code': 200,
                                'summary': f'Get single {resource[:-1] if resource.endswith("s") else resource}',
                                'description': '',
                                'parameters': [
                                    {'name': 'id', 'in': 'path', 'required': True, 'type': 'string'}
                                ],
                                'requires_auth': True
                            })
                    
                    elif auth_status in [401, 403]:
                        discovered.append({
                            'path': path,
                            'method': 'GET',
                            'status_code': auth_status,
                            'summary': f'List {resource}',
                            'description': '',
                            'parameters': [],
                            'requires_auth': True,
                            'auth_note': 'TODO: Complex auth - v0.1 limitation'
                        })
                    
            except requests.exceptions.RequestException:
                continue
        
        return discovered
    
    def _detect_auth_from_headers(self, headers: dict) -> str:
        www_auth = headers.get('WWW-Authenticate', '').lower()
        
        if 'bearer' in www_auth:
            return 'bearer'
        elif 'basic' in www_auth:
            return 'basic'
        elif 'oauth' in www_auth:
            return 'oauth2'
        elif headers.get('X-API-Key') or headers.get('Api-Key'):
            return 'api_key'
        
        return 'unknown'
    
    def _store_discovery(self, discovery_data: Dict) -> None:
        try:
            doc_id = discovery_data['api_url'].replace('/', '_').replace(':', '_').replace('.', '_')
            self.es.index(index='api-discoveries', id=doc_id, document=discovery_data, timeout='2s')
        except Exception:
            pass