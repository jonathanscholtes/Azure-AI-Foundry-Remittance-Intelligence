import { useNavigate } from "react-router-dom";
import {
  makeStyles,
  tokens,
  Card,
  CardHeader,
  Title3,
  Text,
  Badge,
  Table,
  TableHeader,
  TableRow,
  TableHeaderCell,
  TableBody,
  TableCell,
} from "@fluentui/react-components";
import KpiCards from "./KpiCards.jsx";
import { decisionMeta, duration, money, number, statusLabel, warningLabel } from "../format.js";

const STATUS_COLORS = {
  Completed: "#107C10",
  ReviewRequired: "#F7A600",
  Failed: "#C50F1F",
  DuplicateSkipped: "#8A8886",
};

const useStyles = makeStyles({
  page: { display: "flex", flexDirection: "column", gap: "16px" },
  // alignItems:start so a short chart (e.g. one status) keeps a short card instead of stretching to its neighbour.
  charts: { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: "16px", alignItems: "start" },
  chartCard: { padding: "16px" },
  clickable: { cursor: "pointer" },
});

const useBarStyles = makeStyles({
  list: { display: "flex", flexDirection: "column", gap: "11px", paddingTop: "10px" },
  row: { display: "grid", gridTemplateColumns: "minmax(0, 150px) 1fr auto", alignItems: "center", gap: "10px" },
  label: {
    fontSize: "12px",
    color: tokens.colorNeutralForeground2,
    textAlign: "right",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  },
  track: {
    height: "22px",
    borderRadius: "7px",
    backgroundColor: "#eff2f6",
    boxShadow: "inset 0 0 0 1px rgba(16,38,77,0.05)",
    overflow: "hidden",
  },
  fill: {
    height: "100%",
    borderRadius: "7px",
    minWidth: "8px",
    transition: "width 0.5s cubic-bezier(0.22, 1, 0.36, 1)",
    boxShadow: "0 1px 2px rgba(0,0,0,0.14)",
  },
  value: { fontSize: "13px", fontWeight: 700, color: tokens.colorNeutralForeground1, minWidth: "16px", textAlign: "right" },
});

// A compact "bar list": label · gradient-filled bar on a light track · value. Reads cleanly at any
// row count and gives even single-category charts visible structure.
function BarList({ data, color = "#0F6CBD", colorFor }) {
  const styles = useBarStyles();
  const max = Math.max(1, ...data.map((d) => d.value || 0));
  return (
    <div className={styles.list}>
      {data.map((d, index) => {
        const base = colorFor ? colorFor(d) : color;
        const pct = Math.max(6, Math.round(((d.value || 0) / max) * 100));
        return (
          <div className={styles.row} key={d.key || d.name || index}>
            <div className={styles.label} title={d.name}>
              {d.name}
            </div>
            <div className={styles.track}>
              <div
                className={styles.fill}
                style={{ width: `${pct}%`, background: `linear-gradient(180deg, rgba(255,255,255,0.32), rgba(0,0,0,0.10)), ${base}` }}
              />
            </div>
            <div className={styles.value}>{number(d.value)}</div>
          </div>
        );
      })}
    </div>
  );
}

