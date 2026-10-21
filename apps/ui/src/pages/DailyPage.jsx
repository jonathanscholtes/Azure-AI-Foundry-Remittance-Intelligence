import { useEffect, useState } from "react";
import { makeStyles, Card, CardHeader, Title3, Text, Input, Button, Spinner } from "@fluentui/react-components";
import { api } from "../api.js";
import { todayIso } from "../format.js";
import SummaryDashboard from "../components/SummaryDashboard.jsx";

const useStyles = makeStyles({
  page: { display: "flex", flexDirection: "column", gap: "16px" },
  toolbar: { display: "flex", gap: "12px", alignItems: "center" },
});

export default function DailyPage() {
  const styles = useStyles();
  const [date, setDate] = useState(todayIso());
  const [summary, setSummary] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = async (value) => {
    setLoading(true);
    try {
      setSummary(await api.daily(value));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load(date);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [date]);

  return (
    <div className={styles.page}>
      <Card>
        <CardHeader header={<Title3>Daily summary</Title3>} description={<Text>Processing outcomes for a single day.</Text>} />
        <div className={styles.toolbar}>
          <Text>Date</Text>
          <Input type="date" value={date} onChange={(_, d) => setDate(d.value)} />
          <Button onClick={() => load(date)}>Refresh</Button>
        </div>
      </Card>
      {loading ? <Spinner label="Loading summary..." /> : <SummaryDashboard summary={summary} />}
    </div>
  );
}
