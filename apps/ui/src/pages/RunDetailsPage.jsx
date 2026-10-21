import { useCallback, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  makeStyles,
  tokens,
  Card,
  CardHeader,
  Title3,
  Subtitle2,
  Text,
  Badge,
  Button,
  Spinner,
  MessageBar,
  MessageBarBody,
  Table,
  TableHeader,
  TableRow,
  TableHeaderCell,
  TableBody,
  TableCell,
  Link,
  Divider,
} from "@fluentui/react-components";
import {
  ArrowDownload20Regular,
  ArrowLeft20Regular,
  ArrowClockwise20Regular,
  BotSparkle24Regular,
  DocumentSearch24Regular,
  Timeline24Regular,
} from "@fluentui/react-icons";
import { api } from "../api.js";
import { usePolling } from "../usePolling.js";
import {
  bytes,
  dateTime,
  decisionMeta,
  duration,
  FINDING_KIND_LABELS,
  DETAIL_FIELD_LABELS,
  DETAIL_PROBLEM_LABELS,
  RESOLUTION_REASON_LABELS,
  resolutionSkipText,
  SOURCE_ROLE_LABELS,
  isTerminal,
  money,
  number,
  statusAppearance,
  statusLabel,
  warningLabel,
} from "../format.js";
import WorkflowStepper from "../components/WorkflowStepper.jsx";
import PaymentLinesTable from "../components/PaymentLinesTable.jsx";
import ArtifactViewer from "../components/ArtifactViewer.jsx";
import DeleteRunButton from "../components/DeleteRunButton.jsx";
import UsageCard from "../components/UsageCard.jsx";