export default function SummaryDashboard({ summary }) {
  const styles = useStyles();
  const navigate = useNavigate();
  if (!summary) return null;

  const kpis = [
    { label: "Remittances", value: number(summary.count) },
    { label: "Payers", value: number(summary.payerCount) },
    { label: "Processed", value: number(summary.completed), color: STATUS_COLORS.Completed },
    { label: "Needs review", value: number(summary.reviewRequired), color: STATUS_COLORS.ReviewRequired },
    { label: "Failed", value: number(summary.failed), color: STATUS_COLORS.Failed },
    { label: "Review rate", value: `${Math.round((summary.reviewRate || 0) * 100)}%` },
    { label: "Stated total", value: money(summary.totalStatedAmount) },
    { label: "Sum of lines", value: money(summary.totalProcessedAmount) },
    { label: "Payment lines", value: number(summary.totalPaymentLines) },
    { label: "Amount in question", value: money(summary.amountAtRisk), color: STATUS_COLORS.ReviewRequired },
    { label: "Avg time", value: duration(summary.avgDurationMs) },
    { label: "p95 time", value: duration(summary.p95DurationMs) },
  ];

  const statusData = Object.entries(summary.byStatus || {}).map(([name, value]) => ({ name: statusLabel(name), key: name, value }));
  const warningData = (summary.topWarnings || []).map((w) => ({ name: warningLabel(w.warning), value: w.count }));
  const decisionData = Object.entries(summary.assuranceDecisions || {}).map(([name, value]) => ({ name: decisionMeta(name).label, value }));
  const payers = summary.byPayer || [];

  return (
    <div className={styles.page}>
      <KpiCards items={kpis} />
      <div className={styles.charts}>
        <Card className={styles.chartCard}>
          <CardHeader header={<Title3>By status</Title3>} />
          {statusData.length ? (
            <BarList data={statusData} colorFor={(e) => STATUS_COLORS[e.key] || "#0F6CBD"} />
          ) : (
            <Text italic>No data for this period.</Text>
          )}
        </Card>

        <Card className={styles.chartCard}>
          <CardHeader header={<Title3>Top review reasons</Title3>} />
          {warningData.length ? (
            <BarList data={warningData} color="#F7A600" />
          ) : (
            <Text italic>No warnings in this period.</Text>
          )}
        </Card>

        <Card className={styles.chartCard}>
          <CardHeader header={<Title3>Agent recommendations</Title3>} />
          {decisionData.length ? (
            <BarList data={decisionData} color="#0F6CBD" />
          ) : (
            <Text italic>No agent assessments in this period (clean runs never invoke an agent).</Text>
          )}
        </Card>
      </div>

      <Card style={{ overflowX: "auto" }}>
        <CardHeader
          header={<Title3>By payer</Title3>}
          description={<Text>Remittances, payment lines and amounts per ACH payer for this period.</Text>}
        />
        <Table size="small" aria-label="Summary by payer">
          <TableHeader>
            <TableRow>
              <TableHeaderCell>Payer</TableHeaderCell>
              <TableHeaderCell>Remittances</TableHeaderCell>
              <TableHeaderCell>Payment lines</TableHeaderCell>
              <TableHeaderCell>Stated total</TableHeaderCell>
              <TableHeaderCell>Sum of lines</TableHeaderCell>
              <TableHeaderCell>Unreconciled</TableHeaderCell>
              <TableHeaderCell>Amount in question</TableHeaderCell>
              <TableHeaderCell>Processed</TableHeaderCell>
              <TableHeaderCell>Review</TableHeaderCell>
              <TableHeaderCell>Failed</TableHeaderCell>
              <TableHeaderCell>Latest remit date</TableHeaderCell>
            </TableRow>
          </TableHeader>
          <TableBody>
            {payers.map((p) => (
              <TableRow
                key={p.payerName}
                className={styles.clickable}
                onClick={() => navigate(`/runs?payer=${encodeURIComponent(p.payerName)}`)}
              >
                <TableCell>{p.payerName}</TableCell>
                <TableCell>{number(p.runs)}</TableCell>
                <TableCell>{number(p.lines)}</TableCell>
                <TableCell>{money(p.statedAmount)}</TableCell>
                <TableCell>{money(p.calculatedAmount)}</TableCell>
                <TableCell>{p.unreconciledAmount ? money(p.unreconciledAmount) : "—"}</TableCell>
                <TableCell>{p.amountAtRisk ? money(p.amountAtRisk) : "—"}</TableCell>
                <TableCell>{number(p.completed)}</TableCell>
                <TableCell>
                  {p.reviewRequired ? <Badge appearance="tint" color="warning">{p.reviewRequired}</Badge> : 0}
                </TableCell>
                <TableCell>{p.failed ? <Badge appearance="tint" color="danger">{p.failed}</Badge> : 0}</TableCell>
                <TableCell>{p.lastRemittanceDate || "—"}</TableCell>
              </TableRow>
            ))}
            {payers.length === 0 && (
              <TableRow>
                <TableCell colSpan={11}>
                  <Text italic>No remittances in this period.</Text>
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </Card>
    </div>
  );
}
