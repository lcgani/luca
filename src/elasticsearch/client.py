from elasticsearch import Elasticsearch
from typing import Dict, Any
import logging

logger = logging.getLogger(__name__)


class ESClient:
    def __init__(self, url: str, api_key: str = None):
        self.url = url
        if api_key:
            self.client = Elasticsearch(url, api_key=api_key)
        else:
            self.client = Elasticsearch(url)
    
    def index(self, index: str, id: str, document: Dict[str, Any], timeout: str = '10s'):
        return self.client.index(index=index, id=id, document=document, timeout=timeout)
    
    def search(self, index: str, body: Dict[str, Any]):
        return self.client.search(index=index, body=body)
    
    def update(self, index: str, id: str, body: Dict[str, Any]):
        return self.client.update(index=index, id=id, body=body)
    
    def indices_exists(self, index: str) -> bool:
        return self.client.indices.exists(index=index)
    
    def indices_create(self, index: str, body: Dict[str, Any]):
        return self.client.indices.create(index=index, body=body)
