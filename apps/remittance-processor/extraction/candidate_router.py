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


def _spreadsheet_metadata(result: dict, context: str | None, *, endpoint, analyzer_id, analyze) -> str | None:
    """Payer, date, total and reference from the sheet's cells outside the payment table (its title
    block), read by Content Understanding. The lines stay the code's. The metadata is advisory: the
    validator chooses among it like any document source, but a differently written name on the sheet
    never becomes a conflict. Optional - any failure leaves the metadata empty."""
    if not context or not endpoint:
        return None
    try:
        analysis = analyze_candidate_content(
            analyze=analyze, endpoint=endpoint, analyzer_id=analyzer_id, content=context.encode("utf-8"),
            content_type="text/plain", candidate_id=result["candidateId"], source_type=result["sourceType"],
        )
    except Exception as exc:
        result["metadataErrorType"] = type(exc).__name__
        return None
    usage.record_cu(analysis.get("usage"))
    found = analysis.get("normalized") or {}
    result["remittanceMetadata"] = found.get("remittanceMetadata") or {}
    result["metadataEvidence"] = found.get("metadataEvidence") or {}
    result["metadataRole"] = "advisory"
    return analysis.get("operationId")


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
            try:
                parsed = parse_spreadsheet(content, str(candidate.get("name", "")))
            except Exception as exc:  # a corrupt or protected workbook goes to review; the other sources still count
                base_result["warnings"].append("spreadsheet_parse_failed")
                base_result["analysisErrorType"] = type(exc).__name__
                base_result["strategy"] = "deterministic_parser"
                normalized_results.append(base_result)
                raw_results.append({"candidateId": candidate_id, "strategy": "deterministic_parser", "errorType": type(exc).__name__})
                continue
            base_result["paymentLines"] = parsed["paymentLines"]
            base_result["warnings"].extend(parsed["warnings"])
            base_result["strategy"] = "deterministic_parser"
            if parsed.get("amountColumns"):  # a sheet read as a statement: candidates validation may prove
                base_result["amountColumns"] = parsed["amountColumns"]
            metadata_operation = _spreadsheet_metadata(base_result, parsed.get("contextText"), endpoint=endpoint,
                                                       analyzer_id=analyzer_id, analyze=analyze)
            mappings = parsed.get("columnMappings") or ([parsed["columnMapping"]] if parsed.get("columnMapping") else [])
            base_result["columnMappedBy"] = sorted({m.get("source") for m in mappings if m.get("source")})
            normalized_results.append(base_result)
            raw_results.append(
                {
                    "candidateId": candidate_id,
                    "strategy": "deterministic_parser",
                    "result": parsed,
                    "metadataOperationId": metadata_operation,
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