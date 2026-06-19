import { useEffect, useRef, useState } from "react";
import { useApp } from "../useCassette";

interface Props {
  open: boolean;
  onClose: () => void;
}

export function SettingsDialog({ open, onClose }: Props) {
  const { t, getApiKey, saveApiKey } = useApp();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [value, setValue] = useState("");

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open) {
      setValue(getApiKey());
      if (!dialog.open) dialog.showModal();
    } else if (dialog.open) {
      dialog.close();
    }
  }, [open, getApiKey]);

  return (
    <dialog ref={dialogRef} onClose={onClose}>
      <form method="dialog" className="settings">
        <h2>{t("settingsHeading")}</h2>
        <label>
          <span>{t("apiKeyLabel")}</span>
          <input
            type="password"
            autoComplete="off"
            value={value}
            onChange={(event) => setValue(event.target.value)}
            placeholder={t("apiKeyPlaceholder")}
          />
        </label>
        <div className="dialog-actions">
          <button
            type="button"
            id="clearKeyBtn"
            onClick={() => {
              if (getApiKey() && !window.confirm(t("clearConfirm"))) return;
              setValue("");
              saveApiKey("");
            }}
          >
            {t("clear")}
          </button>
          <button type="button" onClick={() => dialogRef.current?.close()}>
            {t("close")}
          </button>
          <button
            type="button"
            id="saveKeyBtn"
            onClick={() => {
              saveApiKey(value.trim());
              dialogRef.current?.close();
            }}
          >
            {t("save")}
          </button>
        </div>
      </form>
    </dialog>
  );
}
