"use client";

import {
  ArrowDown,
  ArrowUp,
  BarChart3,
  Clapperboard,
  Film,
  HardDriveDownload,
  KeyRound,
  LayoutDashboard,
  ListFilter,
  LoaderCircle,
  Menu,
  Plus,
  RefreshCw,
  Search,
  Square,
  Trash2,
  Users,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { QueueTable } from "@/components/QueueTable";
import { ReviewPanel } from "@/components/ReviewPanel";
import type {
  Clip,
  CacheStatus,
  DashboardData,
  Player,
  ProjectSummary,
  QueuePage,
} from "@/lib/types";

type View = "dashboard" | "all" | "players" | "media";
const PAGE_SIZE = 75;

function formatBytes(bytes: number) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(
    units.length - 1,
    Math.floor(Math.log(bytes) / Math.log(1024)),
  );
  return `${(bytes / 1024 ** index).toFixed(index < 2 ? 0 : 1)} ${units[index]}`;
}

function clockDuration(seconds: number) {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.round((seconds % 3600) / 60);
  return hours ? `${hours}h ${minutes}m` : `${minutes}m`;
}

async function bridge<T>(
  action: string,
  payload: Record<string, unknown> = {},
): Promise<T> {
  const response = await fetch("/api/bridge", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(typeof window !== "undefined" && sessionStorage.getItem("gc-token")
        ? { "x-gc-token": sessionStorage.getItem("gc-token") as string }
        : {}),
    },
    body: JSON.stringify({ action, payload }),
  });
  const data = (await response.json()) as { data?: T; error?: string };
  if (!response.ok || data.error) throw new Error(data.error || "Request failed.");
  return data.data as T;
}

function TokenDialog({ onClose }: { onClose: () => void }) {
  const [token, setToken] = useState(
    typeof window === "undefined" ? "" : sessionStorage.getItem("gc-token") || "",
  );

  return (
    <div className="modal-backdrop">
      <div className="setup-dialog token-dialog">
        <header>
          <div>
            <span className="eyebrow">Session authentication</span>
            <h2>GameChanger token</h2>
          </div>
          <button className="icon-button" title="Close" onClick={onClose}>
            <X size={18} />
          </button>
        </header>
        <label>
          GC token
          <input
            type="password"
            value={token}
            autoFocus
            onChange={(event) => setToken(event.target.value)}
          />
        </label>
        <button
          className="primary-button"
          disabled={!token.trim()}
          onClick={() => {
            sessionStorage.setItem("gc-token", token.trim());
            onClose();
          }}
        >
          <KeyRound size={16} /> Use for this tab
        </button>
      </div>
    </div>
  );
}

function SetupDialog({
  onClose,
  onCreated,
}: {
  onClose?: () => void;
  onCreated: (project: ProjectSummary) => void;
}) {
  const [teams, setTeams] = useState<{ id: string; label: string }[]>([]);
  const [selected, setSelected] = useState("");
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    bridge<{ id: string; label: string }[]>("teams")
      .then((items) => {
        setTeams(items);
        setSelected(items[0]?.id || "");
      })
      .catch((reason) => setError(reason.message))
      .finally(() => setBusy(false));
  }, []);

  async function create() {
    setBusy(true);
    setError("");
    try {
      const result = await bridge<{ project: ProjectSummary }>("create_project", {
        teamId: selected,
      });
      onCreated(result.project);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop">
      <div className="setup-dialog">
        <header>
          <div>
            <span className="eyebrow">Season project</span>
            <h2>Add a GameChanger team</h2>
          </div>
          {onClose ? (
            <button className="icon-button" title="Close" onClick={onClose}>
              <X size={18} />
            </button>
          ) : null}
        </header>
        {error ? <div className="error-banner">{error}</div> : null}
        <label>
          Team and season
          <select
            value={selected}
            onChange={(event) => setSelected(event.target.value)}
            disabled={busy}
          >
            {teams.map((team) => (
              <option key={team.id} value={team.id}>
                {team.label}
              </option>
            ))}
          </select>
        </label>
        <button
          className="primary-button"
          disabled={!selected || busy}
          onClick={create}
        >
          {busy ? <LoaderCircle className="spin" size={17} /> : <Plus size={17} />}
          {busy ? "Loading teams" : "Create and import"}
        </button>
      </div>
    </div>
  );
}

