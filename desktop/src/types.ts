// Shared types mirroring the FastAPI server responses.

export interface SchemaShape {
  rows: number;
  columns: number;
}

export interface SessionSchema {
  file_path: string;
  shape: SchemaShape;
  columns: string[];
  dtypes: Record<string, string>;
  null_counts: Record<string, number>;
  semantics: Record<string, string>;
  unique_sports: string[];
  unique_results: string[];
  domain: string;
  domain_confidence: number;
}

export interface SessionRecord {
  id: string;
  name: string;
  file_path: string;
  file_name: string;
  rows: number;
  columns: number;
  domain: string;
  domain_confidence: number;
  created_at: string;
  updated_at: string;
  archived: number;
  schema?: SessionSchema | null;
}

export interface StepResult {
  step_id: string;
  description: string;
  code: string;
  output: string;
  error: string | null;
  elapsed: number;
  chart_path: string | null;
}

export interface ExcelUpdate {
  action: string;
  column: string;
  rows_affected: number;
  timestamp?: string;
  detail?: string;
  success?: boolean;
  error?: string | null;
  backup_path?: string;
}

export interface WebResult {
  title?: string;
  body?: string;
  href?: string;
  entity?: string;
  spreadsheet_date?: string;
  web_date?: string | null;
  match?: boolean;
  confidence?: number;
  source?: string;
  detail?: string;
}

export interface AgentOutput {
  query: string;
  intent: string;
  confidence: number;
  report: string;
  charts: string[];
  excel_updates: ExcelUpdate[];
  web_results: WebResult[];
  step_results: StepResult[];
  elapsed: number;
  success: boolean;
  error: string | null;
  report_id?: string;
}

export interface ChatMessage {
  id: string;
  session_id: string;
  role: "user" | "assistant" | "system";
  content: string;
  intent?: string;
  confidence?: number;
  elapsed?: number;
  charts?: string[];
  web_results?: WebResult[];
  excel_updates?: ExcelUpdate[];
  step_results?: StepResult[];
  created_at: string;
  pending?: boolean;
}

export interface ChartRecord {
  id: string;
  session_id: string;
  message_id?: string;
  path: string;
  title?: string;
  step_id?: string;
  created_at: string;
}

export interface MutationRecord {
  id: string;
  session_id: string;
  action: string;
  column: string;
  rows_affected: number;
  detail?: string;
  backup_path?: string;
  success: number;
  error?: string;
  created_at: string;
}

export interface ReportRecord {
  id: string;
  session_id: string;
  title: string;
  query: string;
  markdown: string;
  html_path?: string;
  md_path?: string;
  created_at: string;
}

export interface RecentFile {
  file_path: string;
  file_name: string;
  last_opened: string;
  open_count: number;
}

export interface HealthInfo {
  status: string;
  version: string;
  ollama: { reachable: boolean; error?: string };
  session: string | null;
  df_loaded: boolean;
}

export interface OllamaStatus {
  reachable: boolean;
  url: string;
  active_model: string;
  model_present: boolean;
  installed: string[];
  error?: string;
}

export interface BackendInfo {
  running: boolean;
  pid: number | null;
  port: number;
  base_url: string;
  log_tail: string[];
}

export type Mode = "chat" | "report" | "verify" | "mutate";
