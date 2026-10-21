"""Create/refresh the `ach-policy` AI Search index and load the neutral deduction-policy corpus
(agents/corpus/*.json) that grounds the assurance agent. Idempotent; re-run when policies change.

No embeddings: the corpus is small, so the agent uses keyword + semantic ranking (no vectorizer to configure).

Usage:
    python scripts/load_policy_corpus.py --search-endpoint https://<name>.search.windows.net [--index-name ach-policy]
"""

import argparse
import json
import logging
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    SearchableField,
    SearchFieldDataType,
    SearchIndex,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    SimpleField,
)

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

CORPUS_DIR = Path(__file__).resolve().parent.parent / "agents" / "corpus"
SEMANTIC_CONFIG = "ach-policy-semantic"


def index_definition(name: str) -> SearchIndex:
    return SearchIndex(
        name=name,
        fields=[
            SimpleField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),
            SimpleField(name="policyId", type=SearchFieldDataType.String, filterable=True),
            SearchableField(name="title", type=SearchFieldDataType.String),
            SimpleField(name="category", type=SearchFieldDataType.String, filterable=True, facetable=True),
            SearchableField(name="content", type=SearchFieldDataType.String),
        ],
        semantic_search=SemanticSearch(
            default_configuration_name=SEMANTIC_CONFIG,
            configurations=[
                SemanticConfiguration(
                    name=SEMANTIC_CONFIG,
                    prioritized_fields=SemanticPrioritizedFields(
                        title_field=SemanticField(field_name="title"),
                        content_fields=[SemanticField(field_name="content")],
                        keywords_fields=[SemanticField(field_name="category")],
                    ),
                )
            ],
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Load the ACH deduction-policy corpus into AI Search.")
    parser.add_argument("--search-endpoint", required=True)
    parser.add_argument("--index-name", default="ach-policy")
    args = parser.parse_args()

    credential = DefaultAzureCredential()
    SearchIndexClient(endpoint=args.search_endpoint, credential=credential).create_or_update_index(
        index_definition(args.index_name)
    )
    logger.info("Index '%s' ready.", args.index_name)

    documents = []
    for path in sorted(CORPUS_DIR.glob("*.json")):
        items = json.loads(path.read_text(encoding="utf-8"))
        documents.extend(items)
        logger.info("Loaded %d policy sections from %s", len(items), path.name)

    results = SearchClient(endpoint=args.search_endpoint, index_name=args.index_name, credential=credential).merge_or_upload_documents(documents)
    failed = [r for r in results if not r.succeeded]
    logger.info("Uploaded %d sections (%d failed).", len(results) - len(failed), len(failed))
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
