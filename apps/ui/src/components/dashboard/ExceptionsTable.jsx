import { useNavigate } from "react-router-dom";
import {
  makeStyles,
  tokens,
  Card,
  Title3,
  Table,
  TableHeader,
  TableRow,
  TableHeaderCell,
  TableBody,
  TableCell,
  Text,
  Badge,
  Link,
} from "@fluentui/react-components";
import { assurancePill, money, warningLabel } from "../../format.js";
import { ChevronRight16Regular, Warning24Regular } from "@fluentui/react-icons";

// Short, human-friendly reason a run needs review: what actually put it in review, never a note
// (runs recorded before reviewReasons existed fall back to their warnings).
function reasonFor(run) {
  if (run.status === "Failed") return warningLabel(run.errorCode) || "Processing failed";
  const blocking = run.reviewReasons ?? (run.warnings || []).filter((w) => !["payment_total_not_provided"].includes(w));
  if (blocking.length) return warningLabel(blocking[0]);
  if (run.isReconciled === false) return "Totals not reconciled";
  return "Needs review";
}

const useStyles = makeStyles({
  card: { padding: "12px 14px", height: "100%", boxSizing: "border-box", overflowX: "auto" },
  head: { display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "6px" },
  heading: { display: "flex", alignItems: "center", gap: "8px" },
  title: { fontFamily: '"Segoe UI", sans-serif', fontSize: "16px", lineHeight: 1.2, fontWeight: 700, color: "#0a2454" },
  headingIcon: { color: "#0a3c78", display: "flex" },
  link: { display: "flex", alignItems: "center", gap: "2px", fontSize: "11px" },
  table: { fontSize: "11px" },
  tableHeader: { backgroundColor: "#e8eff8", color: "#102c59" },
  id: { fontFamily: "monospace", fontSize: "12px" },
  empty: { padding: "12px 0", color: tokens.colorNeutralForeground3 },
  clickable: { cursor: "pointer" },
  // Keep the decision badge on one line - a wrapped Badge is a fixed-height pill, so the
  // second line gets clipped and looks struck through in the narrow Agent column.
  agentCell: { whiteSpace: "nowrap" },
  pill: { whiteSpace: "nowrap" },
});

export default function ExceptionsTable({ items = [] }) {
  const styles = useStyles();
  const navigate = useNavigate();
  const rows = items.slice(0, 5);

  return (
    <Card className={styles.card}>
      <div className={styles.head}>
        <div className={styles.heading}>
          <Warning24Regular className={styles.headingIcon} />
          <Title3 className={styles.title}>Needs review</Title3>
          <Badge appearance="tint" color="warning">Top {rows.length}</Badge>
        </div>
        <Link className={styles.link} onClick={() => navigate("/review")} as="button">
          View All <ChevronRight16Regular />
        </Link>
      </div>
      <Table className={styles.table} size="small" aria-label="Needs review" style={{ tableLayout: "auto" }}>
        <TableHeader className={styles.tableHeader}>
          <TableRow>
            <TableHeaderCell>Transaction ID</TableHeaderCell>
            <TableHeaderCell>Payer</TableHeaderCell>
            <TableHeaderCell>Amount</TableHeaderCell>
            <TableHeaderCell>Reason</TableHeaderCell>
            <TableHeaderCell>Agent</TableHeaderCell>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((r) => (
            <TableRow
              key={`${r.processingId}-${r.runId}`}
              className={styles.clickable}
              onClick={() => navigate(`/runs/${r.processingId}`)}
            >
              <TableCell className={styles.id}>{(r.processingId || "").replace("REM-", "").slice(0, 10) || "—"}</TableCell>
              <TableCell>{r.payerName || "—"}</TableCell>
              <TableCell>{money(r.paymentTotal ?? r.calculatedPaymentTotal, r.currency || "USD")}</TableCell>
              <TableCell>{reasonFor(r)}</TableCell>
              <TableCell className={styles.agentCell}>
                {r.assuranceDecision ? (
                  <Badge
                    appearance="tint"
                    color={assurancePill(r).color}
                    className={styles.pill}
                    title={assurancePill(r).title}
                  >
                    {assurancePill(r).label}
                  </Badge>
                ) : (
                  "—"
                )}
              </TableCell>
            </TableRow>
          ))}
          {rows.length === 0 && (
            <TableRow>
              <TableCell colSpan={5}>
                <Text className={styles.empty} italic>
                  Nothing to review — every processed file reconciled cleanly.
                </Text>
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>
    </Card>
  );
}
