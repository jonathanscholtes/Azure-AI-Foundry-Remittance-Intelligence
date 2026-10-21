import {
  makeStyles,
  tokens,
  Card,
  CardHeader,
  Badge,
  Text,
  Title3,
  Button,
  MessageBar,
  MessageBarBody,
} from "@fluentui/react-components";
import { ArrowDownload20Regular } from "@fluentui/react-icons";
import { money, number, statusAppearance } from "../format.js";
import { api } from "../api.js";

const useStyles = makeStyles({
  grid: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))",
    gap: "16px",
    marginTop: "12px",
  },
  field: { display: "flex", flexDirection: "column", gap: "2px" },
  warnings: { marginTop: "12px", display: "flex", flexDirection: "column", gap: "8px" },
});

function Field({ label, value }) {
  const styles = useStyles();
  return (
    <div className={styles.field}>
      <Text size={200} style={{ color: tokens.colorNeutralForeground3 }}>
        {label}
      </Text>
      <Text size={400} weight="semibold">
        {value ?? "—"}
      </Text>
    </div>
  );
}

export default function SummaryCard({ run }) {
  const styles = useStyles();
  const meta = run.remittanceMetadata || {};
  const quality = run.quality || {};
  const warnings = quality.warnings || run.warnings || [];

  return (
    <Card>
      <CardHeader
        header={<Title3>{meta.payerName || run.payerName || "Unknown payer"}</Title3>}
        description={<Badge appearance="filled" color={statusAppearance(run.status)}>{run.status}</Badge>}
        action={
          run.csvFileName ? (
            <Button
              appearance="primary"
              icon={<ArrowDownload20Regular />}
              as="a"
              href={api.csvUrl(run.processingId)}
            >
              Download CSV
            </Button>
          ) : null
        }
      />
      <div className={styles.grid}>
        <Field label="Remittance date" value={meta.remittanceDate || run.remittanceDate} />
        <Field label="Payment total" value={money(meta.paymentTotal ?? run.paymentTotal, meta.currency || run.currency)} />
        <Field label="Calculated total" value={money(quality.calculatedPaymentTotal ?? run.calculatedPaymentTotal, meta.currency || run.currency)} />
        <Field
          label="Reconciled"
          value={
            (quality.isReconciled ?? run.isReconciled) === null || (quality.isReconciled ?? run.isReconciled) === undefined
              ? "—"
              : (quality.isReconciled ?? run.isReconciled)
              ? "Yes"
              : "No"
          }
        />
        <Field label="Payment lines" value={number(quality.lineCount ?? run.lineCount)} />
        <Field label="Currency" value={meta.currency || run.currency} />
      </div>
      {warnings.length > 0 && (
        <div className={styles.warnings}>
          {warnings.map((w) => (
            <MessageBar key={w} intent={run.status === "Failed" ? "error" : "warning"}>
              <MessageBarBody>{w}</MessageBarBody>
            </MessageBar>
          ))}
        </div>
      )}
    </Card>
  );
}
