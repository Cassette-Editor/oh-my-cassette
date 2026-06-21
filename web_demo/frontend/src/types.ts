export type Lang = "zh" | "en";

export type Connection = "connecting" | "ok" | "error";

export type Role = "user" | "assistant";

export interface ChatEvent {
  id: number;
  role: Role;
  text: string;
  kind?: string;
  client_event_id?: string;
  has_attachment?: boolean;
  attachment_url?: string;
  attachment_type?: string;
  attachment_name?: string;
}

export interface LocalError {
  id: string;
  kind: "error";
  text: string;
  retry?: () => void;
}

export interface LocalMessage {
  id: string;
  role: Role;
  text: string;
  kind?: string;
  client_event_id?: string;
}

export type Message = ChatEvent | LocalError | LocalMessage;

export interface UploadProgress {
  id: string;
  label: string;
  percent: number;
}

export function isLocalError(message: Message): message is LocalError {
  return typeof message.id === "string";
}

export function isErrorMessage(message: Message): boolean {
  return (message as { kind?: string }).kind === "error";
}

export interface Asset {
  original_name?: string;
  asset_id?: string;
  media_type?: string;
  size_bytes?: number;
  exists?: boolean;
}

export interface JobDownload {
  url: string;
  filename: string;
}

export interface Job {
  job_id?: string;
  status?: string;
  report?: { user_summary?: string; latest_progress?: string };
  downloads?: JobDownload[];
  log_url?: string;
}
