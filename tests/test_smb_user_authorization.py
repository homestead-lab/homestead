import ast
from pathlib import Path
import unittest

class SMBUserAuthorizationTests(unittest.TestCase):
    def test_user_inventory_and_mutations_require_admin(self):
        source = ast.parse((Path(__file__).resolve().parents[1] / 'server/server.py').read_text(encoding='utf-8'))
        wanted = []
        for node in source.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in ('ADMIN_ROUTES', 'SELF_ROUTES') for t in node.targets):
                wanted.append(node)
            if isinstance(node, ast.FunctionDef) and node.name == 'needed_role':
                wanted.append(node)
        scope = {}
        exec(compile(ast.Module(body=wanted, type_ignores=[]), '<authorization>', 'exec'), scope)
        for path in ('/api/shares/users', '/api/shares/users/delete'):
            for method in ('GET', 'POST'):
                self.assertEqual('admin', scope['needed_role'](path, method))
