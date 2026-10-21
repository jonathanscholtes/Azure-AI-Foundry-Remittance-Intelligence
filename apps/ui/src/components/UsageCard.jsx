import {
  makeStyles,
  tokens,
  Card,
  CardHeader,
  Title3,
  Text,
  Table,
  TableHeader,
  TableRow,
  TableHeaderCell,
  TableBody,
  TableCell,
} from "@fluentui/react-components";
import { number } from "../format.js";

const AGENT_LABELS = {
  "ach-extraction-expert": "ACH Extraction Expert",
  "ach-resolution-expert": "ACH Resolution Expert",
  "ach-assurance-expert": "ACH Assurance Expert",
};

const useStyles = makeStyles({
  muted: { color: tokens.colorNeutralForeground3 },
  total: { display: "flex", alignItems: "baseline", gap: "10px", flexWrap: "wrap", marginBottom: "6px" },
  table: { fontSize: "12px", tableLayout: "auto" },
  disclaimer: { color: tokens.colorNeutralForeground3, marginBottom: "8px" },
});

// AI costs per run are fractions of a cent to a few cents: show enough decimals to be meaningful.
function cost(value, currency = "USD") {
  if (value === null || value === undefined) return "—";
  const digits = value !== 0 && Math.abs(value) < 0.01 ? 4 : 2;
  return new Intl.NumberFormat("en-US", { style: "currency", currency, minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value);
}

function tokenText({ inputTokens = 0, cachedInputTokens = 0, outputTokens = 0, reasoningTokens = 0 }) {
  const input = `${number(inputTokens)} in${cachedInputTokens ? ` (${number(cachedInputTokens)} cached)` : ""}`;
  const output = `${number(outputTokens)} out${reasoningTokens ? ` (${number(reasoningTokens)} reasoning)` : ""}`;
  return `${input} · ${output}`;
}

function cuText(cu) {
  const pages = Object.entries(cu.pages || {})
    .map(([tier, count]) => `${number(count)} ${tier}`)
    .join(", ");
  const byKind = { input: 0, cached: 0, output: 0 };
  Object.entries(cu.tokens || {}).forEach(([key, count]) => {
    if (key.endsWith("-cached-input")) byKind.cached += count;
    else if (key.endsWith("-input")) byKind.input += count;
    else if (key.endsWith("-output")) byKind.output += count;
  });
  return [
    pages && `pages: ${pages}`,
    cu.contextualizationTokens && `contextualization ${number(cu.contextualizationTokens)}`,
    tokenText({ inputTokens: byKind.input, cachedInputTokens: byKind.cached, outputTokens: byKind.output }),
  ]
    .filter(Boolean)
    .join(" · ");
}

// Cost and usage of one run: raw counts per AI component and the estimate from the
// price table. Runs processed before usage was recorded have none.
export default function UsageCard({ usage }) {
  const styles = useStyles();
  const estimate = usage?.estimate || {};
  const byComponent = estimate.byComponent || {};
  const currency = estimate.currency || "USD";
  const rows = [];
  if (usage?.contentUnderstanding) {
    rows.push({ key: "cu", label: "Content Understanding", calls: usage.contentUnderstanding.calls, detail: cuText(usage.contentUnderstanding), cost: byComponent.contentUnderstanding });
  }
  Object.entries(usage?.agents || {}).forEach(([agent, entry]) => {
    rows.push({ key: agent, label: AGENT_LABELS[agent] || agent, model: entry.model, calls: entry.calls, detail: tokenText(entry), cost: byComponent[agent] });
  });
  if (usage?.decision) {
    rows.push({ key: "decision", label: "Microsoft-Decision-1", calls: usage.decision.calls, detail: tokenText(usage.decision), cost: byComponent.decision });
  }

  return (
    <Card>
      <CardHeader header={<Title3>Cost and usage</Title3>} description={<Text>AI services this run used, and the estimated cost from the price table.</Text>} />
      {!usage ? (
        <Text italic className={styles.muted}>
          Not recorded for this run (runs processed before usage tracking).
        </Text>
      ) : (
        <>
          <div className={styles.total}>
            <Text size={200} className={styles.muted}>
              Estimated cost
            </Text>
            <Text size={500} weight="semibold">
              {estimate.complete === false && !estimate.total ? "Rates not set" : cost(estimate.total, currency)}
            </Text>
            {estimate.complete === false && (
              <Text size={200} className={styles.muted} title={(estimate.missingRates || []).join("\n")}>
                {estimate.total ? "partial: " : ""}
                {number((estimate.missingRates || []).length)} meter(s) have no rate in config/pricing.json
              </Text>
            )}
            {estimate.basis && (
              <Text size={200} className={styles.muted} title={estimate.pricingVersion || undefined}>
                at {estimate.basis}
              </Text>
            )}
          </div>
          <Text block size={200} italic className={styles.disclaimer}>
            {estimate.disclaimer ||
              "Estimate only, from the price table's rates. Actual charges may differ; Azure Cost Management is the billing record."}
          </Text>
          {rows.length === 0 ? (
            <Text italic className={styles.muted}>
              No AI calls: every source was read by the deterministic readers.
            </Text>
          ) : (
            <Table size="small" aria-label="Cost and usage" className={styles.table}>
              <TableHeader>
                <TableRow>
                  <TableHeaderCell>Component</TableHeaderCell>
                  <TableHeaderCell>Calls</TableHeaderCell>
                  <TableHeaderCell>Usage</TableHeaderCell>
                  <TableHeaderCell>Est. cost</TableHeaderCell>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((row) => (
                  <TableRow key={row.key}>
                    <TableCell>
                      {row.label}
                      {row.model && <Text size={100} className={styles.muted}>{` · ${row.model}`}</Text>}
                    </TableCell>
                    <TableCell>{number(row.calls)}</TableCell>
                    <TableCell>{row.detail}</TableCell>
                    <TableCell>{estimate.complete === false && !row.cost ? "—" : cost(row.cost, currency)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </>
      )}
    </Card>
  );
}
