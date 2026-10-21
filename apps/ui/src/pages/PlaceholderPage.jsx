import { makeStyles, tokens, Card, Title2, Text } from "@fluentui/react-components";
import { Wrench24Regular } from "@fluentui/react-icons";

const useStyles = makeStyles({
  card: {
    padding: "48px",
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    gap: "12px",
    textAlign: "center",
  },
  icon: { color: tokens.colorNeutralForeground3, fontSize: "40px" },
  sub: { color: tokens.colorNeutralForeground3, maxWidth: "440px" },
});

export default function PlaceholderPage({ title }) {
  const styles = useStyles();
  return (
    <Card className={styles.card}>
      <Wrench24Regular className={styles.icon} />
      <Title2>{title}</Title2>
      <Text className={styles.sub}>
        This area isn&apos;t wired up yet. The remittance upload, workflow, and audit features live on the
        Dashboard, Process &amp; Workflow, Summaries, and Audit Logs pages.
      </Text>
    </Card>
  );
}
