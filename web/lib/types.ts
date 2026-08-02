export type ProjectSummary = {
  id: string;
  name: string;
  games: number;
  players: number;
  unreviewed: number;
  reviewed: number;
  accepted: number;
  refreshedAt?: string;
};

export type Player = {
  player_id: string;
  display: string;
  number?: string;
  accepted?: number;
  pending?: number;
  deferred?: number;
  skipped?: number;
  unconfirmed?: number;
};

export type Game = {
  event_id: string;
  game_date?: string;
  opponent?: string;
  home_away?: string;
};

export type Participant = {
  player_id: string;
  display: string;
  number?: string;
  role: "batter" | "runner" | "pitcher" | "fielder";
  confidence?: string;
};

export type Clip = {
  clip_key: string;
  play_type?: string;
  play_title?: string;
  play_summary: string;
  source_play_summary?: string;
  score: number;
  score_reason?: string;
  game_date?: string;
  opponent?: string;
  inning?: number;
  inning_half?: string;
  participant_text?: string;
  participants?: Participant[];
  timing_text?: string;
  display_start?: number;
  display_end?: number;
  proposal_start?: number;
  proposal_end?: number;
  final_start?: number;
  final_end?: number;
  asset_duration?: number;
  review_state?: "unreviewed" | "reviewed" | "dismissed";
  side?: "offense" | "defense" | "unknown";
  status?: string;
  source_changed?: number;
  reel_order?: number;
};

export type QueuePage = {
  total: number;
  offset: number;
  rows: Clip[];
};

export type DashboardData = {
  project: ProjectSummary;
  players: Player[];
  games: Game[];
};

export type PreviewData = {
  path: string;
  seek: number;
  inPoint: number;
  outPoint: number;
  sourceStart: number;
  sourceEnd: number;
  fullGame?: boolean;
};

export type CacheGame = {
  eventId: string;
  gameDate?: string;
  opponent?: string;
  assetCount: number;
  cachedAssets: number;
  duration: number;
  bytes: number;
};

export type CacheJob = {
  state: "idle" | "starting" | "running" | "complete" | "failed" | "stopped";
  pid?: number;
  totalAssets?: number;
  processedAssets?: number;
  currentEventId?: string;
  error?: string;
};

export type CacheStatus = {
  games: CacheGame[];
  totalAssets: number;
  cachedAssets: number;
  cachedBytes: number;
  job: CacheJob;
};
