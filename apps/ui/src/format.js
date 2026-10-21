export function money(value, currency = "USD") {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  try {
    return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(Number(value));
  } catch {
    return `$${Number(value).toFixed(2)}`;
  }
}

export function number(value) {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat("en-US").format(Number(value));
}

export function dateTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

export function statusAppearance(status) {
  switch (status) {
    case "Completed":
      return "success";
    case "ReviewRequired":
      return "warning";
    case "Failed":
      return "danger";
    case "DuplicateSkipped":
      return "informative";
    default:
      return "brand";
  }
}

export const TERMINAL = new Set(["Completed", "ReviewRequired", "Failed", "DuplicateSkipped"]);

export function isTerminal(status) {
  return TERMINAL.has(status);
}

// Pipeline steps shown in the UI; each maps the detailed Durable states recorded by the Function.
// `owner` is the short "who ran it" shown on a finished step when there is no more specific detail
// (Run Details shows "<owner or detail> · <duration>", the Home workflow shows `sub`).
export const PIPELINE_STEPS = [
  { key: "queued", label: "Queued", sub: "Service Bus", owner: "Service Bus", states: ["Queued"] },
  { key: "intake", label: "Intake & Parse", sub: "MSG / attachments", owner: "MSG/EML", states: ["Received", "Archived", "Parsed", "CandidatesIdentified"] },
  { key: "extract", label: "Extraction", sub: "CU + ACH Extraction Expert", owner: "CU", states: ["Extracting", "Extracted"] },
  { key: "validate", label: "Validate & Reconcile", sub: "Rules + Decision-1 + Resolution Expert", owner: "Rules", states: ["Validating", "Validated", "Reconciled"] },
  { key: "assurance", label: "Agent Assurance", sub: "ACH Assurance Expert (review runs only)", owner: "Expert", states: ["AssuranceReview", "Assessed"] },
  { key: "complete", label: "Complete", sub: "CSV / review", states: ["Completed", "ReviewRequired", "Failed", "DuplicateSkipped"] },
];

export function stepIndexForState(state) {
  return PIPELINE_STEPS.findIndex((step) => step.states.includes(state));
}

const STATUS_LABELS = {
  Queued: "Queued",
  Completed: "Processed",
  ReviewRequired: "Needs Review",
  Failed: "Failed",
  DuplicateSkipped: "Duplicate",
  AssuranceReview: "Agent review",
};

export function statusLabel(status) {
  return STATUS_LABELS[status] || (status ? status.replace(/([a-z])([A-Z])/g, "$1 $2") : "Processing");
}

// Plain-language review reasons for the payment processing team.
const WARNING_LABELS = {
  payment_total_mismatch: "Payment total does not match the sum of lines",
  payment_total_not_provided: "No stated payment total",
  no_payment_lines_found: "No payment lines found",
  conflicting_payment_line: "Same PRO with different amounts",
  missing_payer_name: "Payer name missing",
  missing_remittance_date: "Remittance date missing",
  invalid_remittance_date: "Remittance date invalid",
  unsupported_currency: "Unsupported currency",
  negative_amount_paid: "Negative line amount",
  line_not_grounded: "Extracted PRO/amount pair not found together in the source",
  spreadsheet_no_paid_amounts: "Spreadsheet has no amounts paid (statement, not payment detail)",
  spreadsheet_not_payment_detail: "Spreadsheet is not payment detail",
  multiple_payments_in_email: "Several separate payments in one email",
  pro_paid_across_payments: "A PRO is paid in parts across separate payments",
  payment_total_derived_from_subtotals: "Payment total derived from the sub-totals",
  amount_normalized_from_edi_addenda: "EDI amount converted to dollars",
  remittance_date_derived_from_schedule: "Remittance date taken from the payment schedule",
  remittance_date_derived_from_subject: "Remittance date taken from the email subject",
  remittance_date_resolved_by_expert: "Remittance date resolved by the ACH Resolution Expert (evidence verified)",
  payment_reference_resolved_by_expert: "Payment reference resolved by the ACH Resolution Expert (evidence verified)",
  remittance_inquiry_thread: "Collections inquiry or dispute thread",
  resolved_by_expert: "Lines resolved by the ACH Resolution Expert (grounded and reconciled)",
  no_remittance_in_email: "No remittance detail in the email",
  spreadsheet_total_mismatch: "Spreadsheet lines don't match its own Total row",
  column_mapping_differs_from_rules: "ACH Extraction Expert and header rules chose different columns",
  column_mapping_unverified: "Column mapping not confirmed by a Total row",
  column_mapping_needs_review: "Spreadsheet columns unverified: no total confirms the mapping",
  zero_amount_paid: "Zero line amount",
  missing_or_invalid_amount_paid: "Line amount missing or invalid",
  missing_pro_number: "PRO number missing",
  invalid_pro_number: "PRO number invalid",
  external_link_not_fetched: "Remittance detail behind a link (not fetched)",
  attachment_parse_warning: "An attachment could not be read",
  unclassified_attachment: "Unrecognized attachment",
  content_understanding_analysis_failed: "Document analysis failed",
  content_understanding_not_configured: "Document analysis not configured",
  unsupported_content_type: "Unsupported attachment type",
  candidate_content_unavailable: "Source content unavailable",
  message_too_large: "Message exceeds the size limit",
  unsupported_extension: "Unsupported file type",
  source_hash_mismatch: "Source changed after submission",
};