export default function Home() {
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [projectId, setProjectId] = useState("");
  const [dashboard, setDashboard] = useState<DashboardData | null>(null);
  const [view, setView] = useState<View>("all");
  const [queue, setQueue] = useState<QueuePage>({ total: 0, offset: 0, rows: [] });
  const [queueLoading, setQueueLoading] = useState(false);
  const [selectedClip, setSelectedClip] = useState<Clip | null>(null);
  const [bulkSelected, setBulkSelected] = useState<Set<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  const [selectedPlayer, setSelectedPlayer] = useState("");
  const [playerStatus, setPlayerStatus] = useState("pending");
  const [role, setRole] = useState("all");
  const [side, setSide] = useState("all");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [initialLoading, setInitialLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [rendering, setRendering] = useState(false);
  const [cacheStatus, setCacheStatus] = useState<CacheStatus | null>(null);
  const [cacheBusy, setCacheBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [showSetup, setShowSetup] = useState(false);
  const [showToken, setShowToken] = useState(false);
  const [mobileNav, setMobileNav] = useState(false);

  const call = useCallback(
    <T,>(action: string, payload: Record<string, unknown> = {}) =>
      bridge<T>(action, payload),
    [],
  );

  const loadProjects = useCallback(async () => {
    try {
      const response = await fetch("/api/bridge");
      const body = (await response.json()) as {
        data?: ProjectSummary[];
        error?: string;
      };
      if (!response.ok) throw new Error(body.error || "Could not load projects.");
      const items = body.data ?? [];
      setProjects(items);
      setProjectId((current) => {
        if (current && items.some((item) => item.id === current)) return current;
        return items[0]?.id || "";
      });
      if (items.length === 0) setShowSetup(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setInitialLoading(false);
    }
  }, []);

  const loadDashboard = useCallback(async () => {
    if (!projectId) return;
    try {
      const data = await call<DashboardData>("dashboard", { project: projectId });
      setDashboard(data);
      setProjects((current) =>
        current.map((project) =>
          project.id === data.project.id ? data.project : project,
        ),
      );
      setSelectedPlayer((current) => current || data.players[0]?.player_id || "");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }, [call, projectId]);

  const loadQueue = useCallback(async () => {
    if (!projectId || view === "dashboard" || view === "media") return;
    if (view === "players" && !selectedPlayer) return;
    setQueueLoading(true);
    setError("");
    try {
      const action = view === "all" ? "all_queue" : "player_queue";
      const data = await call<QueuePage>(action, {
        project: projectId,
        playerId: selectedPlayer,
        status: playerStatus,
        role,
        side,
        search,
        offset,
        limit: PAGE_SIZE,
      });
      setQueue(data);
      const visibleKeys = new Set(data.rows.map((clip) => clip.clip_key));
      setBulkSelected(
        (current) =>
          new Set([...current].filter((clipKey) => visibleKeys.has(clipKey))),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setQueueLoading(false);
    }
  }, [
    call,
    offset,
    playerStatus,
    projectId,
    role,
    search,
    selectedPlayer,
    side,
    view,
  ]);

  const loadCacheStatus = useCallback(async () => {
    if (!projectId) return;
    try {
      setCacheStatus(
        await call<CacheStatus>("cache_status", { project: projectId }),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }, [call, projectId]);

  useEffect(() => {
    loadProjects();
  }, [loadProjects]);

  useEffect(() => {
    if (!projectId) return;
    setOffset(0);
    setSelectedClip(null);
    setBulkSelected(new Set());
    loadDashboard();
  }, [loadDashboard, projectId]);

  useEffect(() => {
    const timer = setTimeout(loadQueue, search ? 250 : 0);
    return () => clearTimeout(timer);
  }, [loadQueue, search]);

  useEffect(() => {
    if (view !== "media") return;
    void loadCacheStatus();
    const timer = window.setInterval(() => void loadCacheStatus(), 2000);
    return () => window.clearInterval(timer);
  }, [loadCacheStatus, view]);

  useEffect(() => {
    if (view !== "media" || !cacheStatus) return;
    void Promise.all([loadProjects(), loadDashboard()]);
  }, [cacheStatus?.cachedAssets, loadDashboard, loadProjects, view]);

  useEffect(() => {
    setOffset(0);
    setSelectedClip(null);
    setBulkSelected(new Set());
  }, [view, role, side, search, selectedPlayer, playerStatus]);

  useEffect(() => {
    setSelectedClip(null);
    setBulkSelected(new Set());
  }, [offset]);

  async function dismissSelectedAsNotNoteworthy() {
    const clipKeys = [...bulkSelected];
    if (!clipKeys.length) return;
    if (
      !window.confirm(
        `Dismiss ${clipKeys.length} selected clip${clipKeys.length === 1 ? "" : "s"} as not noteworthy?`,
      )
    ) {
      return;
    }
    setBulkBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await call<{ count: number }>("dismiss_many", {
        project: projectId,
        clipKeys,
        reason: "not_noteworthy",
      });
      if (selectedClip && bulkSelected.has(selectedClip.clip_key)) {
        setSelectedClip(null);
      }
      setBulkSelected(new Set());
      await Promise.all([loadQueue(), loadDashboard()]);
      setNotice(
        `Dismissed ${result.count} clip${result.count === 1 ? "" : "s"} as not noteworthy.`,
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBulkBusy(false);
    }
  }

  async function refreshProject() {
    if (!projectId) return;
    setRefreshing(true);
    setError("");
    setNotice("");
    try {
      const result = await call<{ imported: { games: number; clips: number } }>(
        "refresh",
        { project: projectId },
      );
      await Promise.all([loadDashboard(), loadQueue()]);
      setNotice(
        `Refresh complete: ${result.imported.games} games and ${result.imported.clips.toLocaleString()} clips indexed.`,
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setRefreshing(false);
    }
  }

  async function renderPlayer() {
    if (!projectId || !selectedPlayer) return;
    setRendering(true);
    setError("");
    setNotice("");
    try {
      const result = await call<{ path: string }>("render_player", {
        project: projectId,
        playerId: selectedPlayer,
      });
      setNotice(`Reel written to ${result.path}.`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setRendering(false);
    }
  }

  async function renderAll() {
    if (!projectId) return;
    setRendering(true);
    setError("");
    setNotice("");
    try {
      const result = await call<{ paths: string[] }>("render_all", {
        project: projectId,
      });
      setNotice(`Rendered ${result.paths.length} player reels.`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setRendering(false);
    }
  }

  async function moveClip(direction: number) {
    if (!selectedClip || !selectedPlayer) return;
    await call("move", {
      project: projectId,
      playerId: selectedPlayer,
      clipKey: selectedClip.clip_key,
      direction,
    });
    await loadQueue();
  }

  async function startCache(eventIds: string[] = []) {
    setCacheBusy(true);
    setError("");
    try {
      await call("cache_start", { project: projectId, eventIds });
      await loadCacheStatus();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setCacheBusy(false);
    }
  }

  async function stopCache() {
    setCacheBusy(true);
    try {
      await call("cache_stop", { project: projectId });
      await loadCacheStatus();
    } finally {
      setCacheBusy(false);
    }
  }

  const activeProject = projects.find((project) => project.id === projectId);
  const activePlayer = dashboard?.players.find(
    (player) => player.player_id === selectedPlayer,
  );
  const cacheRunning =
    cacheStatus?.job.state === "running" ||
    cacheStatus?.job.state === "starting";

  const nav = useMemo(
    () => [
      { id: "dashboard" as const, label: "Dashboard", icon: LayoutDashboard },
      { id: "all" as const, label: "All queue", icon: ListFilter },
      { id: "players" as const, label: "Player reels", icon: Users },
      { id: "media" as const, label: "Game cache", icon: HardDriveDownload },
    ],
    [],
  );

  if (initialLoading) {
    return (
      <main className="center-state">
        <Clapperboard size={30} />
        <LoaderCircle className="spin" size={22} />
      </main>
    );
  }

  return (
    <div className="app-shell">
      <aside className={`sidebar ${mobileNav ? "mobile-open" : ""}`}>
        <div className="brand">
          <span className="brand-mark">
            <Film size={20} />
          </span>
          <div>
            <strong>Season Highlights</strong>
            <span>GameChanger review</span>
          </div>
          <button
            className="icon-button mobile-only"
            title="Close navigation"
            onClick={() => setMobileNav(false)}
          >
            <X size={18} />
          </button>
        </div>

        <div className="project-picker">
          <label htmlFor="project">Project</label>
          <div>
            <select
              id="project"
              value={projectId}
              onChange={(event) => setProjectId(event.target.value)}
            >
              {projects.map((project) => (
                <option value={project.id} key={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
            <button className="icon-button" title="Add project" onClick={() => setShowSetup(true)}>
              <Plus size={16} />
            </button>
          </div>
        </div>

        <nav>
          {nav.map((item) => {
            const Icon = item.icon;
            return (
              <button
                key={item.id}
                className={view === item.id ? "active" : ""}
                onClick={() => {
                  setView(item.id);
                  setMobileNav(false);
                }}
              >
                <Icon size={17} />
                {item.label}
                {item.id === "all" ? (
                  <span className="nav-count">{activeProject?.unreviewed || 0}</span>
                ) : null}
              </button>
            );
          })}
        </nav>

        <div className="sidebar-status">
          <span>{activeProject?.games || 0} games imported</span>
          <span>{activeProject?.reviewed || 0} clips reviewed</span>
        </div>
      </aside>

      <main className="main-workspace">
        <header className="topbar">
          <button
            className="icon-button mobile-only"
            title="Open navigation"
            onClick={() => setMobileNav(true)}
          >
            <Menu size={19} />
          </button>
          <div>
            <span className="eyebrow">{activeProject?.name || "Season project"}</span>
            <h1>
              {view === "dashboard"
                ? "Review progress"
                : view === "media"
                  ? "Full-game cache"
                : view === "all"
                  ? "All unreviewed clips"
                  : activePlayer?.display || "Player reels"}
            </h1>
          </div>
          <div className="topbar-actions">
            <button className="secondary-button token-button" onClick={() => setShowToken(true)}>
              <KeyRound size={16} />
              Token
            </button>
            <button
              className="secondary-button"
              disabled={rendering || !activeProject?.accepted}
              onClick={renderAll}
            >
              {rendering ? <LoaderCircle className="spin" size={16} /> : <Film size={16} />}
              Render all
            </button>
            <button
              className="secondary-button"
              disabled={refreshing}
              onClick={refreshProject}
            >
              <RefreshCw className={refreshing ? "spin" : ""} size={16} />
              Refresh
            </button>
          </div>
        </header>

        {error ? (
          <div className="workspace-error">
            <span>{error}</span>
            <button className="icon-button" title="Dismiss" onClick={() => setError("")}>
              <X size={16} />
            </button>
          </div>
        ) : null}
        {notice ? (
          <div className="workspace-notice">
            <span>{notice}</span>
            <button className="icon-button" title="Dismiss" onClick={() => setNotice("")}>
              <X size={16} />
            </button>
          </div>
        ) : null}

        {view === "media" ? (
          <section className="cache-view">
            <div className="cache-summary">
              <div>
                <span>Cached recordings</span>
                <strong>
                  {cacheStatus?.cachedAssets || 0}/{cacheStatus?.totalAssets || 0}
                </strong>
              </div>
              <div>
                <span>Local storage</span>
                <strong>{formatBytes(cacheStatus?.cachedBytes || 0)}</strong>
              </div>
              <div className="cache-actions">
                {cacheRunning ? (
                  <button
                    className="secondary-button"
                    disabled={cacheBusy}
                    onClick={stopCache}
                  >
                    <Square size={15} /> Stop
                  </button>
                ) : (
                  <button
                    className="primary-button"
                    disabled={cacheBusy || cacheStatus?.cachedAssets === cacheStatus?.totalAssets}
                    onClick={() => startCache()}
                  >
                    <HardDriveDownload size={16} /> Cache missing games
                  </button>
                )}
              </div>
            </div>
            {cacheRunning ? (
              <div className="cache-progress">
                <div>
                  <span
                    style={{
                      width: `${Math.max(
                        2,
                        ((cacheStatus.job.processedAssets || 0) /
                          Math.max(1, cacheStatus.job.totalAssets || 1)) *
                          100,
                      )}%`,
                    }}
                  />
                </div>
                <p>
                  Downloading recording {(cacheStatus.job.processedAssets || 0) + 1} of{" "}
                  {cacheStatus.job.totalAssets || 0}
                </p>
              </div>
            ) : null}
            {cacheStatus?.job.error ? (
              <div className="error-banner">{cacheStatus.job.error}</div>
            ) : null}
            <div className="cache-table-wrap">
              <table className="cache-table">
                <thead>
                  <tr>
                    <th>Game</th>
                    <th>Recordings</th>
                    <th>Duration</th>
                    <th>Local size</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {cacheStatus?.games.map((game) => {
                    const complete = game.cachedAssets === game.assetCount;
                    const active =
                      cacheRunning &&
                      cacheStatus.job.currentEventId === game.eventId;
                    return (
                      <tr key={game.eventId}>
                        <td>
                          <strong>{game.opponent || "Opponent"}</strong>
                          <small>
                            {game.gameDate
                              ? new Date(game.gameDate).toLocaleDateString()
                              : ""}
                          </small>
                        </td>
                        <td>
                          {game.cachedAssets}/{game.assetCount}
                        </td>
                        <td>{clockDuration(game.duration)}</td>
                        <td>{formatBytes(game.bytes)}</td>
                        <td>
                          {complete ? (
                            <span className="cache-state complete">Cached</span>
                          ) : active ? (
                            <span className="cache-state active">Downloading</span>
                          ) : (
                            <button
                              className="secondary-button"
                              disabled={cacheBusy || cacheRunning}
                              onClick={() => startCache([game.eventId])}
                            >
                              <HardDriveDownload size={15} /> Cache
                            </button>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>
        ) : view === "dashboard" ? (
          <section className="dashboard-view">
            <div className="stat-strip">
              <div>
                <span>Unreviewed</span>
                <strong>{activeProject?.unreviewed.toLocaleString() || 0}</strong>
              </div>
              <div>
                <span>Reviewed</span>
                <strong>{activeProject?.reviewed.toLocaleString() || 0}</strong>
              </div>
              <div>
                <span>Accepted moments</span>
                <strong>{activeProject?.accepted.toLocaleString() || 0}</strong>
              </div>
              <div>
                <span>Roster</span>
                <strong>{activeProject?.players || 0}</strong>
              </div>
            </div>
            <div className="section-title">
              <BarChart3 size={17} />
              <h2>Player progress</h2>
            </div>
            <div className="table-wrap progress-table">
              <table>
                <thead>
                  <tr>
                    <th>Player</th>
                    <th>Accepted</th>
                    <th>Pending</th>
                    <th>Deferred</th>
                    <th>Skipped</th>
                    <th>Unconfirmed</th>
                  </tr>
                </thead>
                <tbody>
                  {dashboard?.players.map((player) => (
                    <tr
                      key={player.player_id}
                      onClick={() => {
                        setSelectedPlayer(player.player_id);
                        setView("players");
                      }}
                    >
                      <td>
                        <b>#{player.number || "–"}</b> {player.display}
                      </td>
                      <td>{player.accepted || 0}</td>
                      <td>{player.pending || 0}</td>
                      <td>{player.deferred || 0}</td>
                      <td>{player.skipped || 0}</td>
                      <td>{player.unconfirmed || 0}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        ) : (
          <section className="queue-view">
            {view === "players" ? (
              <div className="player-bar">
                <select
                  value={selectedPlayer}
                  onChange={(event) => setSelectedPlayer(event.target.value)}
                >
                  {dashboard?.players.map((player) => (
                    <option value={player.player_id} key={player.player_id}>
                      #{player.number || "–"} {player.display}
                    </option>
                  ))}
                </select>
                <div className="segmented-control">
                  {["pending", "deferred", "skipped", "accepted"].map((status) => (
                    <button
                      className={playerStatus === status ? "active" : ""}
                      key={status}
                      onClick={() => setPlayerStatus(status)}
                    >
                      {status}
                    </button>
                  ))}
                </div>
                {playerStatus === "accepted" ? (
                  <div className="order-actions">
                    <button
                      className="icon-button"
                      title="Move selected clip earlier"
                      disabled={!selectedClip}
                      onClick={() => moveClip(-1)}
                    >
                      <ArrowUp size={16} />
                    </button>
                    <button
                      className="icon-button"
                      title="Move selected clip later"
                      disabled={!selectedClip}
                      onClick={() => moveClip(1)}
                    >
                      <ArrowDown size={16} />
                    </button>
                  </div>
                ) : null}
                <button
                  className="primary-button render-button"
                  disabled={rendering || !activePlayer?.accepted}
                  onClick={renderPlayer}
                >
                  {rendering ? <LoaderCircle className="spin" size={16} /> : <Film size={16} />}
                  Render reel
                </button>
              </div>
            ) : null}

            <div className="queue-toolbar">
              <div className="search-box">
                <Search size={16} />
                <input
                  aria-label="Search clips"
                  placeholder="Search clips"
                  value={search}
                  onChange={(event) => {
                    setSearch(event.target.value);
                    setOffset(0);
                  }}
                />
              </div>
              <select
                aria-label="Filter by role"
                value={role}
                onChange={(event) => setRole(event.target.value)}
              >
                <option value="all">All roles</option>
                <option value="batter">Hitting</option>
                <option value="runner">Running</option>
                <option value="pitcher">Pitching</option>
                <option value="fielder">Fielding</option>
              </select>
              <select
                aria-label="Filter by offense or defense"
                value={side}
                onChange={(event) => setSide(event.target.value)}
              >
                <option value="all">All sides</option>
                <option value="offense">Offense</option>
                <option value="defense">Defense</option>
              </select>
              {view === "all" && bulkSelected.size ? (
                <button
                  className="secondary-button bulk-dismiss-button"
                  disabled={bulkBusy}
                  onClick={dismissSelectedAsNotNoteworthy}
                >
                  {bulkBusy ? (
                    <LoaderCircle className="spin" size={16} />
                  ) : (
                    <Trash2 size={16} />
                  )}
                  Dismiss {bulkSelected.size} as not noteworthy
                </button>
              ) : null}
            </div>

            <QueueTable
              rows={queue.rows}
              total={queue.total}
              offset={queue.offset}
              limit={PAGE_SIZE}
              selected={selectedClip?.clip_key}
              loading={queueLoading}
              onSelect={(clip) =>
                setSelectedClip((current) =>
                  current?.clip_key === clip.clip_key ? null : clip,
                )
              }
              onPage={setOffset}
              bulkSelected={view === "all" ? bulkSelected : undefined}
              onBulkToggle={
                view === "all"
                  ? (clipKey, checked) =>
                      setBulkSelected((current) => {
                        const next = new Set(current);
                        if (checked) next.add(clipKey);
                        else next.delete(clipKey);
                        return next;
                      })
                  : undefined
              }
              onBulkToggleAll={
                view === "all"
                  ? (clipKeys, checked) =>
                      setBulkSelected((current) => {
                        const next = new Set(current);
                        for (const clipKey of clipKeys) {
                          if (checked) next.add(clipKey);
                          else next.delete(clipKey);
                        }
                        return next;
                      })
                  : undefined
              }
              renderExpanded={(clip) => (
                <ReviewPanel
                  project={projectId}
                  clipKey={clip.clip_key}
                  playerContext={view === "players" ? selectedPlayer : undefined}
                  call={call}
                  onClose={() => setSelectedClip(null)}
                  onChanged={async () => {
                    await Promise.all([loadQueue(), loadDashboard()]);
                  }}
                />
              )}
            />
          </section>
        )}
      </main>

      {showSetup ? (
        <SetupDialog
          onClose={projects.length ? () => setShowSetup(false) : undefined}
          onCreated={(project) => {
            setProjects((current) => [
              ...current.filter((item) => item.id !== project.id),
              project,
            ]);
            setProjectId(project.id);
            setShowSetup(false);
          }}
        />
      ) : null}
      {showToken ? <TokenDialog onClose={() => setShowToken(false)} /> : null}
    </div>
  );
}
