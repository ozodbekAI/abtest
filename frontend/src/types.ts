export type User = {
  id: number;
  email: string;
  first_name: string;
  last_name: string;
  is_verified: boolean;
  is_active: boolean;
  is_admin: boolean;
  created_at: string;
};

export type TokenResponse = {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
  user: User;
};

export type WBConnection = {
  id: number;
  store_name: string;
  connected: boolean;
  status: 'not_connected' | 'ready' | 'partial' | 'pending' | string;
  token_last4: string;
  ready_for_ab_tests: boolean;
  access: {
    content: boolean;
    promotion: boolean;
    analytics: boolean;
    statistics: boolean;
  };
  pings: Record<string, { status: string; http_status?: number | null; message?: string }>;
  last_validated_at?: string | null;
};

export type WBPromotionCashback = {
  sum: number;
  percent: number;
  expiration_date?: string | null;
};

export type WBPromotionBalance = {
  connection_id: number;
  account_balance: number;
  mutual_balance: number;
  promo_bonus_balance: number;
  cashbacks: WBPromotionCashback[];
  fetched_at: string;
};

export type DashboardStats = {
  connection_id: number;
  period: string;
  begin_date: string;
  end_date: string;
  campaign_count: number;
  active_campaign_count: number;
  views: number;
  clicks: number;
  spend_rub: number;
  orders: number;
  ctr: number;
  cpo_rub: number | null;
  completed_campaign_count: number;
  failed_campaign_count: number;
  chart: Array<{
    date: string;
    tests: number;
    views: number;
    clicks: number;
    spend_rub: number;
  }>;
  stats_complete: boolean;
};

export type AdminUserItem = {
  id: number;
  email: string;
  first_name: string;
  last_name: string;
  is_verified: boolean;
  is_active: boolean;
  is_admin: boolean;
  stores_count: number;
  tests_count: number;
  created_at: string;
};

export type AdminUserDetail = {
  user: AdminUserItem;
  stores: AdminStoreItem[];
  tests: AdminTestItem[];
};

export type AdminStoreItem = {
  id: number;
  user_id: number;
  owner_email: string;
  owner_name: string;
  store_name: string;
  status: string;
  ready_for_ab_tests: boolean;
  token_last4: string;
  tests_count: number;
  last_validated_at?: string | null;
  created_at: string;
};

export type AdminStoreDetail = {
  store: AdminStoreItem;
  tests: AdminTestItem[];
};

export type AdminTestItem = {
  id: number;
  connection_id: number;
  store_name: string;
  user_id: number;
  nm_id: number;
  title: string;
  status: string;
  wb_campaign_id?: number | null;
  total_views: number;
  total_clicks: number;
  total_orders: number;
  total_spend_rub: number;
  total_ctr: number;
  last_error?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  created_at: string;
};

export type AdminDashboard = {
  users_count: number;
  active_users_count: number;
  verified_users_count: number;
  stores_count: number;
  tests_count: number;
  running_tests_count: number;
  finished_tests_count: number;
  failed_tests_count: number;
  views: number;
  clicks: number;
  orders: number;
  spend_rub: number;
  registrations: Array<{ date: string; count: number }>;
};

export type AdminSettings = { registration_enabled: boolean };

export type ABTestStatus = 'draft' | 'running' | 'finished' | 'failed' | 'stopped' | string;

export type ABTestCard = {
  nm_id: number;
  vendor_code?: string | null;
  title?: string | null;
  brand?: string | null;
  main_photo_url?: string | null;
  photos: string[];
};

export type ABTestCardsPage = {
  items: ABTestCard[];
  next_cursor?: ABTestCardCursor | null;
  total?: number | null;
};

export type ABTestCardCursor = { updatedAt?: string; nmID?: number; nmId?: number; limit?: number };

export type ABTestVariant = {
  id: number;
  position: number;
  source_type: string;
  file_name?: string | null;
  image_url?: string | null;
  source_url?: string | null;
  wb_url?: string | null;
  views: number;
  clicks: number;
  orders: number;
  ctr: number;
  cpo?: number | null;
  spend_rub: number;
  is_winner: boolean;
};

export type ABTest = {
  id: number;
  connection_id: number;
  store_name: string;
  nm_id: number;
  title: string;
  status: ABTestStatus;
  wb_campaign_id?: number | null;
  bid_type: 'manual' | 'unified';
  skip_current_photo: boolean;
  keep_winner_as_main: boolean;
  delete_test_media: boolean;
  views_per_variant: number;
  cpm_rub: number;
  budget_rub: number;
  placement: string;
  current_variant_order: number;
  winner_variant_order?: number | null;
  winner_decision?: 'winner_found' | 'no_clear_winner' | 'insufficient_data' | 'test_interrupted' | null;
  operation_state: string;
  campaign_state: string;
  media_status: string;
  stats_quality: string;
  incident_id?: string | null;
  unallocated_views: number;
  unallocated_clicks: number;
  unallocated_spend_rub: number;
  total_views: number;
  total_clicks: number;
  total_orders: number;
  total_spend_rub: number;
  total_ctr: number;
  total_cpo?: number | null;
  last_error?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  last_synced_at?: string | null;
  created_at: string;
  variants: ABTestVariant[];
};

export type ABTestCreatePayload = {
  connection_id: number;
  nm_id: number;
  title: string;
  skip_current_photo: boolean;
  keep_winner_as_main: boolean;
  delete_test_media: boolean;
  views_per_variant: number;
  cpm_rub: number;
  budget_rub: number;
  placement: 'combined';
};
