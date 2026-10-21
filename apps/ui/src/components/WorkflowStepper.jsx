import { Fragment } from "react";
import { makeStyles, tokens, Text, Spinner } from "@fluentui/react-components";
import {
  CheckmarkCircle24Filled,
  Circle24Regular,
  ErrorCircle24Filled,
  Warning24Filled,
  SubtractCircle24Regular,
} from "@fluentui/react-icons";
import { PIPELINE_STEPS, duration, isTerminal, resolutionSkipText, stepIndexForState } from "../format.js";

const useStyles = makeStyles({
  root: { display: "flex", alignItems: "flex-start", gap: "4px", padding: "8px 0" },
  step: { display: "flex", flexDirection: "column", alignItems: "center", flex: 1, gap: "3px", minWidth: "70px" },
  connector: { height: "2px", flex: 1, marginTop: "11px", backgroundColor: tokens.colorNeutralStroke2 },
  connectorDone: { backgroundColor: tokens.colorPaletteGreenBackground3 },
  label: { textAlign: "center", lineHeight: 1.15 },
  sub: { textAlign: "center", color: tokens.colorNeutralForeground3, lineHeight: 1.15 },
});

// Sum the recorded stage durations per pipeline step from the live event timeline.
function stepDurations(events = []) {
  const totals = {};
  for (const event of events) {
    const index = stepIndexForState(event.newState);
    const ms = event.durationMs ?? event.details?.durationMs;
    if (index >= 0 && typeof ms === "number") totals[index] = (totals[index] || 0) + ms;
  }
  return totals;
}

const lastEvent = (events, state) => [...(events || [])].reverse().find((e) => e.newState === state);

// Short "how it ran" text per step, plus a hover tooltip with the per-source / per-finding detail.
// Kept to a word or two so the stepper stays scannable; everything else lives in the tooltip.
function stepDetails(events) {
  const details = {};

  const extracted = lastEvent(events, "Extracted")?.details?.candidates;
  if (Array.isArray(extracted) && extracted.length) {
    const method = (c) => {
      const strategy = c.strategy || "";
      const table = strategy.startsWith("html_table") || strategy.startsWith("text_table") || strategy === "deterministic_parser";
      if (table) return c.mappedBy === "agent" ? "Expert" : strategy === "deterministic_parser" ? "Sheet" : "Table";
      return strategy === "content_understanding" ? "CU" : strategy || "—";
    };
    const methods = [...new Set(extracted.map(method))];
    details[stepIndexForState("Extracted")] = {
      short: methods.join(" + "),
      tooltip: extracted
        .map((c) => {
          const how = method(c) === "Expert" ? "table, columns mapped by ACH Extraction Expert" : method(c);
          return `${c.name || c.candidateId}: ${how}, ${c.lineCount ?? 0} line(s)`;
        })
        .join("\n"),
    };
  }

  const validatedDetails = (lastEvent(events, "Validated") || lastEvent(events, "Reconciled"))?.details;
  const validated = validatedDetails?.classification;
  const resolution = validatedDetails?.resolution;
  if (validated || resolution) {
    const parts = [];
    const tips = [];
    if (validated) {
      const ok = validated.status === "completed";
      if (ok) parts.push("Decision-1");
      tips.push(
        ok
          ? `Microsoft-Decision-1 classified ${validated.sourcesClassified} source(s); ${validated.linesExcluded} line(s) excluded` +
              (validated.threadKind ? `; thread: ${validated.threadKind.replace(/_/g, " ")}` : "")
          : "Decision-1 unavailable - lines kept as extracted"
      );
    }
    if (resolution?.status === "skipped") {
      tips.push(`ACH Resolution Expert ${resolutionSkipText(resolution)}`);
    } else if (resolution && resolution.status !== "not_needed" && resolution.status !== "not_configured") {
      parts.push(
        { accepted: "Resolved", confirmed: "Confirmed", recommended: "Recommended" }[resolution.status] || "Not resolved"
      );
      tips.push(
        `ACH Resolution Expert ${resolution.status} (${resolution.reason || resolution.status}); ` +
          `triggered by ${(resolution.triggers || []).join(", ").replace(/_/g, " ")}` +
          (resolution.toolCalls?.length ? `; tools: ${resolution.toolCalls.join(", ")}` : "")
      );
    }
    details[stepIndexForState("Validated")] = { short: parts.join(" + "), tooltip: tips.join("\n") };
  }

  const assessed = lastEvent(events, "Assessed")?.details;
  if (assessed) {
    const statuses = Object.values(assessed.expertStatuses || {});
    const short = statuses.includes("completed")
      ? "Expert"
      : statuses.includes("failed")
      ? "Expert failed"
      : "Rules only";
    details[stepIndexForState("Assessed")] = {
      short,
      tooltip: [
        `${assessed.issueCount ?? "?"} finding(s)${assessed.assuranceHeadline ? `; primary: ${assessed.assuranceHeadline}` : ""}`,
        short === "Rules only"
          ? "No payer issues - ACH Assurance Expert not needed"
          : `ACH Assurance Expert: ${statuses.join(", ")}`,
      ].join("\n"),
    };
  }
  return details;
}