export function warningLabel(code) {
  if (!code) return "";
  if (WARNING_LABELS[code]) return WARNING_LABELS[code];
  if (code.startsWith("conflicting_")) return `Conflicting ${code.replace("conflicting_", "")} across sources`;
  return code.replace(/_/g, " ");
}

const DECISION_LABELS = {
  approve_deduction: { label: "Approve deduction", color: "success" },
  request_remittance_detail: { label: "Request remittance detail", color: "informative" },
  dispute_short_pay: { label: "Dispute short pay", color: "danger" },
  hold_overpayment: { label: "Hold overpayment", color: "informative" },
  confirm_with_payer: { label: "Confirm with payer", color: "informative" },
  route_to_billing: { label: "Route to billing", color: "informative" },
  manual_review: { label: "Manual review", color: "warning" },
  verify_extraction: { label: "Verify extraction", color: "danger" },
  correct_data: { label: "Correct data", color: "warning" },
};

export function decisionMeta(decision) {
  return DECISION_LABELS[decision] || { label: decision ? decision.replace(/_/g, " ") : "—", color: "brand" };
}

// Short verb for the list views; manual_review has none because the run status already says "Needs review".
const DECISION_SHORT = {
  approve_deduction: "approve",
  request_remittance_detail: "request detail",
  dispute_short_pay: "dispute",
  hold_overpayment: "hold excess",
  confirm_with_payer: "confirm with payer",
  route_to_billing: "route to billing",
  verify_extraction: "verify extraction",
  correct_data: "correct data",
};

// Microsoft-Decision-1 source roles and why a line was set aside (Sources / Payment lines cards).
export const SOURCE_ROLE_LABELS = {
  payment_detail: "Payment detail",
  payment_proof: "Payment proof",
  statement: "Statement",
  correspondence: "Correspondence",
  other: "Other",
  cannot_tell: "Cannot tell",
};

export const EXCLUSION_REASON_LABELS = {
  correspondence: "Mentioned in correspondence, not remitted",
  statement: "Amount owed on a statement, not paid",
  other: "Not payment content",
  payment_proof_corroborates: "Transfer proof confirming a listed payment",
};

// Resolution Expert gate outcomes, in analyst language.
export const RESOLUTION_REASON_LABELS = {
  grounded_and_reconciled: "Applied - every line found in its source and the total reconciles",
  drops_trusted_lines: "Not applied - it chose between valid payment lines (the analyst's call)",
  not_reconciled: "Not applied - its lines do not reconcile to the stated total",
  expert_could_not_resolve: "The expert could not resolve the run",
  lines_confirmed: "Confirmed the extracted lines - extraction is right; see the findings for why the total differs",
  details_resolved: "Resolved run details from the evidence (verified in the cited source)",
  details_recommended: "Recommended a value for the analyst to confirm",
  details_not_verified: "Its proposed details could not be verified in the sources",
};

