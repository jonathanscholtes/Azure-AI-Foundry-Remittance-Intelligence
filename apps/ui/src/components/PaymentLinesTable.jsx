import {
  Table,
  TableHeader,
  TableRow,
  TableHeaderCell,
  TableBody,
  TableCell,
  Badge,
  Text,
} from "@fluentui/react-components";
import { EXCLUSION_REASON_LABELS, money, warningLabel } from "../format.js";

// Fluent badges have a fixed height; long labels wrap inside the cell, so let the pill grow with them.
const PILL = { marginRight: 4, marginBlock: 2, height: "auto", minHeight: 20, whiteSpace: "normal", lineHeight: "16px", paddingBlock: 2 };

function pct(value) {
  return value === null || value === undefined ? "—" : `${Math.round(Number(value) * 100)}%`;
}

// Lines Microsoft-Decision-1 set aside, kept visible (collapsed) so nothing disappears silently.
function ExcludedLines({ excluded, currency }) {
  if (!excluded?.length) return null;
  return (
    <details style={{ marginTop: 10 }}>
      <summary style={{ cursor: "pointer" }}>
        <Text size={200} weight="semibold">
          {excluded.length} line{excluded.length === 1 ? "" : "s"} excluded by Microsoft-Decision-1
        </Text>
      </summary>
      <Table size="small" aria-label="Excluded lines">
        <TableHeader>
          <TableRow>
            <TableHeaderCell>PRO / Invoice</TableHeaderCell>
            <TableHeaderCell>Amount</TableHeaderCell>
            <TableHeaderCell>Source</TableHeaderCell>
            <TableHeaderCell>Why it was set aside</TableHeaderCell>
          </TableRow>
        </TableHeader>
        <TableBody>
          {excluded.map((line, index) => (
            <TableRow key={`${line.candidateId}-${line.proInvoiceNumber}-${index}`}>
              <TableCell style={{ fontFamily: "monospace" }}>{line.proInvoiceNumber || "—"}</TableCell>
              <TableCell>{money(line.amountPaid, currency)}</TableCell>
              <TableCell>{line.candidateId}</TableCell>
              <TableCell>{EXCLUSION_REASON_LABELS[line.reason] || line.reason}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </details>
  );
}

export default function PaymentLinesTable({ lines, excluded = [], currency = "USD" }) {
  if (!lines || lines.length === 0) {
    return (
      <>
        <Text italic>No payment lines extracted.</Text>
        <ExcludedLines excluded={excluded} currency={currency} />
      </>
    );
  }
  const showDetail = lines.some((l) => l.discountAmount != null || l.adjustmentAmount != null || l.originalInvoiceAmount != null);
  return (
    <>
      <Table size="small" aria-label="Payment lines">
        <TableHeader>
          <TableRow>
            <TableHeaderCell style={{ width: 40 }}>#</TableHeaderCell>
            <TableHeaderCell>PRO / Invoice</TableHeaderCell>
            <TableHeaderCell>Amount paid</TableHeaderCell>
            {showDetail && <TableHeaderCell>Invoice amt</TableHeaderCell>}
            {showDetail && <TableHeaderCell>Discount</TableHeaderCell>}
            {showDetail && <TableHeaderCell>Adjustment</TableHeaderCell>}
            <TableHeaderCell>Source</TableHeaderCell>
            <TableHeaderCell>Confidence</TableHeaderCell>
            <TableHeaderCell>Flags</TableHeaderCell>
          </TableRow>
        </TableHeader>
        <TableBody>
          {lines.map((line, index) => {
            const src = line.source || {};
            const flags = line.warnings || [];
            return (
              <TableRow key={`${line.proInvoiceNumber || "line"}-${index}`}>
                <TableCell>{index + 1}</TableCell>
                <TableCell style={{ fontFamily: "monospace" }}>{line.proInvoiceNumber || line.rawProInvoiceNumber || "—"}</TableCell>
                <TableCell>{money(line.amountPaid, currency)}</TableCell>
                {showDetail && <TableCell>{money(line.originalInvoiceAmount, currency)}</TableCell>}
                {showDetail && <TableCell>{money(line.discountAmount, currency)}</TableCell>}
                {showDetail && <TableCell>{money(line.adjustmentAmount, currency)}</TableCell>}
                <TableCell>
                  {src.candidateId || "—"}
                  {src.page ? ` · p${src.page}` : ""}
                  {src.worksheet ? ` · ${src.worksheet}` : ""}
                  {src.row ? ` · row ${src.row}` : ""}
                  {src.duplicateCandidateIds?.length ? ` · also in ${src.duplicateCandidateIds.join(", ")}` : ""}
                </TableCell>
                <TableCell>{pct(line.confidence)}</TableCell>
                <TableCell>
                  {src.method === "resolution_expert" && (
                    <Badge appearance="tint" color="brand" style={PILL} title={src.evidence ? `Evidence: ${src.evidence}` : undefined}>
                      ACH Resolution Expert
                    </Badge>
                  )}
                  {flags.map((f) => (
                    <Badge key={f} appearance="tint" color="warning" style={PILL} title={f}>
                      {warningLabel(f)}
                    </Badge>
                  ))}
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
      <ExcludedLines excluded={excluded} currency={currency} />
    </>
  );
}
