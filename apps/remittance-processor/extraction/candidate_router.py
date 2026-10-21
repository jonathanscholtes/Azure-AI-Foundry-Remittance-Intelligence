from __future__ import annotations

from pathlib import Path
from typing import Callable

from extraction.content_understanding import analyze_binary_candidate, analyze_candidate_content
from extraction.spreadsheet_parser import parse_spreadsheet
from shared import usage


SPREADSHEET_EXTENSIONS = {".csv", ".xls", ".xlsx"}
CONTENT_UNDERSTANDING_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".docm",
    ".xlsm",
    ".ods",
    ".odt",
    ".rtf",
    ".txt",
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".bmp",
    ".heic",
    ".heif",
}


def route_candidates(
    candidates: list[dict],
    payloads: dict[str, tuple[bytes, str]],
    *,
    endpoint: str | None,
    analyzer_id: str,
    analyze: Callable = analyze_binary_candidate,
) -> tuple[list[dict], list[dict]]:
    """Extract each eligible candidate, returning normalized and raw outputs."""
    normalized_results = []
    raw_results = []
    for candidate in candidates:
        if candidate.get("classification") != "remittance_candidate":
            continue

        candidate_id = candidate.get("candidateId")
        source_type = candidate.get("sourceType", "attachment")
        extension = str(candidate.get("extension") or Path(candidate.get("name", "")).suffix).lower()
        payload = payloads.get(candidate_id)
        base_result = {
            "candidateId": candidate_id,
            "sourceType": source_type,
            "score": candidate.get("score", 0),
            "remittanceMetadata": {},
            "paymentLines": [],
            "warnings": [],
        }

        if not payload:
            base_result["warnings"].append("candidate_content_unavailable")
            normalized_results.append(base_result)
            continue

        content, content_type = payload
        if extension in SPREADSHEET_EXTENSIONS:
            parsed = parse_spreadsheet(content, str(candidate.get("name", "")))
            base_result["paymentLines"] = parsed["paymentLines"]
            base_result["warnings"].extend(parsed["warnings"])
            base_result["strategy"] = "deterministic_parser"
            mappings = parsed.get("columnMappings") or ([parsed["columnMapping"]] if parsed.get("columnMapping") else [])
            base_result["columnMappedBy"] = sorted({m.get("source") for m in mappings if m.get("source")})
            normalized_results.append(base_result)
            raw_results.append(
                {
                    "candidateId": candidate_id,
                    "strategy": "deterministic_parser",
                    "result": parsed,
                }
            )
            continue

        if extension not in CONTENT_UNDERSTANDING_EXTENSIONS:
            base_result["warnings"].append("unsupported_content_type")
            normalized_results.append(base_result)
            continue
        if not endpoint:
            base_result["warnings"].append("content_understanding_not_configured")
            normalized_results.append(base_result)
            continue

        try:
            analysis = analyze_candidate_content(
                analyze=analyze,
                endpoint=endpoint,
                analyzer_id=analyzer_id,
                content=content,
                content_type=content_type,
                candidate_id=candidate_id,
                source_type=source_type,
            )
        except Exception as exc:
            base_result["warnings"].append("content_understanding_analysis_failed")
            base_result["analysisErrorType"] = type(exc).__name__
            base_result["analyzerId"] = analyzer_id
            normalized_results.append(base_result)
            raw_results.append(
                {
                    "candidateId": candidate_id,
                    "strategy": "content_understanding",
                    "errorType": type(exc).__name__,
                }
            )
            continue

        usage.record_cu(analysis.get("usage"))  # billing usage, summed over chunks; into the activity's collector
        extracted = analysis["normalized"]
        extracted["score"] = base_result["score"]
        extracted["strategy"] = "content_understanding"
        # The analyzer that produced this candidate, recorded on the final result (analyzerVersion).
        extracted["analyzerId"] = analyzer_id
        normalized_results.append(extracted)
        raw_results.append(
            {
                "candidateId": candidate_id,
                "strategy": "content_understanding",
                "operationId": analysis.get("operationId"),
                "result": analysis["raw"],
            }
        )

    return normalized_results, raw_results