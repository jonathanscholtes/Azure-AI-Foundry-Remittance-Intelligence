import { useEffect, useState } from "react";
import { makeStyles, Card, CardHeader, Title3, Text, Input, Button, Spinner } from "@fluentui/react-components";
import { api } from "../api.js";
import { currentIsoWeek } from "../format.js";
import SummaryDashboard from "../components/SummaryDashboard.jsx";

const useStyles = makeStyles({
  page: { display: "flex", flexDirection: "column", gap: "16px" },
  toolbar: { display: "flex", gap: "12px", alignItems: "center" },
});

export default function WeeklyPage() {
  const styles = useStyles();
  const [week, setWeek] = useState(currentIsoWeek());
  const [summary, setSummary] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = async (value) => {
    setLoading(true);
    try {
      setSummary(await api.weekly(value));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load(week);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [week]);

  return (
    <div className={styles.page}>
      <Card>
        <CardHeader header={<Title3>Weekly summary</Title3>} description={<Text>Processing outcomes rolled up by ISO week (e.g. 2026-W42).</Text>} />
        <div className={styles.toolbar}>
          <Text>Week</Text>
          <Input type="week" value={week} onChange={(_, d) => setWeek(d.value)} />
          <Button onClick={() => load(week)}>Refresh</Button>
        </div>
      </Card>
      {loading ? <Spinner label="Loading summary..." /> : <SummaryDashboard summary={summary} />}
    </div>
  );
}
