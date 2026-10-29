"""Golden-set evaluation: score the pipeline against real remittances with verified expected output.

Every change - code, Content Understanding analyzer, prompt, agent, or model version - is measured
against the same labeled samples, so behavior changes are a number, not a guess.

The golden set lives in samples/golden/ (git-ignored: real messages and the expected results labeled
from them are never committed). One folder per sample:

    samples/golden/<sample-id>/
        <original>.msg | .eml       the source message, byte for byte
        expected.json               expected metadata, status, triage issues (schemas/golden-sample-v1.json)
        expected-lines.csv          expected payment lines: pro,amount (one row per line, raw PRO as written)

Commands:
    add      copy a message into the set and draft its labels from the current pipeline output
             (labels start unverified - a person must check them against the source, then `verify`)
    verify   mark a sample's labels as checked by a person
    run      run every sample and score it; writes samples/golden/reports/<timestamp>-<mode>.json and
             compares with the previous report of the same mode

Modes for add/run:
    local    in-process pipeline (intake, table readers, CU, validation, triage) - no agent call.
             CU responses are cached by content hash in samples/golden/.cache/cu/, so a code change
             re-scores in seconds at no cost. --cu cached (default: fail on a cache miss) | live (call CU
             on a miss) | refresh (always call CU - use after analyzer/model changes).
    api      end to end through the deployed pipeline (upload or reprocess, poll, read the result),
             including the policy agent. --api https://<ui-fqdn>

Examples:
    python scripts/golden_eval.py add "C:/path/payer remittance backup.msg" --id wingtip-freight-2026-10 --cu live
    python scripts/golden_eval.py verify wingtip-freight-2026-10 --by "J. Analyst"
    python scripts/golden_eval.py run                    # local, cached CU
    python scripts/golden_eval.py run --cu refresh       # after an analyzer change
    python scripts/golden_eval.py run --mode api --api https://<ui-fqdn>

Local mode needs CONTENT_UNDERSTANDING_ENDPOINT (for CU calls) - read from the environment, the
repo .env, or `--resource-group` (infra output). `az login` provides the credential.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROCESSOR = REPO / "apps" / "remittance-processor"
GOLDEN = REPO / "samples" / "golden"
CACHE = GOLDEN / ".cache" / "cu"
REPORTS = GOLDEN / "reports"
TERMINAL = {"Completed", "ReviewRequired", "Failed", "DuplicateSkipped"}
SOURCE_SUFFIXES = (".msg", ".eml")

sys.path.insert(0, str(PROCESSOR))


# --- environment ---------------------------------------------------------------------------------


def load_dotenv() -> None:
    path = REPO / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def _infra_output(resource_group: str | None, name: str) -> str | None:
    if not resource_group:
        return None
    result = subprocess.run(
        ["az", "deployment", "group", "show", "-g", resource_group, "-n", "main",
         "--query", f"properties.outputs.{name}.value", "-o", "tsv"],
        capture_output=True, text=True, shell=os.name == "nt",
    )
    return result.stdout.strip() or None


def resolve_setting(env_name: str, resource_group: str | None, output_name: str, default: str | None = None) -> str | None:
    """Environment / .env first, then the 'main' deployment output, then the default."""
    value = os.environ.get(env_name) or _infra_output(resource_group, output_name) or default
    if value:
        os.environ[env_name] = value
    return value


def resolve_cu_endpoint(resource_group: str | None) -> str | None:
    return resolve_setting("CONTENT_UNDERSTANDING_ENDPOINT", resource_group, "contentUnderstandingEndpoint")


# --- local pipeline ------------------------------------------------------------------------------


class _Blob:
    def __init__(self, store, container, name):
        self.store, self.key = store, (container, name)

    def exists(self):
        return self.key in self.store.blobs

    def upload_blob(self, data, overwrite=False, content_settings=None):
        self.store.blobs[self.key] = bytes(data)

    def download_blob(self):
        data = self.store.blobs[self.key]
        return type("Download", (), {"readall": lambda _self: data})()


class MemoryBlobService:
    """Just enough of BlobServiceClient for the activities (shared/storage.py)."""

    def __init__(self):
        self.blobs: dict[tuple[str, str], bytes] = {}

    def get_blob_client(self, container, name):
        return _Blob(self, container, name)


class CachedAnalyzer:
    """Content Understanding with a content-hash cache of the raw analyzer response. Normalization
    (and grounding) is re-run from the cached raw result, so code changes are always re-scored."""

    def __init__(self, policy: str):
        from extraction.content_understanding import analyze_binary_candidate, normalize_analysis

        self.policy = policy
        self.live = analyze_binary_candidate
        self.normalize = normalize_analysis
        self.calls = 0
        self.hits = 0
        CACHE.mkdir(parents=True, exist_ok=True)

    def __call__(self, *, endpoint, analyzer_id, content, content_type, candidate_id, source_type):
        key = hashlib.sha256(f"{analyzer_id}|{content_type}|".encode() + content).hexdigest()
        path = CACHE / f"{key}.json"
        if path.exists() and self.policy != "refresh":
            self.hits += 1
            raw = json.loads(path.read_text(encoding="utf-8"))
            return {"operationId": None, "normalized": self.normalize(raw, candidate_id=candidate_id, source_type=source_type), "raw": raw}
        if self.policy == "cached":
            raise RuntimeError(f"CU cache miss for {candidate_id} ({len(content)} bytes) - rerun with --cu live")
        self.calls += 1
        result = self.live(endpoint=endpoint, analyzer_id=analyzer_id, content=content, content_type=content_type,
                           candidate_id=candidate_id, source_type=source_type)
        path.write_text(json.dumps(result["raw"], default=str), encoding="utf-8")
        return result


class CachedAgent:
    """Foundry agent calls (the ACH Extraction Expert) cached by agent + prompt, like CU responses."""

    def __init__(self, policy: str, live):
        self.policy = policy
        self.live = live
        self.calls = 0
        self.hits = 0
        self.directory = GOLDEN / ".cache" / "agents"
        self.directory.mkdir(parents=True, exist_ok=True)

    def __call__(self, agent, prompt):
        key = hashlib.sha256(f"{agent}|{prompt}".encode("utf-8")).hexdigest()
        path = self.directory / f"{key}.json"
        if path.exists() and self.policy != "refresh":
            self.hits += 1
            cached = json.loads(path.read_text(encoding="utf-8"))
            return cached["text"], cached["annotations"]
        if self.policy == "cached":
            raise RuntimeError(f"agent cache miss for {agent} - rerun with --cache live")
        self.calls += 1
        text, annotations = self.live(agent, prompt)
        path.write_text(json.dumps({"agent": agent, "text": text, "annotations": annotations}), encoding="utf-8")
        return text, annotations


RESOLUTION = {"enabled": False}


class CachedResolution:
    """ACH Resolution Expert runs cached by agent + evidence pack (its tools are deterministic code)."""

    def __init__(self, policy: str, live):
        self.policy = policy
        self.live = live
        self.calls = 0
        self.hits = 0
        self.directory = GOLDEN / ".cache" / "resolution"
        self.directory.mkdir(parents=True, exist_ok=True)

    def __call__(self, agent, case, tools):
        key = hashlib.sha256(json.dumps([agent, case], sort_keys=True, default=str).encode("utf-8")).hexdigest()
        path = self.directory / f"{key}.json"
        if path.exists() and self.policy != "refresh":
            self.hits += 1
            cached = json.loads(path.read_text(encoding="utf-8"))
            return cached["text"], cached["trace"]
        if self.policy == "cached":
            raise RuntimeError("Resolution Expert cache miss - rerun with --cache live")
        self.calls += 1
        text, trace = self.live(agent, case, tools)
        path.write_text(json.dumps({"agent": agent, "text": text, "trace": trace}, default=str), encoding="utf-8")
        return text, trace


class CachedDecision:
    """Microsoft-Decision-1 calls cached by request body."""

    def __init__(self, policy: str, live):
        self.policy = policy
        self.live = live
        self.calls = 0
        self.hits = 0
        self.directory = GOLDEN / ".cache" / "decision"
        self.directory.mkdir(parents=True, exist_ok=True)

    def __call__(self, body, **kwargs):
        key = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        path = self.directory / f"{key}.json"
        if path.exists() and self.policy != "refresh":
            self.hits += 1
            return json.loads(path.read_text(encoding="utf-8"))
        if self.policy == "cached":
            raise RuntimeError("Decision-1 cache miss - rerun with --cache live")
        self.calls += 1
        response = self.live(body, **kwargs)
        path.write_text(json.dumps(response), encoding="utf-8")
        return response


def run_local(source: Path, analyzer: CachedAnalyzer) -> dict:
    """Run intake -> extraction -> validation -> triage in-process; return the scored output."""
    from assurance.triage import triage
    from orchestration import activities

    content = source.read_bytes()
    processing_id = activities.processing_id_for(content)
    blobs = MemoryBlobService()
    blob_name = f"{processing_id}/{source.name}"
    blobs.blobs[("incoming", blob_name)] = content
    base = {"processingId": processing_id, "runId": "golden", "correlationId": "golden"}
    started = time.perf_counter()

    intake = activities.intake_parse({**base, "sourceBlobName": blob_name, "sourceName": source.name, "force": True}, blob_service=blobs)
    if intake["outcome"] != "parsed":
        return {"status": "Failed", "error": intake.get("reason"), "durationMs": int((time.perf_counter() - started) * 1000)}
    eligible = [c for c in intake["candidates"] if c.get("classification") == "remittance_candidate"]

    def extract(candidate):
        return activities.extract_candidate({**base, "candidate": candidate}, blob_service=blobs, analyze=analyzer, sleep=lambda _s: None)

    with ThreadPoolExecutor(max_workers=4) as pool:
        extractions = list(pool.map(extract, eligible))
    validated = activities.validate_reconcile(
        {**base, "intake": {k: v for k, v in intake.items() if k not in ("manifest", "candidates")}, "extractions": extractions},
        blob_service=blobs,
    )
    final = validated["finalResult"]

    # Same as the orchestrator: the ACH Resolution Expert for runs that fail verification.
    from resolution import resolver

    if RESOLUTION["enabled"] and resolver.needs_resolution(
            final["quality"]["warnings"], requires_review=bool(final["quality"].get("requiresReview"))):
        resolved = activities.resolve_extraction(
            {**base, "intake": {k: v for k, v in intake.items() if k not in ("manifest", "candidates")},
             "extractions": extractions, "finalResult": final},
            blob_service=blobs,
        )
        final = resolver.apply_resolution(final, resolved)
    review = final["quality"]["requiresReview"]
    return {
        "status": "ReviewRequired" if review else "Completed",
        "metadata": final["remittanceMetadata"],
        "lines": final["paymentLines"],
        "issues": [issue["id"] for issue in triage(final)] if review else [],
        "strategies": {e["candidateId"]: e.get("strategy") for e in extractions},
        "classification": final.get("sourceClassification"),
        "resolution": final.get("resolution"),
        "extractionErrors": {
            e["candidateId"]: e["normalized"].get("analysisErrorType") or "content_understanding_analysis_failed"
            for e in extractions
            if "content_understanding_analysis_failed" in (e.get("warnings") or [])
        },
        "durationMs": int((time.perf_counter() - started) * 1000),
    }


# --- api pipeline --------------------------------------------------------------------------------


def _http(method: str, url: str, data: bytes | None = None, headers: dict | None = None, timeout: float = 120):
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, None


def run_api(source: Path, api: str, timeout_s: int = 900) -> dict:
    """Submit (or reprocess) through the deployed API and wait for a fresh terminal run."""
    base = api.rstrip("/") + "/api/remittances"
    content = source.read_bytes()
    processing_id = "REM-" + hashlib.sha256(content).hexdigest()[:20].upper()
    status, existing = _http("GET", f"{base}/{processing_id}")
    previous_run = (existing or {}).get("runId") if status == 200 else None
    started = time.perf_counter()
    if status == 200 and previous_run:
        _http("POST", f"{base}/{processing_id}/reprocess")
    else:
        boundary = uuid.uuid4().hex
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{source.name}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        _http("POST", base, body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    while time.perf_counter() - started < timeout_s:
        time.sleep(5)
        status, run = _http("GET", f"{base}/{processing_id}")
        if status == 200 and run and run.get("runId") and run.get("runId") != previous_run and run.get("status") in TERMINAL:
            assurance = run.get("assurance") or {}
            return {
                "status": run["status"],
                "metadata": run.get("remittanceMetadata") or {},
                "lines": run.get("paymentLines") or [],
                "issues": [f.get("issueId") for f in assurance.get("findings") or []],
                "assurance": {"headline": assurance.get("headline"), "decision": assurance.get("decision")},
                "processingId": processing_id,
                "runId": run["runId"],
                "durationMs": int((time.perf_counter() - started) * 1000),
            }
    return {"status": "Timeout", "processingId": processing_id, "durationMs": int((time.perf_counter() - started) * 1000)}


# --- labels and scoring --------------------------------------------------------------------------


def _amount(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", "").replace("$", "")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def line_key(line: dict) -> tuple[str, Decimal | None]:
    """PRO exactly as written (leading zeros and suffixes kept) + amount to the cent."""
    pro = line.get("rawProInvoiceNumber") or line.get("proInvoiceNumber") or line.get("pro") or ""
    return " ".join(str(pro).split()).upper(), _amount(line.get("amountPaid", line.get("amount")))


def read_expected_lines(sample: Path) -> list[dict]:
    path = sample / "expected-lines.csv"
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [{"pro": row["pro"], "amount": row["amount"]} for row in csv.DictReader(handle)]


def write_expected_lines(sample: Path, lines: list[dict]) -> None:
    with (sample / "expected-lines.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["pro", "amount"])
        for line in lines:
            pro, amount = line_key(line)
            writer.writerow([pro, format(amount, "f") if amount is not None else ""])


def _same(field: str, expected, actual) -> bool:
    if field == "paymentTotal":
        return _amount(expected) == _amount(actual)
    if field == "payerName":
        return " ".join(str(expected or "").lower().split()) == " ".join(str(actual or "").lower().split())
    return str(expected or "")[:10] == str(actual or "")[:10] if field == "remittanceDate" else str(expected) == str(actual)


def score(sample: Path, label: dict, output: dict) -> dict:
    expected = label.get("expected") or {}
    want = Counter(line_key(line) for line in read_expected_lines(sample))
    got = Counter(line_key(line) for line in output.get("lines") or [])
    matched = want & got
    missing = want - got
    extra = got - want
    n_want, n_got, n_match = sum(want.values()), sum(got.values()), sum(matched.values())
    dollars_want = sum(abs(k[1] or 0) * v for k, v in want.items())
    dollars_match = sum(abs(k[1] or 0) * v for k, v in matched.items())

    metadata = {}
    for field, value in (expected.get("metadata") or {}).items():
        metadata[field] = {"expected": value, "actual": (output.get("metadata") or {}).get(field),
                           "ok": _same(field, value, (output.get("metadata") or {}).get(field))}
    issues = None
    if "issues" in expected:
        want_issues, got_issues = set(expected["issues"]), set(output.get("issues") or [])
        issues = {"missing": sorted(want_issues - got_issues), "unexpected": sorted(got_issues - want_issues),
                  "ok": want_issues == got_issues}
    status_ok = expected.get("status") in (None, output.get("status"))
    precision = n_match / n_got if n_got else (1.0 if not n_want else 0.0)
    recall = n_match / n_want if n_want else (1.0 if not n_got else 0.0)
    passed = (
        precision == 1.0 and recall == 1.0 and status_ok
        and all(m["ok"] for m in metadata.values()) and (issues is None or issues["ok"])
    )
    return {
        "id": label.get("id") or sample.name,
        "verified": bool(label.get("verified")),
        "pass": passed,
        "status": {"expected": expected.get("status"), "actual": output.get("status"), "ok": status_ok},
        "lines": {"expected": n_want, "actual": n_got, "matched": n_match, "precision": round(precision, 4), "recall": round(recall, 4),
                  "dollarRecall": float(dollars_match / dollars_want) if dollars_want else 1.0,
                  "missingSample": [f"{p} {a}" for (p, a) in list(missing.elements())[:10]],
                  "extraSample": [f"{p} {a}" for (p, a) in list(extra.elements())[:10]]},
        "metadata": metadata,
        "issues": issues,
        "durationMs": output.get("durationMs"),
        "detail": {k: output[k] for k in ("strategies", "extractionErrors", "classification", "resolution", "assurance", "processingId", "runId", "error") if k in output},
    }


def samples(only: list[str] | None) -> list[Path]:
    if not GOLDEN.exists():
        return []
    found = [p for p in sorted(GOLDEN.iterdir()) if p.is_dir() and (p / "expected.json").exists()]
    return [p for p in found if not only or p.name in only]


def source_of(sample: Path) -> Path:
    sources = [p for p in sample.iterdir() if p.suffix.lower() in SOURCE_SUFFIXES]
    if len(sources) != 1:
        raise SystemExit(f"{sample.name}: expected exactly one .msg/.eml, found {len(sources)}")
    return sources[0]


# --- commands ------------------------------------------------------------------------------------


def produce(source: Path, args, analyzer) -> dict:
    if args.mode == "api":
        if not args.api:
            raise SystemExit("--mode api needs --api https://<ui-fqdn>")
        return run_api(source, args.api)
    return run_local(source, analyzer)


def make_analyzer(args):
    if args.mode != "local":
        return None
    endpoint = resolve_cu_endpoint(args.resource_group)
    # A placeholder keeps the router on the CU path when every response comes from the cache.
    os.environ["CONTENT_UNDERSTANDING_ENDPOINT"] = endpoint or "https://cache-only.invalid"
    if not endpoint and args.cu != "cached":
        raise SystemExit("Set CONTENT_UNDERSTANDING_ENDPOINT (or pass --resource-group) to call Content Understanding")

    # Column mapping: the ACH Extraction Expert (cached like CU), or the frozen header rules for a baseline.
    from extraction import column_mapping

    if args.column_mapping == "rules":
        os.environ["EXTRACTION_AGENT_NAME"] = ""
    else:
        resolve_setting("FOUNDRY_PROJECT_ENDPOINT", args.resource_group, "foundryProjectEndpoint", "https://cache-only.invalid")
        resolve_setting("EXTRACTION_AGENT_NAME", args.resource_group, "extractionAgentName", "ach-extraction-expert")
        column_mapping.invoke = CachedAgent(args.cu, column_mapping.invoke)

    # Source / thread classification: Microsoft-Decision-1 (cached), or off for a baseline.
    from classification import intake_decision

    if args.decision == "off":
        os.environ["INTAKE_DECISION_DEPLOYMENT"] = ""
    else:
        resolve_setting("INTAKE_DECISION_DEPLOYMENT", args.resource_group, "intakeDecisionDeployment", "intake-decision")
        intake_decision.call = CachedDecision(args.cu, intake_decision.call)

    # ACH Resolution Expert (GPT-5.6 Terra), cached; off for a baseline.
    from resolution import resolver

    RESOLUTION["enabled"] = args.resolution == "on"
    if RESOLUTION["enabled"]:
        resolve_setting("FOUNDRY_PROJECT_ENDPOINT", args.resource_group, "foundryProjectEndpoint", "https://cache-only.invalid")
        resolve_setting("RESOLUTION_AGENT_NAME", args.resource_group, "resolutionAgentName", "ach-resolution-expert")
        resolver.run_agent = CachedResolution(args.cu, resolver.run_agent)
    return CachedAnalyzer(args.cu)


def cmd_add(args) -> int:
    source = Path(args.source)
    sample = GOLDEN / args.id
    if sample.exists() and not args.force:
        raise SystemExit(f"{sample} exists (use --force to redraft its labels)")
    sample.mkdir(parents=True, exist_ok=True)
    target = sample / source.name
    if source.resolve() != target.resolve():
        shutil.copyfile(source, target)
    output = produce(target, args, make_analyzer(args))
    if output.get("extractionErrors") and not args.allow_errors:
        # Never draft labels from a failed extraction - they would encode the failure as "expected".
        print(f"Not drafted: extraction failed for {output['extractionErrors']}. Re-run (often transient), or pass --allow-errors.")
        return 2
    label = {
        "schemaVersion": "1.0",
        "id": args.id,
        "source": target.name,
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "verified": None,
        "notes": args.notes or "DRAFT from the pipeline output - check every line, the metadata and the status against the source before `verify`.",
        "expected": {
            "status": output.get("status"),
            "metadata": {k: (output.get("metadata") or {}).get(k) for k in ("payerName", "remittanceDate", "paymentTotal", "paymentReference")},
            "issues": output.get("issues") or [],
        },
    }
    (sample / "expected.json").write_text(json.dumps(label, indent=2, default=str), encoding="utf-8")
    write_expected_lines(sample, output.get("lines") or [])
    print(f"Drafted {sample}: {len(output.get('lines') or [])} line(s), status {output.get('status')}. Labels are UNVERIFIED.")
    return 0


def cmd_verify(args) -> int:
    path = GOLDEN / args.id / "expected.json"
    label = json.loads(path.read_text(encoding="utf-8"))
    label["verified"] = {"by": args.by, "on": date.today().isoformat()}
    path.write_text(json.dumps(label, indent=2), encoding="utf-8")
    print(f"{args.id} verified by {args.by}")
    return 0


def _print_table(results: list[dict]) -> None:
    header = f"{'sample':32} {'ver':3} {'status (exp/got)':34} {'lines exp/got':13} {'prec':>6} {'recall':>6} {'$rec':>6} {'meta':4} {'issues':6} {'time':>7}  result"
    print(header)
    print("-" * len(header))
    for r in results:
        meta = "ok" if all(m["ok"] for m in r["metadata"].values()) else "DIFF"
        issues = "-" if r["issues"] is None else ("ok" if r["issues"]["ok"] else "DIFF")
        status = f"{r['status']['expected']}/{r['status']['actual']}"
        seconds = f"{(r['durationMs'] or 0) / 1000:.1f}s"
        print(f"{r['id'][:32]:32} {'yes' if r['verified'] else 'no':3} {status[:34]:34} "
              f"{r['lines']['expected']:>5}/{r['lines']['actual']:<7} {r['lines']['precision']:>6.3f} {r['lines']['recall']:>6.3f} "
              f"{r['lines']['dollarRecall']:>6.3f} {meta:4} {issues:6} {seconds:>7}  {'PASS' if r['pass'] else 'FAIL'}")
        if not r["pass"]:
            for field, m in r["metadata"].items():
                if not m["ok"]:
                    print(f"    {field}: expected {m['expected']!r}, got {m['actual']!r}")
            if r["issues"] and not r["issues"]["ok"]:
                print(f"    issues missing {r['issues']['missing']} unexpected {r['issues']['unexpected']}")
            if r["lines"]["missingSample"]:
                print(f"    missing lines (sample): {r['lines']['missingSample']}")
            if r["lines"]["extraSample"]:
                print(f"    extra lines (sample):   {r['lines']['extraSample']}")


def _compare(results: list[dict], mode: str) -> list[str]:
    previous = sorted(REPORTS.glob(f"*-{mode}.json"))
    if not previous:
        return []
    before = {r["id"]: r for r in json.loads(previous[-1].read_text(encoding="utf-8"))["results"]}
    regressions = []
    for r in results:
        old = before.get(r["id"])
        if not old:
            continue
        for metric in ("precision", "recall", "dollarRecall"):
            if r["lines"][metric] < old["lines"][metric] - 1e-9:
                regressions.append(f"{r['id']}: {metric} {old['lines'][metric]:.3f} -> {r['lines'][metric]:.3f}")
        if old["pass"] and not r["pass"]:
            regressions.append(f"{r['id']}: PASS -> FAIL")
    print(f"\nCompared with {previous[-1].name}: " + ("no regressions" if not regressions else f"{len(regressions)} regression(s)"))
    for line in regressions:
        print(f"  REGRESSION {line}")
    return regressions


def cmd_run(args) -> int:
    found = samples(args.only)
    if not found:
        print(f"No samples in {GOLDEN}. Add one with: python scripts/golden_eval.py add <file.msg> --id <name> --cu live")
        return 1
    analyzer = make_analyzer(args)
    results = []
    for sample in found:
        label = json.loads((sample / "expected.json").read_text(encoding="utf-8"))
        try:
            output = produce(source_of(sample), args, analyzer)
        except Exception as exc:  # one broken sample must not hide the others
            output = {"status": "Error", "error": f"{type(exc).__name__}: {exc}"[:300], "lines": []}
        results.append(score(sample, label, output))
    _print_table(results)
    tag = args.mode if args.mode == "api" else (
        f"local-{args.column_mapping}"
        + ("" if args.decision == "on" else "-nodecision")
        + ("" if args.resolution == "on" else "-noresolution")
    )
    regressions = _compare(results, tag)
    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    from extraction import column_mapping

    agent_cache = column_mapping.invoke if isinstance(column_mapping.invoke, CachedAgent) else None
    report = {"createdAtUtc": stamp, "mode": tag, "cache": getattr(args, "cu", None), "results": results,
              "cuCalls": getattr(analyzer, "calls", None), "cuCacheHits": getattr(analyzer, "hits", None),
              "agentCalls": getattr(agent_cache, "calls", None), "agentCacheHits": getattr(agent_cache, "hits", None)}
    path = REPORTS / f"{stamp}-{tag}.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    verified_failures = [r for r in results if r["verified"] and not r["pass"]]
    print(f"\nReport: {path}")
    print(f"{sum(r['pass'] for r in results)}/{len(results)} passed; {len(verified_failures)} verified sample(s) failing"
          + (f"; CU calls {analyzer.calls}, cache hits {analyzer.hits}" if analyzer else "")
          + (f"; agent calls {agent_cache.calls}, cache hits {agent_cache.hits}" if agent_cache else ""))
    return 1 if verified_failures or regressions else 0


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def pipeline_options(p):
        p.add_argument("--mode", choices=["local", "api"], default="local")
        p.add_argument("--cache", "--cu", dest="cu", choices=["cached", "live", "refresh"], default="cached",
                       help="local mode: cache policy for Content Understanding and Foundry agent calls")
        p.add_argument("--column-mapping", choices=["agent", "rules"], default="agent",
                       help="local mode: ACH Extraction Expert (default) or the frozen header rules (baseline)")
        p.add_argument("--decision", choices=["on", "off"], default="on",
                       help="local mode: Microsoft-Decision-1 source/thread classification on (default) or off (baseline)")
        p.add_argument("--resolution", choices=["on", "off"], default="on",
                       help="local mode: ACH Resolution Expert for runs that fail verification on (default) or off (baseline)")
        p.add_argument("--api", help="api mode: https://<ui-fqdn>")
        p.add_argument("--resource-group", help="read CONTENT_UNDERSTANDING_ENDPOINT from this RG's 'main' deployment outputs")

    add = sub.add_parser("add", help="add a message and draft its labels")
    add.add_argument("source")
    add.add_argument("--id", required=True, help="sample id, e.g. wingtip-freight-2026-10")
    add.add_argument("--notes")
    add.add_argument("--force", action="store_true")
    add.add_argument("--allow-errors", action="store_true", help="draft even if an extraction failed")
    pipeline_options(add)

    verify = sub.add_parser("verify", help="mark a sample's labels as checked by a person")
    verify.add_argument("id")
    verify.add_argument("--by", required=True)

    run = sub.add_parser("run", help="score every sample")
    run.add_argument("--only", nargs="*")
    pipeline_options(run)

    args = parser.parse_args()
    return {"add": cmd_add, "verify": cmd_verify, "run": cmd_run}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