// hidePendingSub: steps not reached yet show only their label (the subtitle stays in the tooltip) -
// for dense lists like the in-flight table, where every row would otherwise repeat the long subtitles.
export default function WorkflowStepper({ run, events, compact = false, hidePendingSub = false }) {
  const styles = useStyles();
  const status = run?.status;
  const state = run?.currentState || status || "Queued";
  const terminal = isTerminal(status) ? status : null;
  const activeIndex = run ? Math.max(0, stepIndexForState(state)) : -1;
  const reachedAssurance = (events || []).some((e) => e.newState === "AssuranceReview") || state === "AssuranceReview";
  const durations = stepDurations(events);
  const details = stepDetails(events);
  const textSize = compact ? 100 : 200;

  return (
    <div className={styles.root}>
      {PIPELINE_STEPS.map((step, index) => {
        const isLast = index === PIPELINE_STEPS.length - 1;
        const skipped = step.key === "assurance" && terminal && !reachedAssurance;
        const done = run && (index < activeIndex || (isLast && terminal && terminal !== "Failed"));
        const active = run && index === activeIndex && !terminal;

        let icon;
        if (isLast && terminal === "Failed") icon = <ErrorCircle24Filled primaryFill={tokens.colorPaletteRedForeground1} />;
        else if (isLast && terminal === "ReviewRequired") icon = <Warning24Filled primaryFill={tokens.colorPaletteYellowForeground1} />;
        else if (skipped) icon = <SubtractCircle24Regular primaryFill={tokens.colorNeutralForeground3} />;
        else if (done) icon = <CheckmarkCircle24Filled primaryFill={tokens.colorPaletteGreenForeground1} />;
        else if (active) icon = <Spinner size="tiny" />;
        else icon = <Circle24Regular primaryFill={tokens.colorNeutralStroke2} />;

        let sub = hidePendingSub ? "" : step.sub;
        if (skipped) sub = terminal === "Completed" ? "Not needed (clean)" : "Skipped";
        else if (active) sub = "In progress";
        else if (isLast && terminal) sub = terminal === "ReviewRequired" ? "Needs review" : terminal === "DuplicateSkipped" ? "Duplicate" : terminal;
        else if (done && !isLast) {
          // "Expert + CU · 9.6 s" / "Rules · 29 ms": who ran the step (the specific detail when there is
          // one, else the step's owner - matching the Home workflow), then how long it took.
          sub = [details[index]?.short || step.owner, durations[index] !== undefined ? duration(durations[index]) : null]
            .filter(Boolean)
            .join(" · ");
        }
        const tooltip = [step.sub, details[index]?.tooltip].filter(Boolean).join("\n");

        return (
          <Fragment key={step.key}>
            <div className={styles.step} title={tooltip}>
              {icon}
              <Text size={textSize} weight={active ? "semibold" : "regular"} className={styles.label}>
                {step.label}
              </Text>
              <Text size={100} className={styles.sub}>
                {sub}
              </Text>
            </div>
            {!isLast && <div className={`${styles.connector} ${run && index < activeIndex ? styles.connectorDone : ""}`} />}
          </Fragment>
        );
      })}
    </div>
  );
}
