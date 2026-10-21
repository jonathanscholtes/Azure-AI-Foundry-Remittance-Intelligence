import { useRef, useState } from "react";
import {
  makeStyles,
  tokens,
  Card,
  Text,
  Title3,
  Button,
  Spinner,
  MessageBar,
  MessageBarBody,
} from "@fluentui/react-components";
import { CloudArrowUp32Regular } from "@fluentui/react-icons";
import { shell } from "../../branding.js";

const useStyles = makeStyles({
  card: { padding: "12px 14px", height: "100%", minHeight: 0, boxSizing: "border-box", display: "flex", flexDirection: "column", gap: "2px" },
  title: { fontFamily: '"Segoe UI", sans-serif', fontSize: "16px", fontWeight: 700, lineHeight: 1.2, color: "#0a2454" },
  subtitle: { color: tokens.colorNeutralForeground3, fontSize: "11px", lineHeight: 1.25 },
  drop: {
    marginTop: "6px",
    minHeight: 0,
    flex: 1,
    border: `2px dashed ${tokens.colorNeutralStroke2}`,
    borderRadius: tokens.borderRadiusSmall,
    padding: "6px",
    textAlign: "center",
    backgroundColor: tokens.colorNeutralBackground2,
    cursor: "pointer",
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    justifyContent: "center",
    gap: "2px",
  },
  dropActive: { borderColor: shell.accent, backgroundColor: tokens.colorBrandBackground2 },
  cloud: { color: "#0a3c78", width: "28px", height: "28px" },
  hint: { color: tokens.colorNeutralForeground3, fontSize: "10px" },
  hidden: { display: "none" },
});

// Accepts one or many files; each is submitted to the API, which queues it on Service Bus.
export default function UploadCard({ onFiles, fileName, uploading, error }) {
  const styles = useStyles();
  const inputRef = useRef(null);
  const [dragging, setDragging] = useState(false);

  const pick = (fileList) => {
    const files = Array.from(fileList || []);
    if (files.length) onFiles(files);
  };

  return (
    <Card className={styles.card}>
      <Title3 className={styles.title}>Upload Remittance Files</Title3>
      <Text className={styles.subtitle}>
        Each file is queued on Service Bus and processed by the Durable workflow.
      </Text>

      <div
        className={`${styles.drop} ${dragging ? styles.dropActive : ""}`}
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          pick(e.dataTransfer.files);
        }}
      >
        {uploading ? (
          <Spinner label={uploading === true ? "Uploading..." : uploading} />
        ) : (
          <>
            <CloudArrowUp32Regular className={styles.cloud} />
            <Text weight="semibold">Drag and drop files here</Text>
            <Button size="small" appearance="primary" onClick={(e) => { e.stopPropagation(); inputRef.current?.click(); }}>
              Choose Files
            </Button>
            <Text className={styles.hint}>
              {fileName ? `Last: ${fileName}` : "Outlook .msg (or .eml) remittance emails"}
            </Text>
          </>
        )}
        <input
          ref={inputRef}
          type="file"
          accept=".msg,.eml"
          multiple
          className={styles.hidden}
          onChange={(e) => {
            pick(e.target.files);
            e.target.value = "";
          }}
        />
      </div>

      {error && (
        <MessageBar intent="error" style={{ marginTop: 10 }}>
          <MessageBarBody>{error}</MessageBarBody>
        </MessageBar>
      )}
    </Card>
  );
}
