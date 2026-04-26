export type PurchaseMode = "new_purchase" | "upgrade";
export type PayType = "ALI" | "WE_CHAT";

export interface ApiErrorPayload {
  message?: string;
  details?: unknown;
}

export interface ApiResponse<T> {
  ok: boolean;
  data?: T;
  error?: ApiErrorPayload;
}

export interface HealthPayload {
  transport: string;
  [key: string]: unknown;
}

export interface RuntimeLogsPayload {
  date: string;
  path: string;
  lines: string[];
  text: string;
  truncated?: boolean;
  total?: number;
}

export interface ProductOffer {
  product_id: string;
  product_name: string;
  unit: string;
  sale_price: string;
  plan_type: string;
  purchase_mode: PurchaseMode;
  version?: string;
  sold_out?: boolean;
  forbidden?: boolean;
  last_valid?: boolean;
  can_repurchase?: boolean;
  delay?: boolean;
  effective_time?: string;
  monthly_renew_amount?: string;
  monthly_original_amount?: string;
  campaign_discount_details?: Record<string, unknown>[];
  raw?: Record<string, unknown>;
}

export interface PublicAccountRecord {
  id: string;
  label: string;
  org_id?: string;
  project_id?: string;
  invitation_code?: string;
  proxy_url?: string;
  user_agent?: string;
  browser_impersonate?: string;
  preview_concurrency?: number;
  schedule_enabled?: boolean;
  scheduled_start_time?: string;
  last_scheduled_run_at?: string | null;
  last_scheduled_run_key?: string;
  last_manual_run_at?: string | null;
  last_schedule_status?: string;
  last_schedule_message?: string;
  account_status?: string;
  account_status_message?: string;
  account_checked_at?: string | null;
  has_token?: boolean;
  token_preview?: string;
  has_cookie_header?: boolean;
  last_bootstrap_at?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface AccountSessionState {
  account_id: string;
  org_id?: string;
  project_id?: string;
  customer_number?: string;
  customer_name?: string;
  organizations?: Record<string, unknown>[];
  user_info?: Record<string, unknown>;
  products?: ProductOffer[];
  is_subscribed?: boolean;
  purchase_mode?: PurchaseMode;
  selected_product_id?: string;
  captcha_ticket?: string;
  captcha_randstr?: string;
  captcha_updated_at?: string | null;
  preview?: Record<string, unknown> | null;
  last_sign?: string;
  last_order_id?: string;
  updated_at?: string;
  [key: string]: unknown;
}

export interface PaymentTaskRecord {
  id: string;
  account_id: string;
  product_id: string;
  product_name?: string;
  pay_type: PayType;
  biz_id: string;
  amount?: string;
  sign?: string;
  qr_base64?: string;
  status?: string;
  raw_preview?: Record<string, unknown>;
  raw_sign?: Record<string, unknown>;
  last_check?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
}

export interface AccountDetailResponse {
  account: PublicAccountRecord;
  session: AccountSessionState;
  tasks: PaymentTaskRecord[];
}

export interface AccountImportPayload {
  label: string;
  token: string;
  invitation_code?: string;
}

export interface AccountPreferencesPayload {
  invitation_code?: string | null;
  selected_product_id?: string | null;
  preview_concurrency?: number | null;
  schedule_enabled?: boolean | null;
  scheduled_start_time?: string | null;
}
