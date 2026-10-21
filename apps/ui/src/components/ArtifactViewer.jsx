import { useCallback, useEffect, useState } from "react";
import {
  makeStyles,
  tokens,
  OverlayDrawer,
  DrawerHeader,
  DrawerHeaderTitle,
  DrawerBody,
  Button,
  Spinner,
  Text,
  MessageBar,
  MessageBarBody,
} from "@fluentui/react-components";
import { ArrowDownload20Regular, Dismiss20Regular, Open20Regular } from "@fluentui/react-icons";
import { api } from "../api.js";
import { bytes } from "../format.js";

// Extension buckets decide how an artifact renders. .msg and other binaries fall through to download.
const TEXT_EXT = new Set(["json", "txt", "csv", "eml", "html", "htm", "xml", "log", "md"]);
const IMAGE_EXT = new Set(["png", "jpg", "jpeg", "gif", "webp", "svg"]);

function extensionOf(name) {
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot + 1).toLowerCase() : "";
}

function renderMode(ext) {
  if (ext === "pdf") return "pdf";
  if (IMAGE_EXT.has(ext)) return "image";
  if (TEXT_EXT.has(ext)) return "text";
  return "binary";
}

const useStyles = makeStyles({
  surface: { width: "min(1080px, 94vw)", maxWidth: "94vw" },
  headerSub: { color: tokens.colorNeutralForeground3, fontFamily: "monospace", fontSize: "12px" },
  body: { display: "flex", flexDirection: "column", gap: "10px", height: "100%" },
  pre: {
    margin: 0,
    padding: "12px",
    backgroundColor: tokens.colorNeutralBackground3,
    borderRadius: tokens.borderRadiusMedium,
    fontFamily: "monospace",
    fontSize: "12px",
    lineHeight: "1.5",
    whiteSpace: "pre-wrap",
    wordBreak: "break-word",
    overflow: "auto",
    flex: 1,
  },
  frame: { width: "100%", height: "100%", minHeight: "70vh", border: "none", borderRadius: tokens.borderRadiusMedium },
  image: { maxWidth: "100%", maxHeight: "80vh", objectFit: "contain", alignSelf: "center" },
  center: { display: "flex", flexDirection: "column", alignItems: "center", gap: "8px", padding: "32px" },
});

// A side drawer that renders one run artifact in-app. The raw bytes are proxied through the API
// (same-origin /api/...), so no direct Storage access is required by the browser.
export default function ArtifactViewer({ processingId, file, onClose }) {
  const styles = useStyles();
  const [text, setText] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);

  const open = Boolean(file);
  const ext = file ? extensionOf(file.name || file.path || "") : "";
  const mode = renderMode(ext);
  const url = file ? api.fileUrl(processingId, file.container, file.path) : null;

  const loadText = useCallback(async () => {
    if (!file || mode !== "text") return;
    setLoading(true);
    setText(null);
    setError(null);
    try {
      const raw = await api.fileText(processingId, file.container, file.path);
      if (ext === "json") {
        try {
          setText(JSON.stringify(JSON.parse(raw), null, 2));
        } catch {
          setText(raw); // malformed JSON: show it verbatim so the problem is visible
        }
      } else {
        setText(raw);
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [processingId, file, mode, ext]);

  useEffect(() => {
    loadText();
  }, [loadText]);

  return (
    <OverlayDrawer
      open={open}
      position="end"
      size="large"
      className={styles.surface}
      onOpenChange={(_, data) => {
        if (!data.open) onClose();
      }}
    >
      <DrawerHeader>
        <DrawerHeaderTitle
          action={
            <div style={{ display: "flex", gap: 4 }}>
              {url && (
                <Button appearance="subtle" icon={<Open20Regular />} as="a" href={url} target="_blank" rel="noreferrer" title="Open in new tab" />
              )}
              {url && (
                <Button appearance="subtle" icon={<ArrowDownload20Regular />} as="a" href={url} download={file?.name} title="Download" />
              )}
              <Button appearance="subtle" icon={<Dismiss20Regular />} onClick={onClose} title="Close" />
            </div>
          }
        >
          {file?.name || "Artifact"}
          <div className={styles.headerSub}>
            {file ? `${file.container}/${file.path}${file.size != null ? ` · ${bytes(file.size)}` : ""}` : ""}
          </div>
        </DrawerHeaderTitle>
      </DrawerHeader>
      <DrawerBody>
        <div className={styles.body}>
          {error && (
            <MessageBar intent="error">
              <MessageBarBody>{error}</MessageBarBody>
            </MessageBar>
          )}
          {!file ? null : mode === "image" ? (
            <img className={styles.image} src={url} alt={file.name} />
          ) : mode === "pdf" ? (
            <iframe className={styles.frame} src={url} title={file.name} />
          ) : mode === "binary" ? (
            <div className={styles.center}>
              <Text>This file type can't be previewed in the browser.</Text>
              <Button appearance="primary" icon={<ArrowDownload20Regular />} as="a" href={url} download={file.name}>
                Download {file.name}
              </Button>
            </div>
          ) : loading ? (
            <div className={styles.center}>
              <Spinner label="Loading..." />
            </div>
          ) : text != null ? (
            <pre className={styles.pre}>{text}</pre>
          ) : null}
        </div>
      </DrawerBody>
    </OverlayDrawer>
  );
}