// Run details the ACH Resolution Expert can resolve, and why code did not accept a proposed value.
export const DETAIL_FIELD_LABELS = { payerName: "Payer", remittanceDate: "Remittance date", paymentReference: "Payment reference" };
export const DETAIL_PROBLEM_LABELS = {
  no_evidence: "no evidence given",
  unknown_source: "cites an unknown source",
  evidence_not_in_source: "evidence not found in the cited source",
  date_unreadable: "date unreadable",
  date_not_in_evidence: "date not in its evidence",
  date_far_from_email: "date too far from the email's date",
  reference_not_in_evidence: "reference not in its evidence",
  payer_is_payee: "names the receiving company",
  payer_not_in_evidence: "name not in its evidence",
};

// Why the Resolution Expert was not called on a run whose total is off (timeline + stepper tooltip only).
export function resolutionSkipText(resolution) {
  if (resolution?.reason === "explained_by_multiple_payments") {
    const n = resolution.paymentCount;
    return `not needed - the gap to the stated total is explained by ${n ? `${n} ` : "several "}separate payments`;
  }
  return "not needed";
}

// Who owns a finding, shown on each finding in Run Details.
export const FINDING_KIND_LABELS = {
  extraction: "Extraction check",
  data_quality: "Data correction",
  payer: "ACH Assurance Expert",
};

const CATEGORY_LABELS = {
  no_expert_evidence: "No agent evidence",
};

export function categoryLabel(category) {
  if (!category || category === "other") return "";
  if (CATEGORY_LABELS[category]) return CATEGORY_LABELS[category];
  const text = category.replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

// What the agent found (category) plus its action when it has a concrete one, e.g.
// "Duplicate payment" or "Early payment discount · approve". Falls back to the decision label.
export function assuranceSummary(decision, category) {
  const meta = decisionMeta(decision);
  const found = categoryLabel(category);
  const action = DECISION_SHORT[decision];
  const label = found ? (action ? `${found} · ${action}` : found) : meta.label;
  return { label, color: meta.color, title: `Agent recommendation: ${meta.label}` };
}

// List-view pill for a run row: the primary finding's headline plus its action, and how many other
// findings there are, e.g. "7 negative lines ($2,874.91) · approve +2". Older runs (no headline)
// fall back to the category/decision summary.
export function assurancePill(row) {
  if (!row?.assuranceHeadline) return assuranceSummary(row?.assuranceDecision, row?.assuranceCategory);
  const meta = decisionMeta(row.assuranceDecision);
  const action = DECISION_SHORT[row.assuranceDecision];
  const more = Number(row.assuranceIssueCount) > 1 ? ` +${Number(row.assuranceIssueCount) - 1}` : "";
  return {
    label: `${row.assuranceHeadline}${action ? ` · ${action}` : ""}${more}`,
    color: meta.color,
    title: `${meta.label}${more ? ` (and ${Number(row.assuranceIssueCount) - 1} more finding(s) in Run Details)` : ""}`,
  };
}

export function duration(ms) {
  if (ms === null || ms === undefined || Number.isNaN(Number(ms))) return "—";
  const value = Number(ms);
  if (value < 1000) return `${value} ms`;
  if (value < 60000) return `${(value / 1000).toFixed(1)} s`;
  return `${Math.floor(value / 60000)}m ${Math.round((value % 60000) / 1000)}s`;
}

export function bytes(size) {
  if (size === null || size === undefined) return "—";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

export function todayIso() {
  return new Date().toISOString().slice(0, 10);
}

export function currentIsoWeek() {
  const now = new Date();
  const d = new Date(Date.UTC(now.getFullYear(), now.getMonth(), now.getDate()));
  const day = d.getUTCDay() || 7;
  d.setUTCDate(d.getUTCDate() + 4 - day);
  const yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
  const week = Math.ceil(((d - yearStart) / 86400000 + 1) / 7);
  return `${d.getUTCFullYear()}-W${String(week).padStart(2, "0")}`;
}
