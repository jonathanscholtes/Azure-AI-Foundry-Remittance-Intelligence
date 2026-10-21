import { useCallback, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  makeStyles,
  Card,
  CardHeader,
  Title3,
  Table,
  TableHeader,
  TableRow,
  TableHeaderCell,
  TableBody,
  TableCell,
  Badge,
  Button,
  Spinner,
  Dropdown,
  Option,
  Text,
  Input,
} from "@fluentui/react-components";
import { Search20Regular } from "@fluentui/react-icons";
import { api } from "../api.js";
import { usePolling } from "../usePolling.js";
import DeleteRunButton from "../components/DeleteRunButton.jsx";
import {
  assurancePill,
  dateTime,
  duration,
  isTerminal,
  money,
  number,
  statusAppearance,
  statusLabel,
  warningLabel,
} from "../format.js";

const STATUS_OPTIONS = [
  { value: "All", label: "All" },
  { value: "InFlight", label: "In flight" },
  { value: "Completed", label: "Processed" },
  { value: "ReviewRequired", label: "Needs review" },
  { value: "Failed", label: "Failed" },
  { value: "DuplicateSkipped", label: "Duplicate" },
];

// Decision pill colors. The Fluent Badge would not grow to fit long labels ("Request remittance
// detail"), so its text overflowed the box and the column never measured the real width. A plain
// inline-block pill sizes to its content, so the column grows to fit it. Keyed by decisionMeta color.
const DECISION_PILL = {
  success: { background: "#dff6dd", color: "#0e700e" },
  informative: { background: "#e5f1fb", color: "#0b4f8a" },
  danger: { background: "#fde7e9", color: "#a4262c" },
  warning: { background: "#fdf3d7", color: "#7a4d00" },
  brand: { background: "#ebf3fc", color: "#0b4f8a" },
};

const useStyles = makeStyles({
  page: { display: "flex", flexDirection: "column", gap: "16px" },
  toolbar: { display: "flex", gap: "12px", alignItems: "center", flexWrap: "wrap" },
  clickable: { cursor: "pointer" },
  // Keep the Reason/agent cell on one line; the self-sizing pill below drives the column width.
  reasonCell: { whiteSpace: "nowrap" },
  pill: {
    display: "inline-block",
    whiteSpace: "nowrap",
    padding: "2px 9px",
    borderRadius: "9999px",
    fontSize: "11px",
    fontWeight: 600,
    lineHeight: "1.4",
  },
  // The actions column holds a single icon; pin it narrow so it doesn't absorb the table's spare width.
  actionsCell: { width: "48px", textAlign: "center" },
  // A slightly smaller font so the eleven columns fit without overrunning each other.
  table: { fontSize: "12px", "& .fui-Badge": { whiteSpace: "nowrap" } },
  // Short values (dates, amounts, status, time) stay on one line; payer and file name wrap instead.
  nowrap: { whiteSpace: "nowrap" },
  wrap: { overflowWrap: "anywhere" },
});

