import importlib.util
import unittest
from pathlib import Path

AGENTS = Path(__file__).resolve().parents[3] / "agents"


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _strict_problems(schema: dict, where: str = "$") -> list[str]:
    """Strict structured outputs / strict function tools: every object lists all of its properties as
    required and forbids extra ones - otherwise Foundry rejects every call with BadRequestError."""
    problems = []
    if schema.get("type") == "object":
        properties = schema.get("properties") or {}
        if set(schema.get("required") or []) != set(properties):
            problems.append(f"{where}: required {sorted(schema.get('required') or [])} != properties {sorted(properties)}")
        if schema.get("additionalProperties") is not False:
            problems.append(f"{where}: additionalProperties must be false")
        for name, child in properties.items():
            problems += _strict_problems(child, f"{where}.{name}")
    if schema.get("type") == "array" and isinstance(schema.get("items"), dict):
        problems += _strict_problems(schema["items"], f"{where}[]")
    return problems


class AgentSchemaTests(unittest.TestCase):
    def test_agent_output_schemas_and_tools_are_strict_valid(self):
        modules = [_load(path) for path in sorted(AGENTS.glob("*_agent.py"))]
        self.assertTrue(modules, f"no agent definitions found in {AGENTS}")
        for module in modules:
            schema = getattr(module, "OUTPUT_SCHEMA", None)
            if schema:
                self.assertEqual(_strict_problems(schema), [], module.NAME)
            for tool in getattr(module, "TOOLS", None) or []:
                self.assertEqual(_strict_problems(tool["parameters"]), [], f"{module.NAME} tool {tool['name']}")


if __name__ == "__main__":
    unittest.main()
