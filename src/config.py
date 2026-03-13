import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    elasticsearch_url = os.getenv('ELASTICSEARCH_URL', 'http://localhost:9200')
    elasticsearch_api_key = os.getenv('ELASTICSEARCH_API_KEY')
    
    embedding_model_name = os.getenv('EMBEDDING_MODEL', 'all-MiniLM-L6-v2')
    embedding_dims = 384
    
    request_timeout = 2
    max_endpoints_per_tool = 50
    max_probe_endpoints = 20
    
    api_specific_headers = {
        'notion': {
            'Notion-Version': '2022-06-28'
        }
    }