// Processing runs / exceptions / audit: one searchable list over the ledger; rows open Run Details.
export default function AuditPage({ title = "Processing runs", description, defaultStatus = "All" }) {
  const styles = useStyles();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const payer = searchParams.get("payer") || "";
  const [status, setStatus] = useState(defaultStatus);
  const [search, setSearch] = useState("");
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const params = { limit: 500, q: search || undefined, payer: payer || undefined, status: status !== "All" ? status : undefined };
      const { items } = await api.list(params);
      setRows(items || []);
    } finally {
      setLoading(false);
    }
  }, [status, search, payer]);

  const anyInFlight = rows.some((r) => !isTerminal(r.status));
  usePolling(load, anyInFlight ? 4000 : 20000, true, [status, search, payer]);

  const statusText = STATUS_OPTIONS.find((o) => o.value === status)?.label || status;

  return (
    <div className={styles.page}>
      <Card>
        <CardHeader
          header={<Title3>{title}</Title3>}
          description={<Text>{description || "Every remittance message with its live status. Select a row for full run details and the audit timeline."}</Text>}
        />
        <div className={styles.toolbar}>
          <Text>Status</Text>
          <Dropdown value={statusText} selectedOptions={[status]} onOptionSelect={(_, d) => setStatus(d.optionValue)}>
            {STATUS_OPTIONS.map((s) => (
              <Option key={s.value} value={s.value}>
                {s.label}
              </Option>
            ))}
          </Dropdown>
          <Input
            contentBefore={<Search20Regular />}
            placeholder="Payer, file, reference or processing ID"
            value={search}
            onChange={(_, d) => setSearch(d.value)}
            style={{ minWidth: 280 }}
          />
          {payer && (
            <Badge appearance="tint" color="brand">
              Payer: {payer}{" "}
              <Button size="small" appearance="transparent" onClick={() => setSearchParams({})}>
                ✕
              </Button>
            </Badge>
          )}
          <Button onClick={load}>Refresh</Button>
          <Text size={200}>{number(rows.length)} run(s)</Text>
        </div>
      </Card>

      <Card style={{ overflowX: "auto" }}>
        {loading ? (
          <Spinner label="Loading runs..." />
        ) : (
          // Fluent's Table is table-layout: fixed (equal columns), which let the Reason/agent pill spill
          // into the next column; auto layout sizes each column to its content (the Card scrolls if needed).
          <Table size="small" aria-label="Processed remittances" className={styles.table} style={{ tableLayout: "auto" }}>
            <TableHeader>
              <TableRow>
                <TableHeaderCell>Payer</TableHeaderCell>
                <TableHeaderCell>File</TableHeaderCell>
                <TableHeaderCell>Remit date</TableHeaderCell>
                <TableHeaderCell>Lines</TableHeaderCell>
                <TableHeaderCell>Stated total</TableHeaderCell>
                <TableHeaderCell>Sum of lines</TableHeaderCell>
                <TableHeaderCell>Status</TableHeaderCell>
                <TableHeaderCell className={styles.reasonCell}>Reason / agent</TableHeaderCell>
                <TableHeaderCell>Submitted</TableHeaderCell>
                <TableHeaderCell>Time</TableHeaderCell>
                <TableHeaderCell className={styles.actionsCell}></TableHeaderCell>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((r) => (
                <TableRow key={`${r.processingId}-${r.runId}`} className={styles.clickable} onClick={() => navigate(`/runs/${r.processingId}`)}>
                  <TableCell className={styles.wrap}>{r.payerName || "—"}</TableCell>
                  <TableCell className={styles.wrap}>{r.sourceName || "—"}</TableCell>
                  <TableCell className={styles.nowrap}>{r.remittanceDate || "—"}</TableCell>
                  <TableCell className={styles.nowrap}>{r.lineCount ?? "—"}</TableCell>
                  <TableCell className={styles.nowrap}>{money(r.paymentTotal, r.currency || "USD")}</TableCell>
                  <TableCell className={styles.nowrap}>{money(r.calculatedPaymentTotal, r.currency || "USD")}</TableCell>
                  <TableCell className={styles.nowrap}>
                    <Badge appearance="filled" color={isTerminal(r.status) ? statusAppearance(r.status) : "brand"}>
                      {statusLabel(isTerminal(r.status) ? r.status : r.currentState || r.status)}
                    </Badge>
                  </TableCell>
                  <TableCell className={styles.reasonCell}>
                    {r.assuranceDecision ? (
                      <span
                        className={styles.pill}
                        style={DECISION_PILL[assurancePill(r).color] || DECISION_PILL.brand}
                        title={assurancePill(r).title}
                      >
                        {assurancePill(r).label}
                      </span>
                    ) : r.status === "Failed" ? (
                      warningLabel(r.errorCode)
                    ) : r.requiresReview ? (
                      warningLabel((r.warnings || [])[0])
                    ) : (
                      "—"
                    )}
                  </TableCell>
                  <TableCell className={styles.nowrap}>{dateTime(r.submittedAtUtc || r.createdAtUtc || r.updatedAtUtc)}</TableCell>
                  <TableCell className={styles.nowrap}>{duration(r.durationMs)}</TableCell>
                  <TableCell className={styles.actionsCell} onClick={(e) => e.stopPropagation()}>
                    <DeleteRunButton
                      processingId={r.processingId}
                      iconOnly
                      onDeleted={(pid) => setRows((prev) => prev.filter((row) => row.processingId !== pid))}
                    />
                  </TableCell>
                </TableRow>
              ))}
              {rows.length === 0 && (
                <TableRow>
                  <TableCell colSpan={11}>
                    <Text italic>No runs match. Upload a remittance on the File Upload page.</Text>
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        )}
      </Card>
    </div>
  );
}
