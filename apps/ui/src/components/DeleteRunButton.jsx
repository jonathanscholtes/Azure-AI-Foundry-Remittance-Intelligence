import { useState } from "react";
import {
  Button,
  Dialog,
  DialogSurface,
  DialogBody,
  DialogTitle,
  DialogContent,
  DialogActions,
  Spinner,
  Text,
  MessageBar,
  MessageBarBody,
  tokens,
} from "@fluentui/react-components";
import { Delete20Regular } from "@fluentui/react-icons";
import { api } from "../api.js";

// A delete control with a confirmation dialog. Used both as an icon-only action in the runs
// table and as a labelled button in the run-details header. onDeleted fires after success.
export default function DeleteRunButton({ processingId, iconOnly = false, appearance = "subtle", onDeleted }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const stop = (e) => e.stopPropagation();

  const confirm = async (e) => {
    stop(e);
    setBusy(true);
    setError(null);
    try {
      await api.remove(processingId);
      setOpen(false);
      onDeleted?.(processingId);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Button
        appearance={appearance}
        icon={<Delete20Regular />}
        aria-label="Delete run"
        title="Delete run and all its content"
        onClick={(e) => {
          stop(e);
          setOpen(true);
        }}
      >
        {iconOnly ? null : "Delete"}
      </Button>
      <Dialog open={open} onOpenChange={(_, d) => !busy && setOpen(d.open)}>
        <DialogSurface onClick={stop}>
          <DialogBody>
            <DialogTitle>Delete this remittance?</DialogTitle>
            <DialogContent>
              <Text>
                This permanently removes the run record, its audit timeline, and every stored file
                (source message, attachments, analysis and results) for{" "}
                <Text weight="semibold" style={{ fontFamily: "monospace" }}>
                  {processingId}
                </Text>
                . This cannot be undone.
              </Text>
              {error && (
                <MessageBar intent="error" style={{ marginTop: 10 }}>
                  <MessageBarBody>{error}</MessageBarBody>
                </MessageBar>
              )}
            </DialogContent>
            <DialogActions>
              <Button appearance="secondary" disabled={busy} onClick={(e) => { stop(e); setOpen(false); }}>
                Cancel
              </Button>
              <Button
                appearance="primary"
                disabled={busy}
                icon={busy ? <Spinner size="tiny" /> : <Delete20Regular />}
                style={{ backgroundColor: busy ? undefined : tokens.colorPaletteRedBackground3 }}
                onClick={confirm}
              >
                Delete permanently
              </Button>
            </DialogActions>
          </DialogBody>
        </DialogSurface>
      </Dialog>
    </>
  );
}
