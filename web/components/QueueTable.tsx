"use client";

import {
  AlertTriangle,
  Check,
  ChevronLeft,
  ChevronRight,
} from "lucide-react";
import { Fragment, type ReactNode } from "react";

import type { Clip } from "@/lib/types";

type Props = {
  rows: Clip[];
  total: number;
  offset: number;
  limit: number;
  selected?: string;
  loading?: boolean;
  onSelect: (clip: Clip) => void;
  onPage: (offset: number) => void;
  renderExpanded?: (clip: Clip) => ReactNode;
  bulkSelected?: Set<string>;
  onBulkToggle?: (clipKey: string, checked: boolean) => void;
  onBulkToggleAll?: (clipKeys: string[], checked: boolean) => void;
};

function dateText(value?: string) {
  if (!value) return "";
  return new Date(value).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
}

export function QueueTable({
  rows,
  total,
  offset,
  limit,
  selected,
  loading,
  onSelect,
  onPage,
  renderExpanded,
  bulkSelected,
  onBulkToggle,
  onBulkToggleAll,
}: Props) {
  const start = total ? offset + 1 : 0;
  const end = Math.min(offset + rows.length, total);
  const bulkEnabled = Boolean(bulkSelected && onBulkToggle && onBulkToggleAll);
  const selectedOnPage = bulkEnabled
    ? rows.filter((clip) => bulkSelected?.has(clip.clip_key)).length
    : 0;
  const allOnPageSelected = rows.length > 0 && selectedOnPage === rows.length;
  const someOnPageSelected = selectedOnPage > 0 && !allOnPageSelected;
  const columnCount = bulkEnabled ? 8 : 7;
  return (
    <div className={`queue-region ${selected ? "has-expanded-row" : ""}`}>
      <div className={`table-wrap ${loading ? "is-loading" : ""}`}>
        <table className="clip-table">
          <thead>
            <tr>
              {bulkEnabled ? (
                <th className="bulk-select-column">
                  <input
                    type="checkbox"
                    aria-label="Select all clips on this page"
                    checked={allOnPageSelected}
                    ref={(input) => {
                      if (input) input.indeterminate = someOnPageSelected;
                    }}
                    onChange={() =>
                      onBulkToggleAll?.(
                        rows.map((clip) => clip.clip_key),
                        !allOnPageSelected,
                      )
                    }
                  />
                </th>
              ) : null}
              <th className="score-column">Score</th>
              <th>Play</th>
              <th className="date-column">Game</th>
              <th className="side-column">Side</th>
              <th className="timing-column">Cut</th>
              <th className="participants-column">Players</th>
              <th className="state-column">State</th>
            </tr>
          </thead>
          <tbody>
            {!loading && rows.length === 0 ? (
              <tr>
                <td colSpan={columnCount} className="empty-row">
                  No clips match this view.
                </td>
              </tr>
            ) : null}
            {rows.map((clip) => {
              const expanded = selected === clip.clip_key;
              return (
                <Fragment key={clip.clip_key}>
                  <tr
                    className={expanded ? "selected" : ""}
                    aria-expanded={expanded}
                    tabIndex={0}
                    onClick={() => onSelect(clip)}
                    onKeyDown={(event) => {
                      if (event.key !== "Enter" && event.key !== " ") return;
                      event.preventDefault();
                      onSelect(clip);
                    }}
                  >
                    {bulkEnabled ? (
                      <td
                        className="bulk-select-cell"
                        onClick={(event) => event.stopPropagation()}
                        onKeyDown={(event) => event.stopPropagation()}
                      >
                        <input
                          type="checkbox"
                          aria-label={`Select ${clip.play_title || clip.play_summary}`}
                          checked={bulkSelected?.has(clip.clip_key) ?? false}
                          onChange={(event) =>
                            onBulkToggle?.(clip.clip_key, event.target.checked)
                          }
                        />
                      </td>
                    ) : null}
                    <td className="score-cell">
                      <span>{clip.score}</span>
                      <small>{clip.score_reason}</small>
                    </td>
                    <td className="play-cell">
                      <ChevronRight className="row-chevron" size={15} />
                      <div className="play-type">{clip.play_type?.replaceAll("_", " ")}</div>
                      <div className="summary">{clip.play_title || clip.play_summary}</div>
                    </td>
                    <td className="muted-cell game-cell">
                      <span>{dateText(clip.game_date)}</span>
                      <small>{clip.opponent}</small>
                    </td>
                    <td className="side-cell">
                      <span className={`side-badge side-${clip.side || "unknown"}`}>
                        {clip.side || "unknown"}
                      </span>
                    </td>
                    <td className="mono-cell cut-cell">
                      {clip.display_start !== undefined && clip.display_end !== undefined
                        ? `${Math.max(0, Math.round(clip.display_end - clip.display_start))}s`
                        : "Untimed"}
                    </td>
                    <td className="participants-cell">{clip.participant_text || "None"}</td>
                    <td className="state-cell">
                      <span className={`state-badge state-${clip.status || clip.review_state || "unreviewed"}`}>
                        {clip.source_changed ? <AlertTriangle size={13} /> : null}
                        {clip.status === "accepted" ? <Check size={13} /> : null}
                        {clip.status || clip.review_state || "unreviewed"}
                      </span>
                    </td>
                  </tr>
                  {expanded && renderExpanded ? (
                    <tr className="clip-detail-row">
                      <td className="clip-detail-cell" colSpan={columnCount}>
                        {renderExpanded(clip)}
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="pagination">
        <span>
          {start}-{end} of {total.toLocaleString()}
        </span>
        <div>
          <button
            className="icon-button"
            title="Previous page"
            disabled={offset === 0 || loading}
            onClick={() => onPage(Math.max(0, offset - limit))}
          >
            <ChevronLeft size={17} />
          </button>
          <button
            className="icon-button"
            title="Next page"
            disabled={end >= total || loading}
            onClick={() => onPage(offset + limit)}
          >
            <ChevronRight size={17} />
          </button>
        </div>
      </div>
    </div>
  );
}
