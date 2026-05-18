"""Template library for common code patterns."""

from typing import Optional


class TemplateLibrary:
    """Library of code templates."""

    TEMPLATES = {
        "fastapi_endpoint": {
            "name": "FastAPI Endpoint",
            "description": "Create a new FastAPI endpoint",
            "language": "python",
            "code": '''@app.{method}("{path}")
async def {name}({params}):
    """{description}"""
    {body}
    return {{"status": "ok"}}
''',
            "params": {
                "method": "get",
                "path": "/api/example",
                "name": "example_endpoint",
                "params": "",
                "description": "Example endpoint",
                "body": "# TODO: Implement",
            }
        },
        
        "pydantic_model": {
            "name": "Pydantic Model",
            "description": "Create a Pydantic model",
            "language": "python",
            "code": '''class {name}(BaseModel):
    """{description}"""
    
    {fields}
''',
            "params": {
                "name": "NewModel",
                "description": "New model description",
                "fields": "id: str = Field(...)",
            }
        },
        
        "class": {
            "name": "Python Class",
            "description": "Create a new class",
            "language": "python",
            "code": '''class {name}:
    """{description}"""
    
    def __init__(self{init_params}):
        {init_body}
    
    def {method_name}(self{method_params}):
        """{method_description}"""
        {method_body}
''',
            "params": {
                "name": "NewClass",
                "description": "Class description",
                "init_params": "",
                "init_body": "pass",
                "method_name": "process",
                "method_params": "",
                "method_description": "Process method",
                "method_body": "pass",
            }
        },
        
        "function": {
            "name": "Function",
            "description": "Create a new function",
            "language": "python",
            "code": '''{async_prefix}def {name}({params}):
    """{description}"""
    {body}
''',
            "params": {
                "async_prefix": "",
                "name": "new_function",
                "params": "",
                "description": "Function description",
                "body": "pass",
            }
        },
        
        "test": {
            "name": "Pytest Test",
            "description": "Create a pytest test",
            "language": "python",
            "code": '''def test_{name}():
    """{description}"""
    # Arrange
    {arrange}
    
    # Act
    {act}
    
    # Assert
    {assert_code}
''',
            "params": {
                "name": "example",
                "description": "Test example functionality",
                "arrange": "# Setup",
                "act": "# Execute",
                "assert_code": "assert True",
            }
        },
        
        "docker_compose_service": {
            "name": "Docker Compose Service",
            "description": "Add a service to docker-compose.yml",
            "language": "yaml",
            "code": '''  {name}:
    image: {image}
    container_name: {name}
    restart: {restart}
    ports:
      - "{host_port}:{container_port}"
    environment:
      - {env}
    volumes:
      - {volume}
    networks:
      - {network}
''',
            "params": {
                "name": "service",
                "image": "image:latest",
                "restart": "unless-stopped",
                "host_port": "8080",
                "container_port": "8080",
                "env": "KEY=value",
                "volume": "data:/data",
                "network": "default",
            }
        },
        
        "nginx_config": {
            "name": "Nginx Config",
            "description": "Nginx server block",
            "language": "nginx",
            "code": '''server {{
    listen {port};
    server_name {domain};
    
    location / {{
        proxy_pass http://{upstream};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }}
}}
''',
            "params": {
                "port": "80",
                "domain": "example.com",
                "upstream": "localhost:8080",
            }
        },
        
        "github_actions": {
            "name": "GitHub Actions Workflow",
            "description": "CI/CD workflow",
            "language": "yaml",
            "code": '''name: {name}

on:
  push:
    branches: [ {branch} ]
  pull_request:
    branches: [ {branch} ]

jobs:
  {job_name}:
    runs-on: ubuntu-latest
    
    steps:
    - uses: actions/checkout@v3
    
    - name: Set up Python
      uses: actions/setup-python@v4
      with:
        python-version: '{python_version}'
    
    - name: Install dependencies
      run: |
        pip install -r requirements.txt
    
    - name: Run tests
      run: |
        pytest tests/ -x -q
''',
            "params": {
                "name": "CI",
                "branch": "main",
                "job_name": "test",
                "python_version": "3.11",
            }
        },
    }

    @classmethod
    def list_templates(cls) -> list[dict]:
        """List all available templates."""
        return [
            {
                "id": key,
                "name": template["name"],
                "description": template["description"],
                "language": template["language"],
            }
            for key, template in cls.TEMPLATES.items()
        ]

    @classmethod
    def get_template(cls, template_id: str) -> Optional[dict]:
        """Get template by ID."""
        template = cls.TEMPLATES.get(template_id)
        if not template:
            return None
        return {
            "id": template_id,
            "name": template["name"],
            "description": template["description"],
            "language": template["language"],
            "code": template["code"],
            "default_params": template["params"],
        }

    @classmethod
    def render_template(cls, template_id: str, params: dict) -> Optional[str]:
        """Render template with parameters."""
        template = cls.TEMPLATES.get(template_id)
        if not template:
            return None
        
        # Merge with defaults
        merged_params = {**template["params"], **params}
        
        try:
            return template["code"].format(**merged_params)
        except KeyError as exc:
            raise ValueError(f"Missing parameter: {exc}")
