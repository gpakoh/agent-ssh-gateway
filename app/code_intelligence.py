"""Code intelligence for smart code search and generation."""

import asyncio
import logging
import os
import re
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class CodeSearchResult:
    """Result of code search."""
    path: str
    line: int
    column: int
    content: str
    context_before: str = ""
    context_after: str = ""


@dataclass
class CodeInsertion:
    """Code insertion suggestion."""
    path: str
    insert_after: str  # Text to insert after
    code: str
    explanation: str
    line_number: int = 0


class CodeIntelligence:
    """Smart code search and generation."""

    def __init__(self, ssh_manager, file_editor):
        self._ssh = ssh_manager
        self._file_editor = file_editor

    async def search_code(
        self,
        session_id: str,
        path: str,
        query: str,
        language: str = "python",
        context_lines: int = 3,
    ) -> list[CodeSearchResult]:
        """Search for code pattern in project."""
        results = []
        
        # Use grep to find matches
        cmd = f"cd {path} && grep -rn -C {context_lines} '{query}' --include='*.{language}' 2>/dev/null || true"
        result = await self._ssh.execute(session_id, cmd, timeout=30)
        
        if result["exit_code"] != 0:
            return results
        
        # Parse grep output
        lines = result["stdout"].split("\n")
        current_file = None
        current_line = 0
        
        for line in lines:
            if line.startswith("--"):
                continue
            
            # Match: filename:line:content
            match = re.match(r'^(.+):(\d+):(.*)$', line)
            if match:
                file_path = match.group(1)
                line_num = int(match.group(2))
                content = match.group(3)
                
                results.append(CodeSearchResult(
                    path=file_path,
                    line=line_num,
                    column=content.find(query) if query in content else 0,
                    content=content.strip(),
                ))
        
        return results

    async def find_insertion_point(
        self,
        session_id: str,
        path: str,
        instruction: str,
        language: str = "python",
    ) -> Optional[CodeInsertion]:
        """Find best insertion point based on instruction."""
        
        # Read file
        content = await self._file_editor.read_file(session_id, path)
        lines = content.split("\n")
        
        # Parse instruction
        instruction_lower = instruction.lower()
        
        # Determine insertion strategy based on instruction
        if "endpoint" in instruction_lower or "route" in instruction_lower:
            return await self._find_endpoint_insertion(session_id, path, lines, instruction)
        
        elif "import" in instruction_lower:
            return await self._find_import_insertion(lines, instruction)
        
        elif "class" in instruction_lower:
            return await self._find_class_insertion(lines, instruction)
        
        elif "function" in instruction_lower or "def " in instruction_lower:
            return await self._find_function_insertion(lines, instruction)
        
        elif "middleware" in instruction_lower:
            return await self._find_middleware_insertion(lines, instruction)
        
        else:
            # Default: append to end of file
            return CodeInsertion(
                path=path,
                insert_after=lines[-1] if lines else "",
                code="",
                explanation="Append to end of file",
                line_number=len(lines),
            )

    async def _find_endpoint_insertion(
        self, session_id: str, path: str, lines: list[str], instruction: str
    ) -> CodeInsertion:
        """Find best place to insert FastAPI endpoint."""
        # Look for existing endpoints
        last_endpoint_line = 0
        in_endpoint = False
        
        for i, line in enumerate(lines):
            if '@app.' in line or '@router.' in line:
                in_endpoint = True
                last_endpoint_line = i
            elif in_endpoint and line.strip() and not line.startswith(' '):
                in_endpoint = False
            elif in_endpoint and i > last_endpoint_line:
                last_endpoint_line = i
        
        # Find a good place after last endpoint
        insert_line = last_endpoint_line + 1 if last_endpoint_line > 0 else len(lines)
        
        # Generate endpoint code
        endpoint_code = self._generate_endpoint_code(instruction)
        
        return CodeInsertion(
            path=path,
            insert_after=lines[insert_line - 1] if insert_line > 0 else "",
            code=endpoint_code,
            explanation=f"Insert after line {insert_line} (after existing endpoints)",
            line_number=insert_line,
        )

    async def _find_import_insertion(self, lines: list[str], instruction: str) -> CodeInsertion:
        """Find best place to insert import."""
        last_import_line = 0
        
        for i, line in enumerate(lines):
            if line.startswith('import ') or line.startswith('from '):
                last_import_line = i
        
        return CodeInsertion(
            path="",
            insert_after=lines[last_import_line] if last_import_line >= 0 else "",
            code="",
            explanation=f"Insert after line {last_import_line + 1} (after imports)",
            line_number=last_import_line + 1,
        )

    async def _find_class_insertion(self, lines: list[str], instruction: str) -> CodeInsertion:
        """Find best place to insert class."""
        # Find end of last class or module level
        last_class_end = 0
        indent_level = 0
        
        for i, line in enumerate(lines):
            if line.startswith('class '):
                indent_level = 0
                last_class_end = i
            elif line.strip() and not line.startswith('#'):
                current_indent = len(line) - len(line.lstrip())
                if current_indent == 0 and last_class_end > 0:
                    last_class_end = i - 1
        
        return CodeInsertion(
            path="",
            insert_after=lines[last_class_end] if last_class_end >= 0 else "",
            code="",
            explanation=f"Insert after line {last_class_end + 1} (after last class)",
            line_number=last_class_end + 1,
        )

    async def _find_function_insertion(self, lines: list[str], instruction: str) -> CodeInsertion:
        """Find best place to insert function."""
        last_def_line = 0
        
        for i, line in enumerate(lines):
            if line.startswith('def ') or line.startswith('async def '):
                last_def_line = i
        
        return CodeInsertion(
            path="",
            insert_after=lines[last_def_line] if last_def_line >= 0 else "",
            code="",
            explanation=f"Insert after line {last_def_line + 1} (after last function)",
            line_number=last_def_line + 1,
        )

    async def _find_middleware_insertion(self, lines: list[str], instruction: str) -> CodeInsertion:
        """Find best place to insert middleware."""
        # Look for app.add_middleware or similar
        last_middleware_line = 0
        
        for i, line in enumerate(lines):
            if 'middleware' in line.lower() or 'app.add' in line:
                last_middleware_line = i
        
        return CodeInsertion(
            path="",
            insert_after=lines[last_middleware_line] if last_middleware_line >= 0 else "",
            code="",
            explanation=f"Insert after line {last_middleware_line + 1} (after middleware)",
            line_number=last_middleware_line + 1,
        )

    def _generate_endpoint_code(self, instruction: str) -> str:
        """Generate endpoint code based on instruction."""
        instruction_lower = instruction.lower()
        
        if "health" in instruction_lower:
            return '''
@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok"}
'''
        
        elif "status" in instruction_lower:
            return '''
@app.get("/status")
async def status():
    """Get system status."""
    return {"status": "running", "timestamp": time.time()}
'''
        
        elif "list" in instruction_lower or "get all" in instruction_lower:
            return '''
@app.get("/api/items")
async def list_items():
    """List all items."""
    return {"items": [], "count": 0}
'''
        
        elif "create" in instruction_lower or "post" in instruction_lower:
            return '''
@app.post("/api/items")
async def create_item(request: Request):
    """Create new item."""
    data = await request.json()
    return {"id": str(uuid.uuid4()), "data": data}
'''
        
        else:
            # Generic endpoint
            return f'''
@app.get("/api/new-endpoint")
async def new_endpoint():
    """{instruction}"""
    return {{"message": "Not implemented yet"}}
'''

    async def generate_code(
        self,
        session_id: str,
        instruction: str,
        language: str = "python",
    ) -> str:
        """Generate code using opencode Big Pickle via adapter."""
        import aiohttp
        
        prompt = f"""You are a code generator. Generate only code, no explanations.

Language: {language}
Request: {instruction}

Rules:
- Output ONLY the code block
- No markdown formatting
- No explanations
- Complete, working code
- Follow best practices

Code:"""

        adapter_url = os.environ.get("OPENCODE_ADAPTER_URL", "http://10.0.0.137:8007")
        
        # Делаем до 3 попыток с задержкой
        for attempt in range(3):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{adapter_url}/api/generate",
                        json={
                            "model": "openrouter/auto",
                            "prompt": prompt,
                            "stream": False
                        },
                        timeout=aiohttp.ClientTimeout(total=180)
                    ) as response:
                        if response.status == 200:
                            data = await response.json()
                            generated = data.get("response", "").strip()
                            
                            # Проверяем на ошибку от адаптера
                            if generated.startswith("Error:"):
                                logger.warning(f"⚠️ Attempt {attempt + 1}: Adapter error: {generated}, waiting...")
                                await asyncio.sleep(5 * (attempt + 1))
                                continue
                            
                            # Clean up markdown code blocks
                            if generated.startswith("```"):
                                lines = generated.split("\n")
                                if lines[0].startswith("```"):
                                    lines = lines[1:]
                                if lines and lines[-1].startswith("```"):
                                    lines = lines[:-1]
                                generated = "\n".join(lines).strip()
                            
                            if generated and len(generated) > 50:
                                logger.info("✅ Code generated via OpenRouter (attempt %s)", attempt + 1)
                                return generated
                            else:
                                logger.warning("⚠️ Attempt %s: Empty or short response from adapter", attempt + 1)
                                if attempt < 2:
                                    await asyncio.sleep(3)
                                    continue
                        else:
                            text = await response.text()
                            logger.warning("⚠️ Attempt %s: Adapter returned %s: %s", attempt + 1, response.status, text)
                            if attempt < 2:
                                await asyncio.sleep(3)
                                continue
                            
            except asyncio.TimeoutError:
                logger.warning("⏱️ Attempt %s: Timeout waiting for adapter", attempt + 1)
                if attempt < 2:
                    await asyncio.sleep(5)
                    continue
            except Exception as exc:
                logger.warning("❌ Attempt %s: Adapter request failed: %s", attempt + 1, exc)
                if attempt < 2:
                    await asyncio.sleep(3)
                    continue
        
        # Fallback to template generation
        logger.info("🔄 Using fallback code generation after all attempts failed")
        return self._generate_fallback(instruction, language)

    def _generate_fallback(self, instruction: str, language: str) -> str:
        """Fallback code generation when Ollama is unavailable."""
        instruction_lower = instruction.lower()
        
        if language == "python":
            if "class" in instruction_lower:
                return self._generate_class(instruction)
            elif "function" in instruction_lower or "def " in instruction_lower:
                return self._generate_function(instruction)
            elif "endpoint" in instruction_lower or "route" in instruction_lower:
                return self._generate_endpoint_code(instruction)
        
        return f"""# {instruction}
# TODO: Implement this feature in {language}
"""

    def _generate_class(self, instruction: str) -> str:
        """Generate class code."""
        words = instruction.split()
        class_name = "NewClass"
        for word in words:
            if word[0].isupper():
                class_name = word
                break
        
        return f'''class {class_name}:
    """{instruction}"""
    
    def __init__(self):
        pass
    
    def process(self):
        """Main processing method."""
        pass
'''

    def _generate_function(self, instruction: str) -> str:
        """Generate function code."""
        words = instruction.split()
        func_name = "new_function"
        for word in words:
            if word.isalpha() and word not in ('a', 'an', 'the', 'new', 'add', 'create'):
                func_name = word.lower()
                break
        
        return f'''async def {func_name}():
    """{instruction}"""
    # TODO: Implement
    pass
'''

    async def suggest_completion(
        self,
        session_id: str,
        path: str,
        partial_code: str,
        language: str = "python",
    ) -> str:
        """Suggest code completion based on partial code."""
        # Simple pattern matching for completions
        if partial_code.strip().endswith('('):
            # Function call - suggest parameters
            return "self, *args, **kwargs"
        
        elif 'class ' in partial_code and ':' not in partial_code:
            # Class definition - suggest inheritance
            return "(object):\n    \"\"\"Description\"\"\"\n    \n    def __init__(self):\n        pass"
        
        elif 'def ' in partial_code and ':' not in partial_code:
            # Function definition - suggest body
            return ":\n    \"\"\"Description\"\"\"\n    pass"
        
        elif partial_code.strip().endswith('.'):
            # Method call - suggest common methods
            return "method()"
        
        else:
            return "# Continue implementation here"
