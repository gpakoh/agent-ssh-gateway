"""AST-aware code refactoring utilities."""

import ast
import re


class RenameTransformer(ast.NodeTransformer):
    """Rename a symbol (function, class, or variable) in AST."""

    def __init__(self, old_name: str, new_name: str) -> None:
        self.old_name = old_name
        self.new_name = new_name

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        if node.name == self.old_name:
            node.name = self.new_name
        self.generic_visit(node)
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        if node.name == self.old_name:
            node.name = self.new_name
        self.generic_visit(node)
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        if node.name == self.old_name:
            node.name = self.new_name
        self.generic_visit(node)
        return node

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id == self.old_name:
            node.id = self.new_name
        return node

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        # Handle self.old_name or cls.old_name
        if (
            isinstance(node.value, ast.Name)
            and node.value.id in ("self", "cls")
            and node.attr == self.old_name
        ):
            node.attr = self.new_name
        self.generic_visit(node)
        return node


class ExtractFunctionTransformer(ast.NodeTransformer):
    """Extract a block of statements into a new function."""

    def __init__(self, start_line: int, end_line: int, func_name: str) -> None:
        self.start_line = start_line
        self.end_line = end_line
        self.func_name = func_name
        self.extracted = False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        if self.extracted:
            return node

        new_body = []
        extracted_body = []
        in_extract = False

        for stmt in node.body:
            stmt_start = getattr(stmt, "lineno", 0)
            stmt_end = getattr(stmt, "end_lineno", stmt_start)

            if stmt_start == self.start_line:
                in_extract = True

            if in_extract:
                extracted_body.append(stmt)
                if stmt_end >= self.end_line:
                    in_extract = False
                    self.extracted = True
                    # Add call to new function
                    call = ast.Expr(
                        value=ast.Call(
                            func=ast.Name(id=self.func_name, ctx=ast.Load()),
                            args=[],
                            keywords=[],
                        )
                    )
                    ast.fix_missing_locations(call)
                    new_body.append(call)
            else:
                new_body.append(stmt)

        if self.extracted:
            # Create new function with extracted body
            new_func = ast.FunctionDef(
                name=self.func_name,
                args=ast.arguments(
                    posonlyargs=[],
                    args=[ast.arg(arg="self", annotation=None)]
                    if node.args.args and node.args.args[0].arg == "self"
                    else [],
                    kwonlyargs=[],
                    defaults=[],
                    kw_defaults=[],
                ),
                body=extracted_body,
                decorator_list=[],
                returns=None,
            )
            ast.fix_missing_locations(new_func)
            # Insert new function before the current function
            return [new_func, node]

        self.generic_visit(node)
        return node


class ASTRefactor:
    """AST-aware code refactoring tool."""

    @staticmethod
    def rename_symbol(code: str, old_name: str, new_name: str) -> tuple[str, int]:
        """Rename a symbol in Python code.
        
        Returns:
            tuple of (refactored_code, replacements_count)
        """
        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            raise ValueError(f"Invalid Python syntax: {exc}") from exc

        transformer = RenameTransformer(old_name, new_name)
        transformer.visit(tree)

        # Also replace in comments and strings using regex
        refactored = ast.unparse(tree)
        # Simple regex replacement for docstrings and comments
        refactored = re.sub(
            rf'\b{re.escape(old_name)}\b',
            new_name,
            refactored
        )

        # Count replacements
        count = refactored.count(new_name) - code.count(new_name)
        if count < 0:
            count = refactored.count(new_name)

        return refactored, count

    @staticmethod
    def extract_function(code: str, start_line: int, end_line: int, func_name: str) -> str:
        """Extract a block of code into a new function.
        
        Args:
            code: Python source code
            start_line: Starting line number (1-based)
            end_line: Ending line number (1-based)
            func_name: Name for the new function
            
        Returns:
            Refactored code with extracted function
        """
        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            raise ValueError(f"Invalid Python syntax: {exc}") from exc

        transformer = ExtractFunctionTransformer(start_line, end_line, func_name)
        result = transformer.visit(tree)

        if not transformer.extracted:
            raise ValueError(
                f"Could not find block at lines {start_line}-{end_line}"
            )

        return ast.unparse(result)

    @staticmethod
    def analyze_code(code: str) -> dict:
        """Analyze Python code and return structure info.
        
        Returns:
            dict with functions, classes, imports, and variables
        """
        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            raise ValueError(f"Invalid Python syntax: {exc}") from exc

        functions = []
        classes = []
        imports = []
        variables = []

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                functions.append({
                    "name": node.name,
                    "line": node.lineno,
                    "end_line": getattr(node, "end_lineno", node.lineno),
                    "args": [arg.arg for arg in node.args.args],
                })
            elif isinstance(node, ast.ClassDef):
                classes.append({
                    "name": node.name,
                    "line": node.lineno,
                    "end_line": getattr(node, "end_lineno", node.lineno),
                    "methods": [
                        n.name for n in node.body
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    ],
                })
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.Import):
                    imports.extend([alias.name for alias in node.names])
                else:
                    module = node.module or ""
                    imports.extend([
                        f"{module}.{alias.name}" if alias.name != alias.asname else module
                        for alias in node.names
                    ])
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        variables.append({
                            "name": target.id,
                            "line": node.lineno,
                        })

        return {
            "functions": functions,
            "classes": classes,
            "imports": imports,
            "variables": variables,
        }