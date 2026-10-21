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
  Badge,
  Button,
  Text,
  Link,
} from "@fluentui/react-components";
import { ArrowDownload16Regular, ChevronRight16Regular, Eye16Regular, DocumentBulletList24Regular } from "@fluentui/react-icons";
import { dateTime, money, number, statusAppearance, statusLabel, isTerminal } from "../../format.js";
import { api } from "../../api.js";

// Terminal runs show their outcome; in-flight runs show the live pipeline state.
function statusMeta(run) {
  if (isTerminal(run.status)) return { label: statusLabel(run.status), color: statusAppearance(run.status) };
  return { label: statusLabel(run.currentState || run.status), color: "brand" };
}

const useStyles = makeStyles({
  card: { padding: "10px 12px", overflowX: "auto" },
  head: { display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "4px" },
  heading: { display: "flex", alignItems: "center", gap: "8px", color: "#0a2454" },
  title: { fontFamily: '"Segoe UI", sans-serif', fontSize: "16px", lineHeight: 1.2, fontWeight: 700, color: "#0a2454" },
  headingIcon: { color: "#0a3c78", display: "flex", fontSize: "18px" },
  link: { display: "flex", alignItems: "center", gap: "2px", fontSize: "11px" },
  table: { fontSize: "11px" },
  tableHeader: { backgroundColor: "#e8eff8", color: "#102c59" },
  file: { fontWeight: 600 },
  muted: { color: tokens.colorNeutralForeground3 },
  actions: { display: "flex", gap: "4px" },
  empty: { padding: "12px 0", color: tokens.colorNeutralForeground3 },
});

export default function RecentFilesTable({ items = [], loading }) {
  const styles = useStyles();
  const navigate = useNavigate();
  const rows = items.slice(0, 5);

  return (
    <Card className={styles.card}>
      <div className={styles.head}>
        <div className={styles.heading}>
          <DocumentBulletList24Regular className={styles.headingIcon} />
          <Title3 className={styles.title}>Recent Files</Title3>
        </div>
        <Link className={styles.link} onClick={() => navigate("/runs")} as="button">
          View All <ChevronRight16Regular />
        </Link>
      </div>
      <Table className={styles.table} size="small" aria-label="Recent files">
        <TableHeader className={styles.tableHeader}>
          <TableRow>
            <TableHeaderCell>File Name</TableHeaderCell>
            <TableHeaderCell>Payer</TableHeaderCell>
            <TableHeaderCell>Upload Date &amp; Time</TableHeaderCell>
            <TableHeaderCell>Lines</TableHeaderCell>
            <TableHeaderCell>Total</TableHeaderCell>
            <TableHeaderCell>Status</TableHeaderCell>
            <TableHeaderCell>Actions</TableHeaderCell>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((r) => {
            const meta = statusMeta(r);
            return (
              <TableRow key={`${r.processingId}-${r.runId}`}>
                <TableCell className={styles.file}>{r.sourceName || r.csvFileName || "—"}</TableCell>
                <TableCell>{r.payerName || "—"}</TableCell>
                <TableCell className={styles.muted}>
                  {dateTime(r.submittedAtUtc || r.createdAtUtc || r.updatedAtUtc)}
                </TableCell>
                <TableCell>{number(r.lineCount)}</TableCell>
                <TableCell>{money(r.paymentTotal ?? r.calculatedPaymentTotal, r.currency || "USD")}</TableCell>
                <TableCell>
                  <Badge appearance="filled" color={meta.color}>
                    {meta.label}
                  </Badge>
                </TableCell>
                <TableCell>
                  <div className={styles.actions}>
                    <Button
                      size="small"
                      appearance="subtle"
                      icon={<Eye16Regular />}
                      aria-label="View"
                      onClick={() => navigate(`/runs/${r.processingId}`)}
                    />
                    {r.csvFileName && (
                      <Button
                        size="small"
                        appearance="subtle"
                        icon={<ArrowDownload16Regular />}
                        aria-label="Download CSV"
                        as="a"
                        href={api.csvUrl(r.processingId)}
                      />
                    )}
                  </div>
                </TableCell>
              </TableRow>
            );
          })}
          {!loading && rows.length === 0 && (
            <TableRow>
              <TableCell colSpan={7}>
                <Text className={styles.empty} italic>
                  No files processed yet. Upload a remittance to get started.
                </Text>
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>
    </Card>
  );
}
