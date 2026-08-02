"use client";

import {
  Check,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Clock3,
  FastForward,
  LoaderCircle,
  Play,
  Rewind,
  Save,
  SkipForward,
  UserRoundCheck,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import type { Clip, Player, PreviewData } from "@/lib/types";
import { formatTimecode, parseTimecode } from "@/lib/timecode";

const ROLES = ["batter", "runner", "pitcher", "fielder"] as const;

type Props = {
  project: string;
  clipKey: string;
  playerContext?: string;
  onClose: () => void;
  onChanged: () => void;
  call: <T>(action: string, payload?: Record<string, unknown>) => Promise<T>;
};

export function ReviewPanel({
  project,
  clipKey,
  playerContext,
  onClose,
  onChanged,
  call,
}: Props) {
  const [clip, setClip] = useState<Clip | null>(null);
  const [players, setPlayers] = useState<Player[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [start, setStart] = useState(0);
  const [end, setEnd] = useState(12);
  const [startText, setStartText] = useState("0:00");
  const [endText, setEndText] = useState("0:12");
  const [preview, setPreview] = useState<PreviewData | null>(null);
  const [stopAt, setStopAt] = useState<number | null>(null);
  const [playhead, setPlayhead] = useState(0);
  const [sliceQueued, setSliceQueued] = useState(false);
  const [dismissReason, setDismissReason] = useState("play_not_found");
  const [busy, setBusy] = useState<string | null>("clip");
  const [error, setError] = useState("");
  const panelRef = useRef<HTMLElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      panelRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    return () => cancelAnimationFrame(frame);
  }, [clipKey]);

  useEffect(() => {
    let cancelled = false;
    setBusy("clip");
    setPreview(null);
    setStopAt(null);
    setError("");
    call<{ clip: Clip; players: Player[] }>("clip", { project, clipKey })
      .then((data) => {
        if (cancelled) return;
        setClip(data.clip);
        setPlayers(data.players);
        setStart(Math.round(data.clip.display_start ?? data.clip.proposal_start ?? 0));
        setEnd(Math.round(data.clip.display_end ?? data.clip.proposal_end ?? 12));
        setSelected(
          new Set(
            (data.clip.participants ?? []).map(
              (participant) => `${participant.player_id}|${participant.role}`,
            ),
          ),
        );
      })
      .catch((reason) => setError(reason.message))
      .finally(() => !cancelled && setBusy(null));
    return () => {
      cancelled = true;
    };
  }, [call, clipKey, project]);

  useEffect(() => setStartText(formatTimecode(start)), [start]);
  useEffect(() => setEndText(formatTimecode(end)), [end]);

  const participantPayload = useMemo(
    () =>
      [...selected].map((value) => {
        const [playerId, role] = value.split("|");
        return { playerId, role };
      }),
    [selected],
  );

  function toggleParticipant(playerId: string, role: string) {
    const key = `${playerId}|${role}`;
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function commitTimecode(kind: "start" | "end") {
    const text = kind === "start" ? startText : endText;
    const parsed = parseTimecode(text);
    const sourceEnd = preview
      ? preview.sourceStart + previewDuration
      : (clip?.asset_duration ?? Math.max(end, start + 1));
    const valid =
      parsed !== null &&
      parsed >= 0 &&
      parsed <= sourceEnd &&
      (kind === "start" ? parsed < end : parsed > start);
    if (!valid) {
      setError(
        `Enter a valid ${kind === "start" ? "in" : "out"} point as M:SS or H:MM:SS.`,
      );
      if (kind === "start") setStartText(formatTimecode(start));
      else setEndText(formatTimecode(end));
      return;
    }
    setError("");
    if (kind === "start") setStart(parsed);
    else setEnd(parsed);
  }

  function seekBy(seconds: number) {
    const video = videoRef.current;
    if (!video) return;
    const duration = Number.isFinite(video.duration)
      ? video.duration
      : previewDuration;
    const next = Math.max(0, Math.min(duration, video.currentTime + seconds));
    setStopAt(null);
    video.currentTime = next;
    setPlayhead(next);
  }

  async function loadPreview() {
    setBusy("preview");
    setError("");
    try {
      const data = await call<PreviewData>("preview", { project, clipKey });
      setPreview(data);
      setStart(data.sourceStart + data.inPoint);
      setEnd(data.sourceStart + data.outPoint);
      setPlayhead(data.seek);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }

  async function saveDraft(confirm: boolean) {
    if (end <= start || start < 0) {
      setError("The final clip must end after it starts.");
      return;
    }
    setBusy(confirm ? "confirm" : "save");
    setError("");
    try {
      await call("participants", { project, clipKey, participants: participantPayload });
      await call("timing", { project, clipKey, start, end });
      if (confirm) await call("confirm", { project, clipKey });
      const data = await call<{ clip: Clip; players: Player[] }>("clip", {
        project,
        clipKey,
      });
      setClip(data.clip);
      setSliceQueued(preview !== null);
      onChanged();
      if (confirm) onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }

  async function decide(status: "accepted" | "skipped" | "deferred") {
    if (!playerContext) return;
    setBusy(status);
    setError("");
    try {
      await call("decision", {
        project,
        clipKey,
        playerId: playerContext,
        status,
      });
      onChanged();
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }

  async function dismissClip() {
    setBusy("dismiss");
    setError("");
    try {
      await call("dismiss", { project, clipKey, reason: dismissReason });
      onChanged();
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }

  const videoUrl = preview
    ? `/api/media?project=${encodeURIComponent(project)}&path=${encodeURIComponent(preview.path)}`
    : "";
  const previewSourceStart = preview?.sourceStart ?? 0;
  const previewDuration = preview
    ? preview.sourceEnd - preview.sourceStart
    : Math.max(1, clip?.asset_duration ?? end + 30);
  const relativeIn = Math.max(0, start - previewSourceStart);
  const relativeOut = Math.max(relativeIn, end - previewSourceStart);
  const cutDuration = Math.max(0, end - start);
  const sourceEnd = previewSourceStart + previewDuration;
  const timelineDuration = Math.min(
    previewDuration,
    Math.max(60, cutDuration + 30),
  );
  const timelineMidpoint = (start + end) / 2;
  const timelineStart = Math.max(
    previewSourceStart,
    Math.min(sourceEnd - timelineDuration, timelineMidpoint - timelineDuration / 2),
  );
  const timelineEnd = timelineStart + timelineDuration;
  const inPercent = Math.min(
    100,
    Math.max(0, ((start - timelineStart) / timelineDuration) * 100),
  );
  const outPercent = Math.min(
    100,
    Math.max(0, ((end - timelineStart) / timelineDuration) * 100),
  );

  return (
    <section className="review-panel" ref={panelRef}>
      <header className="review-header">
        <div>
          <span className="eyebrow">Clip review</span>
          <h2>{clip?.play_type?.replaceAll("_", " ") || "Loading clip"}</h2>
        </div>
        <button className="icon-button" title="Close review" onClick={onClose}>
          <X size={18} />
        </button>
      </header>

      <div className="review-scroll">
        <div className="review-media-column">
          <section className="video-section">
            {preview ? (
              <video
                ref={videoRef}
                src={videoUrl}
                controls
                autoPlay
                onLoadedMetadata={(event) => {
                  const duration = event.currentTarget.duration;
                  setPreview((current) =>
                    current ? { ...current, sourceEnd: duration } : current,
                  );
                  event.currentTarget.currentTime = Math.min(relativeIn, duration);
                }}
                onTimeUpdate={(event) => {
                  setPlayhead(event.currentTarget.currentTime);
                  if (stopAt === null || event.currentTarget.currentTime < stopAt) return;
                  event.currentTarget.pause();
                  setStopAt(null);
                }}
              />
            ) : (
              <button
                className="preview-placeholder"
                onClick={loadPreview}
                disabled={busy !== null}
              >
                {busy === "preview" ? (
                  <LoaderCircle className="spin" size={28} />
                ) : (
                  <Play size={30} />
                )}
                <span>{busy === "preview" ? "Opening full game" : "Open full game"}</span>
              </button>
            )}
            {preview ? (
              <div className="video-actions">
                <button
                  className="secondary-button"
                  onClick={() => {
                    if (!videoRef.current) return;
                    videoRef.current.currentTime = relativeIn;
                    setStopAt(relativeOut);
                    void videoRef.current.play();
                  }}
                >
                  <Play size={15} /> Play final cut
                </button>
                <button
                  className="icon-button video-seek-button"
                  title="Back 5 seconds"
                  aria-label="Back 5 seconds"
                  onClick={() => seekBy(-5)}
                >
                  <Rewind size={16} />
                </button>
                <button
                  className="secondary-button"
                  onClick={() =>
                    setStart(Math.min(previewSourceStart + playhead, end - 1))
                  }
                >
                  Set in
                </button>
                <button
                  className="secondary-button"
                  onClick={() =>
                    setEnd(Math.max(previewSourceStart + playhead, start + 1))
                  }
                >
                  Set out
                </button>
                <button
                  className="icon-button video-seek-button"
                  title="Forward 5 seconds"
                  aria-label="Forward 5 seconds"
                  onClick={() => seekBy(5)}
                >
                  <FastForward size={16} />
                </button>
                <span>
                  Full game {formatTimecode(previewDuration)} · playhead{" "}
                  {formatTimecode(previewSourceStart + playhead)} · cut{" "}
                  {formatTimecode(cutDuration)}
                </span>
              </div>
            ) : null}
          </section>

          {error ? <div className="error-banner">{error}</div> : null}

          <section className="clip-facts">
            <div>
              <span>Game</span>
              <strong>
                {clip?.opponent || "Opponent"} ·{" "}
                {clip?.game_date ? new Date(clip.game_date).toLocaleDateString() : ""}
              </strong>
            </div>
            <div>
              <span>Inning</span>
              <strong>
                {clip?.inning_half === "top" ? "Top" : "Bottom"} {clip?.inning || ""}
              </strong>
            </div>
            <p>{clip?.play_title || clip?.play_summary}</p>
          </section>
        </div>

        <div className="review-controls-column">
          <section className="review-section">
            <div className="section-heading">
              <Clock3 size={16} />
              <h3>Final timing</h3>
            </div>
            <div className="timing-grid">
              <label>
                In point
                <div className="stepper">
                  <button
                    title="Move in point one second earlier"
                    onClick={() => setStart(Math.max(0, start - 1))}
                  >
                    <ChevronLeft size={16} />
                  </button>
                  <input
                    type="text"
                    aria-label="In point game time"
                    spellCheck={false}
                    value={startText}
                    onChange={(event) => setStartText(event.target.value)}
                    onBlur={() => commitTimecode("start")}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") event.currentTarget.blur();
                      if (event.key === "Escape") {
                        setStartText(formatTimecode(start));
                        event.currentTarget.blur();
                      }
                    }}
                  />
                  <button
                    title="Move in point one second later"
                    onClick={() => setStart(Math.min(end - 1, start + 1))}
                  >
                    <ChevronRight size={16} />
                  </button>
                </div>
              </label>
              <label>
                Out point
                <div className="stepper">
                  <button
                    title="Move out point one second earlier"
                    onClick={() => setEnd(Math.max(start + 1, end - 1))}
                  >
                    <ChevronLeft size={16} />
                  </button>
                  <input
                    type="text"
                    aria-label="Out point game time"
                    spellCheck={false}
                    value={endText}
                    onChange={(event) => setEndText(event.target.value)}
                    onBlur={() => commitTimecode("end")}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") event.currentTarget.blur();
                      if (event.key === "Escape") {
                        setEndText(formatTimecode(end));
                        event.currentTarget.blur();
                      }
                    }}
                  />
                  <button
                    title="Move out point one second later"
                    onClick={() =>
                      setEnd(
                        Math.min(
                          previewSourceStart + previewDuration,
                          end + 1,
                        ),
                      )
                    }
                  >
                    <ChevronRight size={16} />
                  </button>
                </div>
              </label>
            </div>
            <div className="timing-visual" aria-label="Preview timing">
              <span className="timing-window-start">
                {formatTimecode(timelineStart)}
              </span>
              <span className="timing-window-end">
                {formatTimecode(timelineEnd)}
              </span>
              <div
                className="timing-selection"
                style={{ left: `${inPercent}%`, width: `${Math.max(1, outPercent - inPercent)}%` }}
              />
              <span className="timing-in" style={{ left: `${inPercent}%` }}>
                {formatTimecode(start)}
              </span>
              <span className="timing-out" style={{ left: `${outPercent}%` }}>
                {formatTimecode(end)}
              </span>
            </div>
            <div className="timing-summary">
              <strong>{formatTimecode(cutDuration)} final clip</strong>
              <span>
                Game source {formatTimecode(start)}–{formatTimecode(end)}
              </span>
            </div>
          </section>

          <section className="review-section">
            <div className="section-heading">
              <UserRoundCheck size={16} />
              <h3>Players and roles</h3>
            </div>
            <div className="participant-matrix">
              <div className="participant-head">
                <span>Player</span>
                {ROLES.map((role) => (
                  <span key={role}>{role.slice(0, 1).toUpperCase()}</span>
                ))}
              </div>
              {players.map((player) => (
                <div className="participant-row" key={player.player_id}>
                  <span>
                    <b>#{player.number || "–"}</b> {player.display.replace(/\s+#\d+$/, "")}
                  </span>
                  {ROLES.map((role) => {
                    const key = `${player.player_id}|${role}`;
                    return (
                      <label key={key} title={`${player.display}: ${role}`}>
                        <input
                          type="checkbox"
                          checked={selected.has(key)}
                          onChange={() => toggleParticipant(player.player_id, role)}
                        />
                      </label>
                    );
                  })}
                </div>
              ))}
            </div>
          </section>
        </div>
      </div>

      <footer className="review-footer">
        <div className="dismiss-controls">
          <select
            aria-label="Dismissal reason"
            value={dismissReason}
            onChange={(event) => setDismissReason(event.target.value)}
          >
            <option value="play_not_found">Play not found</option>
            <option value="no_video">No video</option>
            <option value="not_noteworthy">Not noteworthy</option>
          </select>
          <button
            className="secondary-button"
            disabled={busy !== null}
            onClick={dismissClip}
          >
            Dismiss
          </button>
        </div>
        <button
          className="secondary-button"
          disabled={busy !== null}
          onClick={() => saveDraft(false)}
        >
          <Save size={16} /> Save draft
        </button>
        {sliceQueued ? <span className="slice-status">Local slice queued</span> : null}
        {clip?.review_state === "reviewed" && playerContext ? (
          <div className="decision-buttons">
            <button title="Defer" onClick={() => decide("deferred")} disabled={busy !== null}>
              <SkipForward size={16} />
            </button>
            <button title="Skip" onClick={() => decide("skipped")} disabled={busy !== null}>
              <X size={16} />
            </button>
            <button
              className="accept-button"
              title="Accept"
              onClick={() => decide("accepted")}
              disabled={busy !== null}
            >
              <Check size={16} />
            </button>
          </div>
        ) : (
          <>
            {playerContext ? (
              <button
                className="secondary-button skip-player-button"
                disabled={busy !== null}
                onClick={() => decide("skipped")}
              >
                <X size={16} /> Skip for player
              </button>
            ) : null}
            <button
              className="primary-button"
              disabled={busy !== null}
              onClick={() => saveDraft(true)}
            >
              {busy === "confirm" ? (
                <LoaderCircle className="spin" size={16} />
              ) : (
                <CheckCircle2 size={16} />
              )}
              Confirm clip
            </button>
          </>
        )}
      </footer>
    </section>
  );
}
