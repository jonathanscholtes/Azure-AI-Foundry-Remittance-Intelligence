"""
Deploy the ACH remittance assurance Foundry agents.

Each agent is a module declaring NAME / INSTRUCTIONS / tool flags, and this script creates a new immutable
version with ``agents.create_version`` and prunes old versions. The Function references agents by
NAME (latest version), so no agent ids need to be injected after a redeploy.

Usage:
    python agents/deploy.py \\
        --project-endpoint <FOUNDRY_PROJECT_ENDPOINT> \\
        --model-deployment cu-gpt-55 \\
        [--search-connection-name ach-policy-search] \\
        [--index-name ach-policy]
"""

import argparse
import json
import logging
import os
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    AISearchIndexResource,
    AzureAISearchAgentTool,
    AzureAISearchQueryType,
    AzureAISearchToolResource,
    FunctionTool,
    PromptAgentDefinition,
    PromptAgentDefinitionText,
    Reasoning,
    ResponseTextFormatConfigurationJsonSchema,
)
from azure.identity import DefaultAzureCredential

from agents import assurance_agent, extraction_agent, resolution_agent

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# In pipeline order. Each module declares NAME (the Foundry agent name the Function references),
# DISPLAY_NAME / PIPELINE_STEP / DESCRIPTION (shown in Foundry so operators can place the agent in
# the process), INSTRUCTIONS, USES_SEARCH, and optionally OUTPUT_SCHEMA, REASONING_EFFORT, TOOLS
# (function tools the Function executes) and MODEL ("reasoning" -> the GPT-5.6 Terra deployment).
AGENT_MODULES = [extraction_agent, resolution_agent, assurance_agent]
KEEP_VERSIONS = 3


class AgentDeployer:
    def __init__(self, project_endpoint: str, model_deployment: str, search_connection_name: str, index_name: str,
                 reasoning_deployment: Optional[str] = None):
        self.model_deployment = model_deployment
        self.reasoning_deployment = reasoning_deployment or model_deployment
        self.search_connection_name = search_connection_name
        self.index_name = index_name
        self.project_client = AIProjectClient(endpoint=project_endpoint, credential=DefaultAzureCredential())

    def _search_connection_id(self) -> Optional[str]:
        try:
            connection = self.project_client.connections.get(self.search_connection_name)
            logger.info("AI Search connection: %s", connection.name)
            return connection.id
        except Exception:
            logger.warning("Connection '%s' not found - attempting auto-discover.", self.search_connection_name)
        try:
            for connection in self.project_client.connections.list():
                if "search" in (getattr(connection, "connection_type", "") or "").lower():
                    logger.info("Auto-discovered AI Search connection: %s", connection.name)
                    return connection.id
        except Exception as exc:
            logger.warning("Could not list project connections: %s", exc)
        logger.warning("No AI Search connection - agents deploy WITHOUT the policy search tool.")
        return None

    def _search_tools(self, connection_id: Optional[str]) -> list:
        if not connection_id:
            return []
        return [
            AzureAISearchAgentTool(
                azure_ai_search=AzureAISearchToolResource(
                    indexes=[
                        AISearchIndexResource(
                            project_connection_id=connection_id,
                            index_name=self.index_name,
                            # Keyword + semantic ranking over the small policy corpus loaded by
                            # scripts/load_policy_corpus.py (no vectorizer needed).
                            query_type=AzureAISearchQueryType.SEMANTIC,
                        )
                    ]
                )
            )
        ]

    def _prune(self, agent_name: str) -> None:
        try:
            versions = list(self.project_client.agents.list_versions(agent_name=agent_name))
            versions.sort(key=lambda a: int(getattr(a, "version", 0) or 0), reverse=True)
            for old in versions[KEEP_VERSIONS:]:
                self.project_client.agents.delete_version(agent_name=agent_name, version=old.version)
                logger.info("Pruned   %-24s version=%s", agent_name, old.version)
        except Exception as exc:
            logger.warning("Could not prune old versions of %s: %s", agent_name, exc)

    def deploy(self) -> dict:
        connection_id = self._search_connection_id()
        results = {}
        for module in AGENT_MODULES:
            tools = self._search_tools(connection_id) if getattr(module, "USES_SEARCH", False) else []
            tools += [
                FunctionTool(name=t["name"], description=t["description"], parameters=t["parameters"], strict=True)
                for t in getattr(module, "TOOLS", [])
            ]
            schema = getattr(module, "OUTPUT_SCHEMA", None)
            effort = getattr(module, "REASONING_EFFORT", None)
            model = self.reasoning_deployment if getattr(module, "MODEL", None) == "reasoning" else self.model_deployment
            agent = self.project_client.agents.create_version(
                agent_name=module.NAME,
                definition=PromptAgentDefinition(
                    model=model,
                    instructions=module.INSTRUCTIONS,
                    tools=tools or None,
                    # Foundry enforces the answer shape, so the Function never parses free text.
                    text=PromptAgentDefinitionText(
                        format=ResponseTextFormatConfigurationJsonSchema(name=module.NAME.replace("-", "_"), schema=schema, strict=True)
                    ) if schema else None,
                    reasoning=Reasoning(effort=effort) if effort else None,
                ),
                description=getattr(module, "DESCRIPTION", None),
                metadata={
                    key: value
                    for key, value in (
                        ("displayName", getattr(module, "DISPLAY_NAME", None)),
                        ("pipelineStep", getattr(module, "PIPELINE_STEP", None)),
                        ("solution", "ACH Remittance Processing"),
                    )
                    if value
                },
            )
            logger.info("Created  %-24s (%s) model=%s id=%s tools=%d", module.NAME, getattr(module, "DISPLAY_NAME", ""), model, agent.id, len(tools))
            self._prune(module.NAME)
            results[module.NAME] = agent.id
        return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy ACH remittance assurance Foundry agents.")
    parser.add_argument("--project-endpoint", required=True)
    parser.add_argument("--model-deployment", default="cu-gpt-55")
    parser.add_argument("--reasoning-deployment", default=None, help="deployment for MODEL='reasoning' agents (GPT-5.6 Terra)")
    parser.add_argument("--search-connection-name", default="ach-policy-search")
    parser.add_argument("--index-name", default="ach-policy")
    args = parser.parse_args()
    ids = AgentDeployer(args.project_endpoint, args.model_deployment, args.search_connection_name, args.index_name,
                        args.reasoning_deployment).deploy()
    print(json.dumps(ids, indent=2))


if __name__ == "__main__":
    main()
