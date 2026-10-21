# How a Remittance Moves Through the Solution

> Back to the [README](../README.md)

This walks one remittance email from upload to result. The [architecture diagram](../README.md#architecture) in the README shows the same flow as a picture.

1. **Intake.** The UI uploads the email to the API. The API writes it to `incoming/{processingId}/` and puts a reference-only message on the Service Bus queue.
2. **Parse.** The orchestrator splits the email into its body and attachments and scores which parts carry remittance detail.
3. **Extract** (in parallel, per source). Payment tables are read with the Extraction Expert's column mapping and checked against the table's Total row. Everything else goes to Content Understanding and is grounded line by line.
4. **Validate and reconcile.** Decision-1 classifies the sources and the thread when there are several. Lines are merged and de-duplicated, and the stated total is reconciled to the cent. An email carrying several payments is flagged as one finding that names each payment. If verification fails, or key details are missing or disputed, the Resolution Expert reasons across all sources, and code verifies its answer. A clean run publishes the CSV.
5. **Assurance** (review runs only). Triage splits the run into owned issues. Payer questions go to the Assurance Expert, which returns one cited finding per issue, with the **amount in question**: the money that could be misapplied, lost or disputed. For collections inquiries and disputes it also reads an excerpt of the email, because lines that reconcile don't prove the money arrived. Each run separates blocking **review reasons** from informational **notes**.
6. **Record.** The Recorder writes every state change to the run ledger, the live events table and the audit blob, and traces it to Application Insights.
7. **Measure.** The run's usage is priced into a `usage` block in its result and totals go on the ledger row. The UI's Run Details page shows a **Cost and usage** card. One `remittance_run_completed` event feeds the dashboard.
