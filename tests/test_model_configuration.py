"""Check all production request configurations without starting the worker."""
import ast
from pathlib import Path
import unittest


class ModelConfigurationTests(unittest.TestCase):
    def test_all_four_requests_use_tested_haiku_configuration(self):
        tree = ast.parse((Path(__file__).parents[1] / "monitor.py").read_text())
        model = next(n.value.value for n in tree.body if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == "MODEL" for t in n.targets))
        self.assertEqual(model, "claude-haiku-5-5")
        requests = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            fields = {k.value: v for k, v in zip(node.keys, node.values) if isinstance(k, ast.Constant)}
            if "model" not in fields or "messages" not in fields:
                continue
            requests.append(fields)
            self.assertEqual(ast.unparse(fields["model"]), "MODEL")
            self.assertNotIn("temperature", fields)
            self.assertEqual(ast.literal_eval(fields["thinking"]), {"type": "disabled"})
            config = fields["output_config"]
            options = {k.value: v for k, v in zip(config.keys, config.values)}
            self.assertEqual(ast.literal_eval(options["effort"]), "low")
            if "format" in options:
                self.assertIn("CATEGORY_RESULT_SCHEMA", ast.unparse(options["format"]))
        self.assertEqual(len(requests), 4)


if __name__ == "__main__":
    unittest.main()
