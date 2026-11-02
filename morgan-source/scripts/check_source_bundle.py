"""Read-only checks of a clean source snapshot; no app or database opens."""
from pathlib import Path
import ast
import json
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
SKIP = {'.git', '.venv', '.python', '__pycache__', 'audit', 'logs', 'chatdata'}
PAYLOADS = {'.gguf', '.safetensors', '.sqlite', '.sqlite3', '.db', '.exe', '.dll', '.pyd', '.pt', '.pth'}
TOKEN = re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{30,}|sk-(?:proj-)?[A-Za-z0-9_-]{25,}|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----)')

def main():
    errors = []
    count = size = 0
    for p in ROOT.rglob('*'):
        if not p.is_file() or any(part in SKIP for part in p.relative_to(ROOT).parts):
            continue
        rel = p.relative_to(ROOT).as_posix()
        count += 1
        size += p.stat().st_size
        if p.suffix.lower() in PAYLOADS or p.stat().st_size > 5 * 1024 * 1024:
            errors.append('payload: ' + rel)
        if rel.startswith('configs/') and '.example.' not in p.name and p.name != 'README.md':
            errors.append('local config: ' + rel)
        if p.suffix.lower() in {'.py', '.js', '.json', '.md', '.ps1', '.cmd', '.txt'}:
            text = p.read_text(encoding='utf-8-sig')
            if TOKEN.search(text):
                errors.append('credential pattern: ' + rel)
            if re.search(r'(?:[A-Za-z]:[\\/]+(?:Users|project)[\\/]|/home/[^ /]+/)', text):
                errors.append('machine-specific path: ' + rel)
            if p.suffix == '.py':
                try:
                    tree = ast.parse(text, filename=rel)
                    for node in ast.walk(tree):
                        if isinstance(node, ast.ImportFrom) and node.module == 'hermes_lite':
                            for name in node.names:
                                candidate = ROOT / 'src/hermes-lite/hermes_lite' / (name.name + '.py')
                                if name.name != '*' and not candidate.is_file():
                                    errors.append('missing local import: ' + rel + ': ' + name.name)
                except SyntaxError as exc:
                    errors.append('syntax: ' + rel + ': ' + str(exc))
    print(json.dumps({'files': count, 'bytes': size, 'errors': errors}, ensure_ascii=False, indent=2))
    return bool(errors)

if __name__ == '__main__':
    raise SystemExit(main())