const useStyles = makeStyles({
  page: { display: "flex", flexDirection: "column", gap: "12px" },
  header: { display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: "12px", flexWrap: "wrap" },
  titleRow: { display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap" },
  muted: { color: tokens.colorNeutralForeground3 },
  mono: { fontFamily: "monospace", fontSize: "12px" },
  // Sources: content-sized columns, and long file names without spaces wrap inside their column.
  sourcesTable: { fontSize: "12px", tableLayout: "auto" },
  breakAnywhere: { overflowWrap: "anywhere", wordBreak: "break-word" },
  actions: { display: "flex", gap: "8px", flexWrap: "wrap" },
  kpis: { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: "12px", marginTop: "8px" },
  field: { display: "flex", flexDirection: "column", gap: "2px" },
  two: { display: "grid", gridTemplateColumns: "1.2fr 1fr", gap: "12px", "@media (max-width: 1000px)": { gridTemplateColumns: "1fr" } },
  section: { display: "flex", flexDirection: "column", gap: "8px" },
  cardHead: { display: "flex", alignItems: "center", gap: "8px" },
  icon: { color: "#0a3c78", display: "flex" },
  citation: {
    borderLeft: `3px solid ${tokens.colorBrandStroke1}`,
    padding: "4px 10px",
    backgroundColor: tokens.colorNeutralBackground2,
    borderRadius: tokens.borderRadiusSmall,
  },
  finding: {
    display: "flex",
    flexDirection: "column",
    gap: "6px",
    padding: "10px 12px",
    border: `1px solid ${tokens.colorNeutralStroke2}`,
    borderRadius: tokens.borderRadiusMedium,
  },
  findingPrimary: { borderLeft: `4px solid ${tokens.colorBrandStroke1}` },
  timeline: { display: "flex", flexDirection: "column" },
  event: {
    display: "grid",
    gridTemplateColumns: "150px 170px 90px 1fr",
    gap: "10px",
    padding: "6px 0",
    borderBottom: `1px solid ${tokens.colorNeutralStroke2}`,
    alignItems: "baseline",
    "@media (max-width: 700px)": { gridTemplateColumns: "1fr" },
  },
});

function Field({ label, value, tone }) {
  const styles = useStyles();
  return (
    <div className={styles.field}>
      <Text size={200} className={styles.muted}>
        {label}
      </Text>
      <Text size={400} weight="semibold" style={tone ? { color: tone } : undefined}>
        {value ?? "—"}
      </Text>
    </div>
  );
}

function eventSummary(event) {
  const d = event.details || {};
  const parts = [];
  if (d.stream) parts.push(`stream ${d.stream}`);
  if (d.queueLatencyMs != null) parts.push(`queue wait ${duration(d.queueLatencyMs)}`);
  if (d.attachmentCount != null) parts.push(`${d.attachmentCount} attachment(s)`);
  if (d.eligibleCount != null) parts.push(`${d.eligibleCount} of ${d.candidateCount} candidate(s) eligible`);
  if (d.strategy) parts.push(`primary: ${d.strategy}`);
  if (Array.isArray(d.candidates)) {
    parts.push(d.candidates.map((c) => `${c.candidateId} ${c.strategy || ""} ${c.lineCount ?? 0} line(s)`).join("; "));
  }
  if (d.throttleRetries) parts.push(`${d.throttleRetries} throttle retr${d.throttleRetries === 1 ? "y" : "ies"}`);
  if (d.paymentLineCount != null) parts.push(`${d.paymentLineCount} line(s)`);
  if (d.isReconciled != null) parts.push(d.isReconciled ? "reconciled" : "not reconciled");
  if (d.classification?.status === "completed") {
    parts.push(
      `Decision-1: ${d.classification.sourcesClassified} source(s) classified, ${d.classification.linesExcluded} line(s) excluded` +
        (d.classification.threadKind ? `, thread ${d.classification.threadKind.replace(/_/g, " ")}` : "")
    );
  }
  if (d.resolution?.status === "skipped") {
    parts.push(`Resolution Expert: ${resolutionSkipText(d.resolution)}`);
  } else if (d.resolution && !["not_needed", "not_configured"].includes(d.resolution.status)) {
    const fields = (list) => (list || []).map((f) => (DETAIL_FIELD_LABELS[f] || f).toLowerCase()).join(", ");
    parts.push(
      `Resolution Expert: ${RESOLUTION_REASON_LABELS[d.resolution.reason] || d.resolution.status}` +
        (d.resolution.detailsApplied?.length ? `; applied ${fields(d.resolution.detailsApplied)}` : "") +
        (d.resolution.detailsRecommended?.length ? `; recommends ${fields(d.resolution.detailsRecommended)}` : "")
    );
  }
  if (d.assuranceHeadline) parts.push(`${d.issueCount ?? 1} finding(s); primary: ${d.assuranceHeadline}`);
  if (d.assuranceDecision) parts.push(`agent: ${decisionMeta(d.assuranceDecision).label}`);
  if (d.csvPublished != null) parts.push(d.csvPublished ? "CSV published" : "no CSV");
  if (d.reason) parts.push(warningLabel(d.reason));
  if (d.errorType) parts.push(`error ${d.errorType}`);
  return parts.join(" · ");
}

// The ACH Assurance Expert's status in plain words ("skipped" = no payer question to ask it).
const EXPERT_STATUS_LABELS = { completed: "answered", skipped: "not needed (no payer question)", failed: "failed" };

// The ACH Resolution Expert's attempt on a run that failed verification: what triggered it, what it
// proposed, whether the gate applied it, and which agent version answered (audit).
function ResolutionBlock({ resolution, currency }) {
  const styles = useStyles();
  if (!resolution || ["not_needed", "not_configured"].includes(resolution.status)) return null;
  const applied = resolution.status === "accepted";
  const confirmed = resolution.status === "confirmed";
  const recommended = resolution.status === "recommended";
  // Details-only runs carry no line proposal; older runs have no lineProblem flag (lines were the only job).
  const proposal = resolution.lineProblem === false ? [] : resolution.proposal || [];
  const details = resolution.details || {};
  const detailRows = [
    ...Object.entries(details.applied || {}).map(([field, d]) => ({ field, d, outcome: "applied" })),
    ...Object.entries(details.recommended || {}).map(([field, d]) => ({ field, d, outcome: "recommended - confirm" })),
    ...(details.rejected || []).map((d) => ({ field: d.field, d, outcome: `not used: ${DETAIL_PROBLEM_LABELS[d.problem] || d.problem}` })),
  ];
  return (
    <div className={`${styles.finding} ${applied ? styles.findingPrimary : ""}`}>
      <div className={styles.titleRow}>
        <Badge
          appearance="filled"
          color={applied || confirmed ? "success" : recommended ? "informative" : resolution.status === "failed" ? "danger" : "warning"}
        >
          {applied ? "Applied" : confirmed ? "Lines confirmed" : recommended ? "Recommendation" : resolution.status === "failed" ? "Failed" : "Not applied"}
        </Badge>
        <Text weight="semibold">ACH Resolution Expert</Text>
        <Badge appearance="outline" size="small">
          {[resolution.model, resolution.agentVersion ? `v${resolution.agentVersion}` : null].filter(Boolean).join(" · ") || "reasoning agent"}
        </Badge>
        {resolution.confidence != null && <Text className={styles.muted}>confidence {Math.round(resolution.confidence * 100)}%</Text>}
      </div>
      <Text size={200} className={styles.muted}>
        Triggered by: {(resolution.triggers || []).map((t) => warningLabel(t)).join("; ") || "—"}
        {resolution.toolCalls?.length ? ` · tools used: ${resolution.toolCalls.join(", ")}` : ""}
      </Text>
      <Text size={300}>{RESOLUTION_REASON_LABELS[resolution.reason] || resolution.reason || resolution.error}</Text>
      {resolution.errorDetail && (
        <Text size={200} className={styles.mono}>
          {resolution.errorDetail}
        </Text>
      )}
      {resolution.explanation && (
        <Text size={300}>
          <strong>Its reasoning:</strong> {resolution.explanation}
        </Text>
      )}
      {detailRows.map(({ field, d, outcome }) => (
        <Text key={`${field}-${outcome}`} size={200}>
          <strong>{DETAIL_FIELD_LABELS[field] || field}:</strong> {d.value || "—"} <span className={styles.muted}>({outcome})</span>
          {d.reason ? ` - ${d.reason}` : ""}
          {d.evidence && (
            <span className={styles.mono}>
              {" "}
              “{d.evidence}” [{d.sourceId}]
            </span>
          )}
        </Text>
      ))}
      {proposal.length > 0 && !confirmed && (
        <Text size={200} className={styles.mono}>
          {applied ? "Lines applied: " : "Proposed (not applied): "}
          {proposal.map((l) => `${l.pro || "(no PRO)"} ${money(l.amount, currency)} [${l.sourceId}]`).join(" · ")}
        </Text>
      )}
    </div>
  );
}

// One triaged issue: who owns it, what to do, and the exact lines involved.
function FindingCard({ finding, primary, currency }) {
  const styles = useStyles();
  const meta = decisionMeta(finding.decision);
  const lines = finding.lines || [];
  return (
    <div className={primary ? `${styles.finding} ${styles.findingPrimary}` : styles.finding}>
      <div className={styles.titleRow}>
        <Badge appearance="filled" color={meta.color}>
          {meta.label}
        </Badge>
        <Text weight="semibold">{finding.headline || finding.title}</Text>
        <Badge appearance="outline" size="small">
          {FINDING_KIND_LABELS[finding.kind] || finding.kind}
        </Badge>
        {finding.amountAtRisk != null && <Text className={styles.muted}>amount in question {money(finding.amountAtRisk, currency)}</Text>}
        {finding.confidence != null && <Text className={styles.muted}>confidence {Math.round(finding.confidence * 100)}%</Text>}
      </div>
      {finding.rationale && <Text size={300}>{finding.rationale}</Text>}
      {finding.recommendedAction && (
        <MessageBar intent={finding.kind === "extraction" ? "warning" : "info"}>
          <MessageBarBody>
            <strong>Recommended action:</strong> {finding.recommendedAction}
          </MessageBarBody>
        </MessageBar>
      )}
      {lines.length > 0 && (
        <Text size={200} className={styles.mono}>
          {lines
            .map((l) => `${l.rawProInvoiceNumber || l.proInvoiceNumber || "(no PRO)"} ${money(l.amountPaid, currency)}`)
            .join(" · ")}
          {finding.lineCount > lines.length ? ` · +${finding.lineCount - lines.length} more` : ""}
        </Text>
      )}
      {(finding.citations || []).map((c, i) => (
        <div key={i} className={styles.citation}>
          <Text weight="semibold" size={200}>
            {c.policyId ? `${c.policyId} - ` : ""}
            {c.title || "Policy section"}
          </Text>
          {c.excerpt && (
            <Text block size={200}>
              "{c.excerpt}"
            </Text>
          )}
        </div>
      ))}
    </div>
  );
}

function AssurancePanel({ assurance, status, currency }) {
  const styles = useStyles();
  if (!assurance) {
    return (
      <Text className={styles.muted}>
        {status === "Completed"
          ? "Not needed - the remittance reconciled cleanly, so no agent was invoked."
          : isTerminal(status)
          ? "No agent assessment recorded for this run."
          : "Agents run only if validation flags this remittance for review."}
      </Text>
    );
  }
  if (Array.isArray(assurance.findings) && assurance.findings.length) {
    return (
      <div className={styles.section}>
        <Text className={styles.muted}>
          {assurance.findings.length} finding{assurance.findings.length === 1 ? "" : "s"}, most important first.
          {" "}ACH Assurance Expert:{" "}
          {(assurance.experts || []).map((e) => EXPERT_STATUS_LABELS[e.status] || e.status).join(", ") || "—"}
        </Text>
        {assurance.findings.map((finding, index) => (
          <FindingCard key={finding.issueId || index} finding={finding} primary={index === 0} currency={currency} />
        ))}
        <Text size={100} className={styles.muted}>
          Advisory only - the agent recommendation never changes extracted amounts or publishes a CSV. An analyst dispositions every review.
        </Text>
      </div>
    );
  }
  const meta = decisionMeta(assurance.decision);
  return (
    <div className={styles.section}>
      <div className={styles.titleRow}>
        <Badge appearance="filled" color={meta.color} size="large">
          {meta.label}
        </Badge>
        {assurance.category && <Badge appearance="outline">{assurance.category.replace(/_/g, " ")}</Badge>}
        {assurance.confidence != null && <Text className={styles.muted}>confidence {Math.round(assurance.confidence * 100)}%</Text>}
      </div>
      <div className={styles.kpis}>
        <Field label="Amount in question" value={money(assurance.amountAtRisk, currency)} />
        <Field label="Experts" value={(assurance.experts || []).map((e) => `${e.label || e.expert}: ${e.status}`).join(", ")} />
      </div>
      {assurance.rationale && <Text>{assurance.rationale}</Text>}
      {assurance.recommendedAction && (
        <MessageBar intent="info">
          <MessageBarBody>
            <strong>Recommended action:</strong> {assurance.recommendedAction}
          </MessageBarBody>
        </MessageBar>
      )}
      {(assurance.gateReasons || []).length > 0 && (
        <Text size={200} className={styles.muted}>
          Sent to agents because: {assurance.gateReasons.join("; ")}
        </Text>
      )}
      {(assurance.citations || []).length > 0 && (
        <>
          <Subtitle2>Policy evidence</Subtitle2>
          {assurance.citations.map((c, i) => (
            <div key={i} className={styles.citation}>
              <Text weight="semibold">
                {c.policyId ? `${c.policyId} - ` : ""}
                {c.title || "Policy section"}
              </Text>
              {c.excerpt && (
                <Text block size={200}>
                  "{c.excerpt}"
                </Text>
              )}
            </div>
          ))}
        </>
      )}
      <Text size={100} className={styles.muted}>
        Advisory only - the agent recommendation never changes extracted amounts or publishes a CSV. An analyst dispositions every review.
      </Text>
    </div>
  );
}

export default function RunDetailsPage() {
  const styles = useStyles();
  const navigate = useNavigate();
  const { processingId } = useParams();
  const [run, setRun] = useState(null);
  const [files, setFiles] = useState(null);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [reprocessing, setReprocessing] = useState(false);
  // After Reprocess, keep polling until a run other than this one shows up in the ledger.
  const [awaitingRunAfter, setAwaitingRunAfter] = useState(null);
  // The artifact currently open in the in-app viewer drawer (null = closed).
  const [viewerFile, setViewerFile] = useState(null);

  const load = useCallback(async () => {
    try {
      const detail = await api.get(processingId);
      if (awaitingRunAfter && detail.runId === awaitingRunAfter) return;
      if (awaitingRunAfter) {
        setAwaitingRunAfter(null);
        setNotice(null);
      }
      setRun(detail);
      setError(null);
      if (isTerminal(detail.status)) {
        setFiles(await api.files(processingId));
      }
    } catch (e) {
      setError(e.message);
    }
  }, [processingId, awaitingRunAfter]);

  // Live while the run is in flight; stops once it reaches a terminal state.
  usePolling(load, 2000, !run || !isTerminal(run.status) || Boolean(awaitingRunAfter), [processingId, awaitingRunAfter]);

  const reprocess = async () => {
    setReprocessing(true);
    try {
      await api.reprocess(processingId);
      setNotice("Re-queued on Service Bus. The new run will appear here as it progresses.");
      setAwaitingRunAfter(run.runId);
      setFiles(null);
    } catch (e) {
      setNotice(`Reprocess failed: ${e.message}`);
    } finally {
      setReprocessing(false);
    }
  };

  if (error && !run) {
    return (
      <MessageBar intent={error.includes("not found") ? "warning" : "error"}>
        <MessageBarBody>
          {error.includes("not found") ? "This remittance has not been recorded yet - it may still be queued." : error}
        </MessageBarBody>
      </MessageBar>
    );
  }
  if (!run) return <Spinner label="Loading run..." />;

  const meta = run.remittanceMetadata || {};
  const quality = run.quality || {};
  const currency = meta.currency || run.currency || "USD";
  const warnings = quality.warnings || run.warnings || [];
  // Blocking reasons vs. informational notes (decided by the backend; older runs show everything as reasons).
  const reviewReasons = quality.reviewReasons || warnings;
  const notes = quality.reviewReasons ? quality.notes || [] : [];
  const difference = quality.totalDifference ?? run.totalDifference;
  const reconciled = quality.isReconciled ?? run.isReconciled;
  const events = run.events || [];
  const candidateResults = Object.fromEntries((run.candidateResults || []).map((c) => [c.candidateId, c]));
  const roles = run.sourceClassification?.status === "completed" ? run.sourceClassification.roles : null;

  return (
    <div className={styles.page}>
      <Card>
        <div className={styles.header}>
          <div>
            <Button appearance="subtle" size="small" icon={<ArrowLeft20Regular />} onClick={() => navigate(-1)}>
              Back
            </Button>
            <div className={styles.titleRow}>
              <Title3>{meta.payerName || run.payerName || run.sourceName || "Remittance"}</Title3>
              <Badge appearance="filled" color={isTerminal(run.status) ? statusAppearance(run.status) : "brand"}>
                {statusLabel(isTerminal(run.status) ? run.status : run.currentState || run.status)}
              </Badge>
              {run.stream && <Badge appearance="outline">{run.stream}</Badge>}
            </div>
            <Text size={200} className={styles.mono}>
              {run.processingId} · run {run.runId} · correlation {run.correlationId || "—"}
            </Text>
            <Text block size={200} className={styles.muted}>
              {run.sourceName} · {run.message?.subject || run.subject || "—"} · from {run.message?.senderEmail || run.senderEmail || "—"}
            </Text>
          </div>
          <div className={styles.actions}>
            {run.csvFileName && (
              <Button appearance="primary" icon={<ArrowDownload20Regular />} as="a" href={api.csvUrl(run.processingId)}>
                {run.csvFileName}
              </Button>
            )}
            {isTerminal(run.status) && run.status !== "Completed" && (
              <Button icon={<ArrowClockwise20Regular />} onClick={reprocess} disabled={reprocessing}>
                Reprocess
              </Button>
            )}
            <DeleteRunButton processingId={run.processingId} onDeleted={() => navigate("/runs")} />
          </div>
        </div>
        {notice && (
          <MessageBar intent="info" style={{ marginTop: 8 }}>
            <MessageBarBody>{notice}</MessageBarBody>
          </MessageBar>
        )}
        <WorkflowStepper run={run} events={events} />
      </Card>

      <Card>
        <CardHeader header={<Title3>Remittance summary</Title3>} />
        <div className={styles.kpis}>
          <Field label="Payer" value={meta.payerName || run.payerName} />
          <Field label="Remittance date" value={meta.remittanceDate || run.remittanceDate} />
          <Field label="Payment reference" value={meta.paymentReference || run.paymentReference} />
          <Field label="Payment lines" value={number(quality.lineCount ?? run.lineCount)} />
          <Field label="Stated total" value={money(meta.paymentTotal ?? run.paymentTotal, currency)} />
          <Field label="Sum of lines" value={money(quality.calculatedPaymentTotal ?? run.calculatedPaymentTotal, currency)} />
          <Field
            label="Difference"
            value={money(difference, currency)}
            tone={difference && Math.abs(difference) > 0.01 ? tokens.colorPaletteRedForeground1 : undefined}
          />
          <Field label="Reconciled" value={reconciled == null ? "—" : reconciled ? "Yes" : "No"} />
          <Field label="End-to-end time" value={duration(run.durationMs)} />
          <Field label="Queue wait" value={duration(run.queueLatencyMs)} />
        </div>
        {run.status === "Failed" && (
          <MessageBar intent="error" style={{ marginTop: 10 }}>
            <MessageBarBody>
              Processing failed at the {run.stage || "unknown"} stage ({warningLabel(run.errorCode) || run.errorCode}). The
              source message is preserved; use Reprocess after resolving the cause.
            </MessageBarBody>
          </MessageBar>
        )}
        {reviewReasons.length > 0 && (
          <div className={styles.section} style={{ marginTop: 10 }}>
            <Subtitle2>Review reasons</Subtitle2>
            {reviewReasons.map((w) => (
              <MessageBar key={w} intent="warning">
                <MessageBarBody>
                  {warningLabel(w)} <Text size={100} className={styles.mono}>({w})</Text>
                </MessageBarBody>
              </MessageBar>
            ))}
          </div>
        )}
        {notes.length > 0 && (
          <div className={styles.section} style={{ marginTop: 10 }}>
            <Subtitle2>Notes</Subtitle2>
            {notes.map((w) => (
              <Text key={w} size={200} className={styles.muted}>
                • {warningLabel(w)} <Text size={100} className={styles.mono}>({w})</Text>
              </Text>
            ))}
          </div>
        )}
      </Card>

      <div className={styles.two}>
        <Card>
          <div className={styles.cardHead}>
            <BotSparkle24Regular className={styles.icon} />
            <Title3>Agent assurance</Title3>
          </div>
          <ResolutionBlock resolution={run.resolution} currency={currency} />
          <AssurancePanel assurance={run.assurance} status={run.status} currency={currency} />
        </Card>

        <Card>
          <div className={styles.cardHead}>
            <DocumentSearch24Regular className={styles.icon} />
            <Title3>Sources</Title3>
          </div>
          <Table size="small" aria-label="Source candidates" className={styles.sourcesTable} style={{ tableLayout: "auto" }}>
            <TableHeader>
              <TableRow>
                <TableHeaderCell>Source</TableHeaderCell>
                <TableHeaderCell>Classification</TableHeaderCell>
                {roles && <TableHeaderCell>Decision-1</TableHeaderCell>}
                <TableHeaderCell>Strategy</TableHeaderCell>
                <TableHeaderCell>Lines</TableHeaderCell>
              </TableRow>
            </TableHeader>
            <TableBody>
              {(run.sourceCandidates || []).map((c) => {
                const result = candidateResults[c.candidateId] || {};
                const role = roles?.[c.candidateId];
                return (
                  <TableRow key={c.candidateId}>
                    <TableCell className={styles.breakAnywhere}>
                      <Text weight="semibold" size={200} className={styles.breakAnywhere}>{c.name}</Text>
                      <Text block size={100} className={styles.muted}>
                        {c.candidateId} · {bytes(c.size)} · score {c.score}
                        {c.isInline ? " · inline" : ""}
                        {c.embeddedIn ? ` · in ${c.embeddedIn}` : ""}
                      </Text>
                    </TableCell>
                    <TableCell>{(c.classification || "").replace(/_/g, " ")}</TableCell>
                    {roles && (
                      <TableCell title={role ? `Microsoft-Decision-1: ${role.role} (${Math.round((role.probability || 0) * 100)}%)` : undefined}>
                        {role ? `${SOURCE_ROLE_LABELS[role.role] || role.role} · ${Math.round((role.probability || 0) * 100)}%` : "—"}
                      </TableCell>
                    )}
                    <TableCell>{result.strategy ? `${result.strategy.replace(/_/g, " ")} (${duration(result.durationMs)})` : "—"}</TableCell>
                    <TableCell>{result.lineCount ?? "—"}</TableCell>
                  </TableRow>
                );
              })}
              {!(run.sourceCandidates || []).length && (
                <TableRow>
                  <TableCell colSpan={roles ? 5 : 4}>
                    <Text italic className={styles.muted}>
                      {isTerminal(run.status) ? "No source manifest recorded." : "Sources appear once intake completes."}
                    </Text>
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </Card>
      </div>

      <Card>
        <CardHeader header={<Title3>Payment lines</Title3>} description={<Text>{number(quality.lineCount ?? run.lineCount)} line(s)</Text>} />
        <PaymentLinesTable lines={run.paymentLines} excluded={run.sourceClassification?.excluded} currency={currency} />
      </Card>

      {isTerminal(run.status) && <UsageCard usage={run.usage} />}

      <div className={styles.two}>
        <Card>
          <div className={styles.cardHead}>
            <Timeline24Regular className={styles.icon} />
            <Title3>Processing timeline</Title3>
            {!isTerminal(run.status) && <Spinner size="tiny" />}
          </div>
          <div className={styles.timeline}>
            {events.length === 0 && <Text italic className={styles.muted}>Waiting for the orchestration to pick up the message...</Text>}
            {events.map((e) => (
              <div key={`${e.runId}-${e.seq}`} className={styles.event}>
                <Text size={200} className={styles.muted}>
                  {dateTime(e.eventTimeUtc)}
                </Text>
                <Text weight="semibold">{statusLabel(e.newState)}</Text>
                <Text size={200}>{duration(e.durationMs ?? e.details?.durationMs)}</Text>
                <Text size={200}>{eventSummary(e)}</Text>
              </div>
            ))}
          </div>
        </Card>

        <Card>
          <CardHeader header={<Title3>Files &amp; artifacts</Title3>} description={<Text>Original message, attachments, analysis and outputs (Blob Storage).</Text>} />
          {!files ? (
            <Text italic className={styles.muted}>
              {isTerminal(run.status) ? "Loading..." : "Available when the run completes."}
            </Text>
          ) : (
            <Table size="small" aria-label="Artifacts">
              <TableHeader>
                <TableRow>
                  <TableHeaderCell>Container</TableHeaderCell>
                  <TableHeaderCell>File</TableHeaderCell>
                  <TableHeaderCell>Size</TableHeaderCell>
                </TableRow>
              </TableHeader>
              <TableBody>
                {files.files.map((f) => (
                  <TableRow key={`${f.container}/${f.path}`}>
                    <TableCell>{f.container}</TableCell>
                    <TableCell>
                      <Link as="button" onClick={() => setViewerFile({ ...f, name: f.path.split("/").slice(1).join("/") || f.name })}>
                        {f.path.split("/").slice(1).join("/") || f.name}
                      </Link>
                    </TableCell>
                    <TableCell>{bytes(f.size)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
          <Divider style={{ margin: "8px 0" }} />
          <Text size={100} className={styles.muted}>
            Durable instance {run.durableInstanceId || run.processingId} · source SHA-256 {(run.sourceSha256 || "").slice(0, 16)}…
          </Text>
        </Card>
      </div>

      <ArtifactViewer processingId={run.processingId} file={viewerFile} onClose={() => setViewerFile(null)} />
    </div>
  );
}
